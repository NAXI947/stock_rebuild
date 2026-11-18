"""Execution registry changes must not erase positions or reopen eligibility."""
import json
import os
from types import SimpleNamespace
import unittest

from modules.strategy_hermes.ledger import encode
from modules.strategy_hermes.simulator import Rules, moment
from modules.strategy_hermes.stage2.execution_units import apply_candidate_blocks, eligible_report, execution_units
import test_execution_calendar as calendar_fixtures
import test_stage2_postgres as fixtures
from test_stage1 import A, D1, D2, DAYS, ETF, ETF2, quote, report, stamp


class UnitProjection(unittest.TestCase):
    def connection(self, instruments):
        calls = []

        def execute(sql, args):
            calls.append((sql, args))
            row = None if instruments is None else (encode(dict(instruments=instruments)),)
            return SimpleNamespace(fetchone=lambda: row)

        return SimpleNamespace(execute=execute), calls

    def test_live_preserves_frozen_units_and_adds_new_instruments(self):
        conn, calls = self.connection({ETF: 100})
        current = {ETF: 200, ETF2: 100}
        self.assertEqual(execution_units(conn, 'fixture', 'LIVE', current), {ETF: 100, ETF2: 100})
        self.assertEqual(current, {ETF: 200, ETF2: 100})
        self.assertIn('pg_advisory_xact_lock', calls[0][0])
        self.assertEqual(execution_units(conn, 'fixture', 'LIVE', {}), {ETF: 100})

    def test_new_and_frozen_runs_keep_their_own_units(self):
        conn, calls = self.connection(None)
        self.assertEqual(execution_units(conn, 'fixture', 'LIVE', {ETF: 100}), {ETF: 100})
        calls.clear()
        original = {ETF2: 200}
        self.assertIs(execution_units(conn, 'fixture', 'FROZEN', original), original)
        self.assertEqual(calls, [])

    def test_candidate_filter_does_not_rewrite_original_report(self):
        raw = report('new', D1, '09:35', [(A, '新增')], [(ETF, A, 1), (ETF2, A, 2)])
        frozen = {ETF: 100, ETF2: 100}
        current = {ETF2: 200}
        filtered, blocks = eligible_report(raw, current, frozen)
        self.assertEqual(filtered['candidates'], [])
        self.assertEqual(len(raw['candidates']), 2)
        self.assertEqual(filtered['report_json'], raw['report_json'])
        self.assertEqual({row['etf']: row['reason'] for row in blocks},
                         {ETF: 'RULE_UNVERIFIED', ETF2: 'TRADE_UNIT_CHANGED'})

    def test_invalid_input_is_not_hidden_by_frozen_unit(self):
        conn, _ = self.connection({ETF: 100})
        for current in ({ETF: 0}, {'invalid': 100}):
            with self.subTest(current=current), self.assertRaises(ValueError):
                execution_units(conn, 'fixture', 'LIVE', current)

    def test_final_admission_block_is_shared_by_display_and_candidate_projection(self):
        product = dict(etf=ETF,ai_selected=True,execution_eligibility='READY',execution_status='COMPLETE',
            recommendation_status='CONFIRMED',representative_current=True,checks={})
        evaluation = dict(method='etf-direction-first-v2',candidates=[dict(etf=ETF)],
                          sectors={A:dict(products=[product])})
        original = dict(etf_recommendations=evaluation,penetration=evaluation)
        result = apply_candidate_blocks(original,[dict(etf=ETF,reason='TRADE_UNIT_CHANGED')])
        self.assertEqual(result['penetration'],result['etf_recommendations'])
        self.assertEqual(result['etf_recommendations']['candidates'],[])
        saved = result['etf_recommendations']['sectors'][A]['products'][0]
        self.assertEqual(saved['execution_eligibility'],'BLOCKED')
        self.assertEqual(saved['execution_reasons'],['TRADE_UNIT_CHANGED'])
        self.assertFalse(saved['representative_current'])
        self.assertEqual(product['recommendation_status'],'CONFIRMED')

    def test_temporary_admission_pause_is_shared_as_pending_not_rejected(self):
        product = dict(etf=ETF,ai_selected=True,execution_eligibility='READY',execution_status='COMPLETE',
            recommendation_status='CONFIRMED',representative_current=True,checks={})
        evaluation = dict(method='etf-direction-first-v2',candidates=[dict(etf=ETF)],
                          sectors={A:dict(products=[product])})
        result = apply_candidate_blocks(dict(etf_recommendations=evaluation),
                                        [dict(etf=ETF,reason='RULE_REVIEW_REQUIRED')])
        current = result['etf_recommendations']['sectors'][A]['products'][0]
        self.assertEqual(current['recommendation_status'],'PENDING')
        self.assertEqual(current['execution_eligibility'],'PENDING')
        self.assertEqual(current['execution_reasons'],['RULE_REVIEW_REQUIRED'])
        self.assertEqual(current['rejection_reasons'],[])
        self.assertEqual(result['etf_recommendations']['candidates'],[])


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'), 'requires isolated PostgreSQL/Redis')
class ExecutionUnitIntegration(unittest.TestCase):
    def setUp(self):
        fixtures.StageTwo.setUp(self)
        self.initial = DAYS

    def clean(self):
        fixtures.StageTwo.clean(self)
        with self.store._connect() as conn:
            conn.execute('DELETE FROM maintained_mappings WHERE ns=%s', (self.ns,))

    seed = calendar_fixtures.ExecutionCalendarIntegration.seed
    frozen_hashes = calendar_fixtures.ExecutionCalendarIntegration.frozen_hashes
    market = calendar_fixtures.ExecutionCalendarIntegration.market

    def test_revoked_product_can_exit_existing_position_without_regaining_entry(self):
        from modules.strategy_hermes.stage2.worker import apply_market, planned_at, publish_round
        before = self.seed('LIVE')
        hashes = self.frozen_hashes()
        self.map['products'][ETF]['rule']['verified'] = False
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        self.map.update(version='fixture-revoked', parent_mapping_version='fixture-v1')
        publish_mapping(self.store, self.map, {}, at=moment(stamp(D1,'12:00')))
        outcome = apply_market(self.store, moment(stamp(D1, '13:06')), DAYS, self.map,
                               self.market(D1, '13:06', '11'))
        self.assertEqual(outcome['quote_events'], 1)
        cycle, = self.store.inspect()['cycles']
        self.assertEqual(cycle['book']['fills'], before['book']['fills'])
        self.assertEqual(cycle['orders'][-1]['eligible_at'], stamp(D2, '09:30'))

        source = report('exit', D1, '14:05', [(A, '退潮')])
        envelope = dict(input_kind='LIVE', published_at=stamp(D1, '14:05'),
                        report_json=source['report_json'], judgments=source['judgments'])
        claim = self.store.claim_round(D1, '14:05', planned_at(D1, '14:05'))
        publish_round(self.store, claim, envelope, DAYS, self.map, None)
        outcome = apply_market(self.store, moment(stamp(D2, '09:30')), DAYS, self.map,
                               self.market(D2, '09:30', '11'))
        self.assertEqual(outcome['quote_events'], 1)
        cycle, = self.store.inspect()['cycles']
        self.assertEqual(cycle['status'], 'CLOSED')
        self.assertEqual(cycle['rule'], before['rule'])
        self.assertEqual([row['quantity'] for row in cycle['book']['fills']], [1000, 1000])
        self.assertEqual(self.frozen_hashes(), hashes)

        # Even a directly submitted old-code candidate must not gain eligibility
        # merely because its execution unit remains in the frozen registry.
        claim = self.store.claim_round(D2, '10:35', planned_at(D2, '10:35'))
        source = report('no-reentry', D2, '10:35', [(A, '新增')], [(ETF, A, 1)])
        saved = self.store.publish(claim, source, DAYS, {}, {})
        self.assertEqual(saved['candidates'], {})
        self.assertEqual(saved['raw'], source['report_json'])
        self.assertEqual(saved['input_meta']['execution_candidate_blocks'],
                         [dict(etf=ETF, reason='PRODUCT_RULE_UNVERIFIED')])
        self.assertEqual([row['status'] for row in self.store.inspect()['cycles']], ['CLOSED'])
        self.assertEqual(self.frozen_hashes(), hashes)

        # Removal from the current catalog also retains the execution registry.
        self.map['products'].pop(ETF)
        self.assertEqual(apply_market(self.store, moment(stamp(D2, '10:36')), DAYS, self.map,
                                     self.market(D2, '10:36', '11'))['quote_events'], 1)

    def test_changed_units_block_new_entry_but_new_verified_product_can_enter(self):
        from modules.strategy_hermes.stage2.postgres import BoundConnection
        from modules.strategy_hermes.stage2.worker import planned_at
        self.store.origin = 'LIVE'
        from copy import deepcopy
        from modules.strategy_hermes.stage2.maintenance_store import publish_mapping
        self.map['products'][ETF]['trade_unit'] = 200
        self.map['products'][ETF2] = deepcopy(self.map['products'][ETF])
        self.map['products'][ETF2]['trade_unit'] = 100
        publish_mapping(self.store, self.map, {}, at=moment(stamp(D1,'09:00')))
        with self.store._connect() as conn, conn.transaction():
            self.store._store(BoundConnection(conn), [], DAYS, {ETF: 100}, Rules())
        with self.assertRaisesRegex(ValueError, 'previous instrument units must be present and unchanged'):
            with self.store._connect() as conn, conn.transaction():
                self.store._store(BoundConnection(conn), [], DAYS, {ETF: 200}, Rules())
        claim = self.store.claim_round(D1, '09:35', planned_at(D1, '09:35'))
        source = report('different-units', D1, '09:35', [(A, '新增')], [(ETF, A, 1), (ETF2, A, 2)])
        current = {ETF: 200, ETF2: 100}
        saved = self.store.publish(claim, source, DAYS, current, {})
        self.assertEqual(set(saved['candidates']), {ETF2})
        self.assertEqual(saved['input_meta']['execution_candidate_blocks'],
                         [dict(etf=ETF, reason='TRADE_UNIT_CHANGED')])
        self.store.apply_quotes([quote('new-product', D1, '09:36', '10', code=ETF2)], DAYS, current)
        cycle, = self.store.inspect()['cycles']
        self.assertEqual((cycle['etf'], cycle['unit'], cycle['status']), (ETF2, 100, 'OPEN'))
        with self.store._connect() as conn:
            config, = conn.execute('SELECT config FROM runs WHERE ns=%s', (self.ns,)).fetchone()
        self.assertEqual(json.loads(config)['instruments'], {ETF: 100, ETF2: 100})

    def test_frozen_unit_removal_is_still_rejected(self):
        self.seed('FROZEN')
        hashes = self.frozen_hashes()
        with self.assertRaisesRegex(ValueError, 'previous instrument units must be present and unchanged'):
            self.store.apply_quotes([quote('later', D1, '13:06', '11')], DAYS, {})
        self.assertEqual(self.frozen_hashes(), hashes)
