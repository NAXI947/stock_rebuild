"""Current LIVE buy permission from published database facts, never file fallback.

Every writer takes namespace then mapping-publish locks. A research snapshot is
still immutable; this check is an execution decision at the current DB clock.
"""
from copy import deepcopy
from datetime import datetime
import json

import psycopg

from ..ledger import fingerprint
from ..signal_cycle import pending
from ..simulator import CHINA, integer, moment

UNAVAILABLE = "ENTRY_ADMISSION_UNAVAILABLE"
UNPUBLISHED = "ENTRY_REFERENCE_UNPUBLISHED"


def _database_clock(connection):
    return connection.execute("SELECT clock_timestamp()").fetchone()[0]


def read_states(connection, namespace, codes):
    """Hold both locks through the caller's commit; failed reads deny only buys.

    The nested transaction is a savepoint so a reference query error does not
    abort the surrounding transaction which can still close existing holdings.
    """
    codes = sorted(set(codes))
    if not codes:
        return {}
    checked = datetime.now(CHINA)
    try:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (namespace,))
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('mapping-publish:'+namespace,))
            checked = _database_clock(connection)
            row = connection.execute("""SELECT version,available_at,body FROM maintained_mappings
                WHERE ns=%s AND available_at<=%s ORDER BY available_at DESC,version DESC LIMIT 1""",
                (namespace, checked)).fetchone()
            if row is None:
                return {code: dict(status="PAUSED", reason=UNPUBLISHED, checked_at=checked.isoformat())
                        for code in codes}
            version, available, body = row
            body = json.loads(body) if isinstance(body, str) else body
            from .maintenance_store import valid_mapping
            if not valid_mapping(body) or body['version'] != version:
                raise ValueError('invalid published product mapping')
            from .penetration import product_entry_reason
            states = {}
            for code in codes:
                product = body['products'].get(code)
                reason = product_entry_reason(product, checked)
                state = dict(status="PAUSED" if reason else "READY", reason=reason,
                             checked_at=checked.isoformat(), mapping_version=version,
                             mapping_available_at=available.isoformat())
                if isinstance(product, dict):
                    state['product_fingerprint'] = fingerprint({key: product.get(key) for key in
                        ('index_code', 'trade_unit', 'rule', 'list_status', 'list_date', 'catalog_available_at')})
                if reason is None:
                    try:
                        state['trade_unit'] = integer(product['trade_unit'])
                        rule = product['rule']
                        eligible = max(available, moment(rule['verified_at']),
                            moment(rule.get('effective_at', rule['verified_at'])))
                        if eligible > checked:
                            state.update(status='PAUSED', reason='PRODUCT_RULE_UNVERIFIED')
                        else:
                            state['eligible_after'] = eligible.isoformat()
                    except (TypeError, ValueError, KeyError):
                        state.update(status='PAUSED', reason='PRODUCT_RULE_UNVERIFIED')
                states[code] = state
            return states
    except (psycopg.Error, OSError, ValueError, TypeError, KeyError):
        return {code: dict(status="PAUSED", reason=UNAVAILABLE, checked_at=checked.isoformat())
                for code in codes}


def candidate_codes(report):
    candidates = report.get('candidates', [])
    return list(candidates) if isinstance(candidates, dict) else [
        row['etf'] for row in candidates if isinstance(row, dict) and isinstance(row.get('etf'), str)]


def filter_report(report, states, frozen_units):
    """Preserve raw report JSON; filter the execution projection with exact reasons."""
    blocked = {}
    for code in candidate_codes(report):
        state = states.get(code, {})
        reason = state.get('reason') if state.get('status') != 'READY' else None
        if state.get('status') != 'READY':
            blocked[code] = reason or UNAVAILABLE
        elif state.get('trade_unit') != frozen_units.get(code):
            blocked[code] = 'TRADE_UNIT_CHANGED'
    candidates = report.get('candidates', [])
    kept = ({code: rows for code, rows in candidates.items() if code not in blocked}
            if isinstance(candidates, dict) else
            [row for row in candidates if not isinstance(row, dict) or row.get('etf') not in blocked])
    return {**report, 'candidates': kept}, [dict(etf=code, reason=reason) for code, reason in sorted(blocked.items())]


def update_waiting_buy(cycle, state):
    """Pause an unfilled buy without rewriting its node, budget or frozen rule."""
    order = pending(cycle)
    if cycle.get('status') != 'WAIT_BUY' or not order or order['action'] != 'BUY':
        return False
    before = deepcopy(order)
    prior = order.get('entry_admission', {})
    current = dict(state)
    if current.get('status') == 'READY' and current.get('trade_unit') != cycle['unit']:
        current.update(status='PAUSED', reason='TRADE_UNIT_CHANGED')
    if current.get('status') == 'READY':
        if prior.get('status') == 'READY' and prior.get('product_fingerprint') == current.get('product_fingerprint'):
            # Unrelated mapping maintenance does not move an unchanged product's
            # eligibility clock or require another source quote.
            current['eligible_after'] = prior['eligible_after']
        elif prior and prior.get('status') != 'READY':
            # Resume only with a genuinely new observation after this recovery.
            current['eligible_after'] = max(moment(current['eligible_after']), moment(current['checked_at'])).isoformat()
        if prior.get('status') == 'PAUSED' and order.get('last_block') == prior.get('reason'):
            order.pop('last_block', None)
    else:
        order['last_block'] = current.get('reason') or UNAVAILABLE
    # Polling the same state must not write the order on every worker iteration.
    if {k:v for k,v in prior.items() if k != 'checked_at'} == {k:v for k,v in current.items() if k != 'checked_at'}:
        current['checked_at'] = prior['checked_at']
    order['entry_admission'] = current
    return order != before


def refresh_waiting(ledger, connection):
    """Persist current buy pauses even when no fresh quote is available."""
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (ledger.ns,))
    rows = connection.execute("SELECT id,body,opened_at FROM cycles WHERE ns=%s AND status='WAIT_BUY'",
                              (ledger.ns,)).fetchall()
    cycles = [(cid, json.loads(body) if isinstance(body, str) else body, opened) for cid, body, opened in rows]
    states = read_states(connection, ledger.ns, [cycle['etf'] for _, cycle, _ in cycles])
    from .postgres import BoundConnection
    changed = 0
    for cid, cycle, opened in cycles:
        if update_waiting_buy(cycle, states[cycle['etf']]):
            ledger._save(BoundConnection(connection), ledger.ns, cid, cycle, opened)
            changed += 1
    return changed
