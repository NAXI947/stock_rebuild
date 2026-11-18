"""OCTOP's deployed guard permits only evidence-bound static follow-up work."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

path=Path(__file__).resolve().parents[1]/'deploy/octop_native_guard.py'
spec=importlib.util.spec_from_file_location('maintenance_guard_under_test',path)
guard=importlib.util.module_from_spec(spec)
with patch.dict('sys.modules',{'langchain.agents.middleware':SimpleNamespace(AgentMiddleware=object)}):
    spec.loader.exec_module(guard)


def state(*messages):
    return {'messages':[dict(role='user',content='执行ETF资料维护 market，北京时间10:00。'),*messages]}


def receipt(name,body):
    return dict(role='tool',name=guard.SERVER+'_'+name,content=body)


class MaintenanceGuard(unittest.TestCase):
    def test_string_transport_cannot_expand_submitted_verification_scope(self):
        rid='pv-'+'1'*40
        request=SimpleNamespace(state=state(receipt('stock_etf_verification_submit',dict(state='PENDING',request_id=rid))),
            tool_call=dict(name=guard.SERVER+'_stock_etf_maintenance_prepare',args=dict(mode='verify')))
        for value in ([rid],rid,json.dumps([rid])):
            request.tool_call['args']['request_ids']=value
            guard.StockNativeRoundsMiddleware._check_tool(request)
        for value in (None,'ALL','[]','[',json.dumps([rid,rid]),json.dumps(['pv-'+format(i,'040x') for i in range(9)])):
            request.tool_call['args']['request_ids']=value
            guard.StockNativeRoundsMiddleware._check_tool(request)
        request.tool_call['args'].pop('request_ids')
        guard.StockNativeRoundsMiddleware._check_tool(request)
        for value in ('pv-'+'2'*40,json.dumps([rid,'pv-'+'2'*40])):
            request.tool_call['args']['request_ids']=value
            with self.assertRaisesRegex(RuntimeError,'SLOT_MISMATCH'):
                guard.StockNativeRoundsMiddleware._check_tool(request)

    def test_market_followup_verify_requires_submitted_request_receipt(self):
        request=SimpleNamespace(state=state(),tool_call=dict(name=guard.SERVER+'_stock_etf_maintenance_prepare',
            args=dict(mode='verify',request_ids=['pv-'+'1'*40])))
        with self.assertRaisesRegex(RuntimeError,'SLOT_MISMATCH'):
            guard.StockNativeRoundsMiddleware._check_tool(request)
        request.state=state(receipt('stock_etf_verification_submit',dict(state='PENDING',request_id='pv-'+'1'*40)))
        guard.StockNativeRoundsMiddleware._check_tool(request)
        request.tool_call['args']['request_ids']=['pv-'+'2'*40]
        with self.assertRaisesRegex(RuntimeError,'SLOT_MISMATCH'):
            guard.StockNativeRoundsMiddleware._check_tool(request)

    def test_review_tools_are_scoped_to_maintenance_and_do_not_open_research(self):
        middleware=guard.StockNativeRoundsMiddleware()
        for operation in ('context','submit'):
            request=SimpleNamespace(state=state(),tool_call=dict(name=guard.SERVER+'_stock_etf_verification_'+operation,args={}))
            middleware._check_tool(request)
            request.state={'messages':[dict(role='user',content='执行北京时间 09:35 板块轮动')]}
            with self.assertRaisesRegex(RuntimeError,'TOOL_NOT_ALLOWED'):
                middleware._check_tool(request)
        self.assertEqual(len(guard.TOOLS),3)
        self.assertEqual(len(guard.MAINTENANCE_TOOLS),4)

    def test_completion_requires_original_task_and_saved_followup_terminal(self):
        messages=[receipt('stock_etf_maintenance_status',dict(state='PARTIAL',mode='market',run_id='market-job')),
                  receipt('stock_etf_verification_submit',dict(state='PENDING',request_id='pv-'+'1'*40)),
                  receipt('stock_etf_maintenance_status',dict(state='SUCCEEDED',mode='verify',run_id='verify-job',verification_request_ids=['pv-'+'1'*40]))]
        middleware=guard.StockNativeRoundsMiddleware()
        self.assertIsNone(middleware.after_agent(state(*messages),None))
        with self.assertRaisesRegex(RuntimeError,'MAINTENANCE_NOT_COMMITTED'):
            middleware.after_agent(state(*messages[1:]),None)
        manual={'messages':[dict(role='user',content='执行ETF资料维护 verify'),messages[-1]]}
        self.assertIsNone(middleware.after_agent(manual,None))

    def test_ready_materials_cannot_be_silently_left_before_budget_is_used(self):
        done=receipt('stock_etf_maintenance_status',dict(state='PARTIAL',mode='market',run_id='market-job',
            verification=dict(items=[dict(state='AWAITING_REVIEW',target_id='target-one')])))
        context=receipt('stock_etf_verification_context',dict(state='AWAITING_REVIEW',target_id='target-one'))
        deferred=receipt('stock_etf_verification_submit',dict(state='REVIEW_REQUIRED',target_id='target-one'))
        middleware=guard.StockNativeRoundsMiddleware()
        with self.assertRaisesRegex(RuntimeError,'VERIFICATION_REVIEW_REQUIRED'):
            middleware.after_agent(state(done),None)
        self.assertIsNone(middleware.after_agent(state(done,context,deferred),None))
        pending=receipt('stock_etf_verification_submit',dict(state='PENDING',target_id='target-one',request_id='pv-'+'1'*40))
        with self.assertRaisesRegex(RuntimeError,'VERIFICATION_NOT_CHECKED'):
            middleware.after_agent(state(done,context,pending),None)
        exhausted=state(done,*[dict(role='assistant',content='有界处理进度') for _ in range(23)])
        self.assertIsNone(middleware.after_agent(exhausted,None))
