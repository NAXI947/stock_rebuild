"""Build a credential-free bundle for OCTOP's separate ETF maintenance expert."""
import argparse
import json
from pathlib import Path

if __package__:
    from .configure_octop_native import agent_body, MCP_URL, SERVER
else:
    from configure_octop_native import agent_body, MCP_URL, SERVER

MARKER = 'stock-v4-etf-maintenance-v1'
SLOTS = {'market':'10:00', 'retry':'14:15'}
PROMPT = '''你是ETF资料维护执行专家，仅用stock_etf_maintenance_prepare、stock_etf_maintenance_status、stock_etf_verification_context、stock_etf_verification_submit四个受控工具。
定时指令指定mode：market主维护或retry缺项复核；手动materials仅基于已保存活跃方向/持仓关联采集官方静态材料，手动verify只回核已导入官方材料，两者可在盘外运行、不采行情、不新增定时。materials不传request_ids，可按明确请求传最多8项target_codes，服务端逐只核查真实关联；不传时公平选取待资料目标。低频资料由服务端按类型判断到期，retry只补失败或缺失项。先调用prepare(mode)，verify必须同时提供已保存申请ID的JSON数组字符串request_ids。服务端核验实际时段、交易日、幂等和采集预算。
返回排队或运行中时，使用status(run_id)继续等待，每次工具最长等待50秒，最多24次模型调用。
主维护或materials达到SUCCEEDED或PARTIAL后检查verification队列。在剩余调用预算内，对AWAITING_REVIEW目标逐个调用verification_context(target_id)，按返回review_contract核读官方原文，核验身份、境内被动行业/主题范围、跟踪指数、交易单位、实际涨跌幅和T+1。只引用已存document标签和segment定位的原文片段；材料不支持结论时提交NEEDS_MATERIALS与具体缺口，不猜规则、不假称人工签字。
材料完整时verification_submit(target_id,review JSON字符串)提交APPROVE与逐字段证据；导入PENDING不代表发布。收集返回的request_id（最多8项），将其序列化为JSON数组字符串后调用maintenance_prepare(mode=verify,request_ids=该字符串)，再以status确认真实PUBLISHED队列记录及映射版本。target_codes同样使用JSON数组字符串，例如'["512480.SH"]'；单个合法ID/代码字符串也兼容，不用逗号分隔文本。此后再读取原维护status，显示持续队列的最新状态；重复请求复用原任务，不通过新ID重置预算。
若verify返回VERIFICATION_SCOPE_ALREADY_ASSIGNED，先对existing_run_ids读取status，仅将非空unbound_request_ids另行prepare verify，已绑定申请不重建任务。若原回执WAITING_DOCUMENTS，原失败请求不旁路重试，准确转述next_action所示下一原维护窗口。
只使用已保存回执报告已发布、待材料、待审阅、待回核或需复核的具体ETF、申请ID与原因；剩余调用预算不足时将未处理目标明确保留在队列，下一原维护轮次接续。SUCCEEDED或PARTIAL表示维护任务结果已保存，最终回复真实回执的feedback并附QQ实际投递状态。
SKIPPED直接说明原因。FAILED、CANCELLED、PAUSED或接口错误如实报告状态，勿声称维护完成。
工具返回TOOL_ERROR时，按error和expected_arguments在本会话剩余24次调用预算内修正同一请求参数；不能据此扩大范围、改名单或重置任务预算。以随后真实服务回执判断结果。
禁止调用研判工具、委派代理、额外采集、读写文件或另发消息。维护任务不决定板块方向；仅通过审阅提交工具核验官方产品规则，不修改模拟持仓。
缺失或部分资料由服务端局部标记并保留旧资料；不要求100%资料完整率或覆盖率，不以维护结果阻塞Hermes研判。
不要推算来源日期、构造数据或把模型文字当成落库回执。'''


def bundle(model):
    agent = agent_body(model, PROMPT)
    agent.update(name='ETF资料维护', description=MARKER, max_iters=768,
                 max_input_length=128000, max_tokens=8192)
    jobs=[]
    for mode, slot in SLOTS.items():
        hour, minute = map(int, slot.split(':'))
        jobs.append(dict(name='ETF资料维护 '+slot, trigger=f'cron:{minute} {hour} * * mon-fri',
            prompt=f'执行ETF资料维护 {mode}，北京时间{slot}。按系统流程调用prepare并等待status，以真实终态反馈和QQ投递状态结束。',
            fresh_thread=True, enabled=False, model=None, task_type='agent', mcp_servers=[SERVER]))
    return dict(schema=MARKER, server=SERVER, mcp_url=MCP_URL, agent=agent, jobs=jobs)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-ref', required=True)
    parser.add_argument('--bundle-file', type=Path, required=True)
    args=parser.parse_args()
    args.bundle_file.parent.mkdir(parents=True, exist_ok=True)
    args.bundle_file.write_text(json.dumps(bundle(args.model_ref),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(schema=MARKER, slots=SLOTS, model=args.model_ref),ensure_ascii=False))
