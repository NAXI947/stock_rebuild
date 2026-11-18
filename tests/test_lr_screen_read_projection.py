"""In-memory regression checks for bounded LR screen reads; no database needed."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
import re
from unittest import TestCase
from unittest.mock import Mock, patch

from modules.strategy_hermes import left_right_info as info, left_right_views as views
from tests.test_lr_views import saved_sample


FIELDS = {'trade_date', 'ts_code', 'family', 'published_at', 'setup_score',
          'status', 'version_id', 'quality', 'subscores'}
PARAMETERS = dict(min_left=70, min_right_gain=3, max_ma20_distance=.1)


def canonical(row):
    # Match the existing JSON reader, including exact numeric strings.
    return info._object(json.dumps(row, ensure_ascii=False))


def archives(rows):
    return [dict(trade_date=day, items=[row for (when, _, _), row in rows.items() if when == day])
            for day in sorted({key[0] for key in rows})]


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class ProjectionConnection:
    """Model only the promised SELECT projection and its bound parameters.

    This checks read scope and JSON preservation, while real PostgreSQL syntax
    remains covered by the existing integration suite.
    """
    def __init__(self, rows):
        self.rows = list(rows.values())
        self.reads = []

    def transaction(self):
        return nullcontext(self)

    def execute(self, query, args=()):
        text = ' '.join(query.split())
        if text.startswith('SET '):
            return Result()
        if not text.startswith('SELECT ') or 'FROM left_right_assessments' not in text:
            raise AssertionError('Unexpected storage access: ' + text)
        if len(args) not in (4, 5):
            raise AssertionError('Expected bounded namespace/date/code SELECT')
        ns, dates, codes = args[:3]
        cutoff = args[3] if len(args) == 5 else None
        limit = args[-1]
        if ns != 'projection-test' or limit != len(dates) * len(codes) * 2 + 1:
            raise AssertionError('Screen read lost its scoped row bound')
        projection = re.match(
            r'^SELECT to_jsonb\(p\) FROM left_right_assessments (?:AS )?a '
            r'CROSS JOIN LATERAL jsonb_to_record\(a\.body\) AS p\(([^)]+)\) WHERE ', text)
        if projection is None or len(re.findall(r'\bjsonb_to_record\s*\(', text)) != 1:
            raise AssertionError('Screen must unpack the original JSON body exactly once')
        columns = [re.fullmatch(r'([a-z_]+)\s+(jsonb)', column.strip())
                   for column in projection.group(1).split(',')]
        if (len(columns) != len(FIELDS) or any(column is None for column in columns)
                or {column.group(1) for column in columns} != FIELDS):
            raise AssertionError('All nine screen fields must retain their JSONB types')
        conditions = text[projection.end():]
        if (not all(re.search(r'\ba\.' + name + r'\s*(?:=|IN\b)', conditions)
                    for name in ('ns', 'trade_date', 'code', 'family'))
                or re.search(r'(?<![\w.])(?:ns|trade_date|code|family|published_at)\s*(?:=|<=|IN\b)', conditions)
                or re.search(r'\bp\.', conditions)
                or not re.search(r'ORDER BY a\.trade_date,\s*a\.code,\s*a\.family LIMIT %s$', conditions)
                or bool(re.search(r'\ba\.published_at\s*<=\s*%s', conditions)) != (cutoff is not None)):
            raise AssertionError('Filtering, publication cutoff and ordering must use SQL columns on a')
        selected_days = {when.isoformat() for when in dates}
        self.reads.append(dict(days=selected_days, codes=set(codes), cutoff=cutoff, limit=limit))
        selected = []
        for row in sorted(self.rows, key=lambda item: (item['trade_date'], item['ts_code'], item['family'])):
            if row['trade_date'] not in selected_days or row['ts_code'] not in codes:
                continue
            stamp = row.get('published_at')
            if cutoff is not None and (not stamp or datetime.fromisoformat(stamp) > cutoff):
                continue
            selected.append((json.dumps({key: row.get(key) for key in FIELDS}, ensure_ascii=False),))
        return Result(selected[:limit])


class Ledger:
    ns = 'projection-test'

    def __init__(self, connection):
        self.connection = connection

    def _connect(self):
        return nullcontext(self.connection)


class ScreenProjectionTests(TestCase):
    def setUp(self):
        rows, self.bars, self.days = saved_sample()
        self.rows = {key: canonical(row) for key, row in rows.items()}
        self.six = self.days[-6:]
        self.day = self.days[-1]
        self.code = '003910.SZ'
        self.cutoff = datetime.fromisoformat(self.day + 'T16:00:00+08:00')

    def latest(self, rows):
        return {key: row for key, row in rows.items() if key[0] == self.day}

    def publications(self, rows):
        return {key: datetime.fromisoformat(row['published_at'])
                for key, row in self.latest(rows).items() if row.get('published_at')}

    def read(self, rows=None, *, latest=None, seeds=(), cutoff=None, cutoffs=None, codes=None,
             indexed_publications=None):
        rows = self.rows if rows is None else rows
        conn = ProjectionConnection(rows)
        result = views._screen_assessments(conn, 'projection-test', self.six,
            [self.code] if codes is None else codes, self.latest(self.rows) if latest is None else latest,
            seeds=seeds, cutoff=self.cutoff if cutoff is None else cutoff,
            cutoffs={self.code: self.cutoff} if cutoffs is None else cutoffs,
            indexed_publications=self.publications(rows) if indexed_publications is None else indexed_publications)
        return result, conn

    def test_current_loader_preserves_sql_publication_separately_from_full_body(self):
        key = (self.day, self.code, 'RIGHT')
        sql_row = dict(self.rows[key], setup_score='99', published_at=self.day + 'T15:00:00+08:00',
                       note='complete indexed body')
        sql_published = datetime.fromisoformat(self.day + 'T19:00:00+08:00')
        conn = Mock()
        conn.execute.return_value = Result([(json.dumps(sql_row), sql_published)])
        latest, publications = views._screen_latest(conn, 'projection-test', self.day, archives(self.rows))
        self.assertEqual(latest[key], sql_row)
        self.assertEqual(publications, {key: sql_published})
        left = (self.day, self.code, 'LEFT')
        self.assertEqual(latest[left], self.rows[left])
        self.assertNotIn(left, publications)
        self.assertEqual(conn.execute.call_args.args[1][1], [self.cutoff.date()])

    def test_projected_history_preserves_both_frozen_pattern_results_and_full_latest(self):
        for key, row in self.rows.items():
            row.update(note='saved explanation', sectors=[{'id': 'kaipanla:000001', 'name': '原板块'}],
                       ui_extension={'source_detail': 'retain this complete published row'})
            if key[0] != self.day:
                row['large_unused_history_field'] = 'x' * 10000
        original = deepcopy(self.rows)
        projected, conn = self.read()
        self.assertEqual(conn.reads[0]['days'], set(self.six[:-1]))
        self.assertEqual(conn.reads[0]['codes'], {self.code})
        self.assertNotIn(self.day, conn.reads[0]['days'])
        for key, row in projected.items():
            self.assertEqual(set(row), FIELDS if key[0] != self.day else set(original[key]))
        universe = {self.code: self.rows[(self.day, self.code, 'RIGHT')]}
        for mode in ('STARTUP', 'PULLBACK'):
            with self.subTest(mode=mode):
                expected = views.screen_saved(self.rows, self.bars, self.days, universe, {},
                                              mode=mode, parameters=PARAMETERS)
                actual = views.screen_saved(projected, self.bars, self.days, universe, {},
                                            mode=mode, parameters=PARAMETERS)
                self.assertEqual(actual, expected)
                self.assertEqual(actual[1]['matched'], 1)
                self.assertEqual(actual[0][0]['ui_extension'], original[(self.day, self.code, 'RIGHT')]['ui_extension'])
        self.assertEqual(self.rows, original)

    def test_sql_overrides_legacy_before_the_per_stock_cutoff_removes_a_late_revision(self):
        key = (self.six[-2], self.code, 'RIGHT')
        seeds = archives(self.rows)
        indexed = deepcopy(self.rows)
        indexed[key].update(setup_score='99', published_at=self.day + 'T17:00:00+08:00')
        result, _ = self.read(indexed, seeds=seeds,
            cutoff=datetime.fromisoformat(self.day + 'T18:00:00+08:00'))
        self.assertNotIn(key, result)  # An older legacy value must not fill this slot.
        gate = views.score_gate(views._scores(result, self.code, self.six))
        self.assertEqual(gate['status'], 'INSUFFICIENT_DATA')

    def test_sql_after_global_cutoff_keeps_the_available_legacy_revision(self):
        key = (self.six[-2], self.code, 'RIGHT')
        indexed = deepcopy(self.rows)
        indexed[key].update(setup_score='99', published_at=self.day + 'T19:00:00+08:00')
        result, _ = self.read(indexed, seeds=archives(self.rows),
            cutoff=datetime.fromisoformat(self.day + 'T18:00:00+08:00'))
        self.assertEqual(result[key]['setup_score'], self.rows[key]['setup_score'])

    def test_indexed_zero_overrides_a_nonzero_legacy_score(self):
        key = (self.six[-2], self.code, 'RIGHT')
        indexed = deepcopy(self.rows)
        indexed[key]['setup_score'] = '0'
        result, _ = self.read(indexed, seeds=archives(self.rows))
        self.assertEqual(result[key]['setup_score'], '0')

    def test_current_sql_after_global_cutoff_falls_back_to_the_visible_full_seed(self):
        key = (self.day, self.code, 'RIGHT')
        latest = deepcopy(self.latest(self.rows))
        latest[key].update(setup_score='99', published_at=self.day + 'T15:00:00+08:00')
        publications = self.publications(self.rows)
        publications[key] = datetime.fromisoformat(self.day + 'T19:00:00+08:00')
        result, _ = self.read(latest=latest, seeds=archives(self.rows), indexed_publications=publications,
            cutoff=datetime.fromisoformat(self.day + 'T18:00:00+08:00'))
        self.assertEqual(result[key], self.rows[key])
        self.assertNotEqual(result[key]['setup_score'], latest[key]['setup_score'])

    def test_visible_current_sql_overrides_seed_before_its_body_is_rejected(self):
        key = (self.day, self.code, 'RIGHT')
        latest = deepcopy(self.latest(self.rows))
        # SQL is globally visible; its JSON timestamp is beyond both waterlines.
        latest[key].update(setup_score='99', published_at=self.day + 'T19:00:00+08:00')
        publications = self.publications(self.rows)
        publications[key] = datetime.fromisoformat(self.day + 'T17:00:00+08:00')
        result, _ = self.read(latest=latest, seeds=archives(self.rows), indexed_publications=publications,
            cutoff=datetime.fromisoformat(self.day + 'T18:00:00+08:00'))
        self.assertNotIn(key, result)
        self.assertEqual(views.score_gate(views._scores(result, self.code, self.six))['status'],
                         'INSUFFICIENT_DATA')

    def test_unpublished_current_seed_is_not_resurrected_from_latest(self):
        key = (self.day, self.code, 'LEFT')
        seed_rows = deepcopy(self.rows)
        seed_rows[key]['published_at'] = None
        indexed = {identity: row for identity, row in self.rows.items() if identity != key}
        result, _ = self.read(indexed, latest=self.latest(seed_rows), seeds=archives(seed_rows))
        self.assertNotIn(key, result)
        self.assertEqual(views.score_gate(views._scores(result, self.code, self.six))['status'],
                         'INSUFFICIENT_DATA')

    def test_latest_null_or_unavailable_publication_stays_a_missing_slot(self):
        key = (self.day, self.code, 'LEFT')
        for stamp in (None, self.day + 'T17:00:00+08:00'):
            with self.subTest(stamp=stamp):
                latest = deepcopy(self.latest(self.rows))
                latest[key]['published_at'] = stamp
                result, _ = self.read(latest=latest)
                self.assertNotIn(key, result)
                self.assertEqual(views.score_gate(views._scores(result, self.code, self.six))['status'],
                                 'INSUFFICIENT_DATA')

    def test_latest_is_limited_to_this_chunk_and_preserves_complete_body(self):
        other, _, _ = saved_sample('003911.SZ')
        latest = {**self.latest(self.rows), **self.latest(other)}
        result, _ = self.read(latest=latest,
                              indexed_publications=self.publications({**self.rows, **other}))
        self.assertEqual({key[1] for key in result}, {self.code})
        for key in self.latest(self.rows):
            self.assertEqual(result[key], self.rows[key])

    def test_projected_quality_and_missing_parts_keep_the_original_gate_decision(self):
        key = (self.six[-2], self.code, 'LEFT')
        mutations = [
            lambda row: row.update(quality={'daily_count': 80, 'completed_weeks': 40}),
            lambda row: row.update(quality='0.60'),
            lambda row: row.update(quality='0.59'),
            lambda row: row.update(quality=1),
            lambda row: row.update(quality=.60),
            lambda row: row.update(quality=False),
            lambda row: row.update(quality=['0.60']),
            lambda row: row.update(quality=None),
            lambda row: row.pop('quality'),
            lambda row: row.update(setup_score='0'),
            lambda row: row.update(setup_score=0),
            lambda row: row.update(setup_score=70.125),
            lambda row: row.update(setup_score=None),
            lambda row: row['subscores']['parts'].update(safety=60.25),
            lambda row: row['subscores']['parts'].pop('safety'),
            lambda row: row['subscores']['parts'].update(safety=None),
            lambda row: row['subscores']['weights'].update(safety=.15),
            lambda row: row['subscores']['weights'].pop('safety'),
            lambda row: row['subscores']['weights'].update(safety=None),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                source = deepcopy(self.rows)
                mutate(source[key])
                expected = {identity: canonical(row) for identity, row in source.items()}
                projected, _ = self.read(source)
                for field in FIELDS:
                    self.assertEqual(projected[key][field], expected[key].get(field))
                self.assertEqual(views._scores(projected, self.code, self.six),
                                 views._scores(expected, self.code, self.six))
                self.assertEqual(views.score_gate(views._scores(projected, self.code, self.six)),
                                 views.score_gate(views._scores(expected, self.code, self.six)))

    def test_unindexed_legacy_stock_and_family_remain_in_the_window(self):
        legacy_rows, legacy_bars, _ = saved_sample('003915.SZ')
        legacy_rows = {key: canonical(row) for key, row in legacy_rows.items()}
        full = {**self.rows, **legacy_rows}
        # One indexed family coexists with the other family's older saved rows.
        indexed = {key: row for key, row in self.rows.items() if key[2] == 'RIGHT'}
        result, conn = self.read(indexed, seeds=archives(full), latest=self.latest(full),
            codes=[self.code, '003915.SZ'], cutoffs={code: self.cutoff for code in (self.code, '003915.SZ')})
        self.assertEqual(len(result), 24)
        universe = {code: full[(self.day, code, 'RIGHT')] for code in (self.code, '003915.SZ')}
        bars = {**self.bars, **legacy_bars}
        for mode in ('STARTUP', 'PULLBACK'):
            self.assertEqual(views.screen_saved(result, bars, self.days, universe, {}, mode=mode, parameters=PARAMETERS),
                             views.screen_saved(full, bars, self.days, universe, {}, mode=mode, parameters=PARAMETERS))
        self.assertTrue(all(read['days'] == set(self.six[:-1]) for read in conn.reads))

    def structure_read(self, full, bars, metadata=None):
        metadata = metadata or {}
        latest = self.latest(full)
        conn = ProjectionConnection(full)
        bar_scopes = []
        def price_rows(connection, codes, days, cutoff, cutoffs=None):
            if codes:
                bar_scopes.append(set(codes))
            return {key: row for key, row in bars.items() if key[0] in codes and key[1] in days}
        with patch.object(views, 'SCREEN_CHUNK', 2), \
                patch.object(views, '_calendar', return_value=self.days), \
                patch.object(info, 'load_history', return_value=dict(archives=[], issues=[])), \
                patch.object(views, '_screen_latest', return_value=(latest, self.publications(full))) as latest_read, \
                patch.object(views, '_metadata', return_value=metadata), \
                patch.object(views, '_bars', side_effect=price_rows):
            result = views.read_structure(Ledger(conn), day=self.day, available=[self.day],
                mode='STARTUP', parameters=PARAMETERS, query='', page=1, page_size=100,
                seeds=dict(archives=[], issues=[]))
        self.assertEqual(latest_read.call_count, 1)
        self.assertEqual(latest_read.call_args.args[2], self.day)
        return result, conn, bar_scopes

    def test_early_exclusion_reduces_reads_without_changing_complete_universe_counts(self):
        full, bars = {}, {}
        for code, name, shift in (('003910.SZ', '正常公司', 0), ('003911.SZ', '高分公司', 10),
                                  ('003912.SZ', '*ST排除', 20), ('300910.SZ', '非主板', 30),
                                  ('003913.SZ', '退市公司', 20), ('003914.SZ', '缺评分', 5)):
            rows, prices, _ = saved_sample(code, shift, name)
            full.update({key: canonical(row) for key, row in rows.items()})
            bars.update(prices)
        del full[(self.six[-2], '003914.SZ', 'LEFT')]
        universe = {code: row for (_, code, _), row in self.latest(full).items()}
        expected, counts, failures = views.screen_saved(full, bars, self.days, universe, {},
                                                       mode='STARTUP', parameters=PARAMETERS)
        expected.sort(key=lambda row: (-Decimal(row['setup_score']), row['ts_code']))
        actual, conn, bar_scopes = self.structure_read(full, bars)
        self.assertEqual(actual['items'], expected)
        self.assertEqual(actual['total'], len(expected))
        self.assertEqual(actual['structure_counts'], {**counts, 'returned': len(expected)})
        self.assertEqual(actual['insufficient_reasons'],
                         [dict(reason=reason, count=count) for reason, count in failures.most_common()])
        self.assertEqual(actual['structure_counts']['universe'], 6)
        self.assertEqual(actual['structure_counts']['excluded'], 3)
        self.assertEqual(actual['structure_counts']['insufficient'], 1)
        self.assertEqual(actual['batch'], dict(published_at=self.cutoff.isoformat(),
            version_id='right_v3.1.2_adj', source='frozen local fixture'))
        eligible = {'003910.SZ', '003911.SZ', '003914.SZ'}
        self.assertEqual(set().union(*(read['codes'] for read in conn.reads)), eligible)
        self.assertTrue(all(read['days'] == set(self.six[:-1]) for read in conn.reads))
        self.assertTrue(all(scope <= eligible for scope in bar_scopes))

    def test_later_left_publication_sets_stock_cutoff_and_keeps_full_right_response(self):
        full = deepcopy(self.rows)
        latest_left = (self.day, self.code, 'LEFT')
        latest_right = (self.day, self.code, 'RIGHT')
        full[latest_left]['published_at'] = self.day + 'T17:00:00+08:00'
        full[(self.six[-2], self.code, 'RIGHT')]['published_at'] = self.day + 'T16:30:00+08:00'
        full[latest_right]['note'] = 'RIGHT retains its own earlier publication and all fields'
        result, _, _ = self.structure_read(full, self.bars)
        expected, _, _ = views.screen_saved(full, self.bars, self.days,
            {self.code: full[latest_right]}, {}, mode='STARTUP', parameters=PARAMETERS)
        self.assertEqual(result['items'], expected)
        self.assertEqual(result['structure_counts']['matched'], 1)
        self.assertEqual(result['input_as_of'], full[latest_left]['published_at'])
        self.assertEqual(result['batch']['published_at'], full[latest_left]['published_at'])
        self.assertEqual(result['items'][0]['published_at'], full[latest_right]['published_at'])

    def test_all_excluded_chunk_needs_no_historical_or_price_rows(self):
        full, bars = {}, {}
        for code, name in (('003912.SZ', '*ST公司'), ('300910.SZ', '非主板')):
            rows, prices, _ = saved_sample(code, name=name)
            full.update({key: canonical(row) for key, row in rows.items()})
            bars.update(prices)
        result, conn, bar_scopes = self.structure_read(full, bars)
        self.assertEqual(conn.reads, [])
        self.assertEqual(bar_scopes, [])
        self.assertEqual(result['items'], [])
        self.assertEqual(result['structure_counts'], dict(universe=2, excluded=2, evaluated=0,
            insufficient=0, matched=0, watch=0, confirmed=0, returned=0))
