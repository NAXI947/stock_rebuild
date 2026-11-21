"""Verify the native agent's small authenticated command boundary."""
from io import BytesIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import psycopg

from modules.strategy_hermes.native_mcp import application, tools_schema, MAX_BODY

REQUEST_ID='pv-fdca36cb1f92865c64fc233747999a35ecb1fa42'


class NativeMCP(unittest.TestCase):
    def setUp(self):
        self.queue = Mock()
        self.app = application(self.queue, "x" * 48, wait_seconds=0)

    def request(self, body, **changes):
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        env = dict(REQUEST_METHOD="POST", PATH_INFO="/mcp", CONTENT_TYPE="application/json",
                   CONTENT_LENGTH=str(len(raw)), HTTP_AUTHORIZATION="Bearer " + "x" * 48,
                   **{"wsgi.input": BytesIO(raw)})
        env.update(changes)
        response = []
        data = b"".join(self.app(env, lambda status, headers: response.append((status, headers))))
        return int(response[0][0].split()[0]), json.loads(data) if data else None

    def test_authentication_and_origin_precede_tool_execution(self):
        req = dict(jsonrpc="2.0", id=1, method="tools/call", params={"name":"stock_hermes_prepare","arguments":{"slot":"09:35"}})
        self.assertEqual(self.request(req, HTTP_AUTHORIZATION="Bearer wrong")[0], 401)
        self.assertEqual(self.request(req, HTTP_ORIGIN="https://unexpected.example")[0], 403)
        self.assertEqual(self.request(req, REQUEST_METHOD="GET")[0], 405)
        self.queue.prepare.assert_not_called()

    def test_stateless_negotiation_and_notification_do_not_start_work(self):
        status, body = self.request(dict(jsonrpc="2.0", id=1, method="initialize", params={"protocolVersion":"2025-03-26"}))
        self.assertEqual((status,body["result"]["protocolVersion"]),(200,"2025-03-26"))
        self.assertEqual(self.request(dict(jsonrpc="2.0", method="notifications/initialized")), (202,None))
        _, listed = self.request(dict(jsonrpc="2.0",id=2,method="tools/list"))
        self.assertEqual(len(listed["result"]["tools"]),3)
        self.assertFalse(self.queue.mock_calls)

    def test_only_declared_command_and_arguments_reach_queue(self):
        self.queue.prepare.return_value={"run_id":"round","state":"READY","analysis":{"sectors":[]}}
        _, body=self.request(dict(jsonrpc="2.0",id=3,method="tools/call",params={"name":"stock_hermes_prepare","arguments":{"slot":"09:35"}}))
        self.assertEqual(json.loads(body["result"]["content"][0]["text"])["state"],"READY")
        self.queue.prepare.assert_called_once_with("09:35")
        _, bad=self.request(dict(jsonrpc="2.0",id=4,method="tools/call",params={"name":"stock_hermes_prepare","arguments":{"slot":"09:35","trade_date":"1999-01-01"}}))
        self.assertTrue(bad['result']['isError'])
        self.assertEqual(json.loads(bad['result']['content'][0]['text'])['error'],'UNKNOWN_TOOL_OR_ARGUMENTS')
        self.queue.prepare.assert_called_once()

    def test_failure_is_visible_and_malformed_input_does_not_call_queue(self):
        self.queue.status.return_value={"state":"FAILED","error":"NATIVE_DEADLINE_EXCEEDED"}
        _, body=self.request(dict(jsonrpc="2.0",id=5,method="tools/call",params={"name":"stock_hermes_status","arguments":{"run_id":"round"}}))
        self.assertTrue(body["result"]["isError"])
        for body in ['{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}', '[]']:
            self.assertEqual(self.request(body)[1]["error"]["code"],-32602)
        self.assertEqual(self.request({},CONTENT_LENGTH=str(MAX_BODY+1))[1]["error"]["code"],-32602)

    def test_native_octop_string_arguments_are_decoded_once(self):
        decisions=[{"sector_id":"kaipanla:801027","phase":"UNKNOWN","evidence_refs":["ref"]}]
        self.queue.submit.return_value={"state":"PUBLISHED","run_id":"round","report_id":"report"}
        _, body=self.request(dict(jsonrpc="2.0",id=6,method="tools/call",params={"name":"stock_hermes_submit",
            "arguments":{"run_id":"round","decisions":json.dumps(decisions)}}))
        self.queue.submit.assert_called_once_with("round",decisions)
        self.assertFalse(body["result"]["isError"])

    def test_maintenance_schema_is_opt_in_and_research_contract_unchanged(self):
        original = tools_schema()
        combined = tools_schema(maintenance=True)
        self.assertEqual([tool["name"] for tool in original],
                         ["stock_hermes_prepare", "stock_hermes_submit", "stock_hermes_status"])
        self.assertEqual(combined[:3], original)
        self.assertEqual(len(combined), 7)
        prepare, status = combined[3:5]
        self.assertEqual(prepare["name"], "stock_etf_maintenance_prepare")
        self.assertEqual(status["name"], "stock_etf_maintenance_status")
        self.assertEqual(prepare["inputSchema"]["required"], ["mode"])
        self.assertEqual(prepare["inputSchema"]["properties"]["mode"]["enum"], ["market", "retry", "materials", "verify"])
        self.assertEqual(prepare['inputSchema']['properties']['request_ids']['type'],'string')
        self.assertIn('JSON数组字符串',prepare['inputSchema']['properties']['request_ids']['description'])
        self.assertEqual(prepare['inputSchema']['properties']['target_codes']['type'],'string')
        self.assertEqual(status["inputSchema"]["required"], ["run_id"])
        self.assertEqual(status["inputSchema"]["properties"]["run_id"]["maxLength"], 80)
        for tool in combined:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.app = application(self.queue, "x" * 48, wait_seconds=0, maintenance=Mock())
        _, listed = self.request(dict(jsonrpc="2.0", id=7, method="tools/list"))
        self.assertEqual(listed["result"]["tools"], combined)

    def test_static_verification_scope_and_review_reach_only_bounded_service(self):
        maintenance=Mock()
        self.app=application(self.queue,'x'*48,wait_seconds=0,maintenance=maintenance)
        maintenance.prepare.return_value=dict(state='QUEUED',run_id='verification-job')
        self.request(dict(jsonrpc='2.0',id=20,method='tools/call',params=dict(
            name='stock_etf_maintenance_prepare',arguments=dict(mode='verify',request_ids=[REQUEST_ID]))))
        maintenance.prepare.assert_called_once_with('verify',request_ids=[REQUEST_ID])
        self.request(dict(jsonrpc='2.0',id=24,method='tools/call',params=dict(
            name='stock_etf_maintenance_prepare',arguments=dict(mode='materials',target_codes=['588100.SH']))))
        maintenance.prepare.assert_called_with('materials',target_codes=['588100.SH'])
        maintenance.context.return_value=dict(state='AWAITING_REVIEW',target_id='target-one')
        self.request(dict(jsonrpc='2.0',id=21,method='tools/call',params=dict(
            name='stock_etf_verification_context',arguments=dict(target_id='target-one'))))
        maintenance.context.assert_called_once_with('target-one')
        review=dict(decision='NEEDS_MATERIALS',reason='范围证据未取得')
        maintenance.submit_review.return_value=dict(state='NEEDS_MATERIALS',target_id='target-one')
        self.request(dict(jsonrpc='2.0',id=22,method='tools/call',params=dict(
            name='stock_etf_verification_submit',arguments=dict(target_id='target-one',review=json.dumps(review)))))
        maintenance.submit_review.assert_called_once_with('target-one',review)
        for invalid in (review,'{"decision":"APPROVE","decision":"NEEDS_MATERIALS"}'):
            _,result=self.request(dict(jsonrpc='2.0',id=23,method='tools/call',params=dict(
                name='stock_etf_verification_submit',arguments=dict(target_id='target-one',review=invalid))))
            self.assertTrue(result['result']['isError'])
        maintenance.submit_review.assert_called_once()
        self.assertFalse(self.queue.mock_calls)

    def test_maintenance_dispatch_uses_only_maintenance_service(self):
        maintenance = Mock()
        self.app = application(self.queue, "x" * 48, wait_seconds=0, maintenance=maintenance)
        for mode in ("market", "retry", "materials", "verify"):
            receipt = dict(state="PARTIAL", mode=mode, run_id="maintenance-test",
                           feedback="成功项已保存，缺口保留旧值", qq={"state": "SENT", "delivery_receipt": "test-qq"})
            maintenance.prepare.return_value = receipt
            options={'request_ids':[REQUEST_ID]} if mode=='verify' else {}
            _, body = self.request(dict(jsonrpc="2.0", id=8, method="tools/call",
                params={"name": "stock_etf_maintenance_prepare", "arguments": {"mode": mode,**options}}))
            self.assertEqual(json.loads(body["result"]["content"][0]["text"]), receipt)
            self.assertFalse(body["result"]["isError"])
            maintenance.prepare.assert_called_with(mode,**options)
        maintenance.status.return_value = dict(state="FAILED", mode="close", run_id="maintenance-test", error="SOURCE_UNAVAILABLE")
        _, body = self.request(dict(jsonrpc="2.0", id=9, method="tools/call",
            params={"name": "stock_etf_maintenance_status", "arguments": {"run_id": "maintenance-test"}}))
        self.assertTrue(body["result"]["isError"])
        maintenance.status.assert_called_once_with("maintenance-test")
        self.assertFalse(self.queue.mock_calls)

    def test_disabled_maintenance_and_extra_arguments_do_not_start_work(self):
        names = (("stock_etf_maintenance_prepare", {"mode": "close"}),
                 ("stock_etf_maintenance_status", {"run_id": "maintenance-test"}))
        for name, args in names:
            _, body = self.request(dict(jsonrpc="2.0", id=10, method="tools/call",
                params={"name": name, "arguments": args}))
            self.assertEqual(body["error"]["code"], -32602)
        maintenance = Mock()
        self.app = application(self.queue, "x" * 48, wait_seconds=0, maintenance=maintenance)
        for name, args in names:
            for invalid in ({}, {**args, "request_limit": 9999}, json.dumps(args)):
                _, body = self.request(dict(jsonrpc="2.0", id=11, method="tools/call",
                    params={"name": name, "arguments": invalid}))
                if isinstance(invalid,dict):self.assertTrue(body['result']['isError'])
                else:self.assertEqual(body["error"]["code"], -32602)
        self.assertFalse(maintenance.mock_calls)
        self.assertFalse(self.queue.mock_calls)

    def test_actual_octop_scope_string_and_legacy_arrays_share_strict_list_input(self):
        maintenance=Mock();maintenance.prepare.return_value=dict(state='QUEUED',run_id='existing-job')
        self.app=application(self.queue,'x'*48,wait_seconds=0,maintenance=maintenance)
        for field,mode,item in [('request_ids','verify',REQUEST_ID),('target_codes','materials','512480.SH')]:
            for value in ([item],item,json.dumps([item])):
                with self.subTest(field=field,value=value):
                    _,body=self.request(dict(jsonrpc='2.0',id=30,method='tools/call',params=dict(
                        name='stock_etf_maintenance_prepare',arguments={'mode':mode,field:value})))
                    self.assertFalse(body['result']['isError'])
                    maintenance.prepare.assert_called_with(mode,**{field:[item]})
        self.assertFalse(self.queue.mock_calls)

    def test_malformed_scope_returns_tool_error_without_starting_work_or_notification(self):
        maintenance=Mock();self.app=application(self.queue,'x'*48,wait_seconds=0,maintenance=maintenance)
        invalid=[None,'ALL','',REQUEST_ID+','+REQUEST_ID,'[]','[',json.dumps([REQUEST_ID]*2),
                 json.dumps(['pv-'+format(i,'040x') for i in range(9)]),'[null]',[[REQUEST_ID]],{'id':REQUEST_ID}]
        with patch('modules.strategy_hermes.qq_task_notices.enqueue_status') as notify:
            for value in invalid:
                _,body=self.request(dict(jsonrpc='2.0',id=31,method='tools/call',params=dict(
                    name='stock_etf_maintenance_prepare',arguments=dict(mode='verify',request_ids=value))))
                self.assertNotIn('error',body)
                self.assertTrue(body['result']['isError'])
                receipt=json.loads(body['result']['content'][0]['text'])
                self.assertEqual((receipt['state'],receipt['error']),('TOOL_ERROR','INVALID_REQUEST_IDS_FORMAT'))
                self.assertIn('JSON数组字符串',receipt['expected_arguments'])
            _,body=self.request(dict(jsonrpc='2.0',id=34,method='tools/call',params=dict(
                name='stock_etf_maintenance_prepare',arguments=dict(mode='verify'))))
            self.assertTrue(body['result']['isError'])
            self.assertEqual(json.loads(body['result']['content'][0]['text'])['error'],'REQUEST_IDS_REQUIRED_FOR_VERIFY')
            notify.assert_not_called()
        self.assertFalse(maintenance.mock_calls)
        self.assertFalse(self.queue.mock_calls)

    def test_known_business_error_can_be_corrected_in_the_same_application(self):
        maintenance=Mock();maintenance.prepare.side_effect=[ValueError('verification request not found'),
            dict(state='QUEUED',run_id='original-verification',requests_used=17,request_budget=120)]
        self.app=application(self.queue,'x'*48,wait_seconds=0,maintenance=maintenance)
        with patch('modules.strategy_hermes.qq_task_notices.enqueue_status') as notify:
            outputs=[]
            for item in ('pv-'+'0'*40,REQUEST_ID):
                _,body=self.request(dict(jsonrpc='2.0',id=32,method='tools/call',params=dict(
                    name='stock_etf_maintenance_prepare',arguments=dict(mode='verify',request_ids=item))))
                outputs.append(body)
            notify.assert_not_called()
        self.assertNotIn('error',outputs[0])
        self.assertEqual(json.loads(outputs[0]['result']['content'][0]['text'])['error'],'VERIFICATION_REQUEST_NOT_FOUND')
        self.assertFalse(outputs[1]['result']['isError'])
        self.assertEqual(json.loads(outputs[1]['result']['content'][0]['text'])['requests_used'],17)
        _,unknown=self.request(dict(jsonrpc='2.0',id=33,method='unknown/method'))
        self.assertEqual(unknown['error']['code'],-32601)
        self.assertEqual(maintenance.prepare.call_count,2)

    def test_maintenance_wait_returns_saved_terminal_and_final_qq_receipt(self):
        maintenance, stop = Mock(), Mock()
        stop.wait.return_value = False
        maintenance.prepare.return_value = dict(state="QUEUED", mode="close", run_id="maintenance-test")
        final = dict(state="PARTIAL", mode="close", run_id="maintenance-test", feedback="维护回执",
                     qq={"state": "SENT", "delivery_receipt": "test-qq"})
        maintenance.status.side_effect = [
            dict(state="RUNNING", mode="close", run_id="maintenance-test"),
            {**final, "qq": {"state": "PENDING"}}, final,
        ]
        self.app = application(self.queue, "x" * 48, stop=stop, wait_seconds=50, maintenance=maintenance)
        with patch("modules.strategy_hermes.native_mcp.monotonic", return_value=0):
            _, body = self.request(dict(jsonrpc="2.0", id=12, method="tools/call",
                params={"name": "stock_etf_maintenance_prepare", "arguments": {"mode": "close"}}))
        self.assertEqual(json.loads(body["result"]["content"][0]["text"]), final)
        self.assertEqual(maintenance.status.call_count, 3)
        self.assertEqual(stop.wait.call_count, 3)
        self.assertFalse(self.queue.mock_calls)

    def test_passing_database_errors_are_retried_inside_the_wait_limit(self):
        stop = Mock()
        stop.wait.return_value = False
        lost = psycopg.OperationalError("connection reset")
        self.queue.submit.side_effect = [lost, dict(state="SUBMITTED", run_id="round")]
        self.queue.status.side_effect = [lost, dict(state="PUBLISHED", run_id="round", report_id="octop-1")]
        self.app = application(self.queue, "x" * 48, stop=stop, wait_seconds=50)
        with patch("modules.strategy_hermes.native_mcp.monotonic", return_value=0), \
                self.assertLogs("modules.strategy_hermes.native_mcp", "WARNING"):
            status, body = self.request(dict(jsonrpc="2.0", id=20, method="tools/call",
                params={"name": "stock_hermes_submit", "arguments": {"run_id": "round", "decisions": "[]"}}))
        self.assertEqual((status, json.loads(body["result"]["content"][0]["text"])["state"]), (200, "PUBLISHED"))
        self.assertEqual(self.queue.submit.call_args_list, [(("round", []),)] * 2)
        self.assertEqual(self.queue.status.call_count, 2)

    def test_a_poll_that_keeps_failing_returns_the_last_saved_state(self):
        stop, clock = Mock(), iter(range(1000))
        stop.wait.return_value = False
        self.queue.status.side_effect = [dict(state="SUBMITTED", run_id="round")] + [OSError("unreachable")] * 100
        self.app = application(self.queue, "x" * 48, stop=stop, wait_seconds=50)
        with patch("modules.strategy_hermes.native_mcp.monotonic", side_effect=lambda: next(clock)), \
                self.assertLogs("modules.strategy_hermes.native_mcp", "WARNING"):
            status, body = self.request(dict(jsonrpc="2.0", id=21, method="tools/call",
                params={"name": "stock_hermes_status", "arguments": {"run_id": "round"}}))
        self.assertEqual((status, json.loads(body["result"]["content"][0]["text"])), (200, dict(state="SUBMITTED", run_id="round")))

    def test_an_unavailable_service_and_other_database_errors_still_answer_503(self):
        stop, clock = Mock(), iter(range(1000))
        stop.wait.return_value = False
        request = dict(jsonrpc="2.0", id=22, method="tools/call",
                       params={"name": "stock_hermes_prepare", "arguments": {"slot": "09:35"}})
        self.app = application(self.queue, "x" * 48, stop=stop, wait_seconds=50)
        self.queue.prepare.side_effect = psycopg.OperationalError("down")
        with patch("modules.strategy_hermes.native_mcp.monotonic", side_effect=lambda: next(clock)), \
                self.assertLogs("modules.strategy_hermes.native_mcp", "WARNING"):
            self.assertEqual(self.request(request), (503, {"error": "NATIVE_ROUND_SERVICE_UNAVAILABLE"}))
        self.assertGreater(self.queue.prepare.call_count, 1)
        self.queue.prepare.reset_mock()
        self.queue.prepare.side_effect = psycopg.ProgrammingError("bug")
        self.assertEqual(self.request(request), (503, {"error": "NATIVE_ROUND_SERVICE_UNAVAILABLE"}))
        self.queue.prepare.assert_called_once()

    def test_terminal_maintenance_without_pending_delivery_does_not_poll(self):
        for state, qq in (("SUCCEEDED", {"state": "SENT"}), ("PARTIAL", {"state": "FAILED"}), ("SKIPPED", {}), ("FAILED", {})):
            maintenance, stop = Mock(), Mock()
            receipt = dict(state=state, mode="preopen", run_id="maintenance-test", qq=qq)
            maintenance.status.return_value = receipt
            self.app = application(self.queue, "x" * 48, stop=stop, wait_seconds=50, maintenance=maintenance)
            with self.subTest(state=state), patch("modules.strategy_hermes.native_mcp.monotonic", return_value=0):
                _, body = self.request(dict(jsonrpc="2.0", id=13, method="tools/call",
                    params={"name": "stock_etf_maintenance_status", "arguments": {"run_id": "maintenance-test"}}))
                self.assertEqual(json.loads(body["result"]["content"][0]["text"]), receipt)
                self.assertEqual(body["result"]["isError"], state == "FAILED")
                maintenance.status.assert_called_once_with("maintenance-test")
                stop.wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
