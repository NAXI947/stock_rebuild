"""A failed live publication must not starve already saved maintenance notices."""
from contextlib import nullcontext, redirect_stdout
from io import StringIO
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from modules.strategy_hermes.stage2 import schedule


class DeliveryIsolation(TestCase):
    def run_once(self, *, failure='market', redis_failure=False, paused=lambda:False, native_result=None):
        ledger = SimpleNamespace(origin='LIVE',settings=SimpleNamespace(env='production'))
        args = SimpleNamespace(received_at=None,once=True,poll_seconds=15,qq=True,trial_day=None,
            tickflow_config=None,kaipanla_captures=None,manual_queue=False,octop_stdio=False,
            trigger_mode='octop',calendar='local',mapping='local',snapshot_key='local',snapshot_file=None)
        def delivery(store,target,send):
            if target=='redis' and redis_failure:
                raise ValueError('stored redis view unavailable')
            return 'SENT'
        output=StringIO()
        with patch.object(schedule,'connection',return_value=nullcontext(Mock())), \
             patch('modules.strategy_hermes.stage2.native_rounds.NativeRounds',return_value=Mock()), \
             patch('modules.strategy_hermes.stage2.native_rounds.native_tick',
                   side_effect=ValueError('publication failed') if failure=='native' else None,
                   return_value=native_result or {'state':'IDLE'}), \
             patch.object(schedule,'runtime_calendar',return_value=['2026-11-16','2026-11-17']), \
             patch.object(schedule,'live_mapping',return_value={}), \
             patch.object(schedule,'shared_market',return_value={}), \
             patch('modules.strategy_hermes.stage2.worker.apply_market',
                   side_effect=ValueError('calendar update rejected') if failure=='market' else None,
                   return_value={}), \
             patch.object(schedule,'dispatch',side_effect=delivery) as dispatch, redirect_stdout(output):
            code=schedule.serve(ledger,Mock(),args,paused=paused)
        return code,json.loads(output.getvalue()),dispatch

    def test_ledger_failure_still_delivers_one_saved_qq_notice(self):
        code,result,dispatch=self.run_once()
        self.assertEqual(code,1)
        self.assertEqual(result['round'],'WORKER_ERROR')
        self.assertEqual(result['qq_delivery'],'SENT')
        self.assertEqual([c.args[1] for c in dispatch.call_args_list],['redis','octop'])

    def test_native_failure_and_redis_failure_cannot_starve_qq(self):
        _,result,dispatch=self.run_once(failure='native',redis_failure=True)
        self.assertEqual(result['redis_delivery'],'UNAVAILABLE')
        self.assertEqual(result['qq_delivery'],'SENT')
        self.assertEqual(dispatch.call_count,2)

    def test_successful_iteration_dispatches_only_once_per_target(self):
        code,result,dispatch=self.run_once(failure=None)
        self.assertEqual(code,0)
        self.assertEqual(result['qq_delivery'],'SENT')
        self.assertEqual(dispatch.call_count,2)

    def test_native_status_error_and_stage_are_retained_in_log(self):
        failed = dict(state='FAILED',run_id='fixture-run',slot='10:35',
                      error='CALENDAR_HISTORY_CONFLICT',stage='PUBLISH')
        _,result,dispatch=self.run_once(failure=None,native_result=failed)
        self.assertEqual(result['round'],'NATIVE_FAILED')
        self.assertEqual(result['native'],failed)
        self.assertEqual(dispatch.call_count,2)

    def test_release_pause_after_processing_prevents_delivery(self):
        pause=Mock(side_effect=[False,True])
        _,result,dispatch=self.run_once(paused=pause)
        dispatch.assert_not_called()
        self.assertNotIn('qq_delivery',result)
