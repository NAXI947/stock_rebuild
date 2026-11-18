"""A recovery claim can share a transaction without resetting round budgets."""
import json
import os
import unittest

from modules.strategy_hermes.simulator import moment
import test_stage2_postgres as fixtures
from test_stage1 import D1, stamp


@unittest.skipUnless(os.environ.get('STAGE2_TEST_ENV_FILE'), 'requires isolated PostgreSQL/Redis')
class TransactionalRoundClaim(unittest.TestCase):
    def setUp(self):
        fixtures.StageTwo.setUp(self)

    def clean(self):
        fixtures.StageTwo.clean(self)

    def test_caller_rollback_removes_claim_and_retry_retains_original_budget(self):
        planned = moment(stamp(D1, '09:35'))
        with self.assertRaisesRegex(RuntimeError, 'rollback fixture'):
            with self.store._connect() as conn, conn.transaction():
                claim = self.store._claim_round(conn, D1, '09:35', planned)
                self.assertIsNotNone(claim)
                raise RuntimeError('rollback fixture')
        with self.store._connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM round_tasks WHERE ns=%s',
                                          (self.ns,)).fetchone()[0], 0)
        first = self.store.claim_round(D1, '09:35', planned)
        self.assertTrue(self.store.take_call(first, 'mx', 1))
        self.store.fail_round(first, 'TRANSIENT')
        with self.store._connect() as conn, conn.transaction():
            retry = self.store._claim_round(conn, D1, '09:35', moment(stamp(D1, '10:35')),
                                           lease_seconds=60)
            self.assertIsNotNone(retry)
            self.assertNotEqual(retry.token, first.token)
            row = conn.execute('''SELECT planned_at,attempts,calls,
                lease_until>clock_timestamp() AND lease_until<=clock_timestamp()+interval '61 seconds'
                FROM round_tasks WHERE ns=%s AND slot='09:35' ''', (self.ns,)).fetchone()
        self.assertEqual(row[:2], (planned, 2))
        self.assertEqual(json.loads(row[2]), {'mx': 1})
        self.assertTrue(row[3])
        self.assertFalse(self.store.take_call(retry, 'mx', 1))
        self.store.fail_round(retry, 'TRANSIENT')
        self.assertIsNone(self.store.claim_round(D1, '09:35', planned))

    def test_explicit_model_rejection_remains_terminal(self):
        planned = moment(stamp(D1, '10:35'))
        claim = self.store.claim_round(D1, '10:35', planned)
        self.store.fail_round(claim, 'OctopRejected')
        with self.store._connect() as conn, conn.transaction():
            self.assertIsNone(self.store._claim_round(conn, D1, '10:35', planned))
