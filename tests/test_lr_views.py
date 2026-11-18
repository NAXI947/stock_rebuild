"""Read-only LR shapes, exact history slots and real PostgreSQL/API contracts."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from io import BytesIO
import json
import os
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from modules.strategy_hermes import left_right_info as info, left_right_views as views
from modules.strategy_hermes.ledger import encode
from tests.test_lr_structure_screen import sample


def saved_sample(code='003910.SZ', shift=0, name='样本'):
    scores, bars = sample()
    rows = {}
    for point in scores:
        for family in ('LEFT', 'RIGHT'):
            original = point[family.lower()]
            row = dict(ts_code=code, name=name, trade_date=point['trade_date'], family=family,
                setup_score=str(original['score'] + (shift if family == 'RIGHT' else 0)),
                status='READY', version_id=family.lower() + '_v3.1.2_adj',
                quality=dict(daily_count=80, completed_weeks=40, sector_return_available=True),
                subscores=original['subscores'], reasons=[], return_ratio='.03', close='10.3',
                volume_ratio='1', turnover_ratio='.02', source='frozen local fixture', sectors=[],
                observed_at=point['trade_date']+'T15:00:00+08:00',
                published_at=point['trade_date']+'T16:00:00+08:00', assessment_id=code+point['trade_date']+family)
            rows[(point['trade_date'], code, family)] = row
    days = [row['trade_date'] for row in bars]
    return rows, {(code, row['trade_date']): row for row in bars}, days


class ViewAdapterTests(TestCase):
    def test_query_bounds_and_duplicates_are_rejected_before_storage(self):
        for query in ('structure_mode=OTHER', 'structure_mode=STARTUP&min_left=49',
                      'min_right_gain=NaN', 'max_ma20_distance=.21', 'min_left=',
                      'min_left=70&min_left=80', 'structure_mode=STARTUP&unknown=1'):
            with self.subTest(query=query), self.assertRaises(ValueError):
                info._query(query)
        for query in ('', 'ts_code=bad', 'ts_code=003910.SZ&days=31', 'ts_code=003910.SZ&days=0',
                      'ts_code=003910.SZ&days=1.5', 'ts_code=003910.SZ&trade_date=2026-02-30',
                      'ts_code=003910.SZ&ts_code=000001.SZ', 'ts_code=003910.SZ&x=1'):
            with self.subTest(query=query), self.assertRaises(ValueError):
                views._history_query(query)

    def test_adapter_preserves_quality_version_missing_and_soft_left_reference(self):
        rows, bars, days = saved_sample()
        original = deepcopy(rows)
        universe = {'003910.SZ': rows[(days[-1], '003910.SZ', 'RIGHT')]}
        params = dict(min_left=70, min_right_gain=3, max_ma20_distance=.1)
        result, counts, failures = views.screen_saved(rows, bars, days, universe, {}, mode='STARTUP', parameters=params)
        self.assertEqual((counts['matched'], counts['confirmed'], failures), (1, 1, {}))
        self.assertEqual(result[0]['setup_score'], '71')
        result, counts, _ = views.screen_saved(rows, bars, days, universe, {}, mode='STARTUP', parameters={**params, 'min_left': 90})
        self.assertEqual((counts['matched'], counts['watch']), (1, 1))
        self.assertEqual(rows, original)
        for change in ('quality', 'version', 'hole'):
            changed = deepcopy(rows)
            point = changed[(days[-3], '003910.SZ', 'RIGHT')]
            if change == 'quality': point['quality'] = '.59'
            elif change == 'version': point['version_id'] = 'unverified_version'
            else: del changed[(days[-3], '003910.SZ', 'LEFT')]
            result, counts, _ = views.screen_saved(changed, bars, days, universe, {}, mode='STARTUP', parameters=params)
            self.assertEqual((result, counts['insufficient']), ([], 1), change)

    def test_main_board_and_st_scope_matches_legacy(self):
        for code, name, expected in (('600123.SH', '公司', True), ('003910.SZ', '公司', True),
                                     ('300123.SZ', '公司', False), ('688123.SH', '公司', False),
                                     ('003910.SZ', '*ST公司', False), ('600123.SH', '退市公司', False)):
            self.assertEqual(views._main_board(dict(ts_code=code, name=name), {}, '2026-09-30'), expected)
        self.assertFalse(views._main_board(dict(ts_code='003910.SZ', name='公司'),
            {'003910.SZ': dict(list_date='20261001')}, '2026-09-30'))


ENV = os.environ.get('STAGE2_TEST_ENV_FILE')


@skipUnless(ENV, 'requires isolated PostgreSQL STAGE2_TEST_ENV_FILE')
class SavedViewsSqlTests(TestCase):
    def setUp(self):
        from modules.strategy_hermes.stage2.postgres import PostgresLedger
        from modules.strategy_hermes.stage2.settings import Settings
        from modules.strategy_hermes.workspace_web import application
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, 'test')
        self.store = PostgresLedger(replace(settings, namespace=settings.redis_prefix+'lr-views:'+uuid4().hex), 'FROZEN')
        self.store.migrate()
        self.batch = 'lr-views-'+uuid4().hex
        self.rows, self.bars = {}, {}
        for code, shift, name in (('003910.SZ', 0, '首股'), ('003911.SZ', 10, '高分股'),
                                  ('003912.SZ', 5, '中分股'), ('003913.SZ', 3, '缺日股'), ('003914.SZ', 20, '*ST股')):
            rows, bars, self.days = saved_sample(code, shift, name)
            self.rows.update(rows)
            self.bars.update(bars)
        del self.rows[(self.days[-3], '003913.SZ', 'LEFT')]
        # Use an isolated historical calendar range, retaining the 20-session fixture.
        shift = date(1996, 9, 2) - date.fromisoformat(self.days[0])
        def shifted(day): return (date.fromisoformat(day)+shift).isoformat()
        self.days = [shifted(day) for day in self.days]
        self.rows = {(shifted(day), code, family): dict(row, trade_date=shifted(day),
            observed_at=shifted(day)+'T15:00:00+08:00', published_at=shifted(day)+'T16:00:00+08:00')
            for (day, code, family), row in self.rows.items()}
        self.bars = {(code, shifted(day)): dict(row, trade_date=shifted(day)) for (code, day), row in self.bars.items()}
        self.anchor = self.days[-1]
        self.calendar = list(self.days)
        prior = date.fromisoformat(self.days[0])
        while len(self.calendar) < 30:
            prior -= timedelta(days=1)
            if prior.weekday() < 5: self.calendar.insert(0, prior.isoformat())
        self.addCleanup(self.clean)
        with self.store._connect() as conn, conn.transaction():
            conn.execute("""INSERT INTO supplier_batches(batch_id,api,requested_scope,collected_at,row_count,raw_rows,normalized)
                VALUES(%s,'daily','{}',%s,100,'[]','{}')""", (self.batch, self.anchor+'T16:00:00+08:00'))
            conn.execute("""INSERT INTO supplier_market_batches(batch_id,provider,endpoint,scope,collected_at,row_count,body)
                VALUES(%s,'tushare','adj_factor','{}',%s,100,'{}')""", (self.batch, self.anchor+'T16:00:00+08:00'))
            for day in self.calendar:
                conn.execute("INSERT INTO supplier_trade_calendar(exchange,cal_date,is_open,batch_id) VALUES('SSE',%s,true,%s)", (day, self.batch))
            for (code, day), row in self.bars.items():
                conn.execute("""INSERT INTO supplier_daily_bars
                    (code,trade_date,open,high,low,close,pre_close,volume_shares,amount_cny,return_ratio,batch_id)
                    VALUES(%s,%s,%s,%s,%s,%s,10,%s,%s,.03,%s)""",
                    (code, day, row['open'], row['high'], row['low'], row['close'], row['volume_shares'], row['amount_cny'], self.batch))
                conn.execute("INSERT INTO supplier_lr_features(api,code,trade_date,body,batch_id) VALUES('adj_factor',%s,%s,'{\"adj_factor\":\"1\"}',%s)", (code, day, self.batch))
            for (day, code, family), row in self.rows.items():
                conn.execute("""INSERT INTO left_right_assessments
                    (ns,trade_date,code,family,published_at,evidence_sha256,status,setup_score,return_ratio,name,version_id,source,body)
                    VALUES(%s,%s,%s,%s,%s,'fixture','READY',%s,.03,%s,%s,'fixture',%s)""",
                    (self.store.ns, day, code, family, row['published_at'], row['setup_score'], row['name'], row['version_id'], encode(row)))
        self.token = 'v'*40
        self.app = application(self.store, self.token, calendar=self.calendar)

    def clean(self):
        with self.store._connect() as conn, conn.transaction():
            conn.execute('DELETE FROM left_right_assessments WHERE ns=%s', (self.store.ns,))
            conn.execute('DELETE FROM snapshot_state WHERE ns=%s', (self.store.ns,))
            conn.execute('DELETE FROM supplier_lr_features WHERE batch_id=ANY(%s)', ([self.batch, self.batch+'-late'],))
            conn.execute('DELETE FROM supplier_reference_rows WHERE batch_id=ANY(%s)', ([self.batch, self.batch+'-late'],))
            conn.execute('DELETE FROM supplier_market_batches WHERE batch_id=ANY(%s)', ([self.batch, self.batch+'-late'],))
            conn.execute('DELETE FROM supplier_daily_bars WHERE batch_id=ANY(%s)', ([self.batch, self.batch+'-late'],))
            conn.execute('DELETE FROM supplier_trade_calendar WHERE batch_id=%s', (self.batch,))
            conn.execute('DELETE FROM supplier_batches WHERE batch_id=ANY(%s)', ([self.batch, self.batch+'-late'],))

    def request(self, route, auth=True):
        path, _, query = route.partition('?')
        env = dict(REQUEST_METHOD='GET', PATH_INFO='/api/v2/workspace/'+path, QUERY_STRING=query,
                   CONTENT_LENGTH='0', **{'wsgi.input': BytesIO(b'')})
        if auth: env['HTTP_AUTHORIZATION'] = 'Bearer '+self.token
        statuses = []
        body = b''.join(self.app(env, lambda status, headers: statuses.append(int(status.split()[0]))))
        return statuses[0], json.loads(body)

    def test_full_universe_is_screened_then_ranked_and_paginated_with_scoped_search(self):
        # Make the chunk smaller than the universe to exercise the all-pool boundary.
        with patch.object(views, 'SCREEN_CHUNK', 2), patch('modules.strategy_hermes.stage2.market_bootstrap.fetch', side_effect=AssertionError('GET must not collect')):
            first = info.read_info(self.store, 'structure_mode=STARTUP&page_size=1')
            second = info.read_info(self.store, 'structure_mode=STARTUP&page_size=1&page=2')
            search = info.read_info(self.store, 'structure_mode=STARTUP&q=首股')
            pullback = info.read_info(self.store, 'structure_mode=PULLBACK&page_size=100')
        self.assertEqual([first['items'][0]['ts_code'], second['items'][0]['ts_code']], ['003911.SZ', '003912.SZ'])
        self.assertEqual((first['total'], first['pages'], first['summary']['total']), (3, 3, 3))
        self.assertEqual((first['structure_counts']['universe'], first['structure_counts']['insufficient'], first['structure_counts']['excluded']), (5, 1, 1))
        self.assertEqual((search['total'], search['items'][0]['ts_code']), (1, '003910.SZ'))
        self.assertEqual(search['summary'], first['summary'])
        self.assertEqual([row['ts_code'] for row in pullback['items']], ['003911.SZ', '003912.SZ', '003910.SZ'])
        self.assertTrue(first['insufficient_reasons'])
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM left_right_assessments WHERE ns=%s', (self.store.ns,)).fetchone()[0], len(self.rows))

    def test_history_retains_thirty_calendar_slots_missing_days_versions_and_zero(self):
        code = '003913.SZ'
        target = self.rows[(self.days[-2], code, 'RIGHT')]
        target = dict(target, setup_score='0', version_id='right_historical_version')
        with self.store._connect() as conn:
            conn.execute("UPDATE left_right_assessments SET body=%s,setup_score=0 WHERE ns=%s AND code=%s AND trade_date=%s AND family='RIGHT'",
                         (encode(target), self.store.ns, code, self.days[-2]))
        result = views.read_score_history(self.store, 'ts_code='+code+'&days=30&trade_date='+self.anchor)
        self.assertEqual(result['trade_dates'], self.calendar)
        self.assertEqual(len(result['items']), 60)
        self.assertEqual(len(result['bars']), 30)
        self.assertTrue(all(row['setup_score'] is None for row in result['items'][:48]))
        missing = next(row for row in result['items'] if row['trade_date'] == self.days[-3] and row['family'] == 'LEFT')
        self.assertEqual((missing['setup_score'], missing['status'], missing['missing_reason']), (None, 'MISSING', 'NO_ASSESSMENT'))
        zero = next(row for row in result['items'] if row['trade_date'] == self.days[-2] and row['family'] == 'RIGHT')
        self.assertEqual((zero['setup_score'], zero['version_id'], zero['status']), ('0', 'right_historical_version', 'READY'))
        self.assertIsNone(result['bars'][0]['close'])
        self.assertEqual(result['bars'][-1]['close'], '10.3')
        earlier = views.read_score_history(self.store, 'ts_code='+code+'&days=3&trade_date='+self.days[-2])
        self.assertEqual(earlier['trade_dates'], self.days[-4:-1])

    def test_routes_require_authentication_validate_bounds_and_only_read(self):
        for route in ('left-right-info?structure_mode=STARTUP', 'left-right-history?ts_code=003910.SZ'):
            self.assertEqual(self.request(route, auth=False)[0], 401)
            with patch('modules.strategy_hermes.stage2.market_bootstrap.fetch', side_effect=AssertionError('no GET collection')):
                status, result = self.request(route)
            self.assertEqual(status, 200, result)
        for route in ('left-right-info?structure_mode=bad', 'left-right-info?min_left=NaN',
                      'left-right-history?ts_code=003910.SZ&days=31', 'left-right-history?ts_code=bad'):
            self.assertEqual(self.request(route)[0], 400, route)

    def test_later_name_and_input_revisions_do_not_rewrite_published_screen(self):
        late = self.anchor+'T17:00:00+08:00'
        with self.store._connect() as conn, conn.transaction():
            conn.execute("""INSERT INTO supplier_market_batches(batch_id,provider,endpoint,scope,collected_at,row_count,body)
                VALUES(%s,'tushare','stock_basic','{}',%s,1,'{}')""", (self.batch+'-late', late))
            conn.execute("""INSERT INTO supplier_batches(batch_id,api,requested_scope,collected_at,row_count,raw_rows,normalized)
                VALUES(%s,'daily','{}',%s,1,'[]','{}')""", (self.batch+'-late', late))
            for business, observed, batch, name in ((self.days[-2], self.days[-2]+'T16:00:00+08:00', self.batch, '正常公司'),
                                                   (self.anchor, late, self.batch+'-late', '*ST后来改名')):
                conn.execute("""INSERT INTO supplier_reference_rows(dataset,entity_key,business_date,observed_at,body,batch_id)
                    VALUES('stock_basic','003910.SZ',%s,%s,%s,%s)""", (business, observed, encode(dict(name=name)), batch))
            # Exercise optional metadata fallback when the assessment lacked a name.
            conn.execute("UPDATE left_right_assessments SET body=body||'{\"name\":null}'::jsonb WHERE ns=%s AND code='003910.SZ'", (self.store.ns,))
            # Another symbol's subset rerun must not advance this stock's waterline.
            conn.execute("""UPDATE left_right_assessments SET published_at=%s,
                body=body||jsonb_build_object('published_at',%s::text)
                WHERE ns=%s AND code='003914.SZ' AND trade_date=%s""", (late, late, self.store.ns, self.anchor))
        unchanged = info.read_info(self.store, 'structure_mode=STARTUP')
        self.assertEqual(unchanged['total'], 3)
        with self.store._connect() as conn, conn.transaction():
            conn.execute("UPDATE supplier_daily_bars SET batch_id=%s WHERE code='003911.SZ' AND trade_date=%s", (self.batch+'-late', self.anchor))
            conn.execute("UPDATE supplier_lr_features SET batch_id=%s WHERE code='003912.SZ' AND trade_date=%s AND api='adj_factor'", (self.batch+'-late', self.days[-4]))
        revised = info.read_info(self.store, 'structure_mode=STARTUP')
        self.assertEqual([row['ts_code'] for row in revised['items']], ['003910.SZ'])
        self.assertEqual(revised['structure_counts']['insufficient'], 3)
        self.assertEqual(revised['structure_counts']['excluded'], 1)

    def test_later_previous_day_score_rerun_remains_a_gap_until_target_is_recomputed(self):
        later = self.anchor+'T16:30:00+08:00'
        with self.store._connect() as conn, conn.transaction():
            conn.execute("""UPDATE left_right_assessments SET published_at=%s,
                body=body||jsonb_build_object('published_at',%s::text)
                WHERE ns=%s AND code='003911.SZ' AND trade_date=%s AND family='RIGHT'""",
                (later, later, self.store.ns, self.days[-2]))
            conn.execute("""UPDATE left_right_assessments SET published_at=%s,
                body=body||jsonb_build_object('published_at',%s::text)
                WHERE ns=%s AND code='003914.SZ' AND trade_date=%s""",
                (self.anchor+'T17:00:00+08:00', self.anchor+'T17:00:00+08:00', self.store.ns, self.anchor))
        result = info.read_info(self.store, 'structure_mode=STARTUP')
        self.assertEqual([row['ts_code'] for row in result['items']], ['003912.SZ', '003910.SZ'])
        self.assertEqual(result['structure_counts']['insufficient'], 2)
        history = views.read_score_history(self.store, 'ts_code=003911.SZ&days=6')
        gap = next(row for row in history['items'] if row['trade_date'] == self.days[-2] and row['family'] == 'RIGHT')
        self.assertIsNone(gap['setup_score'])
        self.assertEqual(gap['missing_reason'], 'NO_ASSESSMENT')

    def test_current_sql_publication_after_cutoff_reuses_visible_full_legacy_row(self):
        code = '003910.SZ'
        legacy = deepcopy(self.rows[(self.anchor, code, 'RIGHT')])
        legacy.update(setup_score='75', published_at=self.anchor+'T15:59:00+08:00',
                      note='完整旧归档', legacy_extension={'preserve': ['all', 'response', 'fields']})
        archives = [dict(trade_date=self.anchor, items=[legacy])]
        with self.store._connect() as conn:
            # There is no database constraint equating this SQL timestamp and
            # body's timestamp. The second old-style read would omit this SQL row.
            conn.execute("""UPDATE left_right_assessments SET published_at=%s
                WHERE ns=%s AND trade_date=%s AND code=%s AND family='RIGHT'""",
                (self.anchor+'T17:00:00+08:00', self.store.ns, self.anchor, code))
        with patch.object(info, 'load_history', return_value=dict(archives=archives, issues=[])):
            result = info.read_info(self.store, 'structure_mode=STARTUP')
        returned = next(row for row in result['items'] if row['ts_code'] == code)
        self.assertEqual(result['total'], 3)
        self.assertEqual({key: value for key, value in returned.items() if key != 'structure'}, legacy)
        self.assertEqual(result['structure_counts']['universe'], 5)

    def test_historical_sql_overrides_legacy_before_body_stock_cutoff_is_applied(self):
        code, prior = '003911.SZ', self.days[-2]
        legacy = deepcopy(self.rows[(prior, code, 'RIGHT')])
        changed = dict(legacy, published_at=self.anchor+'T16:30:00+08:00')
        archives = [dict(trade_date=prior, items=[legacy])]
        with self.store._connect() as conn:
            # SQL visibility permits this record, while its original body is
            # later than this stock's current-day publication cutoff.
            conn.execute("""UPDATE left_right_assessments SET published_at=%s,body=%s
                WHERE ns=%s AND trade_date=%s AND code=%s AND family='RIGHT'""",
                (self.anchor+'T15:00:00+08:00', encode(changed), self.store.ns, prior, code))
        with patch.object(info, 'load_history', return_value=dict(archives=archives, issues=[])):
            result = info.read_info(self.store, 'structure_mode=STARTUP')
        self.assertEqual([row['ts_code'] for row in result['items']], ['003912.SZ', '003910.SZ'])
        self.assertEqual(result['structure_counts']['insufficient'], 2)
        self.assertEqual(result['structure_counts']['excluded'], 1)
        self.assertEqual(result['structure_counts']['universe'], 5)

    def test_historical_projection_retains_json_types_and_missing_values(self):
        code, prior = '003910.SZ', self.days[-2]
        key = (prior, code, 'RIGHT')
        fields = ('trade_date', 'ts_code', 'family', 'published_at', 'setup_score',
                  'status', 'version_id', 'quality', 'subscores')
        original = self.rows[key]
        nested = {'weights': {'first': '0.60', 'second': 0.4},
                  'extra': [True, None, {'score': '001.00'}]}
        cases = [dict(original, setup_score='0071.00', quality=nested, subscores=nested),
                 dict(original, setup_score=71.25, quality='0.75', subscores=nested),
                 dict(original, setup_score=0, quality=[True, None, '0.75'], subscores=None),
                 {field: original[field] for field in ('trade_date', 'ts_code', 'family', 'published_at')}]
        for body in cases:
            with self.subTest(body=body):
                with self.store._connect() as conn:
                    conn.execute("""UPDATE left_right_assessments SET body=%s
                        WHERE ns=%s AND trade_date=%s AND code=%s AND family='RIGHT'""",
                        (encode(body), self.store.ns, prior, code))
                with self.store._connect() as conn:
                    latest, publications = views._screen_latest(conn, self.store.ns, self.anchor)
                    baseline = views._assessments(conn, self.store.ns, self.days[-6:], [code])
                    projected = views._screen_assessments(conn, self.store.ns, self.days[-6:], [code],
                                                         latest, indexed_publications=publications)
                self.assertEqual(projected[key], {field: baseline[key].get(field) for field in fields})
                self.assertEqual(projected[(self.anchor, code, 'RIGHT')], latest[(self.anchor, code, 'RIGHT')])

    def test_non_object_historical_body_is_rejected_instead_of_returning_success(self):
        from psycopg.errors import InvalidParameterValue

        code, prior = '003910.SZ', self.days[-2]
        for body in (None, [], ['invalid'], 'invalid', 1, True):
            with self.subTest(body=body):
                with self.store._connect() as conn:
                    conn.execute("""UPDATE left_right_assessments SET body=%s
                        WHERE ns=%s AND trade_date=%s AND code=%s AND family='RIGHT'""",
                        (encode(body), self.store.ns, prior, code))
                with self.store._connect() as conn, self.assertRaisesRegex(ValueError, 'object required'):
                    views._assessments(conn, self.store.ns, self.days[-6:], [code])
                with self.assertRaises(InvalidParameterValue):
                    info.read_info(self.store, 'structure_mode=STARTUP')
