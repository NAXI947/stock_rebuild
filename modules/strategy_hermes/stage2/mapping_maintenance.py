"""Bounded reference maintenance. Each item can advance independently.

The caller owns the durable request budget and publication. This module never
changes a direction, trading threshold, original report, or active position.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

import psycopg
from xlrd.biffh import XLRDError

from ..ledger import encode, fingerprint
from ..left_right_backfill import Interrupted
from ..simulator import CHINA, CODE, decimal, moment
from .build_sources import stock_identity, csi_evidence, CSI
from .discovery import select_directions
from .market_bootstrap import ProbeError, save as save_market
from .membership_refresh import _page
from .member_history import assemble_members, cached_members, saved_pages, usable_members
from .provider_errors import error_diagnostic
from .reference_policy import ReferencePolicy
from .tushare_bootstrap import INDEX_CODE, SourceFailure, Tushare, save as save_reference, weight_rows

MAX_SECTORS = 8
MAX_PENDING_MEMBERS = 256
MAX_PAGES = 8
MAX_INDEXES = 8
MAX_RULE_DOCUMENTS = 8
VERSION = 'etf-reference-maintenance-v1'


def _decode(raw):
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


def _record(summary, kind, key, state, **extra):
    summary.setdefault(kind, {})[key] = dict(status=state, **extra)


def _read_market(ledger, batch, *, with_batch=False):
    with ledger._connect() as conn:
        row = conn.execute('SELECT body,collected_at FROM supplier_market_batches WHERE batch_id=%s', (batch,)).fetchone()
    body = _decode(row[0])
    return dict(body, source_batch=batch) if with_batch else body, row[1]


def _cached(ledger, endpoint, scope):
    with ledger._connect() as conn:
        return conn.execute("SELECT batch_id FROM supplier_market_batches WHERE provider='maintenance' "
                            'AND endpoint=%s AND scope=%s::jsonb ORDER BY collected_at DESC LIMIT 1',
                            (endpoint, encode(scope))).fetchone()


def _obtain(ledger, api, params, config, now, request, checkpoint, *, period=None, refresh=False):
    """Successful source checks are reusable across bounded maintenance jobs."""
    scope = dict(api=api, params=params, period=period or now.date().isoformat(), version=VERSION)
    cached = _cached(ledger, api, scope)
    if not cached and api == 'etf_share_size' and not refresh:
        with ledger._connect() as conn:
            cached = conn.execute("SELECT batch_id FROM supplier_market_batches WHERE provider='maintenance' "
                "AND endpoint='etf_share_size' AND scope->'params'->>'trade_date'=%s AND collected_at<=%s "
                "ORDER BY collected_at DESC LIMIT 1", (params['trade_date'], now)).fetchone()
    if cached and not refresh:
        return _read_market(ledger, cached[0], with_batch=api == 'etf_basic')
    checkpoint()
    def operation():
        client = Tushare(config)
        client.config['timeout_seconds'] = min(15, int(client.config.get('timeout_seconds', 15)))
        raw = client.fetch(api, params, allow_empty=True)
        received = datetime.now(CHINA)
        # Empty live catalog or incomplete size/weight publication must remain
        # eligible for the next bounded maintenance attempt.
        if not raw and api not in ('etf_basic',):
            raise SourceFailure('source data not published yet', kind='NOT_PUBLISHED')
        if not raw and params.get('list_status') == 'L':
            raise SourceFailure('live ETF catalog empty', kind='NOT_PUBLISHED')
        if not raw:
            # Durable job receipts can represent an empty optional catalog;
            # supplier batch tables deliberately require a positive row count.
            return dict(status='EMPTY', rows=0, available_at=received.isoformat())
        return save_market(ledger, 'maintenance', api, scope, {'rows': raw}, len(raw), None, received)
    result = request('maintenance:' + api, scope, operation)
    checkpoint()
    if result.get('status') != 'SAVED':
        raise SourceFailure.from_receipt(result)
    if result['receipt'].get('status') == 'EMPTY':
        return {'rows':[]}, moment(result['receipt']['available_at'])
    return _read_market(ledger, result['receipt']['batch'], with_batch=api == 'etf_basic')


def catalog_updates(mapping, rows, available, batch=None):
    """Catalog classification remains evidence; it never grants trading approval."""
    changed, invalid = 0, 0
    for row in rows:
        code, index, state = row.get('ts_code'), row.get('index_code'), row.get('list_status')
        if not isinstance(code, str) or not CODE.fullmatch(code) or state not in ('L', 'D', 'P'):
            invalid += 1
            continue
        old = mapping['products'].get(code, {})
        if not isinstance(index, str) or not INDEX_CODE.fullmatch(index):
            # A delisting can still retire a known product when its source no
            # longer returns an index. Unknown new relationships stay pending.
            index = old.get('index_code')
        if not index:
            invalid += 1
            continue
        item = deepcopy(old)
        relationship_changed = bool(old) and old.get('index_code') != index
        classification_changed = bool(old.get('source_classification')) and old['source_classification'] != row.get('etf_type')
        item.update(index_code=index, name=row.get('csname') or old.get('name') or code,
                    list_status=state, source_classification=row.get('etf_type'),
                    list_date=row.get('list_date'))
        if not old or relationship_changed or classification_changed or state != 'L':
            item['rule'] = dict(verified=False, index_code=index, scope=None,
                                reason='PRODUCT_DELISTED' if state == 'D' else 'PRODUCT_REFERENCE_REQUIRES_VERIFICATION')
        if item != old:
            item['catalog_available_at'] = available.isoformat()
            if batch:
                item['catalog_batch'] = batch
            mapping['products'][code] = item
            changed += 1
    return changed, invalid


def _catalog_review(mapping, body, state):
    """Separate a valid source catalog from relationships outside this adapter."""
    rows = body.get('rows') if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise SourceFailure('catalog rows malformed', kind='FIELD_MISMATCH')
    review = dict(count=0, reasons={}, samples=[])
    for row in rows:
        if (not isinstance(row, dict) or not {'ts_code', 'index_code', 'list_status'} <= row.keys()
                or row['list_status'] != state or not isinstance(row['ts_code'], str)
                or not re.fullmatch(r'[0-9]{6}\.[A-Z]{2,8}', row['ts_code'])
                or (row['index_code'] is not None and not isinstance(row['index_code'], str))):
            raise SourceFailure('catalog fields or requested status mismatch', kind='FIELD_MISMATCH')
        code, index = row['ts_code'], row['index_code']
        reason = None
        if not CODE.fullmatch(code):
            reason = 'product_scope'
        elif (not index or not INDEX_CODE.fullmatch(index)) and not mapping['products'].get(code, {}).get('index_code'):
            reason = 'missing_relationship' if not index else 'unsupported_index'
        if reason:
            review['count'] += 1
            review['reasons'][reason] = review['reasons'].get(reason, 0) + 1
            if len(review['samples']) < 5:
                # Fixed identity fields only; arbitrary source text never enters diagnostics.
                safe_index = index if index and re.fullmatch(r'[A-Z0-9_.-]{1,48}', index) else None
                review['samples'].append(dict(ts_code=code, index_code=safe_index, list_status=state, reason=reason))
    return review


def _catalog_retry_cache(ledger, pending, state, now):
    """Reclassify the batch saved by a previous FIELD_MISMATCH attempt offline."""
    if pending.get('error_kind') != 'FIELD_MISMATCH' or not pending.get('attempted_at'):
        return None
    since = max(moment(pending['attempted_at']), now - timedelta(days=7))
    with ledger._connect() as conn:
        row = conn.execute("SELECT batch_id,body,collected_at FROM supplier_market_batches "
            "WHERE provider='maintenance' AND endpoint='etf_basic' "
            "AND scope->'params'->>'list_status'=%s AND collected_at>=%s AND collected_at<=%s "
            "ORDER BY collected_at DESC LIMIT 1", (state, since, now)).fetchone()
    return (dict(_decode(row[1]), source_batch=row[0]), row[2]) if row else None


def _catalog_maintenance(ledger, updated, config, now, day, request, checkpoint, summary, policy):
    summary['catalog_review'] = deepcopy(updated.get('catalog_review', {}))
    for state in ('L', 'D', 'P'):
        key = 'catalog:' + state
        cached = None
        if policy:
            try:
                cached = _catalog_retry_cache(ledger, policy.pending.get(key) or {}, state, now)
                if cached:
                    _catalog_review(updated, cached[0], state)
            except (SourceFailure, ValueError, TypeError, KeyError):
                cached = None
            if not cached and (not policy.due(key) or not policy.begin(key, day.isoformat())):
                continue
        try:
            body, available = cached or _obtain(ledger, 'etf_basic', {'list_status':state}, config, now,
                request, checkpoint, refresh=bool(policy and policy.pending[key]['attempts'] > 1))
            review = _catalog_review(updated, body, state)
            rows = body['rows']
            valid = [r for r in rows if CODE.fullmatch(r['ts_code'])
                     and r['index_code'] and INDEX_CODE.fullmatch(r['index_code'])]
            receipt = save_reference(ledger, 'etf_basic',
                {'list_status':state, 'maintenance_day':now.date().isoformat()}, rows, valid) if valid else None
            count, excluded = catalog_updates(updated, rows, available, receipt['batch'] if receipt else None)
            review.update(source_batch=body.get('source_batch'), source_available_at=available.isoformat(),
                          rows=len(rows), status='MANUAL_REVIEW' if excluded else 'CURRENT')
            updated.setdefault('catalog_review', {})[state] = review
            summary['catalog_review'][state] = deepcopy(review)
            _record(summary, 'catalog', state, 'UPDATED' if count else 'EMPTY' if not rows else 'CURRENT',
                    changed=count, manual_review_count=excluded, source_batch=body.get('source_batch'))
            if policy:
                policy.succeeded(key, day.isoformat())
        except (SourceFailure, OSError, ValueError, TypeError, KeyError, psycopg.Error) as exc:
            diagnostic = error_diagnostic(exc)
            _record(summary, 'catalog', state, 'RETAINED', error=type(exc).__name__, **diagnostic)
            if policy:
                policy.failed(key, diagnostic)


def weight_basis(rows, index, first, last, classes, previous=None, *, identity_batch=None, previous_identity_batch=None):
    normalized = weight_rows(rows, index, first, last)
    total = sum((decimal(r['weight_ratio']) for r in normalized['rows']), Decimal(0))
    if total <= 0 or total > Decimal('1.005'):
        raise SourceFailure('weight total outside source ratio bounds')
    previous = previous or {}
    before = {r['code']: r for r in previous.get('rows', ())}
    current = []
    for row in normalized['rows']:
        code = row['code']
        market = classes.get(code)
        mainboard = (market == '主板') if market in ('主板', '创业板', '科创板', '北交所') else None
        # A missing new identity field does not erase dated verified identity.
        class_source = identity_batch if mainboard is not None else None
        if mainboard is None and code in before:
            mainboard = before[code].get('mainboard')
            class_source = before[code].get('mainboard_source_batch') or previous_identity_batch
        current.append(dict(code=code, weight=row['weight_ratio'], mainboard=mainboard,
                            mainboard_source_batch=class_source))
    same_weights = (previous.get('as_of_date') == normalized['weight_date']
                    and {r['code']: decimal(r['weight']) for r in previous.get('rows', ())}
                    == {r['code']: decimal(r['weight']) for r in current})
    result = dict(as_of_date=normalized['weight_date'], rows=current, complete=False,
                  completeness_basis='SOURCE_WEIGHTS_UNVERIFIED', source='tushare:index_weight')
    if same_weights:
        result.update({k: deepcopy(v) for k, v in previous.items()
                       if k not in ('rows', 'available_at', 'source_batches')})
    return result


def aum_updates(mapping, rows, source_day, available, valid_until):
    updated, invalid = 0, 0
    for row in rows:
        code = row.get('ts_code')
        if code not in mapping['products']:
            continue
        try:
            observed = date.fromisoformat(row['trade_date'])
            size = decimal(row['total_size']) * 10000  # source unit: CNY 10,000
            shares = decimal(row['total_share']) * 10000
            if observed != source_day or size < 0 or shares < 0:
                raise ValueError('invalid AUM scope')
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        stats = mapping['products'][code].setdefault('stats', {})
        # A late retry is archived, but never rolls a product back to an older day.
        updated += 1
        if stats.get('aum_as_of', '') > observed.isoformat():
            continue
        if stats.get('aum_as_of') == observed.isoformat() and stats.get('aum_available_at'):
            try:
                if moment(stats['aum_available_at']) > available:
                    continue
            except (ValueError, TypeError):
                pass
        stats.update(aum_yuan=str(size), aum_as_of=observed.isoformat(), aum_verified=True,
                     aum_available_at=available.isoformat(), aum_valid_until=valid_until.isoformat(),
                     aum_source='tushare:etf_share_size', shares_units=str(shares))
    return updated, invalid


def _aum_valid_until(calendar, source_day):
    future = sorted(date.fromisoformat(d) for d in calendar if date.fromisoformat(d) > source_day)
    return future[1] - timedelta(days=1) if len(future) > 1 else future[0] if future else source_day


def _hydrate_members(ledger, mapping, market, identities, identity_batch, day, now, checkpoint, summary, *, policy=None):
    """Absorb all bounded visible cached lists, independently of HTTP cadence."""
    if not identities or not identity_batch:
        return
    with ledger._connect() as conn:
        row = conn.execute('SELECT collected_at FROM supplier_batches WHERE batch_id=%s AND collected_at<=%s',
                           (identity_batch, now)).fetchone()
    if not row:
        return
    boards = {v['sector_id']: v for v in (market or {}).get('sectors', ()) if isinstance(v, dict) and 'sector_id' in v}
    targets = sorted(set(mapping['sectors']) | set((mapping.get('member_maintenance') or {}).get('pending', [])))
    summary['cache_members'] = dict(checked=0, updated=0, deferred=max(0, len(targets)-MAX_PENDING_MEMBERS))
    for sid in targets[:MAX_PENDING_MEMBERS]:
        checkpoint()
        old = mapping['sectors'].get(sid, {})
        name = boards.get(sid, {}).get('sector_name') or old.get('sector_name') or sid
        summary['cache_members']['checked'] += 1
        try:
            with ledger._connect() as conn:
                pages = saved_pages(conn, sid, day.isoformat(), now)
            candidate = None
            if pages:
                candidate = assemble_members(pages, day.isoformat(), identities, identity_batch, row[0], name)
            if not usable_members(candidate or {}, now, day):
                candidate = cached_members(ledger, sid, day, now, identities, identity_batch, row[0], name, checkpoint)
            if candidate and (not usable_members(old, now, day) or
                    (candidate['as_of_date'], candidate.get('available_at', '')) >
                    (old['as_of_date'], old.get('available_at', ''))):
                mapping['sectors'][sid] = candidate
                summary['cache_members']['updated'] += 1
            key = 'members:'+sid
            current = mapping['sectors'].get(sid, {})
            if policy and key in policy.pending and usable_members(current, now, day):
                target = policy.target(key, day.isoformat())
                if current['as_of_date'] >= target:
                    policy.succeeded(key, target)
        except (OSError, ValueError, TypeError, KeyError, IndexError, psycopg.Error) as exc:
            summary['cache_members']['error'] = error_diagnostic(exc).get('error_kind', 'SOURCE_FAILURE')


def _aum_maintenance(ledger, mapping, calendar, config, now, day, request, checkpoint, summary, policy):
    """Reuse saved batches and retry persisted missing dates, under the same budget."""
    with ledger._connect() as conn:
        cached = conn.execute("SELECT scope,body,collected_at FROM supplier_market_batches "
            "WHERE provider='maintenance' AND endpoint='etf_share_size' AND collected_at<=%s "
            "ORDER BY collected_at DESC LIMIT 8", (now,)).fetchall()
    for scope, body, at in reversed(cached):
        checkpoint()
        try:
            source_day = date.fromisoformat(_decode(scope)['params']['trade_date'])
            if source_day <= day:
                count, invalid = aum_updates(mapping, _decode(body)['rows'], source_day, at,
                                             _aum_valid_until(calendar, source_day))
                value = source_day.isoformat()
                key = 'aum:'+value
                if count and not invalid and (key in policy.pending or
                        policy.checks.get(key, {}).get('target') != value):
                    policy.succeeded(key, value)
                    _record(summary, 'aum', value, 'CURRENT', changed=count, rejected=0)
        except (ValueError, TypeError, KeyError):
            continue
    pending = sorted(key[4:] for key in policy.pending if key.startswith('aum:'))
    targets = list(dict.fromkeys([day.isoformat(), *pending]))
    selected = [d for d in targets if d <= day.isoformat() and
                policy.due('aum:'+d, days=0, target=d)][:3]
    for value in selected:
        checkpoint()
        key, target = 'aum:'+value, date.fromisoformat(value)
        if not policy.begin(key, value):
            continue
        try:
            body, available = _obtain(ledger, 'etf_share_size', {'trade_date':target.strftime('%Y%m%d')},
                config, now, request, checkpoint, period='source-date:'+value,
                refresh=policy.pending[key]['attempts'] > 1)
            count, invalid = aum_updates(mapping, body['rows'], target, available, _aum_valid_until(calendar, target))
            if not count:
                raise SourceFailure('size batch has no usable products', kind='FIELD_MISMATCH')
            if invalid:
                policy.failed(key, dict(error_kind='FIELD_MISMATCH'))
            else:
                policy.succeeded(key, value)
            _record(summary, 'aum', value, 'PARTIAL' if invalid else 'UPDATED', changed=count, rejected=invalid)
        except (SourceFailure, OSError, ValueError, TypeError, KeyError, psycopg.Error) as exc:
            diagnostic = error_diagnostic(exc)
            policy.failed(key, diagnostic)
            _record(summary, 'aum', value, 'PENDING', **diagnostic)


def _priorities(ledger):
    with ledger._connect() as conn:
        cycles = conn.execute("SELECT etf,body FROM cycles WHERE ns=%s AND status IN ('OPEN','WAIT_BUY','WAIT_EXIT') ORDER BY id LIMIT 100", (ledger.ns,)).fetchall()
        rotation = conn.execute('SELECT sector_id,body FROM rotation WHERE ns=%s ORDER BY sector_id LIMIT 100', (ledger.ns,)).fetchall()
    held = [r[0] for r in cycles]
    sectors = []
    for _, body in cycles:
        row = _decode(body)
        sectors.extend([row.get('primary_sector'), *(p.get('sector_id') for p in row.get('entry_sources', ()))])
    active = [sid for sid, body in rotation if _decode(body).get('member') is True]
    return held, [s for s in dict.fromkeys(sectors + active) if isinstance(s, str)]


def _member_attempt(mapping, sid, now, priorities):
    """Persist bounded rotation progress even when the attempted source fails."""
    cursor = mapping.setdefault('member_maintenance', {})
    cursor['priority_after' if sid in priorities else 'other_after'] = sid
    cursor['last_attempted_at'] = now.isoformat()


def _after_cursor(values, cursor):
    values = sorted(set(values))
    if not isinstance(cursor, str):
        return values
    return [sid for sid in values if sid > cursor] + [sid for sid in values if sid <= cursor]


def sector_targets(mapping, market, priorities, now, *, scope=None, policy=None):
    boards = {r['sector_id']: r for r in (market or {}).get('sectors', ()) if isinstance(r, dict) and 'sector_id' in r}
    current = []
    if boards:
        at = moment(market['observed_at'])
        current, _ = select_directions(boards, {}, [], at)
    cursor = mapping.get('member_maintenance') or {}
    primary = _after_cursor(priorities, cursor.get('priority_after'))
    pending = cursor.get('pending') or []
    secondary = _after_cursor((set(current) | set(mapping['sectors']) | set(pending)) - set(primary), cursor.get('other_after'))
    if policy:
        primary = [sid for sid in primary if policy.due('members:'+sid)]
        secondary = [sid for sid in secondary if policy.due('members:'+sid)]
    # Holdings/active directions take precedence; reserve two slots so they
    # cannot permanently crowd out newly discovered or existing other boards.
    primary_limit = MAX_SECTORS - 2 if secondary else MAX_SECTORS
    chosen = primary[:primary_limit]
    chosen += secondary[:MAX_SECTORS-len(chosen)]
    chosen += [sid for sid in primary if sid not in chosen][:MAX_SECTORS-len(chosen)]
    if scope is not None:
        deferred = [sid for sid in primary + secondary if sid not in chosen]
        scope.update(candidate_count=len(primary)+len(secondary), selected_count=len(chosen),
                     deferred_count=len(deferred), deferred=deferred[:16],
                     new_candidates=sorted((set(priorities) | set(current)) - set(mapping['sectors'])),
                     method='two-cursor-round-robin-v1')
    return chosen, boards


def _members(ledger, mapping, market, identities, identity_batch, priorities, day, captures,
             now, request, checkpoint, summary, *, review=False, policy=None):
    scope = {}
    targets, boards = sector_targets(mapping, market, priorities, now, scope=scope, policy=policy)
    summary['members_scope'] = scope
    identity_at = None
    if identities and identity_batch:
        with ledger._connect() as conn:
            row = conn.execute('SELECT collected_at FROM supplier_batches WHERE batch_id=%s AND collected_at<=%s',
                               (identity_batch, now)).fetchone()
            identity_at = row[0] if row else None
    latest_day = day
    for sid in targets:
        checkpoint()
        key = 'members:'+sid
        day = min(latest_day, date.fromisoformat(policy.target(key, latest_day.isoformat()))) if policy else latest_day
        if policy and not policy.begin(key, day.isoformat()):
            continue
        _member_attempt(mapping, sid, now, priorities)
        old = mapping['sectors'].get(sid, {})
        name = boards.get(sid, {}).get('sector_name') or old.get('sector_name') or sid
        valid_old = usable_members(old, now, latest_day)
        if valid_old and old['as_of_date'] >= day.isoformat() and not review:
            if policy:
                policy.succeeded(key, day.isoformat())
            _record(summary, 'members', sid, 'CURRENT', usable=True, known=len(old['members']),
                    expected_count=old['reported_count'], missing=0, as_of_date=old['as_of_date'])
            continue
        selected, source = (old, 'existing_mapping') if valid_old else (None, None)
        # Hydrate an existing complete cached reference before spending HTTP
        # budget. Cancellation/deadline still propagates through checkpoint.
        try:
            history = cached_members(ledger, sid, day, now, identities, identity_batch, identity_at, name, checkpoint)
            if history and (selected is None or history['as_of_date'] > selected['as_of_date']):
                selected, source = history, 'history_cache'
                mapping['sectors'][sid] = history
        except (OSError, ValueError, TypeError, KeyError, psycopg.Error) as exc:
            _record(summary, 'members', sid, 'PENDING', usable=False, error=type(exc).__name__)
        if review and identities and identity_at is not None and (
                selected is None or selected['as_of_date'] < day.isoformat()):
            # A fresh review may fail after a same-day list was already saved.
            # Recover that complete source first, while still refreshing below.
            checkpoint()
            try:
                with ledger._connect() as conn:
                    current_cache = saved_pages(conn, sid, day.isoformat(), now)
                candidate = assemble_members(current_cache, day.isoformat(), identities,
                                             identity_batch, identity_at, name)
                if usable_members(candidate, now, day):
                    selected, source = candidate, 'target_cache'
                    mapping['sectors'][sid] = candidate
            except (OSError, ValueError, TypeError, KeyError, IndexError, psycopg.Error) as exc:
                _record(summary, 'members', sid, 'PENDING', usable=False, error=type(exc).__name__)
        if selected:
            _record(summary, 'members', sid, 'RETAINED', usable=True, source=source,
                    known=len(selected['members']), expected_count=selected['reported_count'], missing=0,
                    as_of_date=selected['as_of_date'], available_at=selected.get('available_at'),
                    reason='REVIEW_PENDING' if review and selected['as_of_date'] == day.isoformat()
                    else 'TARGET_DATE_PENDING')
        else:
            _record(summary, 'members', sid, 'PENDING', usable=False, known=0,
                    expected_count=None, missing=None, reason='NO_USABLE_MEMBERS')
        if not identities or identity_at is None:
            summary['members'][sid]['error'] = 'IDENTITIES_UNAVAILABLE'
            if policy:
                policy.failed(key, dict(error_kind='INVALID_IDENTITY'))
            continue
        if policy and selected and selected['as_of_date'] >= day.isoformat():
            policy.succeeded(key, day.isoformat())
            summary['members'][sid]['status'] = 'CURRENT'
            continue
        try:
            cached = []
            if not review:
                with ledger._connect() as conn:
                    cached = saved_pages(conn, sid, day.isoformat(), now)
            pages = {int(_decode(s)['Index']): (b, _decode(body), at) for b,s,body,at in cached}
            if len(pages) > MAX_PAGES:
                raise SourceFailure('member page limit exceeded', kind='PAGE_MISMATCH')
            offset, total, current_pages = 0, None, []
            for _ in range(MAX_PAGES):
                checkpoint()
                if offset not in pages:
                    if not captures:
                        raise SourceFailure('member source not configured', kind='CONFIG_UNAVAILABLE')
                    scope = dict(day=day.isoformat(), sector_id=sid, offset=offset)
                    if review:
                        # The existing durable job budget still owns this call;
                        # separate its receipt from an earlier non-review page.
                        scope['review'] = True
                    result = request('maintenance:members', scope,
                        lambda: _page(ledger, captures, sid, day.isoformat(), offset))
                    if result.get('status') != 'SAVED':
                        raise SourceFailure.from_receipt(result)
                    batch = result['receipt']['batch']
                    body, at = _read_market(ledger, batch)
                else:
                    batch, body, at = pages[offset]
                expected = body['reported_total']
                if type(expected) is not int or expected <= 0 or (total is not None and expected != total):
                    raise SourceFailure('member page population changed', kind='PAGE_MISMATCH')
                total = expected
                current_pages.append((batch, {'Index': offset}, body, at))
                candidate = assemble_members(current_pages, day.isoformat(), identities,
                                             identity_batch, identity_at, name)
                summary['members'][sid].update(target_known=len(candidate['members']),
                    target_expected_count=candidate['reported_count'], target_missing=candidate['unresolved_count'])
                if not selected:
                    summary['members'][sid].update(known=len(candidate['members']),
                        expected_count=candidate['reported_count'], missing=candidate['unresolved_count'])
                offset += len(body['rows'])
                if offset >= total:
                    break
            if candidate['unresolved_count'] == 0:
                if not selected or candidate['as_of_date'] >= selected['as_of_date']:
                    mapping['sectors'][sid] = candidate
                _record(summary, 'members', sid, 'UPDATED', usable=True, known=len(candidate['members']),
                        expected_count=candidate['reported_count'], missing=0,
                        as_of_date=candidate['as_of_date'], available_at=candidate['available_at'])
                if policy:
                    policy.succeeded(key, day.isoformat())
            else:
                if policy:
                    policy.failed(key, dict(error_kind='INVALID_IDENTITY'))
                summary['members'][sid].update(error='INCOMPLETE_TARGET_MEMBERS',
                    target_known=len(candidate['members']), target_expected_count=candidate['reported_count'],
                    target_missing=candidate['unresolved_count'])
                if not selected:
                    summary['members'][sid].update(known=len(candidate['members']),
                        expected_count=candidate['reported_count'], missing=candidate['unresolved_count'])
        except (SourceFailure, ProbeError, OSError, ValueError, TypeError, KeyError, IndexError, psycopg.Error) as exc:
            summary['members'][sid]['error'] = type(exc).__name__
            summary['members'][sid].update(error_diagnostic(exc))
            if policy:
                policy.failed(key, error_diagnostic(exc))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _official_bytes(url):
    from ..product_verification import official_url
    try:
        url = official_url(url)
    except ValueError as exc:
        raise SourceFailure('unapproved rule reference host') from exc
    # Each counted operation makes one HTTP request, including when a server
    # redirects. A later controlled job may review a changed reference URL.
    with build_opener(_NoRedirect()).open(Request(url, headers={'User-Agent':'stock-v4-reference-maintenance/1.0'}), timeout=10) as response:
        content = response.read(8 * 1024 * 1024 + 1)
    if not content or len(content) > 8 * 1024 * 1024:
        raise SourceFailure('rule reference size invalid')
    return content


def _rule_hash(url):
    return sha256(_official_bytes(url)).hexdigest()


def _maintain(ledger, mapping, market, calendar, config, captures, now, request, checkpoint, mode,
              updated, summary):
    """Return a candidate version plus per-item receipts; caller publishes it.

    Requests are made only through request(). Partial source failures never
    invalidate the old mapping or wait for global data completeness.
    """
    if mode not in ('close', 'review', 'preopen', 'market', 'retry'):
        raise ValueError('invalid maintenance mode')
    now = now.astimezone(CHINA)
    policy = ReferencePolicy(updated, now, mode) if mode in ('market', 'retry') else None
    closed = [date.fromisoformat(d) for d in calendar if date.fromisoformat(d) < now.date()
              or (date.fromisoformat(d) == now.date() and now.hour >= 15)]
    if not closed:
        summary['reason'] = 'COMPLETED_CALENDAR_UNAVAILABLE'
        return _finish(mapping,updated,summary)
    day = max(closed)
    held, priorities = _priorities(ledger)
    identities, classes, identity_batch = defaultdict(list), {}, None
    with ledger._connect() as conn:
        reference = conn.execute("SELECT batch_id FROM supplier_batches WHERE api='stock_basic' "
                                 'AND collected_at<=%s ORDER BY collected_at DESC LIMIT 1', (now,)).fetchone()
        if reference:
            try:
                identities, classes = stock_identity(conn, reference[0]); identity_batch = reference[0]
            except (KeyError, TypeError, ValueError):
                summary['identity_status'] = 'UNAVAILABLE'
    if policy:
        for product in updated['products'].values():
            stats = product.get('stats') or {}
            if stats.get('aum_verified') and not all(k in stats for k in ('aum_available_at', 'aum_valid_until')):
                stats.update(aum_verified=False, aum_status='HISTORICAL_REFERENCE')
        _hydrate_members(ledger, updated, market, identities, identity_batch, day, now, checkpoint, summary, policy=policy)
    if mode != 'preopen':
        _catalog_maintenance(ledger, updated, config, now, day, request, checkpoint, summary, policy)
        if not identities:
            summary['identity_status'] = 'UNAVAILABLE'
        _members(ledger,updated,market,identities,identity_batch,priorities,day,captures,now,request,checkpoint,summary,
                 review=mode == 'review', policy=policy)
        # Held/verified products first, with a rolling two-index lane to avoid
        # starving new catalog products while keeping a fixed request bound.
        primary = list(dict.fromkeys([updated['products'][c]['index_code'] for c in held if c in updated['products']]
                    + [p['index_code'] for p in updated['products'].values() if p.get('rule',{}).get('verified') is True]))
        indexes = sorted({p['index_code'] for p in updated['products'].values() if p.get('list_status','L') == 'L'
                          and isinstance(p.get('index_code'),str) and INDEX_CODE.fullmatch(p['index_code'])})
        rotating = [i for i in indexes if i not in primary[:MAX_INDEXES-2]]
        if rotating:
            shift = day.toordinal() * 2 % len(rotating)
            rotating = rotating[shift:] + rotating[:shift]
        candidates = list(dict.fromkeys(primary[:MAX_INDEXES-2] + rotating + primary))
        if policy:
            candidates = [i for i in candidates if policy.due('weights:'+i)]
        targets = candidates[:MAX_INDEXES]
        first = day - timedelta(days=62)
        for index in targets:
            key = 'weights:'+index
            if policy and not policy.begin(key, day.isoformat()):
                continue
            try:
                body, available = _obtain(ledger,'index_weight',dict(index_code=index,start_date=first.strftime('%Y%m%d'),end_date=day.strftime('%Y%m%d')),
                                          config,now,request,checkpoint,
                                          refresh=(mode == 'review' and (index in updated.get('pending_weights',{})
                                              or updated['weights'].get(index,{}).get('complete') is not True))
                                              or bool(policy and policy.pending[key]['attempts'] > 1))
                old = updated['weights'].get(index, {})
                basis = weight_basis(body['rows'],index,first,day,classes,old,identity_batch=identity_batch,
                                     previous_identity_batch=mapping.get('stock_catalog_batch'))
                if old.get('as_of_date','') > basis['as_of_date']:
                    _record(summary,'weights',index,'RETAINED',reason='OLDER_SOURCE')
                    if policy:
                        policy.failed(key, dict(error_kind='DATE_MISMATCH'))
                    continue
                normalized = weight_rows(body['rows'],index,first,day)
                receipt = save_reference(ledger,'index_weight',{'index_code':index,'maintenance_day':now.date().isoformat()},body['rows'],normalized)
                if not basis['complete']:
                    try:
                        scope = dict(index=index, period=now.date().isoformat())
                        cached = _cached(ledger,'weight_constituents',scope)
                        if cached:
                            evidence, _ = _read_market(ledger,cached[0])
                        else:
                            def check_constituents(index=index,scope=scope):
                                url = CSI + index.split('.')[0] + 'cons.xls'
                                try:
                                    evidence = csi_evidence(_official_bytes(url),index.split('.')[0],url)
                                except (XLRDError,ValueError,TypeError,IndexError):
                                    raise SourceFailure('constituent document unavailable') from None
                                return save_market(ledger,'maintenance','weight_constituents',scope,evidence,
                                                   len(evidence['codes']),None,datetime.now(CHINA))
                            checkpoint()
                            response = request('maintenance:weight_constituents',scope,check_constituents)
                            if response.get('status') != 'SAVED':
                                raise SourceFailure.from_receipt(response)
                            evidence, _ = _read_market(ledger,response['receipt']['batch'])
                        known = sum((decimal(r['weight']) for r in basis['rows']),Decimal(0))
                        complete = (set(evidence['codes']) == {r['code'] for r in basis['rows']}
                                    and date.fromisoformat(basis['as_of_date']) <= date.fromisoformat(evidence['as_of']) <= day
                                    and abs(known-1) <= Decimal('.005'))
                        if complete:
                            basis.update(complete=True,completeness_basis='CSI_SET_MATCH',
                                         csi_evidence={k:v for k,v in evidence.items() if k!='codes'})
                    except (SourceFailure,OSError,ValueError,TypeError,KeyError,psycopg.Error) as exc:
                        _record(summary,'weight_evidence',index,'NOT_CHECKED',error=type(exc).__name__, **error_diagnostic(exc))
                if old.get('complete') is True and basis.get('complete') is not True:
                    # A new partial source is retained for audit/next review;
                    # it does not erase the last verified calculation basis.
                    previous_pending = updated.get('pending_weights',{}).get(index,{})
                    if not (basis.get('as_of_date') == previous_pending.get('as_of_date')
                            and basis.get('rows') == previous_pending.get('rows')):
                        basis.update(available_at=available.isoformat(),source_batches=[receipt['batch']])
                        updated.setdefault('pending_weights',{})[index] = basis
                    _record(summary,'weights',index,'RETAINED',as_of=old['as_of_date'],
                            pending_as_of=basis['as_of_date'],reason='NEW_WEIGHT_EVIDENCE_PENDING')
                    if policy:
                        policy.failed(key, dict(error_kind='FIELD_MISMATCH'))
                    continue
                same = (basis.get('as_of_date') == old.get('as_of_date') and basis.get('rows') == old.get('rows')
                        and basis.get('complete') == old.get('complete'))
                if not same:
                    basis.update(available_at=available.isoformat(),source_batches=[receipt['batch']])
                    updated['weights'][index] = basis
                if basis.get('complete') is True:
                    updated.get('pending_weights',{}).pop(index,None)
                _record(summary,'weights',index,'CURRENT' if same else 'UPDATED',as_of=basis['as_of_date'],complete=basis['complete'])
                if policy:
                    if basis['complete']:
                        policy.succeeded(key, day.isoformat())
                    else:
                        policy.failed(key, dict(error_kind='FIELD_MISMATCH'))
            except (SourceFailure,OSError,ValueError,TypeError,KeyError,psycopg.Error) as exc:
                diagnostic = error_diagnostic(exc)
                _record(summary,'weights',index,'RETAINED',error=type(exc).__name__, **diagnostic)
                if policy:
                    policy.failed(key, diagnostic)
    if policy:
        _aum_maintenance(ledger, updated, calendar, config, now, day, request, checkpoint, summary, policy)
    if mode == 'preopen':
        try:
            body, available = _obtain(ledger,'etf_share_size',{'trade_date':day.strftime('%Y%m%d')},config,now,request,checkpoint)
            future = [date.fromisoformat(d) for d in calendar if date.fromisoformat(d) > now.date()]
            valid_until = min(future)-timedelta(days=1) if future else now.date()
            count, invalid = aum_updates(updated,body['rows'],day,available,valid_until)
            _record(summary,'aum',day.isoformat(),'UPDATED' if count else 'UNAVAILABLE',changed=count,rejected=invalid)
        except (SourceFailure,OSError,ValueError,TypeError,KeyError,psycopg.Error) as exc:
            _record(summary,'aum',day.isoformat(),'RETAINED',error=type(exc).__name__)
    # Source hashes are checked weekly. A document change requests review; a
    # hash match never grants approval to an unverified product.
    if mode == 'close' or policy:
        week = f'{now.isocalendar().year}-W{now.isocalendar().week}'
        count = 0
        for code in list(dict.fromkeys(held + sorted(updated['products']))):
            rule = updated['products'][code].get('rule') or {}
            if rule.get('verified') is not True:
                continue
            for label, evidence in (rule.get('evidence') or {}).items():
                if count >= MAX_RULE_DOCUMENTS:
                    break
                if not isinstance(evidence,dict) or not evidence.get('sha256'):
                    continue
                url = evidence.get('download') or evidence.get('url')
                if not url:
                    continue
                key = 'rules:'+code+':'+label
                if policy and (not policy.due(key) or not policy.begin(key, day.isoformat())):
                    continue
                scope = dict(url=url, week=week)
                try:
                    cached = _cached(ledger,'rule_hash',scope)
                    if cached:
                        body, at = _read_market(ledger,cached[0])
                    else:
                        def check_document(url=url,scope=scope):
                            digest = _rule_hash(url)
                            return save_market(ledger,'maintenance','rule_hash',scope,{'sha256':digest},1,None,datetime.now(CHINA))
                        checkpoint()
                        receipt = request('maintenance:rule_hash',scope,check_document)
                        count += 1
                        if receipt.get('status') != 'SAVED':
                            raise SourceFailure.from_receipt(receipt)
                        body, at = _read_market(ledger,receipt['receipt']['batch'])
                    same = body['sha256'] == evidence['sha256']
                    _record(summary,'rules',code+':'+label,'UNCHANGED' if same else 'REVIEW_REQUIRED',checked_at=at.isoformat())
                    if not same:
                        rule['review_required'] = True
                        rule['review_reason'] = 'OFFICIAL_REFERENCE_CHANGED'
                        # Keep the last explicit rule until a reviewed replacement
                        # is published; a document hash is not a trading rule.
                    if policy:
                        policy.succeeded(key, day.isoformat())
                except (SourceFailure,OSError,ValueError,TypeError,KeyError,psycopg.Error) as exc:
                    diagnostic = error_diagnostic(exc)
                    _record(summary,'rules',code+':'+label,'NOT_CHECKED',error=type(exc).__name__, **diagnostic)
                    if policy:
                        policy.failed(key, diagnostic)
    if policy:
        summary['retry_queue'] = policy.report()
    return _finish(mapping,updated,summary)


def _finish(mapping, updated, summary):
    if 'reference_maintenance' in updated and 'retry_queue' not in summary:
        pending = updated['reference_maintenance'].get('pending') or {}
        summary['retry_queue'] = dict(pending_count=len(pending),
            review_required=sum(v.get('attempts', 0) >= 4 for v in pending.values()))
    member_scope = summary.get('members_scope')
    if member_scope is not None:
        cursor = updated.setdefault('member_maintenance', {})
        usable = {sid for sid, row in summary.get('members', {}).items()
                  if row.get('usable') is True or row.get('status') in ('CURRENT', 'UPDATED')}
        pending = list(dict.fromkeys([*(cursor.get('pending') or []),
                                     *member_scope.get('new_candidates', []),
                                     *(sid for sid, row in summary.get('members', {}).items()
                                       if row.get('usable') is False)]))
        pending = [sid for sid in pending if sid not in usable]
        cursor['pending'] = pending[:MAX_PENDING_MEMBERS]
        overflow = max(0, len(pending)-MAX_PENDING_MEMBERS)
        cursor['pending_overflow_count'] = overflow
        member_scope.update(pending_count=len(cursor['pending']), overflow_count=overflow)
    summary['unverified_products'] = sum(p.get('rule',{}).get('verified') is not True for p in updated['products'].values())
    if updated != mapping:
        updated['parent_mapping_version'] = mapping['version']
        updated['version'] = 'maintained-' + fingerprint({k:v for k,v in updated.items() if k != 'version'})[:32]
    statuses = [v['status'] for group in summary.values() if isinstance(group,dict) for v in group.values() if isinstance(v,dict) and 'status' in v]
    deferred = (summary.get('members_scope') or {}).get('deferred_count', 0)
    overflow = (summary.get('members_scope') or {}).get('overflow_count', 0)
    summary.update(status='PARTIAL' if any(s in ('RETAINED','PENDING','PARTIAL','UNAVAILABLE','NOT_CHECKED','REVIEW_REQUIRED') for s in statuses)
                   or summary.get('interrupted') or summary.get('reason')
                   or summary.get('identity_status') == 'UNAVAILABLE' or deferred or overflow
                   or (summary.get('cache_members') or {}).get('error')
                   or (summary.get('retry_queue') or {}).get('pending_count')
                   or (summary.get('retry_queue') or {}).get('overflow_count') else 'READY',
                   mapping_version=updated['version'],products=len(updated['products']),sectors=len(updated['sectors']),
                   indexes=len(updated['weights']),changed=updated != mapping)
    refreshed = sum(s=='UPDATED' for s in statuses)
    retained = sum(s=='RETAINED' for s in statuses)
    pending = sum(s in ('PENDING','PARTIAL','UNAVAILABLE','NOT_CHECKED','REVIEW_REQUIRED') for s in statuses)
    retained_text = f'，沿用{retained}项' if retained else ''
    deferred_text = f'；本轮名额外{deferred}个板块待后续维护' if deferred else ''
    if overflow:
        deferred_text += f'；待补队列容量已满，另{overflow}个板块未纳入队列'
    catalog_review = sum(row.get('count', 0) for row in summary.get('catalog_review', {}).values())
    catalog_text = f'；目录另有{catalog_review}条产品范围或指数关联待人工整理' if catalog_review else ''
    summary['message'] = f'资料更新{refreshed}项{retained_text}，待补{pending}项{deferred_text}；映射含{len(updated["sectors"])}板块、{len(updated["products"])}只ETF，其中{summary["unverified_products"]}只产品规则待核验{catalog_text}。'
    return updated, summary


def maintain(ledger, mapping, market, calendar, config, captures, now, request, checkpoint, mode='close'):
    """Keep completed item changes even when the shared budget/window ends."""
    updated, summary = deepcopy(mapping), {'mode':mode,'version':VERSION}
    try:
        return _maintain(ledger,mapping,market,calendar,config,captures,now,request,checkpoint,mode,updated,summary)
    except Interrupted as exc:
        summary['interrupted'] = str(exc)
        return _finish(mapping,updated,summary)
    except psycopg.Error:
        summary['reason'] = 'REFERENCE_STORAGE_UNAVAILABLE'
        return _finish(mapping,updated,summary)
