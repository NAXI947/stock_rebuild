"""Current rule changes cannot be bypassed by frozen research or waiting buys."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
import json
import os
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import psycopg

from modules.strategy_hermes.signal_cycle import create_cycle, fill
from modules.strategy_hermes.simulator import Calendar, Rules, moment
from modules.strategy_hermes.stage2 import entry_admission as admission
from modules.strategy_hermes.stage2.pending_quotes import eligible_orders
from test_stage1 import A, D1, D2, DAYS, ETF, ETF2, quote, report, stamp


def product(unit=100):
    return dict(index_code='index-A', trade_unit=unit, rule=dict(verified=True,
        verified_at=stamp(D1, '09:00'), index_code='index-A', scope='domestic_passive_sector',
        settlement_cycle='T+1', price_limit_ratio='.1'))


def state(status='READY', reason=None, clock='09:35', version='v1'):
    return dict(status=status, reason=reason, checked_at=stamp(D1, clock), mapping_version=version,
                product_fingerprint=version, trade_unit=100, eligible_after=stamp(D1, '09:00'))


def waiting():
    return create_cycle(ETF, 100, Rules(), dict(id='entry', at=stamp(D1, '09:35'),
        sectors={A:dict(state='UP')}), dict(sector_id=A), [])


class AdmissionUnit(unittest.TestCase):
    def connection(self, products=None, *, error=False, unpublished=False):
        calls = []
        def execute(sql, args=()):
            calls.append((sql, args))
            if 'FROM maintained_mappings' in sql:
                if error:
                    raise psycopg.OperationalError('reference read unavailable')
                row = None if unpublished else ('v1', moment(stamp(D1, '09:00')),
                    dict(schema_version='stock-v4-etf-mapping-v1',version='v1',sectors={},weights={},
                         products=products or {ETF:product()}))
            elif 'clock_timestamp' in sql:
                row = (moment(stamp(D1, '10:00')),)
            else:
                row = None
            return SimpleNamespace(fetchone=lambda:row)
        return SimpleNamespace(transaction=lambda:nullcontext(), execute=execute), calls

    def test_authoritative_read_uses_lock_order_and_current_clock(self):
        p = product(); p['rule']['review_required'] = True
        conn, calls = self.connection({ETF:p, ETF2:product()})
        states = admission.read_states(conn, 'test:ns', [ETF, ETF2])
        self.assertEqual(states[ETF]['reason'], 'RULE_REVIEW_REQUIRED')
        self.assertEqual(states[ETF2]['status'], 'READY')
        self.assertEqual([args for sql,args in calls if 'pg_advisory_xact_lock' in sql],
                         [('test:ns',), ('mapping-publish:test:ns',)])
        self.assertEqual(calls[-1][1][1], moment(stamp(D1, '10:00')))

    def test_no_database_version_and_read_error_never_accept_fallback(self):
        for options, reason in (({'unpublished':True}, admission.UNPUBLISHED), ({'error':True}, admission.UNAVAILABLE)):
            conn, _ = self.connection(**options)
            self.assertEqual(admission.read_states(conn, 'test:ns', [ETF])[ETF]['reason'], reason)

    def test_invalid_one_product_and_future_effective_time_do_not_block_others(self):
        p = product(); p['rule']['effective_at'] = 'bad'
        conn, _ = self.connection({ETF:p, ETF2:product()})
        states = admission.read_states(conn, 'test:ns', [ETF, ETF2])
        self.assertEqual(states[ETF]['status'], 'PAUSED')
        self.assertEqual(states[ETF2]['status'], 'READY')
        p['rule']['effective_at'] = stamp(D2, '09:00')
        conn, _ = self.connection({ETF:p})
        self.assertEqual(admission.read_states(conn, 'test:ns', [ETF])[ETF]['reason'], 'PRODUCT_RULE_UNVERIFIED')

    def test_pause_preserves_node_and_recovery_requires_real_new_quote(self):
        c = waiting(); original = deepcopy(c)
        admission.update_waiting_buy(c, state('PAUSED', 'RULE_REVIEW_REQUIRED'))
        fill(c, quote('blocked', D1, '09:36', '10'), Calendar(DAYS))
        self.assertEqual(c['status'], 'WAIT_BUY')
        self.assertEqual(c['book']['fills'], [])
        self.assertEqual(c['orders'][0]['requested_at'], original['orders'][0]['requested_at'])
        self.assertEqual(c['rule'], original['rule'])
        self.assertEqual(eligible_orders([c], Calendar(DAYS), moment(stamp(D1, '09:40'))), {})
        recovered = state(clock='10:00', version='v2')
        self.assertTrue(admission.update_waiting_buy(c, recovered))
        self.assertFalse(admission.update_waiting_buy(c, dict(recovered, checked_at=stamp(D1, '10:01'))))
        fill(c, quote('old', D1, '09:59', '10'), Calendar(DAYS))
        self.assertEqual(c['book']['fills'], [])
        fresh = quote('new', D1, '10:01', '10')
        fill(c, fresh, Calendar(DAYS)); fill(c, fresh, Calendar(DAYS))
        self.assertEqual(len(c['book']['fills']), 1)
        self.assertEqual(c['status'], 'OPEN')

    def test_unrelated_mapping_update_does_not_move_eligible_clock(self):
        c = waiting(); admission.update_waiting_buy(c, state())
        updated = dict(state(), mapping_version='unrelated', eligible_after=stamp(D1, '10:00'))
        admission.update_waiting_buy(c, updated)
        self.assertEqual(c['orders'][0]['entry_admission']['eligible_after'], stamp(D1, '09:00'))

    def test_current_gate_filters_frozen_choices_without_rewriting_raw(self):
        raw = report('frozen', D1, '09:35', [(A,'新增')], [(ETF,A,1), (ETF2,A,2)])
        saved = deepcopy(raw)
        filtered, blocks = admission.filter_report(raw, {ETF:state('PAUSED','RULE_REVIEW_REQUIRED'), ETF2:state()},
                                                   {ETF:100, ETF2:100})
        self.assertEqual([row['etf'] for row in filtered['candidates']], [ETF2])
        self.assertEqual(blocks, [dict(etf=ETF,reason='RULE_REVIEW_REQUIRED')])
        self.assertEqual(raw, saved)

    def test_missing_market_still_persists_current_wait_reason(self):
        from modules.strategy_hermes.stage2.worker import apply_market
        from unittest.mock import Mock
        ledger = Mock(origin='LIVE')
        result = apply_market(ledger, moment(stamp(D1, '09:36')), DAYS, {}, None)
        ledger.refresh_entry_admission.assert_called_once_with()
        self.assertEqual(result['quote_events'], 0)


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'), 'requires isolated PostgreSQL/Redis')
class AdmissionIntegration(unittest.TestCase):
    def setUp(self):
        from test_stage2_postgres import StageTwo
        StageTwo.setUp(self)
        self.store.origin = 'LIVE'
        self.clock = moment(stamp(D1, '09:35'))
        clock = patch.object(admission, '_database_clock', side_effect=lambda connection:self.clock)
        clock.start(); self.addCleanup(clock.stop)

    def clean(self):
        from test_stage2_postgres import StageTwo
        StageTwo.clean(self)
        with self.store._connect() as conn:
            conn.execute('DELETE FROM maintained_mappings WHERE ns=%s', (self.ns,))

    def publish_mapping(self, *, review=False, extra=False, version='v1', at='09:00'):
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        mapping = deepcopy(self.map)
        mapping['version'] = version
        mapping['products'][ETF]['rule']['review_required'] = review
        if extra:
            mapping['products'][ETF2] = product()
        with self.store._connect() as conn:
            row = conn.execute('SELECT version FROM maintained_mappings WHERE ns=%s ORDER BY available_at DESC,version DESC LIMIT 1', (self.ns,)).fetchone()
        if row: mapping['parent_mapping_version'] = row[0]
        publish_mapping(self.store, mapping, {}, at=moment(stamp(D1, at)))
        return mapping

    def publish(self, rid='entry', hour='09:35', selected=(ETF,)):
        from modules.strategy_hermes.stage2.worker import planned_at
        claim = self.store.claim_round(D1, hour, planned_at(D1,hour))
        raw = report(rid,D1,hour,[(A,'新增')],[(code,A,i+1) for i,code in enumerate(selected)])
        return self.store.publish(claim,raw,DAYS,{code:100 for code in selected},{})

    def test_current_review_blocks_frozen_candidate_but_other_product_publishes(self):
        self.publish_mapping(review=True,extra=True)
        saved = self.publish(selected=(ETF,ETF2))
        self.assertEqual(set(saved['candidates']), {ETF2})
        self.assertEqual(saved['input_meta']['execution_candidate_blocks'], [dict(etf=ETF,reason='RULE_REVIEW_REQUIRED')])
        self.assertEqual(saved['input_meta']['entry_admission'][ETF]['status'],'PAUSED')

    def test_unpublished_database_prevents_initial_buy(self):
        saved = self.publish()
        self.assertEqual(saved['candidates'],{})
        self.assertEqual(saved['input_meta']['execution_candidate_blocks'],[dict(etf=ETF,reason=admission.UNPUBLISHED)])
        self.assertEqual(self.store.inspect()['cycles'], [])

    def test_waiting_buy_pause_survives_restart_recovery_and_duplicate_price(self):
        from modules.strategy_hermes.stage2.postgres import PostgresLedger
        self.publish_mapping(); self.publish()
        self.publish_mapping(review=True,version='v2',at='09:36')
        self.clock = moment(stamp(D1,'09:37'))
        self.store.apply_quotes([quote('held',D1,'09:37','10')],DAYS,{ETF:100})
        c, = self.store.inspect()['cycles']
        self.assertEqual(c['pending_reason'],'RULE_REVIEW_REQUIRED')
        self.assertEqual(c['book']['fills'],[])
        self.store = PostgresLedger(self.settings,'FROZEN'); self.store.origin='LIVE'
        self.publish_mapping(version='v3',at='09:38'); self.clock=moment(stamp(D1,'09:39'))
        self.store.refresh_entry_admission()
        self.store.apply_quotes([quote('too-old',D1,'09:38','10')],DAYS,{ETF:100})
        self.assertEqual(self.store.inspect()['cycles'][0]['book']['fills'],[])
        q=quote('recovered',D1,'09:40','10'); self.clock=moment(stamp(D1,'09:40'))
        self.store.apply_quotes([q],DAYS,{ETF:100}); self.store.apply_quotes([q],DAYS,{ETF:100})
        c,=self.store.inspect()['cycles']
        self.assertEqual((c['status'],len(c['book']['fills'])),('OPEN',1))
        self.assertEqual(c['orders'][0]['requested_at'],stamp(D1,'09:35'))

    def test_future_review_version_does_not_retroactively_apply(self):
        self.publish_mapping(); self.publish_mapping(review=True,version='v2',at='10:00')
        self.assertEqual(set(self.publish()['candidates']),{ETF})
        self.clock=moment(stamp(D1,'10:01')); self.store.refresh_entry_admission()
        self.assertEqual(self.store.inspect()['cycles'][0]['pending_reason'],'RULE_REVIEW_REQUIRED')

    def test_query_failure_savepoint_preserves_exit_and_pauses_waiting_buy(self):
        from modules.strategy_hermes.stage2.postgres import BoundConnection
        self.publish_mapping(extra=True)
        with self.store._connect() as conn, conn.transaction():
            self.store._store(BoundConnection(conn),[report('held-entry',D1,'09:35',[(A,'新增')],[(ETF,A,1)]),
                quote('held-fill',D1,'09:36','10'),report('weak',D1,'10:35',[(A,'减弱')])],DAYS,{ETF:100,ETF2:100},Rules())
        self.clock=moment(stamp(D2,'09:35'))
        # This direction already belongs to the rotation chain: STRENGTHEN is
        # the valid new-entry phase; NEW would correctly normalize to HOLD.
        with self.store._connect() as conn, conn.transaction():
            self.store._store(BoundConnection(conn),[report('pending-second',D2,'09:35',[(A,'加强')],[(ETF2,A,1)])],DAYS,{ETF:100,ETF2:100},Rules())
        self.assertEqual({c['etf']:c['status'] for c in self.store.inspect()['cycles']},
                         {ETF:'OPEN',ETF2:'WAIT_BUY'})
        original = admission.read_states
        class FailingReference:
            def __init__(self, conn): self.conn=conn
            def transaction(self): return self.conn.transaction()
            def execute(self, sql, args=()):
                return self.conn.execute('SELECT 1/0') if 'FROM maintained_mappings' in sql else self.conn.execute(sql,args)
        with patch.object(admission,'read_states',side_effect=lambda conn,ns,codes:original(FailingReference(conn),ns,codes)):
            self.store.apply_quotes([quote('sell',D2,'09:36','11'),quote('deny-buy',D2,'09:36','10',code=ETF2)],DAYS,{ETF:100,ETF2:100})
        cycles={c['etf']:c for c in self.store.inspect()['cycles']}
        self.assertEqual([f['quantity'] for f in cycles[ETF]['book']['fills']],[1000,500])
        self.assertEqual(cycles[ETF2]['pending_reason'],admission.UNAVAILABLE)
        self.assertEqual(cycles[ETF2]['book']['fills'],[])

    def test_mapping_publication_and_quote_commit_share_lock_order(self):
        self.publish_mapping(); self.publish()
        self.clock=moment(stamp(D1,'09:37'))
        started=Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.store._connect() as conn,conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(self.ns,))
                mapping=deepcopy(self.map); mapping['version']='v2';mapping['parent_mapping_version']='v1'
                mapping['products'][ETF]['rule']['review_required']=True
                from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
                publish_mapping(self.store,mapping,{},at=moment(stamp(D1,'09:36')),connection=conn)
                def ingest():
                    started.set(); self.store.apply_quotes([quote('racing',D1,'09:37','10')],DAYS,{ETF:100})
                future=pool.submit(ingest)
                self.assertTrue(started.wait(2))
                self.assertFalse(future.done())
            future.result(timeout=10)
        c,=self.store.inspect()['cycles']
        self.assertEqual(c['book']['fills'],[])
        self.assertEqual(c['pending_reason'],'RULE_REVIEW_REQUIRED')
