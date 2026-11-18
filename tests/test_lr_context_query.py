"""Real PostgreSQL equivalence for the bounded historical membership query."""
import ast
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import inspect
import json
import os
from unittest import TestCase, skipUnless
from uuid import uuid4

import psycopg

from modules.strategy_hermes.stage2 import left_right_eod as eod
from modules.strategy_hermes.stage2.postgres import PostgresLedger
from modules.strategy_hermes.stage2.settings import Settings


ORIGINAL = """SELECT scope,jsonb_build_object(
    'trade_days',body->'trade_days','reported_total',body->'reported_total',
    'codes',(SELECT jsonb_agg(r->0) FROM jsonb_array_elements(body->'rows') r),
    'matches',(SELECT jsonb_agg(jsonb_build_array(r->>0,r->>1))
        FROM jsonb_array_elements(body->'rows') r WHERE r->>0=ANY(%s))),
    collected_at,batch_id
    FROM supplier_market_batches WHERE provider='kaipanla' AND endpoint='046'
    AND collected_at<=%s AND scope->>'Date'<=%s
    ORDER BY scope->>'Date' DESC,collected_at DESC,batch_id DESC LIMIT 3000"""


def candidate_query():
    values = [node.value for node in ast.walk(ast.parse(inspect.getsource(eod._database_context)))
              if isinstance(node, ast.Constant) and isinstance(node.value, str)
              and 'WITH requested AS MATERIALIZED' in node.value]
    if len(values) != 1:
        raise AssertionError('one actual membership query required')
    return values[0]


ENV = os.environ.get('STAGE2_TEST_ENV_FILE')
DAY = date(2001, 6, 8)
AS_OF = datetime(2001, 6, 9, tzinfo=timezone.utc)


@skipUnless(ENV, 'requires isolated PostgreSQL STAGE2_TEST_ENV_FILE')
class MembershipQueryTests(TestCase):
    def setUp(self):
        settings = Settings.read(ENV)
        self.assertEqual(settings.env, 'test')
        store = PostgresLedger(replace(settings, namespace=settings.redis_prefix + 'lr-context:' + uuid4().hex), 'FROZEN')
        self.connection = store._connect()
        self.conn = self.connection.__enter__()
        self.addCleanup(self.connection.__exit__, None, None, None)
        # Temporary tables shadow shared tables only on this test connection.
        self.conn.execute('''CREATE TEMP TABLE supplier_market_batches (
            scope jsonb,body jsonb,collected_at timestamptz,batch_id text PRIMARY KEY,
            provider text,endpoint text,source_clock timestamptz)''')
        self.conn.execute('CREATE TEMP TABLE supplier_batches (batch_id text,collected_at timestamptz)')
        self.conn.execute('''CREATE TEMP TABLE supplier_trade_calendar
            (cal_date date,is_open boolean,batch_id text,exchange text)''')
        self.conn.execute('''CREATE TEMP TABLE supplier_reference_rows
            (entity_key text,body jsonb,batch_id text,observed_at timestamptz,business_date date,dataset text)''')
        self.query = candidate_query()

    def save(self, identifier, rows=None, *, body=None, day=DAY, observed=AS_OF,
             plate='123456', offset=0, total=None, provider='kaipanla', endpoint='046'):
        if body is None:
            body = dict(trade_days=[str(day)], reported_total=len(rows or []) if total is None else total, rows=rows or [])
        scope = dict(Date=str(day), PlateID=plate, Index=str(offset))
        self.conn.execute('''INSERT INTO supplier_market_batches
            VALUES(%s::jsonb,%s::jsonb,%s,%s,%s,%s,NULL)''',
            (json.dumps(scope), json.dumps(body), observed, identifier, provider, endpoint))

    def response(self, query, codes):
        # Savepoints preserve the fixture after an expected malformed-input error.
        try:
            with self.conn.transaction():
                result = self.conn.execute(query, (codes, AS_OF, str(DAY))).fetchall()
                return 'OK', [(json.loads(scope), json.loads(body), at, batch) for scope, body, at, batch in result]
        except psycopg.Error as exc:
            return 'ERROR', exc.sqlstate

    def equal(self, codes):
        old, new = self.response(ORIGINAL, codes), self.response(self.query, codes)
        self.assertEqual(new, old)
        return new

    def test_repeated_requested_codes_preserve_duplicate_source_rows_and_order(self):
        rows = [['000002', '乙'], ['000001', '甲'], ['000002', '重复乙'],
                [None, '空'], [1, {'mixed': True}], ['000001', None], ['absent', False]]
        self.save('ordered', rows)
        status, result = self.equal(['000002', '000001', '000002', None, '1'])
        self.assertEqual(status, 'OK')
        body = result[0][1]
        self.assertEqual(body['codes'], [row[0] for row in rows])
        self.assertEqual([row[0] for row in body['matches']], ['000002', '000001', '000002', '1', '000001'])
        self.assertEqual(body['matches'][2], ['000002', '重复乙'])

    def test_empty_null_and_unmatched_request_arrays_keep_all_codes(self):
        self.save('members', [['000001', '甲'], ['000002', '乙']])
        for codes in ([], None, [None], ['absent'], ['absent', None]):
            with self.subTest(codes=codes):
                status, result = self.equal(codes)
                self.assertEqual(status, 'OK')
                self.assertEqual(result[0][1]['codes'], ['000001', '000002'])
                self.assertIsNone(result[0][1]['matches'])

    def test_json_types_missing_fields_and_nonobject_bodies_keep_old_semantics(self):
        bodies = [{}, {'rows': []}, {'trade_days': {'x': 1}, 'reported_total': '2', 'rows': [['000001', {'a': 2}]]},
                  [], [1, 2], ['rows'], [{'rows': [['000001', 'must not unwrap']]}], 'text', 1, True, None,
                  {'rows': [None, True, 7, 'scalar', {}, [], ['000001'], ['000001', ['nested']]]}]
        for index, body in enumerate(bodies):
            with self.subTest(body=body):
                self.conn.execute('TRUNCATE supplier_market_batches')
                self.conn.execute('INSERT INTO supplier_market_batches VALUES(%s::jsonb,%s::jsonb,%s,%s,%s,%s,NULL)',
                    (json.dumps(dict(Date=str(DAY))), json.dumps(body), AS_OF, str(index), 'kaipanla', '046'))
                self.assertEqual(self.equal(['000001', '7', 'scalar'])[0], 'OK')

    def test_nonarray_rows_keep_error_sqlstate(self):
        for rows in (None, {}, 'text', 2, False):
            with self.subTest(rows=rows):
                self.conn.execute('TRUNCATE supplier_market_batches')
                self.save('malformed', body={'rows': rows})
                self.assertEqual(self.equal(['000001']), ('ERROR', '22023'))

    def test_cutoff_date_provider_and_tie_order_are_unchanged(self):
        self.save('a', [['000001', 'old']], observed=AS_OF-timedelta(seconds=1))
        self.save('b', [['000001', 'b']])
        self.save('c', [['000001', 'c']])
        self.save('future-publication', [['000001', 'hidden']], observed=AS_OF+timedelta(microseconds=1))
        self.save('future-day', [['000001', 'hidden']], day=DAY+timedelta(days=1))
        self.save('wrong-provider', [['000001', 'hidden']], provider='other')
        self.save('wrong-endpoint', [['000001', 'hidden']], endpoint='042')
        self.save('previous', [['000001', 'previous']], day=DAY-timedelta(days=1))
        self.assertEqual([row[3] for row in self.equal(['000001'])[1]], ['c', 'b', 'a', 'previous'])

    def test_page_limit_precedes_expansion_and_keeps_exact_3000_batches(self):
        self.save('00000', body={'rows': None})  # The 3001st page must never be expanded.
        with self.conn.cursor() as cursor:
            cursor.executemany('INSERT INTO supplier_market_batches VALUES(%s::jsonb,%s::jsonb,%s,%s,%s,%s,NULL)',
                [(json.dumps(dict(Date=str(DAY))), json.dumps(dict(rows=[['000001', str(index)]])),
                  AS_OF, f'{index:05}', 'kaipanla', '046') for index in range(1, 3001)])
        status, result = self.equal(['000001', '000001'])
        self.assertEqual(status, 'OK')
        self.assertEqual(len(result), 3000)
        self.assertEqual((result[0][3], result[-1][3]), ('03000', '00001'))

    def test_complete_context_preserves_latest_complete_pages_and_ambiguous_codes(self):
        self.conn.execute('INSERT INTO supplier_batches VALUES(%s,%s)', ('calendar', AS_OF))
        with self.conn.cursor() as cursor:
            cursor.executemany('INSERT INTO supplier_trade_calendar VALUES(%s,TRUE,%s,%s)',
                [(DAY-timedelta(days=delta), 'calendar', 'SSE') for delta in range(8)])
        prior = DAY-timedelta(days=1)
        self.save('part-zero', [['000001', '甲'], ['000002', '乙']], day=prior, total=3)
        self.save('part-two', [['000003', '丙']], day=prior, offset=2, total=3)
        self.save('latest-incomplete', [['000001', '不完整新名']], total=3)
        self.save('duplicate-page', [['000001', '重名'], ['000001', '重复']], plate='654321', total=2)
        for delta in range(5):
            when = DAY-timedelta(days=delta)
            scope = dict(Date=str(when), RStart='0925', REnd='1500')
            body = dict(trade_days=[str(when)], rows=[['123456', '板块', '80', '2']])
            self.conn.execute('INSERT INTO supplier_market_batches VALUES(%s::jsonb,%s::jsonb,%s,%s,%s,%s,NULL)',
                (json.dumps(scope), json.dumps(body), AS_OF, 'board-'+str(delta), 'kaipanla', '042'))
        codes = ['000001.SZ', '000002.SZ', '000003.SZ', '000003.SH']
        real = self.conn
        query = self.query
        class Ledger:
            def __init__(self, old): self.old = old
            @contextmanager
            def _connect(self): yield self
            def execute(self, statement, args=()):
                return real.execute(ORIGINAL if self.old and statement == query else statement, args)
        old = eod._database_context(Ledger(True), DAY, codes, AS_OF)
        new = eod._database_context(Ledger(False), DAY, codes, AS_OF)
        self.assertEqual(new, old)
        self.assertEqual(new['names']['000001.SZ'], '甲')
        self.assertNotIn('000003.SZ', new['names'])
        self.assertEqual(new['links']['000001.SZ'][0]['membership_date'], str(prior))
        self.assertIn('000001.SZ', new['sector_values'])
