#! /usr/bin/python3

import struct
import sqlite3
import decimal
D = decimal.Decimal
decimal.getcontext().prec = 8

from . import (util, config, bitcoin)

FORMAT = '>QQQQHQ'        # give_id, give_amount, get_id, get_amount, expiration, fee_required
ID = 10

def order (source, give_id, give_amount, get_id, get_amount, expiration, fee_required, fee_provided):
    if util.balance(source, give_id) < give_amount:
        raise exceptions.BalanceError('Insufficient funds. (Check that the database is up‐to‐date.)')
    data = config.PREFIX + struct.pack(config.TXTYPE_FORMAT, ID) + struct.pack(FORMAT, give_id, give_amount, get_id, get_amount, expiration, fee_required)
    return bitcoin.transaction(source, None, config.DUST_SIZE, fee_provided, data)

def parse_order (db, cursor, tx1, message):
    # Ask for forgiveness…
    validity = 'Valid'

    # Unpack message.
    try:
        give_id, give_amount, get_id, get_amount, expiration, fee_required = struct.unpack(FORMAT, message)
        assert give_id != get_id    # TODO
    except Exception:
        give_id, give_amount, get_id, get_amount, expiration, fee_required = None, None, None, None, None, None
        validity = 'Invalid: could not unpack'


    give_amount = D(give_amount)
    get_amount = D(get_amount)
    ask_price = get_amount / give_amount

    # Debit the address that makes the order. Check for sufficient funds.
    if validity == 'Valid':
        if util.balance(tx1['source'], give_id) >= give_amount:
            if give_id:  # No need (or way) to debit BTC.
                db, cursor, validity = util.debit(db, cursor, tx1['source'], give_id, give_amount)
        else:
            validity = 'Invalid: insufficient funds.'

    # Add parsed transaction to message‐type–specific table.
    cursor.execute('''INSERT INTO orders(
                        tx_index,
                        tx_hash,
                        block_index,
                        source,
                        give_id,
                        give_amount,
                        give_remaining,
                        get_id,
                        get_amount,
                        ask_price,
                        expiration,
                        fee_required,
                        fee_provided,
                        validity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (tx1['tx_index'],
                        tx1['tx_hash'],
                        tx1['block_index'],
                        tx1['source'],
                        give_id,
                        int(give_amount),
                        int(give_amount),
                        get_id,
                        int(get_amount),
                        float(ask_price),
                        expiration,
                        fee_required,
                        tx1['fee'],
                        validity)
                  )
    db.commit()

    if validity == 'Valid':
        # give_name, get_name = ASSET_NAME[give_id], ASSET_NAME[get_id]
        if util.is_divisible(give_id): give_unit = config.UNIT
        else: give_unit = 1
        if util.is_divisible(get_id): get_unit = config.UNIT
        else: get_unit = 1
        print('\tOrder: sell', give_amount/give_unit, give_id, 'for', get_amount/get_unit, get_id, 'at', ask_price, str(get_id) + '/' + str(give_id), 'in', expiration, 'blocks', '(' + tx1['tx_hash'] + ')') # TODO (and fee_required, fee_provided)

        db, cursor = make_deal(db, cursor, give_id, give_amount, get_id, get_amount, ask_price, expiration, fee_required, tx1)

    return db, cursor

def make_deal (db, cursor, give_id, give_amount, get_id, get_amount,
        ask_price, expiration, fee_required, tx1):
    cursor.execute('''SELECT * FROM orders \
                      WHERE (give_id=? AND get_id=? AND block_index>=?) \
                      ORDER BY ask_price ASC, tx_index''',
                   (get_id, give_id, tx1['block_index'] - expiration))
    give_remaining = give_amount
    for tx0 in cursor.fetchall():
        # NOTE: tx0 is an order; tx1 is a transaction.

        # Check whether fee conditions are satisfied.
        if not get_id and tx0['fee_provided'] < fee_required: continue
        elif not give_id and tx1['fee'] < tx0['fee_required']: continue

        # Make sure that that both orders still have funds remaining [to be sold].
        if tx0['give_remaining'] <= 0 or give_remaining <= 0: continue

        # If the prices agree, make the trade. The found order sets the price,
        # and they trade as much as they can.
        price = D(tx0['get_amount']) / D(tx0['give_amount'])
        if price <= 1/ask_price:  # Ugly
            forward_amount = min(D(tx0['give_remaining']),
                                     get_amount / price)
            backward_amount = give_amount * forward_amount/D(tx0['give_amount'])

            forward_id, backward_id = get_id, give_id
            # forward_name, backward_name = ASSET_NAME[forward_id], ASSET_NAME[backward_id]
            deal_id = tx0['tx_hash'] + tx1['tx_hash']

            if util.is_divisible(forward_id): forward_unit = config.UNIT
            else: forward_unit = 1
            if util.is_divisible(backward_id): backward_unit = config.UNIT
            else: backward_unit = 1
            print('\t\tDeal:', forward_amount/forward_unit, forward_id, 'for', backward_amount/backward_unit, backward_id, 'at', price, str(backward_id) + '/' + str(forward_id), '(' + deal_id + ')') # TODO

            if 0 in (give_id, get_id):
                validity = 'Valid: waiting for bitcoins'
            else:
                validity = 'Valid'
                # Credit.
                db, cursor = credit(db, cursor, tx1['source'], get_id,
                                    forward_amount)
                db, cursor = credit(db, cursor, tx0['source'], tx0['get_id'],
                                    backward_amount)

            # Debit the order, even if it involves giving bitcoins, and so one
            # can’t debit the sending account.
            give_remaining -= backward_amount

            # Update give_remaining.
            cursor.execute('''UPDATE orders \
                              SET give_remaining=? \
                              WHERE tx_hash=?''',
                          (int(tx0['give_remaining'] - forward_amount),
                           tx0['tx_hash']))
            cursor.execute('''UPDATE orders \
                              SET give_remaining=? \
                              WHERE tx_hash=?''',
                          (int(give_remaining),
                           tx1['tx_hash']))

            # Record order fulfillment.
            cursor.execute('''INSERT into deals(
                                tx0_index,
                                tx0_hash,
                                tx0_address,
                                tx1_index,
                                tx1_hash,
                                tx1_address,
                                forward_id,
                                forward_amount,
                                backward_id,
                                backward_amount,
                                tx0_block_index,
                                tx1_block_index,
                                tx0_expiration,
                                tx1_expiration,
                                validity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                (tx0['tx_index'],
                                tx0['tx_hash'],
                                tx0['source'],
                                tx1['tx_index'],
                                tx1['tx_hash'],
                                tx1['source'],
                                forward_id,
                                int(forward_amount),
                                backward_id,
                                int(backward_amount),
                                tx0['block_index'],
                                tx1['block_index'],
                                tx0['expiration'],
                                expiration,
                                validity)
                          )
            db.commit()
    return db, cursor

def expire (db, cursor, block_index):
    # Expire orders and give refunds.
    cursor.execute('''SELECT * FROM orders''')
    for order in cursor.fetchall():
        time_left = order['block_index'] + order['expiration'] - block_index # Inclusive/exclusive expiration? DUPE
        if time_left <= 0 and order['validity'] == 'Valid':
            cursor.execute('''UPDATE orders SET validity=? WHERE tx_hash=?''', ('Invalid: expired', order['tx_hash']))
            db, cursor = util.credit(db, cursor, order['source'], order['give_id'], order['give_amount'])
            print('\tExpired order:', order['tx_hash'])
        db.commit()

    # Expire deals for BTC with no BTC.
    cursor.execute('''SELECT * FROM deals''')
    for deal in cursor.fetchall():
        tx0_time_left = deal['tx0_block_index'] + deal['tx0_expiration'] - block_index # Inclusive/exclusive expiration? DUPE
        tx1_time_left = deal['tx1_block_index'] + deal['tx1_expiration'] - block_index # Inclusive/exclusive expiration? DUPE
        if (tx0_time_left <= 0 or tx1_time_left <=0) and deal['validity'] == 'Valid: waiting for bitcoins':
            cursor.execute('''UPDATE deals SET validity=? WHERE (tx0_hash=? AND tx1_hash=?)''', ('Invalid: expired while waiting for bitcoins', deal['tx0_hash'], deal['tx1_hash']))
            if not deal['forward_id']:
                db, cursor = util.credit(db, cursor, deal['tx1_address'],
                                    deal['backward_id'],
                                    deal['backward_amount'])
            elif not deal['backward_id']:
                db, cursor = util.credit(db, cursor, deal['tx0_address'],
                                    deal['forward_id'],
                                    deal['forward_amount'])
            print('\tExpired deal waiting for bitcoins:',
                  deal['tx0_hash'] + deal['tx1_hash'])    # TODO
    db.commit()

    return db, cursor

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4