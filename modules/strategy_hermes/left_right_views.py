"""Read-only shape screening and dated score curves over committed local data.

The structure_watch_v2 formulas remain frozen. Their database adapter is new;
the user's ranking choice is RIGHT setup score descending after full screening.
No provider, refresh, score producer or task queue is called by these views.
"""
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qs

from . import left_right_info as info
from .structure_screen import RULES, VERSION, evaluate_structure, score_gate

MAX_SCREEN_STOCKS = 10000
SCREEN_CHUNK = 250


def _calendar(conn, day, count):
    if not day:
        return []
    return [row[0].isoformat() for row in reversed(conn.execute("""SELECT cal_date
        FROM supplier_trade_calendar WHERE exchange='SSE' AND is_open AND cal_date<=%s
        ORDER BY cal_date DESC LIMIT %s""", (day, count)).fetchall())]


def _seed_rows(archives, days, codes=None):
    return {(row['trade_date'], row['ts_code'], row['family']): row
            for archive in archives if archive['trade_date'] in days
            for row in archive['items'] if codes is None or row['ts_code'] in codes}


def _assessments(conn, ns, days, codes=None, seeds=(), cutoff=None, cutoffs=None):
    if not days:
        return {}
    values = _seed_rows(seeds, days, set(codes) if codes is not None else None)
    clause = " AND code=ANY(%s)" if codes is not None else ""
    args = (ns, [date.fromisoformat(day) for day in days]) + ((codes,) if codes is not None else ())
    if cutoff is not None:
        values = {key: row for key, row in values.items() if row.get('published_at')
                  and datetime.fromisoformat(row['published_at']) <= cutoff}
        clause += " AND published_at<=%s"
        args += (cutoff,)
    bound = len(days) * 2 * (len(codes) if codes is not None else MAX_SCREEN_STOCKS)
    rows = conn.execute("""SELECT body FROM left_right_assessments
        WHERE ns=%s AND trade_date=ANY(%s) AND family IN ('LEFT','RIGHT')""" + clause +
        " ORDER BY trade_date,code,family LIMIT %s", args + (bound + 1,)).fetchall()
    if len(rows) > bound:
        raise ValueError('LEFT/RIGHT saved universe exceeds screening bound')
    for (raw,) in rows:
        row = info._object(raw)
        values[(row['trade_date'], row['ts_code'], row['family'])] = row
    if cutoffs is not None:
        values = {key: row for key, row in values.items() if cutoffs.get(key[1]) is not None
                  and row.get('published_at') and datetime.fromisoformat(row['published_at']) <= cutoffs[key[1]]}
    return values


def _screen_latest(conn, ns, day, seeds=()):
    """Keep the SQL publication times alongside the full current response rows."""
    values = _seed_rows(seeds, [day])
    bound = MAX_SCREEN_STOCKS * 2
    rows = conn.execute("""SELECT body,published_at FROM left_right_assessments
        WHERE ns=%s AND trade_date=ANY(%s) AND family IN ('LEFT','RIGHT')
        ORDER BY trade_date,code,family LIMIT %s""", (ns, [date.fromisoformat(day)], bound + 1)).fetchall()
    if len(rows) > bound:
        raise ValueError('LEFT/RIGHT saved universe exceeds screening bound')
    indexed_publications = {}
    for raw, published in rows:
        row = info._object(raw)
        key = row['trade_date'], row['ts_code'], row['family']
        values[key] = row
        indexed_publications[key] = published
    return values, indexed_publications


def _screen_assessments(conn, ns, days, codes, latest, seeds=(), cutoff=None, cutoffs=None,
                        *, indexed_publications):
    """Read only historical score inputs; reuse the full current response rows.

    Project from body so JSON types, missing inputs and legacy precedence match
    _assessments. SQL overrides visible seeds before per-stock cutoffs are applied;
    a later SQL revision must remain a gap rather than resurrect an older seed.
    """
    if not days or not codes:
        return {}
    history_days, current_day = days[:-1], days[-1]
    fields = ('trade_date', 'ts_code', 'family', 'published_at', 'setup_score',
              'status', 'version_id', 'quality', 'subscores')
    values = {key: {field: row[field] for field in fields if field in row}
              for key, row in _seed_rows(seeds, history_days, set(codes)).items()}
    clause = ''
    args = (ns, [date.fromisoformat(day) for day in history_days], codes)
    if cutoff is not None:
        values = {key: row for key, row in values.items() if row.get('published_at')
                  and datetime.fromisoformat(row['published_at']) <= cutoff}
        clause = ' AND a.published_at<=%s'
        args += (cutoff,)
    if history_days:
        bound = len(history_days) * 2 * len(codes)
        # Expand body once: separate -> calls can repeatedly decompress TOAST.
        # jsonb columns preserve the original values, including nested weights.
        rows = conn.execute("""SELECT to_jsonb(p)
            FROM left_right_assessments a
            CROSS JOIN LATERAL jsonb_to_record(a.body) AS p(
                trade_date jsonb,ts_code jsonb,family jsonb,published_at jsonb,setup_score jsonb,
                status jsonb,version_id jsonb,quality jsonb,subscores jsonb)
            WHERE a.ns=%s AND a.trade_date=ANY(%s) AND a.code=ANY(%s) AND a.family IN ('LEFT','RIGHT')""" + clause +
            ' ORDER BY a.trade_date,a.code,a.family LIMIT %s', args + (bound + 1,)).fetchall()
        if len(rows) > bound:
            raise ValueError('LEFT/RIGHT saved universe exceeds screening bound')
        for (raw,) in rows:
            row = info._object(raw)
            values[(row['trade_date'], row['ts_code'], row['family'])] = row
    current = _seed_rows(seeds, [current_day], set(codes))
    if cutoff is not None:
        current = {key: row for key, row in current.items() if row.get('published_at')
                   and datetime.fromisoformat(row['published_at']) <= cutoff}
    for code in codes:
        for family in ('LEFT', 'RIGHT'):
            key = current_day, code, family
            # Replay the original SQL filter before overwriting a visible seed.
            # SQL and body timestamps can differ; the latter is filtered below.
            published = indexed_publications.get(key)
            if published is not None and (cutoff is None or published <= cutoff):
                current[key] = latest[key]
    values.update(current)
    if cutoffs is not None:
        values = {key: row for key, row in values.items() if cutoffs.get(key[1]) is not None
                  and row.get('published_at') and datetime.fromisoformat(row['published_at']) <= cutoffs[key[1]]}
    return values


def _bars(conn, codes, days, cutoff, cutoffs=None):
    """20/30 exact exchange dates; no missing sessions are compressed out."""
    if not codes or not days or cutoff is None:
        return {}
    rows = conn.execute("""SELECT d.code,d.trade_date,d.open,d.high,d.low,d.close,
            d.volume_shares,d.amount_cny,d.return_ratio,f.body->>'adj_factor',
            daily_batch.collected_at,feature_batch.collected_at
        FROM supplier_daily_bars d JOIN supplier_batches daily_batch ON daily_batch.batch_id=d.batch_id
        LEFT JOIN (supplier_lr_features f JOIN supplier_market_batches feature_batch ON feature_batch.batch_id=f.batch_id)
          ON f.api='adj_factor' AND f.code=d.code AND f.trade_date=d.trade_date AND feature_batch.collected_at<=%s
        WHERE d.code=ANY(%s) AND d.trade_date=ANY(%s) AND daily_batch.collected_at<=%s
        ORDER BY d.code,d.trade_date""",
        (cutoff, codes, [date.fromisoformat(day) for day in days], cutoff)).fetchall()
    result = {}
    for row in rows:
        stock_cutoff = cutoffs.get(row[0]) if cutoffs is not None else cutoff
        if stock_cutoff is None or row[-2] > stock_cutoff:
            continue
        bar = dict(trade_date=row[1].isoformat(), **{key: info._number(value) for key, value in zip(
            ('open', 'high', 'low', 'close', 'volume_shares', 'amount_cny', 'return_ratio', 'adj_factor'), row[2:-2])})
        if row[-1] is None or row[-1] > stock_cutoff:
            bar['adj_factor'] = None
        result[(row[0], row[1].isoformat())] = bar
    return result


def _quality(row):
    value = row.get('quality')
    if isinstance(value, dict):
        # Migrated EOD producers retain diagnostics as quality. In the frozen
        # selection adapter the same complete READY result had quality=1.0.
        return 1.0 if row.get('status') == 'READY' and row.get('version_id') in info.VERSIONS else None
    return info._number(value, minimum=0, maximum=1)


def _score(row):
    return dict(score=row.get('setup_score'), status=row.get('status'), quality=_quality(row),
                version_id=row.get('version_id'), subscores=row.get('subscores'))


def _scores(rows, code, days):
    return [dict(trade_date=day, **{family.lower(): _score(rows[(day, code, family)])
            for family in ('LEFT', 'RIGHT') if (day, code, family) in rows}) for day in days]


def _main_board(row, metadata, day):
    code, suffix = row['ts_code'].split('.')
    if not ((suffix == 'SH' and code.startswith(('600', '601', '603', '605')))
            or (suffix == 'SZ' and code.startswith(('000', '001', '002', '003')))):
        return False
    details = metadata.get(row['ts_code'], {})
    name = (row.get('name') or details.get('name') or '').upper().replace(' ', '')
    if name.startswith(('ST', '*ST', 'S*ST')) or '退' in name:
        return False
    for key, forbidden in (('list_date', lambda d: d > day), ('delist_date', lambda d: d <= day)):
        value = details.get(key)
        if value:
            try:
                if forbidden(date.fromisoformat(value).isoformat()):
                    return False
            except (TypeError, ValueError):
                pass  # Unknown optional listing metadata does not exclude the stock.
    return True


def _metadata(conn, codes, day, cutoff, cutoffs=None):
    if not codes or cutoff is None:
        return {}
    per_stock = " AND observed_at<=(%s::jsonb->>entity_key)::timestamptz" if cutoffs is not None else ""
    args = (codes, day, cutoff) + ((info.encode({code: value.isoformat() if value else None for code, value in cutoffs.items()}),) if cutoffs is not None else ())
    rows = conn.execute("""SELECT DISTINCT ON(entity_key) entity_key,body FROM supplier_reference_rows
        WHERE dataset='stock_basic' AND entity_key=ANY(%s) AND business_date<=%s AND observed_at<=%s
        """ + per_stock + " ORDER BY entity_key,observed_at DESC,business_date DESC,batch_id DESC", args).fetchall()
    return {code: info._object(raw) for code, raw in rows}


def _cutoff(rows):
    stamps = [datetime.fromisoformat(row['published_at']) for row in rows if row.get('published_at')]
    return max(stamps) if stamps else None


def _summary(items):
    scores = [Decimal(row['setup_score']) for row in items if row.get('setup_score') is not None]
    returns = [Decimal(row['return_ratio']) for row in items if row.get('return_ratio') is not None]
    return dict(total=len(items), valid_score_count=len(scores), max_score=str(max(scores)) if scores else None,
                valid_return_count=len(returns), up_count=sum(value > 0 for value in returns),
                high_return_count=sum(value >= Decimal('.095') for value in returns))


def screen_saved(rows, bars, days, universe, metadata, *, mode, parameters):
    """Pure batch adapter, preserving the two frozen pattern decisions."""
    counts = dict(universe=len(universe), excluded=0, evaluated=0, insufficient=0, matched=0,
                  watch=0, confirmed=0, returned=0)
    failures, matches = Counter(), []
    for code, representative in universe.items():
        if not _main_board(representative, metadata, days[-1]):
            counts['excluded'] += 1
            continue
        scores = _scores(rows, code, days[-6:])
        evaluated = evaluate_structure(scores, [bars.get((code, day), {'trade_date': day}) for day in days],
                                       pattern=mode, **parameters)
        if evaluated['status'] == 'INSUFFICIENT_DATA':
            counts['insufficient'] += 1
            failures.update(evaluated.get('reasons', [])[:1])
            continue
        counts['evaluated'] += 1
        if evaluated['status'] != 'MATCH':
            continue
        latest = rows[(days[-1], code, 'RIGHT')]
        matches.append(dict(latest, structure=evaluated['structure']))
    counts.update(matched=len(matches), watch=sum(row['structure']['stage'] == 'WATCH' for row in matches),
                  confirmed=sum(row['structure']['stage'] == 'CONFIRMED' for row in matches))
    return matches, counts, failures


def read_structure(ledger, *, day, available, mode, parameters, query, page, page_size,
                   seeds, eod_file=None, history_file=None):
    counts = dict(universe=0, excluded=0, evaluated=0, insufficient=0, matched=0, watch=0, confirmed=0, returned=0)
    result = dict(available_dates=available, selected_trade_date=day, family='RIGHT', structure_mode=mode,
        parameters=parameters, strategy_version=VERSION, q=query, page=page, page_size=page_size,
        items=[], total=0, pages=0, summary=_summary([]), structure_counts=counts, insufficient_reasons=[],
        batch=dict(published_at=None, version_id=None, source=None), issues=list(seeds['issues']),
        rules=[value for key, value in RULES[mode].items() if key in ('description', 'price_rule', 'common_rule')])
    if not day:
        return result
    with ledger._connect() as conn, conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout='15000ms'")
        days = _calendar(conn, day, 20)
        # Small pre-index snapshots remain readable; indexed rows take precedence.
        legacy = info.load_history(ledger, eod_file, history_file, dates=days or [day])
        result['issues'] = sorted(set(result['issues']) | set(legacy['issues']))
        latest, indexed_publications = _screen_latest(conn, ledger.ns, day, legacy['archives'])
        cutoff = _cutoff(latest.values())
        result['input_as_of'] = cutoff.isoformat() if cutoff else None
        universe = {code: row for (_, code, _), row in latest.items()}
        cutoffs = dict.fromkeys(universe)
        for (_, code, _), row in latest.items():
            stamp = _cutoff([row])
            if stamp is not None and (cutoffs[code] is None or stamp > cutoffs[code]):
                cutoffs[code] = stamp
        counts['universe'] = len(universe)
        if len(universe) > MAX_SCREEN_STOCKS:
            raise ValueError('LEFT/RIGHT saved universe exceeds screening bound')
        if len(days) != 20 or days[-1] != day:
            counts['insufficient'] = len(universe)
            result['insufficient_reasons'] = [dict(reason='需完整20个交易日日历', count=len(universe))]
            return result
        metadata = _metadata(conn, sorted(universe), day, cutoff, cutoffs)
        matches, failures = [], Counter()
        counts['universe'] = 0
        for start in range(0, len(universe), SCREEN_CHUNK):
            codes = sorted(universe)[start:start + SCREEN_CHUNK]
            eligible = [code for code in codes if _main_board(universe[code], metadata, day)]
            rows = _screen_assessments(conn, ledger.ns, days[-6:], eligible, latest,
                                       legacy['archives'], cutoff=cutoff, cutoffs=cutoffs,
                                       indexed_publications=indexed_publications)
            candidates = [code for code in eligible
                          if score_gate(_scores(rows, code, days[-6:]), parameters['min_left'],
                                         parameters['min_right_gain'])['status'] == 'MATCH']
            bars = _bars(conn, candidates, days, cutoff, cutoffs)
            batch, stats, missing = screen_saved(rows, bars, days, {code: universe[code] for code in codes},
                                                 metadata, mode=mode, parameters=parameters)
            matches.extend(batch)
            failures.update(missing)
            for key, value in stats.items():
                counts[key] += value
    # Filter and paginate only after the entire saved universe has been evaluated.
    matches.sort(key=lambda row: (-Decimal(row['setup_score']), row['ts_code']))
    filtered = [row for row in matches if not query or query.casefold() in
                (row['ts_code'] + ' ' + (row.get('name') or '')).casefold()]
    chosen = filtered[(page - 1) * page_size:page * page_size]
    versions = sorted({row['version_id'] for row in latest.values() if row.get('family') == 'RIGHT' and row.get('version_id')})
    published = [row['published_at'] for row in latest.values() if row.get('published_at')]
    result.update(items=chosen, summary=_summary(matches), total=len(filtered), pages=(len(filtered) + page_size - 1) // page_size,
        batch=dict(published_at=max(published) if published else None, version_id=versions[0] if len(versions) == 1 else None,
                   source=next((row['source'] for row in latest.values() if row.get('source')), None)),
        insufficient_reasons=[dict(reason=reason, count=count) for reason, count in failures.most_common()])
    counts['returned'] = len(chosen)
    return result


def _history_query(query):
    if not isinstance(query, str) or len(query) > 1000:
        raise ValueError('invalid LEFT/RIGHT history query')
    args = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3, errors='strict')
    if set(args) - {'ts_code', 'trade_date', 'days'} or any(len(value) != 1 for value in args.values()):
        raise ValueError('invalid LEFT/RIGHT history fields')
    code, day, count = (args.get(key, [default])[0] for key, default in
                        (('ts_code', ''), ('trade_date', ''), ('days', '30')))
    if not info.STOCK.fullmatch(code) or not count.isascii() or not count.isdigit() or not 1 <= int(count) <= 30:
        raise ValueError('valid stock and 1–30 history days required')
    if day:
        info._day(day)
    return code, day, int(count)


def read_score_history(ledger, query, eod_file=None, history_file=None):
    code, day, count = _history_query(query)
    available = info._available_dates(ledger)
    seeds = info.load_history(ledger, eod_file, history_file, include_database=False)
    available = sorted(set(available) | {row['trade_date'] for row in seeds['archives']}, reverse=True)
    day = day or (available[0] if available else None)
    result = dict(ts_code=code, selected_trade_date=day, window=count, trade_dates=[], items=[], bars=[],
                  issues=list(seeds['issues']), recompute_on_client=False)
    if not day:
        return result
    with ledger._connect() as conn, conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout='5000ms'")
        days = _calendar(conn, day, count)
        legacy = info.load_history(ledger, eod_file, history_file, dates=days)
        result['issues'] = sorted(set(result['issues']) | set(legacy['issues']))
        saved_cutoff = conn.execute('SELECT max(published_at) FROM left_right_assessments WHERE ns=%s AND trade_date=%s AND code=%s',
                                    (ledger.ns, day, code)).fetchone()[0]
        seed_cutoff = _cutoff(row for archive in legacy['archives'] if archive['trade_date'] == day
                              for row in archive['items'] if row['ts_code'] == code)
        if saved_cutoff is None and seed_cutoff is None:
            saved_cutoff = conn.execute('SELECT max(published_at) FROM left_right_assessments WHERE ns=%s AND trade_date=%s',
                                        (ledger.ns, day)).fetchone()[0]
        cutoff = max(value for value in (saved_cutoff, seed_cutoff) if value is not None) if saved_cutoff or seed_cutoff else None
        result['input_as_of'] = cutoff.isoformat() if cutoff else None
        rows = _assessments(conn, ledger.ns, days, [code], legacy['archives'], cutoff=cutoff)
        bars = _bars(conn, [code], days, cutoff)
    result['trade_dates'] = days
    if len(days) < count:
        result['issues'].append('LR_HISTORY_CALENDAR_INCOMPLETE')
    for when in days:
        for family in ('LEFT', 'RIGHT'):
            row = rows.get((when, code, family), {})
            result['items'].append(dict(trade_date=when, family=family, setup_score=row.get('setup_score'),
                status=row.get('status', 'MISSING'), version_id=row.get('version_id'),
                reasons=row.get('reasons') or [], missing_reason=None if row else 'NO_ASSESSMENT',
                published_at=row.get('published_at')))
        bar = bars.get((code, when))
        if bar is None:
            # Older static archives contain genuine dated closing observations.
            saved = next((rows[(when, code, family)] for family in ('RIGHT', 'LEFT')
                          if (when, code, family) in rows and rows[(when, code, family)].get('close') is not None), {})
            bar = {key: saved.get(key) for key in ('close', 'return_ratio')}
        result['bars'].append(dict(trade_date=when, close=bar.get('close'), return_ratio=bar.get('return_ratio')))
    return result
