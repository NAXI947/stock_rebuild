"use strict";

const apiRoot = "/api/v2/workspace";
const backtests = "/api/v2/backtests";
const state = {token: "", page: "overview", view: null, draft: null, editor: null, parent: null, mode: "manual", timer: null, compareDraftIds: []};
const panoramaUI = {request:0, token:"", data:null, slot:null};
const sectorEtfUI = {request:0, timer:null, reload:null, kind:"sectors", q:"", sort:"return_desc", page:1};
const lrDefaults = () => ({min_left:70,min_right_gain:3,max_ma20_distance:0.10});
const lrUI = {request:0,timer:null,family:"RIGHT",trade_date:"",q:"",page:1,structure_mode:"ALL",parameters:lrDefaults()};
const hotUI = {request:0,timer:null,days:"5",limit:"10",q:"",selected:"",selectedDay:"",metric:"strength_raw",data:null,reload:null};
const actions = {BUY:"买入",ADD:"加仓",REDUCE:"减仓",SELL:"卖出",EXIT:"清仓"};
const sizes = {AMOUNT:"金额",SHARES:"份额",CASH_RATIO:"现金比例",POSITION_RATIO:"持仓比例",ALL:"全部"};
const modes = {BUY:["AMOUNT","SHARES","CASH_RATIO"],ADD:["AMOUNT","SHARES","CASH_RATIO"],
  REDUCE:["POSITION_RATIO","SHARES"],SELL:["ALL","SHARES"]};
const statuses = {PARTIAL:"局部证据",READY:"就绪",UNAVAILABLE:"数据待补",UNKNOWN:"待核实",WAIT_BUY:"待买入",
  OPEN:"持仓中",WAIT_EXIT:"待清仓",CLOSED:"已结束",DRAFT:"草稿",QUEUED:"排队",RUNNING:"计算中",
  CANCELLED:"已取消",SUCCEEDED:"已完成",FAILED:"失败",WAIT_DATA:"待补数据",FILLED:"已成交",PENDING:"待执行",
  SUPERSEDED:"已替代",CONFLICT:"有冲突",INCOMPLETE:"未完成",COMPLETE:"计算完成",WAIT_T1:"等待 T+1",
  WAIT_PRICE:"待价格",WAIT_CALENDAR:"待交易日历",INSUFFICIENT_CASH:"现金不足",WAITING_REPORT:"待报告",
  RULE_REVIEW_REQUIRED:"产品规则待复核，暂停新入场",SETTLEMENT_RULE_CONFLICT:"结算规则冲突待核验",
  ENTRY_ADMISSION_UNAVAILABLE:"准入资料读取失败，暂停新买",ENTRY_REFERENCE_UNPUBLISHED:"正式准入资料未发布",
  PRODUCT_RULE_UNVERIFIED:"交易规则待核验",TRADE_UNIT_CHANGED:"交易单位与冻结规则冲突"};
const regimes = {BULL_OFFENSE:["强势进攻","bull"],RANGE_ROTATION:["震荡轮动","range"],BEAR_DEFENSE:["弱势防御","bear"]};
const exitReasons = {STOP_LOSS_HIT:"成本止损",BREAKEVEN_PROTECT:"余仓保本",TRAILING_TAKE_PROFIT:"移动止盈",
  TIME_EXIT_STAGNANT:"滞涨到期",TIME_EXIT_MAX_DAYS:"持有到期",TARGET_PROFIT_REDUCE:"目标止盈减半",AI_RETREAT:"明确退潮",
  AI_WEAK_CROSS_DAY_2:"跨日第2次转弱",BEAR_EARLY_WEAK_EXIT:"弱势档早衰",AI_FIRST_WEAK_REDUCE:"首次转弱减半"};
const exitFlags = {ATR_UNAVAILABLE:"ATR20 缺失，各线取下限",REGIME_FALLBACK_RANGE:"大盘数据不足，按震荡档"};
const roundStates = {RUNNING:"研判中",PUBLISHED:"报告已保存",FAILED:"轮次失败",MISSING:"未发布"};
const deliveryStates = {SENT:"已接收",NOT_QUEUED:"无投递记录",PENDING:"待投递",SENDING:"投递处理中",FAILED:"投递失败"};
const qqFormatStates = {
  PENDING:"等待 OCTOP 排版", FORMATTING:"OCTOP 排版中", RETRYING:"等待重试排版",
  FAILED:"排版失败", READY:"OCTOP Markdown 已排版", HISTORICAL:"历史文本", UNAVAILABLE:"暂无归档正文",
};
const deliveryErrors = {
  QQFormatUnavailable:"OCTOP 排版暂时不可用", QQFormatRejected:"OCTOP 未能使用简报模板排版",
  DeliveryUncertain:"QQ 接收结果未知", DeliveryPermanent:"QQ 未接受此次推送", TimeoutError:"投递超时或暂时不可用",
  DELIVERY_LEASE_EXPIRED:"处理超时",
};
const labels = {up:"上涨",down:"下跌",flat:"持平",unknown:"未知"};
const $ = id => document.getElementById(id);
const display = x => x ?? "—";
const statusName = s => statuses[s] || display(s);
const time = x => x ? String(x).replace("T"," ").replace("+08:00","") : "—";
const pct = (x, scale = 1) => x === null || x === undefined || !Number.isFinite(Number(x)) ? "—" : (Number(x)*scale).toFixed(2)+"%";

function elt(tag,text,cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function button(label, click, cls) {
  const node = elt("button",label,cls);
  node.type = "button";
  node.addEventListener("click", () => action(click));
  return node;
}
function card(title) {
  const node = elt("section",undefined,"card");
  node.append(elt("h2",title));
  return node;
}
function empty(text) { return elt("p",text,"empty"); }
function tag(text) { return elt("span",text,"tag"); }
function title(name, context, control) {
  const row = elt("div",undefined,"head"), copy = elt("div");
  copy.append(elt("h1",name),elt("p",context));
  row.append(copy);
  if (control) row.append(control);
  return row;
}
function metrics(items) {
  const grid = elt("div",undefined,"grid");
  for (const [label,value] of items) {
    const block = elt("section",undefined,"card");
    block.append(elt("span",label,"muted"),elt("strong",display(value),"metric"));
    grid.append(block);
  }
  return grid;
}
function table(columns, rows, missing) {
  if (!rows.length) return empty(missing);
  const scroll = elt("div",undefined,"table-scroll"), t = elt("table"), head = t.createTHead().insertRow();
  for (const c of columns) { const th = elt("th",c); th.scope="col"; head.append(th); }
  const body = t.createTBody();
  for (const line of rows) {
    const row = body.insertRow();
    for (const cell of line) {
      const td = row.insertCell();
      if (cell instanceof Node) td.append(cell); else td.textContent = display(cell);
    }
  }
  scroll.append(t);
  return scroll;
}
function field(label,node) {
  const wrap=elt("label",label);
  wrap.append(node);
  return wrap;
}
function input(type,name,required=false) {
  const node=elt("input");
  node.type=type; node.name=name; node.required=required;
  return node;
}
function select(name, options) {
  const node=elt("select"); node.name=name;
  for (const [value,label] of options) {
    const option=elt("option",label); option.value=value; node.append(option);
  }
  return node;
}
function newRequestId() {
  // getRandomValues is also available when the workspace is served over HTTP.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2,"0")).join("");
  return [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join("-");
}
function chinaTimestamp(value) {
  return value+(value.length===16?":00":"")+"+08:00";
}
function chinaInput() {
  return new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai",dateStyle:"short",timeStyle:"short"})
    .format(new Date()).replace(" ","T");
}
function note(text="", bad=false) {
  const box=$("message"); box.textContent=text; box.hidden=!text; box.classList.toggle("error",bad);
}
async function action(fn) {
  try { await fn(); } catch (error) { note(error.message || "请求失败",true); }
}
async function api(path, method="GET", body) {
  const headers = {Authorization:"Bearer "+state.token};
  if (body !== undefined) headers["Content-Type"]="application/json";
  const response=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body),cache:"no-store"});
  const data=await response.json();
  if (!response.ok) {
    const errors={AUTH_REQUIRED:"访问令牌无效",QUEUE_FULL:"回测队列已满",FILL_REQUEST_CONFLICT:"此请求编号对应另一笔成交",
      INVALID_REQUEST:"请核对字段、时间与数值格式",WORKSPACE_UNAVAILABLE:"调测数据库暂不可用",
      NO_LOCAL_HISTORY:"所选区间暂无已保存的可用报价",HISTORY_RANGE_TOO_LARGE:"请缩短查询区间",
      TEMP_STORAGE_LIMIT:"临时空间已达上限，请稍后下载"};
    throw new Error(errors[data.error] || data.error || "服务请求失败");
  }
  return data;
}
function stopPoll() {
  clearTimeout(state.timer);
  state.timer=null;
}
function logout() {
  stopMarketStream();
  stopPoll();
  Object.assign(panoramaUI,{request:panoramaUI.request+1,token:"",data:null,slot:null});
  clearTimeout(sectorEtfUI.timer);
  Object.assign(sectorEtfUI,{request:sectorEtfUI.request+1,timer:null,reload:null,kind:"sectors",q:"",sort:"return_desc",page:1});
  clearTimeout(lrUI.timer);clearTimeout(hotUI.timer);
  Object.assign(lrUI,{request:lrUI.request+1,timer:null,family:"RIGHT",trade_date:"",q:"",page:1,structure_mode:"ALL",parameters:lrDefaults()});
  Object.assign(hotUI,{request:hotUI.request+1,timer:null,days:"5",limit:"10",q:"",selected:"",selectedDay:"",metric:"strength_raw",data:null,reload:null});
  Object.assign(state,{token:"",page:"overview",view:null,draft:null,editor:null,parent:null,mode:"manual",compareDraftIds:[]});
  $("workspace").hidden=true; $("screen").replaceChildren(); $("login").hidden=false; $("logout").hidden=true;
  $("login-form").reset(); $("freshness").textContent=""; $("login-status").textContent="";
}
async function refresh() {
  const token = state.token, view = await api(apiRoot+"/overview");
  if (!token || token !== state.token) return false;
  state.view = view;
  $("environment").textContent=view.environment==="test"?"冻结调测":view.environment==="development"?"开发调测":"当前环境";
  $("freshness").textContent=view.market?.observed_at ? "行情观测 "+time(view.market.observed_at) : "行情待更新";
  return true;
}
async function navigate(page) {
  if (!state.token) return;
  stopPoll(); note();
  clearTimeout(sectorEtfUI.timer);sectorEtfUI.request++;sectorEtfUI.reload=null;
  clearTimeout(lrUI.timer);clearTimeout(hotUI.timer);lrUI.request++;hotUI.request++;hotUI.reload=null;
  document.querySelector(".lr-dialog[open]")?.close();
  state.page=page;
  for (const node of document.querySelectorAll("[data-page]")) {
    if (node.dataset.page===page) node.setAttribute("aria-current","page"); else node.removeAttribute("aria-current");
  }
  if (page==="trades") await renderTrades();
  else if (page==="backtest") await renderBacktests();
  else if (page==="status") await renderRuntimeStatus();
  else if (page==="data-updates" || page==="collections") {
    const token=state.token;
    await (page==="data-updates"?window.renderDataUpdates:window.renderDataUpdateJobs)({screen:$("screen"),api,apiRoot,newRequestId,navigate,isCurrent:()=>state.token===token&&state.page===page});
  }
  else if (page==="sector-etf") await renderSectorEtf();
  else if (page==="left-right") await renderLeftRight();
  else if (page==="hot-sectors") await renderHotSectors();
  else if (await refresh() && state.page===page) page==="overview"?renderOverview():await renderMarket();
}
function renderOverview() {
  const view=state.view, market=view.market, screen=$("screen");
  const header=title("轮动与机会","查看五轮研判、板块状态与已有模拟持仓",button("刷新",async()=>{if(await refresh())renderOverview();}));
  const dayReports=view.reports.length ? view.reports[0].at.slice(0,10) : null;
  const strip=metrics([
    ["最新报告日",dayReports],["活动方向",Object.keys(view.rotation).length],
    ["自动持仓与待执行",view.evaluation.open_or_pending],["ETF 报价",market?.quality.etf_quote_count],
  ]);
  const panes=elt("div",undefined,"split");
  const rounds=card("五轮报告"), selected=card("报告详情");
  selected.append(empty("选择一份已落库的报告"));
  let detailRequest = 0;
  async function openReport(id) {
    const request = ++detailRequest;
    const detail = await api(apiRoot+"/report?id="+encodeURIComponent(id));
    if (request === detailRequest && selected.isConnected) reportDetail(selected,detail,()=>openReport(id));
  }
  for (const slot of view.slots) {
    const report=view.reports.find(r=>r.slot===slot && r.at.slice(0,10)===dayReports);
    const row=elt("div",undefined,"slot");
    row.append(elt("strong",slot,"numeric"));
    if (report) {
      row.append(elt("p",report.summary || statusName(report.status)),button("查看报告",()=>openReport(report.id)));
    } else row.append(elt("span","待报告","muted"));
    rounds.append(row);
  }
  panes.append(rounds,selected);
  const chain=card("活动轮动方向");
  chain.append(table(["板块标识","状态","报告"],Object.entries(view.rotation).map(([id,row])=>[id,tag(row.label||statusName(row.phase)),row.report_id]),"当前暂无已确认的活动方向"));
  const timeline=card("1.5 日轮动轨迹");
  timeline.append(table(["交易日","轮次","状态","报告"],view.rotation_window.map(r=>[
    r.trade_date,r.slot,roundStates[r.status] || statusName(r.status),r.report_id
  ]),"暂无已保存的轮动轨迹"));
  const holding=card("自动持仓与待执行");
  holding.append(table(["ETF","关联板块","状态","执行进度"],view.cycles
    .filter(c=>["WAIT_BUY","OPEN","WAIT_EXIT"].includes(c.status))
    .map(c=>[c.etf,c.primary_sector,statusName(c.status),statusName(c.pending_reason)]),"暂无活动周期"));
  screen.replaceChildren(header,strip,panes,chain,timeline,holding,archiveSearch("reports",openReport));
}
function qqDeliveryLabel(brief) {
  if (brief.format_state==="FAILED") return "尚未发送";
  if (["PENDING","FORMATTING","RETRYING"].includes(brief.format_state)) return "等待排版后投递";
  return deliveryStates[brief.state] || statusName(brief.state);
}
function qqBrief(brief, refreshReport) {
  const section = elt("section");
  section.append(elt("h3","QQ 简报"));
  if (!brief || brief.state === "NOT_QUEUED") {
    section.append(empty("该报告暂无 QQ 投递记录"));
    return section;
  }
  section.append(elt("p",[
    qqFormatStates[brief.format_state],
    qqDeliveryLabel(brief),
    brief.delivered_at ? "接收于 "+time(brief.delivered_at) : null,
  ].filter(Boolean).join(" · "),"muted"));
  if (brief.template) {
    const [, source, revision] = brief.template.split(":");
    const template = source==="workspace" ? "my-stock 工作区模板" : "部署模板";
    section.append(elt("p",template+" · "+revision.slice(0,12),"muted"));
  }
  if (brief.template_fallback) section.append(elt("p","my-stock 工作区模板校验未通过，已用部署模板排版","muted"));
  if (brief.error) section.append(elt("p",deliveryErrors[brief.error] || brief.error,"muted"));
  if (brief.text) section.append(elt("pre",brief.text,"details"));
  if (["PENDING","SENDING"].includes(brief.state)) section.append(button("刷新简报状态",refreshReport));
  return section;
}
function checkStatus(value) {
  return ({PASS:"通过",FAIL:"未通过",UNKNOWN:"待核验",COMPLETE:"已核验",PARTIAL:"部分未核验",FAILED:"明确超限",
    NOT_APPLICABLE:"不适用",OBSERVE_ONLY:"仅观察",pass:"通过",fail:"未通过",unknown:"待核验",
    not_applicable:"不适用",observe_only:"仅观察",VERIFIED:"已核验",READY:"就绪",PENDING:"待资料",BLOCKED:"执行受限"})[value] || "待核验";
}
function recommendationStatus(value, directionFirst=false) {
  if (directionFirst) return ({CONFIRMED:"AI已选·执行就绪",PENDING:"AI已选·执行资料待补",OBSERVE:"关联观察",REJECTED:"执行受限"})[value] || "待核验";
  return ({CONFIRMED:"动向已确认",STATIC_ONLY:"仅静态匹配",PENDING:"待核验",REJECTED:"静态未通过"})[value] || "待核验";
}
function etfCoverageRows(statistics) {
  const {sectors,pairs,indexes,products,orders}=statistics;
  const tiers=pairs.selectable_by_tier;
  return [
    ["可选板块",sectors.with_selectable+"/"+sectors.total],
    ["可选配对",pairs.selectable+"/"+pairs.total],
    ["有权重指数",indexes.weights_available+"/"+indexes.total],
    ["可选 ETF",products.selectable+"/"+products.total],
    ["本轮节点",orders.total],["主干配对",tiers.core],["低暴露配对",tiers.below_core],["未知暴露配对",tiers.unknown],
  ];
}
function reportDetail(panel,row,refreshReport) {
  const data=JSON.parse(row.raw);
  const recovery=row.input_meta?.manual_recovery;
  panel.replaceChildren(elt("h2",row.round_slot+(recovery ? "轮·手动恢复" : " 轮报告")),tag(statusName(row.status)),elt("p",data.summary || "", "details"));
  const reportClock=recovery ? "事实截止 "+time(recovery.source_cutoff)+" · 恢复发布 "+time(recovery.executed_at)
    : "发布 "+time(row.at)+" · 数据 "+time(row.data_as_of);
  panel.append(elt("p",reportClock,"muted"),qqBrief(row.qq_brief,refreshReport));
  const scope = row.input_meta?.market_scope;
  if (scope) {
    const counts=scope.board_counts;
    panel.append(metrics([["板块",scope.board_count],["上涨",counts.up],["下跌",counts.down],
      ["ETF 报价",scope.etf_count],["已映射板块",scope.mapped_count],["策略研判板块",scope.analyzed_count]]));
  }
  const etfStatistics=row.input_meta?.representation?.statistics;
  if (etfStatistics) panel.append(metrics(etfCoverageRows(etfStatistics)));
  const names = Object.fromEntries((data.sectors || []).map(s=>["kaipanla:"+s.kaipanla_plate_id,s.sector_name]));
  const withoutCarrier=Object.entries(row.input_meta?.representation?.pairs || {})
    .filter(([,group])=>!Object.keys(group || {}).length).map(([sid])=>names[sid] || sid);
  if (withoutCarrier.length) panel.append(elt("p","当前资料范围未找到："+withoutCarrier.join("、"),"muted"));
  const phaseLabels = {NEW:"新增",MAINTAIN:"维持",STRENGTHEN:"加强",WEAKEN:"减弱",RETREAT:"退潮",UNKNOWN:"待核实"};
  panel.append(table(["板块","轮动判断","证据"],(data.sectors||[]).map(s=>{
    const decision=row.sectors?.["kaipanla:"+s.kaipanla_plate_id];
    return [s.sector_name,decision?.label || phaseLabels[decision?.phase] || statusName(s.observation_state),(s.reasons||[]).join(" · ")];
  }),"本轮没有板块判断"));
  const hybrid=row.input_meta?.etf_recommendations;
  const directionFirst=["etf-direction-first-v2","etf-direction-first-v3"].includes(hybrid?.method);
  const explain=(hybrid || row.input_meta?.penetration)?.sectors || {};
  const missingWeights=Math.max(0,...Object.values(explain).map(s=>s.coverage_gaps?.weights?.products || 0));
  if (missingWeights) panel.append(elt("p","资料待补：目录"+missingWeights+"只ETF缺可用成分权重，穿透待核验。","muted"));
  const rejectionLabels = {
    SECTOR_MEMBERS_UNAVAILABLE:"缺板块成分", WEIGHTS_UNVERIFIED:"权重待核验", PRODUCT_RULE_UNVERIFIED:"交易规则待核验",
    MAINBOARD_LOW_OR_UNKNOWN:"主板权重不足或待核验", EXPOSURE_LOW:"板块暴露不足", CORE_EXPOSURE_LOW:"核心股暴露不足",
    LIQUIDITY_BELOW_MINIMUM:"流动性不足", AUM_BELOW_MINIMUM:"规模不足", TRADING_SUSPENDED:"停牌",
    TRADE_UNIT_UNVERIFIED:"交易单位待核验", PRODUCT_IDENTITY_INVALID:"产品身份待核验", PRODUCT_IDENTITY_UNVERIFIED:"产品身份待核验",
    PRODUCT_OUTSIDE_SCOPE:"产品交易范围不符", PRODUCT_NOT_LISTED:"产品非上市交易状态", TRADING_UNAVAILABLE:"当前暂停交易",
    USABLE_PRICE_UNAVAILABLE:"有效报价待补", MARKET_DATE_MISMATCH:"行情日期待更新", EOD_OBSERVATION_ONLY:"收盘快照仅供观察",
    RULE_UNVERIFIED:"交易规则待核验", TRADE_UNIT_CHANGED:"交易单位与冻结规则冲突",
    RULE_REVIEW_REQUIRED:"产品规则待复核，暂停新入场", SETTLEMENT_RULE_CONFLICT:"结算规则冲突待核验",
    ENTRY_ADMISSION_UNAVAILABLE:"准入资料读取失败，暂停新买", ENTRY_REFERENCE_UNPUBLISHED:"正式准入资料未发布",
    NO_OVERLAP_CONFIRMED:"正式板块无正权重交集", WEAK_OVERLAP_REJECTED:"弱关联观察", BROAD_SECTOR_UNATTRIBUTED:"大板块待产业归属",
    SINGLE_STOCK_CROSSOVER_REJECTED:"单股跨界观察", WEAK_OVERLAP_UPPER_BOUND_REJECTED:"暴露上界低于底线",
    REPRESENTATIVENESS_UNKNOWN:"代表性待核验", ASSOCIATION_UNCONFIRMED:"关联待核验",
    EMPTY_AFTER_WEAK_PAIR_STRIP:"弱关联剔除后本轮未选",
  };
  const rows=Object.entries(explain).flatMap(([sector,s])=>{
    const name=names[sector] || sector;
    if (s.reason && !s.products?.length) return [hybrid
      ? [name,null,null,null,null,null,null,rejectionLabels[s.reason] || s.reason]
      : [name,null,null,null,null,null,rejectionLabels[s.reason] || s.reason]];
    return (s.products||[]).map(p=>{
      const rejected=(p.rejection_reasons || []).map(reason=>rejectionLabels[reason] || reason);
      const selected=(row.candidates?.[p.etf] || []).some(candidate=>candidate.sector_id===sector);
      const state=rejected.length ? rejected.join(" · ") : selected ? "本轮候选"
        : s.representatives?.includes(p.etf) ? "穿透观察" : "已通过筛选";
      if (hybrid) {
        const pair=p.representativeness;
        const exposure=pair ? pair.known_exposure : s.scope_mode==="event_members" ? p.metrics?.E : p.metrics?.E_sector;
        const representative=s.representatives?.includes(p.etf);
        const placement=representative ? " · 代表 ETF" : s.static_representatives?.includes(p.etf)
          ? " · 静态代表" : p.recommendation_status==="CONFIRMED" ? " · 备选" : "";
        const scope=pair
          ? " · "+({COMPLETE:"完整",KNOWN_LOWER_BOUND:"已知下界",UNAVAILABLE:"待数据"}[pair.exposure_basis]||"待数据")
            +" · 匹配 "+(pair.matched_count??"待查")+"，正权重 "+(pair.positive_weight_matched_count??"待查")
            +" · 上界 "+(pair.upper_bound_status==="PROVEN" ? pct(pair.max_possible_exposure,100) : "待证实")
          : p.exposure_basis==="KNOWN_LOWER_BOUND"
          ? " · 已知下界（成员 "+p.coverage.known_members+"/"+(p.coverage.reported_members??"未知")+"，已知权重 "+pct(p.coverage.known_weight,100)+"）" : "";
        const limits=directionFirst && p.ai_selected && ["PENDING","REJECTED"].includes(p.recommendation_status)
          ? (p.execution_reasons||[]).map(reason=>rejectionLabels[reason] || reason).join(" · ") : "";
        return [name,p.etf,pct(exposure,100)+scope,pct(p.metrics?.Q,100),
          checkStatus(directionFirst ? p.association_status : p.static_status),checkStatus(p.momentum_status),
          checkStatus(directionFirst ? p.execution_eligibility : p.execution_status),
          (rejectionLabels[pair?.representativeness_status]||recommendationStatus(p.recommendation_status,directionFirst))
            +placement+(limits ? " · "+limits : "")];
      }
      return [name,p.etf,pct(p.metrics?.E_sector,100),pct(p.metrics?.Q,100),p.iopv?.iopv,time(p.iopv?.observed_at),state];
    });
  });
  panel.append(table(hybrid ? ["板块","ETF","板块暴露","报价覆盖",directionFirst ? "关联证据" : "静态匹配",directionFirst ? "动向参考" : "动向确认","交易条件","推荐评价"]
    : ["板块","ETF","权重暴露","报价覆盖","IOPV","IOPV 观测时间","筛选状态"],rows,"暂无 ETF 穿透结果"));
  const support=row.input_meta?.representation?.current_support || [];
  if (support.length) panel.append(table(["ETF","来源板块","有效加强分支","入选分支","并集暴露","方向冲突"],
    support.map(group=>[group.etf,group.source_sector_ids.map(sid=>names[sid]||sid).join("、"),
      group.up_branch_count,group.selected_up_branch_count,pct(group.union_exposure,100),
      Object.keys(group.direction_conflicts).join("、") || "—"]),"暂无同源支持"));
  if (hybrid) {
    panel.append(elt("p",directionFirst ? "AI依据板块方向和关联证据选择ETF。策略指标用于参考，交易条件决定是否生成模拟入场候选。" : "推荐评价依据当轮已保存的证据；模拟节点单独按信号周期记录。","muted"));
    const labels={product:"产品规则",weights:"指数权重",mainboard:"主板占比",exposure:"目标暴露",
      liquidity:"历史流动性",aum:"基金规模",core:"核心股穿透",heavyweight:"重仓共振",resonance:"重仓共振",etf_return:"ETF 涨幅",vwap:"均价承接",ma5:"五日均线",
      follow:"相对跟随",follow_ratio:"相对跟随",volume_ratio:"分时量比",vr:"分时量比",premium:"IOPV 溢价",
      spread:"盘口价差",divergence:"拟合偏离",quote:"当轮报价",direction:"板块方向",trade_unit:"交易单位",entry_quote:"入场报价",execution_registry:"冻结执行规则"};
    for (const [sid,sector] of Object.entries(explain)) {
      for (const product of sector.products || []) {
        const detail=elt("details"), summary=elt("summary",(names[sid]||sid)+" · "+product.etf+" · 逐项核验");
        detail.append(summary,table(["检查项","用途","结果","观测值"],Object.entries(product.checks||{}).map(([key,c])=>[
          labels[key] || key,c.effect==="REFERENCE" ? "研判参考" : c.required===false ? "补充资料" : "必要核验",checkStatus(c.result),c.value===null || c.value===undefined ? "待数据" :
            typeof c.value==="object" ? JSON.stringify(c.value) : String(c.value)
        ]),"暂无逐项核验"));
        panel.append(detail);
      }
    }
  }
  const breadthRows=Object.entries(data.source_facts || {}).filter(([,f])=>f?.breadth).map(([sid,f])=>{
    const b=f.breadth;
    return [names[sid]||sid,b.member_count,b.observed_count,pct(b.coverage,100),b.up,b.down,
      pct(b.up_ratio,100),pct(b.median_return,100)];
  });
  if (breadthRows.length) panel.append(table(["板块广度","成员数","有效报价","覆盖","上涨","下跌","上涨占比","涨幅中位数"],breadthRows));
  const discovery=row.input_meta?.octop?.discovery;
  if (discovery) panel.append(elt("p","市场初筛 "+discovery.observed_count+" 个板块，本轮研判 "+discovery.analyzed_count+
    " 个；其余 "+discovery.unevaluated_count+" 个未逐项研判。","muted"));
  const leaders=row.input_meta?.octop?.core_leaders || {};
  if (Object.keys(leaders).length) panel.append(table(["板块","领涨／中军证据","识别状态"],Object.entries(leaders).map(([sid,v])=>[
    names[sid]||sid,(v.codes||[]).join("、")||"暂无",v.status==="VERIFIED"?"已核验":"待核验"
  ])));
  const watch = row.input_meta?.stock_watchlist || {};
  const stockEvidence = row.input_meta?.octop?.stock_research || {};
  const research = row.input_meta?.octop?.research_explanations || {};
  const researchRoles = {MAINLINE:"主线",ROTATION:"轮动",WATCH:"观察",EVENT_FIRST:"事件驱动"};
  const counterStatuses = {FOUND:"已发现",NOT_FOUND:"比较后未发现",NOT_CHECKED:"待核实"};
  const researchRows = Object.entries(research).map(([sector, item])=>[
    names[sector] || sector, researchRoles[item.role], item.rationale,
    [counterStatuses[item.counter_evidence_status], ...(item.counter_evidence_refs || [])].filter(Boolean).join(" · "),
    item.invalidation_condition, item.key_unknown,
  ]);
  if (researchRows.length) panel.append(table(["板块","角色","研判依据","反证","失效条件","待核实"],researchRows));
  const stocks = Object.entries(watch).flatMap(([sector,codes])=>codes.map(code=>{
    const evidence = (stockEvidence[sector] || []).find(item=>item.code===code);
    return [names[sector] || sector,code,evidence?.price_cny,pct(evidence?.return_ratio,100),
      time(evidence?.observed_at),evidence?.source];
  }));
  const stockGap = ["NOT_COLLECTED","UNAVAILABLE"].includes(scope?.stock_quotes)
    ? "缺本轮已核验个股报价" : "本轮无已核验个股观察标的";
  panel.append(table(["板块","个股观察","报价（元）","涨跌幅","观测时间","来源"],stocks,stockGap));
}
function panoramaNumber(value) {
  if (value === null || value === undefined || typeof value === "boolean" || String(value).trim() === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function panoramaAmount(value) {
  const n=panoramaNumber(value);
  return n===null || n<0 ? "—" : (n/1e8).toLocaleString("zh-CN",{minimumFractionDigits:2,maximumFractionDigits:2});
}
function panoramaRatio(value, signed=false) {
  const n=panoramaNumber(value);
  return n===null ? "—" : (signed && n>0 ? "+" : "")+(n*100).toFixed(2)+"%";
}
function panoramaTone(value) {
  const n=panoramaNumber(value);
  return n===null || n===0 ? "neutral" : n>0 ? "up" : "down";
}
function panoramaCount(value) {
  const n=panoramaNumber(value);
  return n===null || n<0 ? "—" : n.toLocaleString("zh-CN");
}
function panoramaSvg(name, attributes={}) {
  const node=document.createElementNS("http://www.w3.org/2000/svg",name);
  for (const [key,value] of Object.entries(attributes)) node.setAttribute(key,String(value));
  return node;
}
function panoramaSection(name, subtitle) {
  const section=elt("section",undefined,"card panorama-section"), heading=elt("header",undefined,"panorama-section-heading");
  const copy=elt("div"); copy.append(elt("h2",name));
  if (subtitle) copy.append(elt("p",subtitle,"muted"));
  heading.append(copy); section.append(heading);
  section.setAttribute("aria-label",name);
  return {section,heading};
}
function panoramaSource(data, daily=false) {
  const footer=elt("div",undefined,"panorama-source");
  if (data?.source) footer.append(elt("span",data.source));
  footer.append(elt("span",data?.observed_at ? (daily ? "日线日期 "+String(data.observed_at).slice(0,10) : "数据时间 "+time(data.observed_at)) : "数据时间未提供"));
  if (data?.collected_at) footer.append(elt("span","采集时间 "+time(data.collected_at)));
  return footer;
}
function renderMarketBackground(data) {
  const {section,heading}=panoramaSection("市场背景","上涨与下跌家数");
  const badge=elt("span",undefined,"panorama-badge");
  badge.append(elt("span","涨股占涨跌股"),elt("b",panoramaRatio(data?.up_ratio),"numeric"));
  heading.append(badge);
  const up=panoramaNumber(data?.up_count),down=panoramaNumber(data?.down_count);
  const total=up!==null && down!==null && up>=0 && down>=0 ? up+down : null;
  const body=elt("div",undefined,"breadth-body"),figure=elt("figure",undefined,"breadth-figure");
  const svg=panoramaSvg("svg",{viewBox:"0 0 160 160",role:"img","aria-label":"上涨 "+panoramaCount(up)+" 家，下跌 "+panoramaCount(down)+" 家，平盘单列"});
  svg.append(panoramaSvg("circle",{cx:80,cy:80,r:64,fill:"none","stroke-width":16,class:"breadth-ring-track"}));
  if (total>0) {
    const circumference=2*Math.PI*64, share=up/total;
    svg.append(panoramaSvg("circle",{cx:80,cy:80,r:64,fill:"none","stroke-width":16,class:"breadth-ring-down"}));
    svg.append(panoramaSvg("circle",{cx:80,cy:80,r:64,fill:"none","stroke-width":16,
      class:"breadth-ring-up","stroke-dasharray":`${circumference*share} ${circumference*(1-share)}`,
      transform:"rotate(-90 80 80)"}));
  }
  const count=panoramaSvg("text",{x:80,y:77,"text-anchor":"middle",class:"breadth-ring-value"}); count.textContent=panoramaCount(total);
  const caption=panoramaSvg("text",{x:80,y:100,"text-anchor":"middle",class:"breadth-ring-label"}); caption.textContent="涨跌家数";
  svg.append(count,caption); figure.append(svg,elt("figcaption",total===null?"等待市场采样":"上涨 / 下跌"));
  const legend=elt("div",undefined,"breadth-legend");
  for (const [name,key,tone,subset] of [["上涨","up_count","up",false],["涨停","limit_up_count","up",true],
    ["下跌","down_count","down",false],["跌停","limit_down_count","down",true]]) {
    const row=elt("div",undefined,"breadth-legend-row"+(subset?" breadth-subset":""));
    const label=elt("span",undefined,"breadth-label"),dot=elt("i",undefined,"breadth-dot "+tone);
    dot.setAttribute("aria-hidden","true"); label.append(dot,elt("span",name));
    if(subset)label.append(elt("small","子集"));
    const value=panoramaNumber(data?.[key]);
    row.append(label,elt("b",panoramaCount(value),"numeric "+tone),elt("span",panoramaRatio(total>0 && value!==null?value/total:null),"numeric muted"));
    legend.append(row);
  }
  const context=elt("div",undefined,"breadth-context");
  context.append(elt("span","交易日","muted"),elt("strong",data?.trade_date || "—","numeric"),
    elt("span","平盘","muted"),elt("strong",panoramaCount(data?.flat_count)+" 家","numeric"));
  body.append(figure,legend,context); section.append(body);
  const definition=elt("details",undefined,"panorama-definition");
  definition.append(elt("summary","平盘单列 · 查看占比口径"),elt("p","圆环仅包含上涨和下跌家数，图例以两者之和为分母；涨停、跌停分别属于上涨、下跌子集。"));
  section.append(definition,panoramaSource(data));
  return section;
}
function renderMarketRegime(data) {
  const names=(data?.indexes || []).map(item=>item.name).join(" + ");
  const {section,heading}=panoramaSection("大盘三档",(names || "指数日K线")+" · 近5个交易日收盘判定");
  const days=Array.isArray(data?.days)?data.days:[];
  if (!days.length) {
    section.append(empty("暂无已保存的指数日K线，收盘后采集到数据即显示"),panoramaSource(data,true));
    return section;
  }
  const latest=days[days.length-1],[latestName,latestTone]=regimes[latest.market_regime] || ["数据不足","missing"];
  const badge=elt("span",undefined,"panorama-badge");
  badge.append(elt("span",latest.trade_date+" 收盘"),elt("b",latestName,"regime-text-"+latestTone));heading.append(badge);
  const levels=["BULL_OFFENSE","RANGE_ROTATION","BEAR_DEFENSE"],width=430,step=width/days.length,rowHeight=38;
  const x=index=>step*(index+.5),y=regime=>rowHeight*(levels.indexOf(regime)+.5);
  const summary=days.map(day=>day.trade_date.slice(5)+" "+(regimes[day.market_regime]?.[0] || "数据不足")).join("，");
  const svg=panoramaSvg("svg",{viewBox:"0 0 "+width+" "+rowHeight*3,role:"img","aria-label":"近5个交易日大盘档位："+summary,class:"regime-chart"});
  // Row names are HTML beside the drawing so they keep their size on a narrow screen.
  const labels=elt("div",undefined,"regime-levels");labels.setAttribute("aria-hidden","true");
  for (const [index,level] of levels.entries()) {
    svg.append(panoramaSvg("rect",{x:0,y:rowHeight*index+2,width,height:rowHeight-4,rx:4,class:"regime-band regime-band-"+regimes[level][1]}));
    labels.append(elt("span",regimes[level][0]));
  }
  // Only consecutive judged sessions are joined; a gap stays visible as a gap.
  for (let index=1;index<days.length;index++) {
    const [before,after]=[days[index-1],days[index]];
    if (regimes[before.market_regime] && regimes[after.market_regime])
      svg.append(panoramaSvg("line",{x1:x(index-1),y1:y(before.market_regime),x2:x(index),y2:y(after.market_regime),class:"regime-line"}));
  }
  for (const [index,day] of days.entries()) {
    if (regimes[day.market_regime]) svg.append(panoramaSvg("circle",{cx:x(index),cy:y(day.market_regime),r:8,class:"regime-dot regime-dot-"+regimes[day.market_regime][1]}));
    else svg.append(panoramaSvg("line",{x1:x(index),y1:8,x2:x(index),y2:rowHeight*3-8,class:"regime-gap"}));
  }
  const strip=elt("div",undefined,"regime-days");strip.style.setProperty("--regime-days",String(days.length));
  for (const day of days) {
    const [name,tone]=regimes[day.market_regime] || ["数据不足","missing"],cell=elt("div",undefined,"regime-day");
    cell.append(elt("span",day.trade_date.slice(5),"numeric muted"),elt("strong",name,"regime-text-"+tone),
      elt("small","量能比 "+(day.volume_ratio==null?"—":Number(day.volume_ratio).toFixed(2)),"numeric muted"));
    for (const item of day.indexes || []) {
      const line=elt("small",undefined,"muted regime-index");
      line.append(elt("span",item.name),elt("span",item.above_ma20==null?"待补":(item.above_ma20?"MA20上方":"MA20下方")+(item.ma20_rising?" ↗":" ↘")));
      line.title=item.above_ma20==null?item.name+"：数据待补":item.name+" 收盘 "+item.close+"，MA20 "+item.ma20+"，均线较5个交易日前"+(item.ma20_rising?"上升":"未上升");
      cell.append(line);
    }
    strip.append(cell);
  }
  const chart=elt("div",undefined,"regime-figure");chart.append(labels,svg,strip);
  section.append(chart,elt("p","每个交易日收盘后判定一次，用于下一交易日买入成交的 v2 周期。量能比为近5日均额除以近20日均额；↗ / ↘ 表示 MA20 较5个交易日前上升 / 未上升。数据不足的交易日不判档。","panorama-note"),panoramaSource(data,true));
  return section;
}
function renderMarketTurnover(data) {
  const {section,heading}=panoramaSection("市场成交",(data?.scope || "沪深A股")+" · 近5个交易日");
  if (data?.trade_date) heading.append(elt("span",data.trade_date+" · 北京时间","panorama-badge numeric"));
  section.append(elt("p","上证A股指数与深证A股指数成交额合计；按同一时点比较累计成交额。","panorama-note"));
  const slotTimes=["09:35","10:35","11:35","13:05","14:05","15:00"];
  const slots=Array.isArray(data?.slots)?data.slots:[];
  const current=slotTimes.includes(panoramaUI.slot) ? panoramaUI.slot : slotTimes.includes(data?.default_slot)?data.default_slot:"15:00";
  const selected=slots.find(s=>s.time===current);
  const controls=elt("div",undefined,"toolbar panorama-slots"); controls.setAttribute("role","group"); controls.setAttribute("aria-label","累计成交额采样时段（北京时间）");
  for (const value of slotTimes) {
    const control=button(value,()=>{
      panoramaUI.slot=value;
      const replacement=renderMarketTurnover(data); section.replaceWith(replacement);
      replacement.querySelector('[data-slot="'+value+'"]').focus({preventScroll:true});
    });
    control.dataset.slot=value;control.setAttribute("aria-pressed",String(current===value));controls.append(control);
  }
  section.append(controls);
  const strip=elt("div",undefined,"turnover-metrics");
  const baseline=panoramaNumber(selected?.baseline_count);
  for (const [label,value,detail,tone] of [
    [(data?.trade_date || "交易日待更新")+" · "+current+" 累计（亿元）",panoramaAmount(selected?.current_amount_cny),current==="15:00"?"收盘成交额":current==="11:35"?"上午收盘累计成交额":"时段累计成交额","neutral"],
    ["较上一交易日同一时点",panoramaRatio(selected?.previous_ratio,true),selected?.previous_ratio==null?"等待上一交易日同一时点数据":"上一交易日同一时点比较",panoramaTone(selected?.previous_ratio)],
    ["较前5日同一时点均值",panoramaRatio(selected?.average5_ratio,true),"有效基线 "+panoramaCount(baseline)+" / 5 日",panoramaTone(selected?.average5_ratio)],
  ]) {
    const item=elt("div",undefined,"turnover-metric");
    item.append(elt("span",label,"muted"),elt("strong",value,"numeric "+tone),elt("small",detail,"muted"));strip.append(item);
  }
  section.append(strip);
  const figure=elt("figure",undefined,"turnover-chart"),caption=elt("figcaption");
  caption.append(elt("strong","累计成交额（亿元）"),elt("span",selected?.average5_amount_cny==null?"前5日基线待补齐":"前5日均值 "+panoramaAmount(selected.average5_amount_cny)+" 亿元","muted"));figure.append(caption);
  const points=Array.isArray(selected?.points)?selected.points:[];
  const amounts=points.map(p=>panoramaNumber(p.amount_cny)).filter(n=>n!==null && n>=0);
  const scale=amounts.length?Math.max(...amounts):null;
  if (!points.length) figure.append(empty("暂无该时段的交易日快照"));
  for (const [index,point] of points.entries()) {
    const row=elt("div",undefined,"turnover-day"+(point.trade_date===data?.trade_date?" turnover-day-current":""));
    const date=elt("span",point.trade_date?.slice(5) || "—","numeric turnover-date");date.title=point.trade_date || "";
    const amount=panoramaNumber(point.amount_cny),valid=amount!==null && amount>=0;
    const chart=panoramaSvg("svg",{viewBox:"0 0 100 12",preserveAspectRatio:"none","aria-hidden":"true",class:"turnover-bar-chart"});
    chart.append(panoramaSvg("rect",{x:0,y:1,width:100,height:10,rx:1,class:"turnover-track"}));
    if(valid && scale>0)chart.append(panoramaSvg("rect",{x:0,y:1,width:Math.min(100,amount/scale*100),height:10,rx:1,class:"turnover-bar"}));
    const detail=elt("p",undefined,"turnover-detail");detail.hidden=true;detail.id="turnover-detail-"+current.replace(":","")+"-"+index;
    const sourceClock=point.observed_at ? (current==="15:00"?"日线日期 "+String(point.observed_at).slice(0,10):"数据时间 "+time(point.observed_at)) : "数据时间未提供";
    detail.textContent=point.trade_date+" · "+current+" · "+(valid?String(point.amount_cny)+" 元":"成交额待补")+" · "+sourceClock;
    if(point.collected_at)detail.textContent+=" · 采集时间 "+time(point.collected_at);
    const value=valid?button(panoramaAmount(amount),()=>{detail.hidden=!detail.hidden;value.setAttribute("aria-expanded",String(!detail.hidden));},"turnover-amount numeric"):elt("span","—","turnover-amount numeric muted");
    if (valid) {
      value.setAttribute("aria-label",point.trade_date+"，"+panoramaAmount(amount)+"亿元，查看金额及数据时间");
      value.setAttribute("aria-expanded","false");value.setAttribute("aria-controls",detail.id);
    }
    row.append(date,valid?chart:elt("span","数据待补","turnover-missing"),value,detail);figure.append(row);
  }
  section.append(figure);
  const currentPoint=points.find(p=>p.trade_date===data?.trade_date);
  section.append(panoramaSource({...data,observed_at:currentPoint?.observed_at || null,collected_at:currentPoint?.collected_at || null},current==="15:00"));
  return section;
}
function renderGlobalIndices(data) {
  const {section,heading}=panoramaSection("全球指数","全球市场行情");
  heading.append(elt("span","红涨 · 绿跌 · 灰平/缺失","panorama-note"));
  const groups=elt("div",undefined,"global-sections");
  const items=Array.isArray(data?.items)?data.items:[];
  for (const [group,label] of [["全球股指","全球大盘指数"],["股指期货","股指期货"],["全球商品","全球商品"],["汇率","汇率与美元指数"]]) {
    const part=elt("section",undefined,"global-section"),rows=items.filter(item=>item.group===group);
    const groupHeading=elt("div",undefined,"global-heading");groupHeading.append(elt("h3",label),elt("span",rows.length+" 项","muted"));part.append(groupHeading);
    const grid=elt("div",undefined,"global-grid"),detail=elt("div",undefined,"global-detail");detail.hidden=true;
    let selected=null;
    for (const item of rows) {
      const tone=panoramaTone(item.return_ratio),tile=button("",()=>{
        const open=selected!==tile;
        for(const node of grid.children){node.setAttribute("aria-pressed","false");node.setAttribute("aria-expanded","false");}
        selected=open?tile:null;detail.hidden=!open;
        if(open){
          tile.setAttribute("aria-pressed","true");tile.setAttribute("aria-expanded","true");
          detail.replaceChildren(elt("strong",item.name),elt("span",item.code,"numeric muted"),
            elt("p","指数或价格 "+display(item.price)+" · 涨跌幅 "+panoramaRatio(item.return_ratio,true)),
            elt("p",item.observed_at?"数据时间 "+time(item.observed_at):"数据时间未提供","muted"));
        }
      },"global-tile global-"+tone);
      tile.setAttribute("aria-pressed","false");tile.setAttribute("aria-expanded","false");
      tile.setAttribute("aria-label",item.name+"，"+panoramaRatio(item.return_ratio,true)+"，查看详情");
      tile.append(elt("strong",item.name),elt("b",panoramaRatio(item.return_ratio,true),"numeric"),elt("span",item.price==null?"暂无报价":item.price,"numeric"));
      grid.append(tile);
    }
    if(rows.length)part.append(grid,detail);else part.append(elt("p","暂无已保存行情","global-empty"));
    groups.append(part);
  }
  section.append(groups,panoramaSource(data));return section;
}
async function renderMarket() {
  const request=++panoramaUI.request,token=state.token,screen=$("screen");
  const reload=button("读取中…",renderMarket,"primary");reload.disabled=true;
  const head=title("大盘全景","市场广度、成交比较与全球指数",reload),body=elt("div",undefined,"panorama");
  const notice=elt("p","","panorama-notice");notice.hidden=true;notice.setAttribute("role","status");
  const draw=data=>body.replaceChildren(renderMarketBackground(data?.background),renderMarketRegime(data?.regime),renderMarketTurnover(data?.turnover),renderGlobalIndices(data?.global_indices));
  const saved=panoramaUI.token===token?panoramaUI.data:null;
  if(saved)draw(saved);
  else for(const name of ["市场背景","大盘三档","市场成交","全球指数"]){
    const {section}=panoramaSection(name);section.append(elt("div","正在读取已保存行情…","panorama-loading"));body.append(section);
  }
  body.setAttribute("aria-busy","true");screen.replaceChildren(head,notice,body);
  try {
    const data=await api(apiRoot+"/market-panorama");
    if(request!==panoramaUI.request || token!==state.token || state.page!=="market" || !head.isConnected)return;
    panoramaUI.data=data;panoramaUI.token=token;draw(data);
    $("freshness").textContent="数据时间见各区域";
  } catch(error) {
    if(request!==panoramaUI.request || token!==state.token || state.page!=="market" || !head.isConnected)return;
    if(!saved)draw(null);
    notice.hidden=false;notice.textContent="大盘行情读取失败，请重试。"+(saved?" 当前显示上次读取的快照。":"");
  } finally {
    if(head.isConnected){reload.disabled=false;reload.textContent="刷新行情";body.setAttribute("aria-busy","false");}
  }
}
function sectorEtfSummary(market) {
  const group=elt("div",undefined,"sector-etf-summary");
  group.append(metrics([["交易日",market?.trade_date],["板块数",market?.quality?.board_count],
    ["ETF 报价数",market?.quality?.etf_quote_count],["ETF 快照成交额（亿元）",panoramaAmount(market?.etf_amount_cny)]]));
  group.append(elt("p","ETF 成交额按当前快照中的 "+panoramaCount(market?.etf_amount_count)+" 只有效报价汇总。","sector-etf-scope"));
  const panes=elt("div",undefined,"split sector-etf-panels"),breadth=card("板块涨跌分布"),leaders=card("涨幅靠前板块");
  const counts=market?.counts || {},total=Object.values(counts).reduce((sum,value)=>sum+(panoramaNumber(value)||0),0);
  for(const key of ["up","down","flat","unknown"]){
    const value=panoramaNumber(counts[key]),row=elt("div",undefined,"bar-row");
    const bar=elt("progress",undefined,"sector-breadth-"+key);bar.max=total||1;bar.value=value||0;
    bar.setAttribute("aria-label",labels[key]+"板块");
    row.append(elt("span",labels[key]),bar,elt("span",panoramaCount(value),"numeric"));breadth.append(row);
  }
  breadth.append(panoramaSource(market));
  const rows=Array.isArray(market?.leaders)?market.leaders:[];
  leaders.append(table(["板块","涨跌幅"],rows.map(row=>[
    row.name || row.id,elt("span",panoramaRatio(row.return_ratio,true),"numeric "+panoramaTone(row.return_ratio))
  ]),"暂无已保存板块涨跌数据"));
  panes.append(breadth,leaders);group.append(panes);return group;
}
function sectorEtfRows(items,kind) {
  const isEtf=kind==="etfs";
  const columns=isEtf?["ETF / 代码","报价（元）","涨跌幅","成交额（亿元）","成交量（万份）","已存报告关联","来源与时间"]:["板块 / 标识","涨跌幅","板块强度","已存报告关联","来源与时间"];
  const rows=items.map(row=>{
    const identity=elt("div",undefined,"sector-etf-identity"),code=row.code || row.id;
    identity.append(elt("strong",row.name || code || "—"),elt("span",row.name?(code || "—"):"名称待补",row.name?"numeric muted":"muted"));
    const provenance=elt("details",undefined,"sector-etf-provenance");
    provenance.append(elt("summary","查看来源与时间"),elt("p",row.source || "来源未提供"),
      elt("p",row.observed_at?"数据时间 "+time(row.observed_at):"数据时间未提供","numeric"));
    if(row.collected_at)provenance.append(elt("p","采集时间 "+time(row.collected_at),"numeric"));
    const related=isEtf?row.sectors:row.etfs;
    let association=elt("span","暂无已存关联","muted");
    if(Array.isArray(related) && related.length){
      association=elt("details",undefined,"sector-etf-associations");
      association.append(elt("summary",related.length+(isEtf?" 个板块":" 只 ETF")));
      const list=elt("ul");
      for(const entry of related){
        const item=elt("li"),identity=entry.name?[entry.name,entry.code || entry.id].filter(Boolean).join(" · "):(entry.code || entry.id || "—");
        item.append(elt("strong",identity),elt("p","报告时间 "+time(entry.report_at)),elt("p",entry.report_id || "报告标识待补","numeric"));list.append(item);
      }
      association.append(list);
    }
    const values=[identity];
    if(isEtf)values.push(elt("span",display(row.price_cny),"numeric"));
    values.push(elt("span",panoramaRatio(row.return_ratio,true),"numeric "+panoramaTone(row.return_ratio)));
    if(isEtf){
      const volume=panoramaNumber(row.volume_shares);
      values.push(elt("span",panoramaAmount(row.amount_cny),"numeric"),elt("span",volume===null?"—":(volume/1e4).toLocaleString("zh-CN",{maximumFractionDigits:2}),"numeric"));
    }else values.push(elt("span",display(row.strength),"numeric"));
    values.push(association,provenance);
    return values;
  });
  const content=table(columns,rows,"没有匹配的"+(isEtf?"ETF 报价":"板块")+"，可调整名称或代码重新搜索。");
  content.classList.add("sector-etf-results");
  const resultTable=content.querySelector("table");
  if(resultTable){
    resultTable.classList.add("sector-etf-table");
    for(const row of resultTable.tBodies[0].rows)for(const [index,cell] of [...row.cells].entries())cell.dataset.label=columns[index];
  }
  return content;
}
async function renderSectorEtf() {
  const token=state.token,screen=$("screen"),pageRoot=elt("div",undefined,"sector-etf-page");
  const summary=elt("div"),panel=card("板块与 ETF 信息"),results=elt("div"),notice=elt("p","正在读取已保存行情…","sector-etf-notice");
  notice.setAttribute("role","status");
  const search=input("search","q");search.placeholder="搜索名称或代码";search.maxLength=80;search.value=sectorEtfUI.q;
  const ordering=select("sort",[]);
  function populateSort(){
    const options=[["return_desc","涨幅从高到低"],["return_asc","涨幅从低到高"],
      sectorEtfUI.kind==="etfs"?["amount_desc","成交额从高到低"]:["strength_desc","板块强度从高到低"],
      ["name_asc","名称顺序"],["code_asc","代码顺序"]];
    if(!options.some(([value])=>value===sectorEtfUI.sort))sectorEtfUI.sort="return_desc";
    ordering.replaceChildren(...options.map(([value,label])=>{const option=elt("option",label);option.value=value;return option;}));
    ordering.value=sectorEtfUI.sort;
  }
  populateSort();
  const form=elt("form",undefined,"sector-etf-filters"),submit=elt("button","搜索","primary");submit.type="submit";
  form.append(field("名称或代码",search),field("排序",ordering),submit);
  const tabs=elt("div",undefined,"toolbar tabs sector-etf-tabs");tabs.setAttribute("role","group");tabs.setAttribute("aria-label","行情类别");
  const controls=[];
  for(const [kind,label] of [["sectors","板块列表"],["etfs","已采集 ETF 行情"]]){
    const tab=button(label,()=>{
      sectorEtfUI.kind=kind;sectorEtfUI.page=1;
      populateSort();
      for(const item of controls)item.setAttribute("aria-pressed",String(item.dataset.kind===kind));
      return load();
    });
    tab.dataset.kind=kind;tab.setAttribute("aria-pressed",String(sectorEtfUI.kind===kind));controls.push(tab);tabs.append(tab);
  }
  const pagination=elt("div",undefined,"sector-etf-pagination"),pageLabel=elt("span","","muted");
  const previous=button("上一页",()=>{sectorEtfUI.page--;return load();});
  const next=button("下一页",()=>{sectorEtfUI.page++;return load();});
  previous.disabled=true;next.disabled=true;pagination.append(pageLabel,previous,next);
  const reload=button("刷新行情",()=>load());
  const head=title("板块与 ETF","查看板块强弱与 ETF 行情",reload);
  panel.append(tabs,form,elt("p","关联来自已保存报告，展开可查看报告时间。","sector-etf-scope"),notice,results,pagination);pageRoot.append(head,summary,panel);screen.replaceChildren(pageRoot);
  async function load(){
    clearTimeout(sectorEtfUI.timer);sectorEtfUI.timer=null;
    if(state.page!=="sector-etf" || token!==state.token || !pageRoot.isConnected)return;
    const request=++sectorEtfUI.request,kind=sectorEtfUI.kind;
    const params=new URLSearchParams({kind,q:sectorEtfUI.q,sort:sectorEtfUI.sort,page:String(sectorEtfUI.page),page_size:"20"});
    notice.textContent="正在读取已保存行情…";notice.hidden=false;notice.classList.remove("error");results.setAttribute("aria-busy","true");
    previous.disabled=true;next.disabled=true;reload.disabled=true;submit.disabled=true;
    try{
      const data=await api(apiRoot+"/sector-etf-info?"+params);
      if(request!==sectorEtfUI.request || state.page!=="sector-etf" || token!==state.token || !pageRoot.isConnected)return;
      const items=Array.isArray(data.items)?data.items:[];
      summary.replaceChildren(sectorEtfSummary(data.summary));results.replaceChildren(sectorEtfRows(items,kind));
      sectorEtfUI.page=data.page;
      const pages=Math.max(1,data.pages || Math.ceil(data.total/data.page_size));
      pageLabel.textContent="共 "+panoramaCount(data.total)+" 项 · 第 "+data.page+" / "+pages+" 页";
      notice.hidden=true;previous.disabled=data.page<=1;next.disabled=data.page>=pages;
      $("freshness").textContent=data.summary?.observed_at?"行情观测 "+time(data.summary.observed_at):"数据时间见各条目";
    }catch(error){
      if(request!==sectorEtfUI.request || state.page!=="sector-etf" || token!==state.token || !pageRoot.isConnected)return;
      results.replaceChildren();pageLabel.textContent="";
      notice.textContent="行情读取失败，请重新搜索或刷新。";notice.classList.add("error");
    }finally{
      if(request===sectorEtfUI.request && pageRoot.isConnected){reload.disabled=false;submit.disabled=false;results.setAttribute("aria-busy","false");}
    }
  }
  function scheduleSearch(){
    sectorEtfUI.q=search.value.trim();sectorEtfUI.page=1;sectorEtfUI.request++;
    clearTimeout(sectorEtfUI.timer);previous.disabled=true;next.disabled=true;
    sectorEtfUI.timer=setTimeout(()=>action(load),250);
  }
  let composing=false;
  search.addEventListener("compositionstart",()=>{composing=true;clearTimeout(sectorEtfUI.timer);sectorEtfUI.request++;});
  search.addEventListener("compositionend",()=>{composing=false;scheduleSearch();});
  search.addEventListener("input",()=>{if(!composing)scheduleSearch();});
  ordering.addEventListener("change",()=>{sectorEtfUI.sort=ordering.value;sectorEtfUI.page=1;action(load);});
  form.addEventListener("submit",event=>{event.preventDefault();sectorEtfUI.q=search.value.trim();sectorEtfUI.page=1;action(load);});
  sectorEtfUI.reload=()=>composing?undefined:load();
  await load();
}
function researchNumber(value, digits=2) {
  const n=panoramaNumber(value);
  return n===null?"—":n.toLocaleString("zh-CN",{minimumFractionDigits:digits,maximumFractionDigits:digits});
}
function researchStrings(value) {
  if(Array.isArray(value))return value.flatMap(researchStrings);
  if(value && typeof value==="object")return Object.entries(value).flatMap(([key,item])=>researchStrings(item).map(text=>key+"："+text));
  return value===null || value===undefined || value===""?[]:[String(value)];
}
function researchIssues(issues) {
  const lines=researchStrings(issues),box=elt("details",undefined,"research-issues");
  if(lines.length){box.append(elt("summary","数据说明（"+lines.length+"）"));const list=elt("ul");for(const line of lines)list.append(elt("li",line));box.append(list);}
  box.hidden=!lines.length;return box;
}
function researchSearch(search,form,ui,load) {
  let composing=false;
  function schedule(){ui.q=search.value.trim();if("page" in ui)ui.page=1;ui.request++;clearTimeout(ui.timer);ui.timer=setTimeout(()=>action(load),250);}
  search.addEventListener("compositionstart",()=>{composing=true;clearTimeout(ui.timer);ui.request++;});
  search.addEventListener("compositionend",()=>{composing=false;schedule();});
  search.addEventListener("input",()=>{if(!composing)schedule();});
  form.addEventListener("submit",event=>{event.preventDefault();if(composing)return;ui.q=search.value.trim();if("page" in ui)ui.page=1;action(load);});
  return ()=>composing;
}
const lrPartNames={left_wash:"活跃收敛",convergence:"量价收敛",safety:"均线安全",resistance:"上方阻力",ma_state:"均线基底",weekly:"周线状态",relative_strength:"相对板块",trend:"趋势位置",slope:"均线斜率",memory:"活跃记忆",efficiency:"量价效率"};
const lrReasonNames={ADJ_FACTOR_MISSING:"复权因子缺失",INVALID_OR_MISSING_PRICE_FACTOR:"价格或复权因子缺失、无效",COST85_UNAVAILABLE:"筹码成本缺失",SECTOR_RETURN_UNAVAILABLE:"板块五日收益缺失",STOCK_RETURN_UNAVAILABLE:"股票区间收益缺失",WEEKLY_INSUFFICIENT_COMPLETED_WEEKS:"完整周线不足26周",WEEKLY_CLOSE_INVALID:"周线收盘价无效",TURNOVER_UNAVAILABLE:"换手率缺失",MA20_UNAVAILABLE:"MA20资料不足",MA_UNAVAILABLE:"均线资料不足",MA_NON_POSITIVE:"均线价格无效",MA_SLOPE_UNAVAILABLE:"均线斜率资料不足",INSUFFICIENT_HISTORY_FOR_MEMORY:"活跃记忆日线窗口不足",INSUFFICIENT_HISTORY_FOR_EFFICIENCY:"量价效率日线窗口不足",INSUFFICIENT_HISTORY_FOR_A_B:"活跃收敛日线窗口不足",INSUFFICIENT_HISTORY_FOR_A:"量价收敛日线窗口不足",AMPLITUDE_UNKNOWN:"振幅资料缺失",VOLUME_RATIO_PARTIAL:"量比资料不完整",VOLUME_RATIO_FALLBACK_UNAVAILABLE:"成交量窗口不足",BASE_WINDOW_UNKNOWN:"基准窗口资料缺失",A_WINDOW_INPUT_MISSING:"近期量价资料缺失",A_WINDOW_ZERO_BASELINE:"近期量价基准为零",CHAOS_UNAVAILABLE:"量价效率资料缺失",INPUT_MISSING:"必要输入缺失",CONDITION_UNKNOWN:"必要条件待核实",INVALID_BOUNDS:"指标范围无效",NO_ASSESSMENT:"该日暂无评分记录"};
function lrReason(value){return lrReasonNames[value] || (lrPartNames[value]?lrPartNames[value]+"资料待补":String(value));}
function lrStatus(value){return value==="MISSING"?"资料待补":statusName(value);}
function lrMissing(row){
  if(row.missing_reason)return lrReason(row.missing_reason);
  const reasons=[...new Set(researchStrings(row.reasons).map(lrReason))];
  return reasons.length?reasons.join("、"):row.note || "该日必要评分资料待补";
}
function lrHistoryModel(data,cutoff){
  const dayPattern=/^\d{4}-\d{2}-\d{2}$/;
  const items=Array.isArray(data.items)?data.items:[],bars=Array.isArray(data.bars)?data.bars:[];
  const days=[...new Set(Array.isArray(data.trade_dates)?data.trade_dates:[...items,...bars].map(row=>row.trade_date))]
    .filter(day=>typeof day==="string"&&dayPattern.test(day)&&(!cutoff||day<=cutoff)).sort().slice(-30);
  const prices=new Map(bars.map(row=>[row.trade_date,row]));
  const scores=new Map(items.map(row=>[row.trade_date+":"+row.family,row]));
  const values={LEFT:[],RIGHT:[],PRICE:[]};
  days.forEach((day,index)=>{
    for(const family of ["LEFT","RIGHT"]){
      const row=scores.get(day+":"+family),n=panoramaNumber(row?.setup_score);
      values[family].push({day,index,row,value:row?.status==="READY"&&n!==null&&n>=0&&n<=100?n:null,version:row?.version_id || null});
    }
    const row=prices.get(day),n=panoramaNumber(row?.close);
    values.PRICE.push({day,index,row,value:n!==null&&n>0?n:null,version:"price"});
  });
  const segments={};
  for(const key of Object.keys(values)){
    const parts=[];let current=[];
    for(const point of values[key]){
      if(point.value===null){if(current.length)parts.push(current);current=[];continue;}
      if(current.length&&(key!=="PRICE"&&(!point.version||current.at(-1).version!==point.version))){parts.push(current);current=[];}
      current.push(point);
    }
    if(current.length)parts.push(current);segments[key]=parts;
  }
  return {days,values,segments};
}
function lrHistoryChart(data,cutoff){
  const model=lrHistoryModel(data,cutoff),section=elt("section",undefined,"lr-history"),heading=elt("div",undefined,"lr-history-head");
  heading.append(elt("h3","近30个交易日 · 评分与收盘价"));
  const legend=elt("div",undefined,"lr-history-legend"),visible=new Set(["LEFT","RIGHT","PRICE"]),chart=elt("div");
  legend.setAttribute("role","group");legend.setAttribute("aria-label","趋势曲线显示开关");
  for(const [key,label] of [["LEFT","LEFT"],["RIGHT","RIGHT"],["PRICE","收盘价"]]){
    const toggle=button(label,()=>{if(visible.has(key))visible.delete(key);else visible.add(key);toggle.setAttribute("aria-pressed",String(visible.has(key)));draw();},"lr-legend-"+key.toLowerCase());
    toggle.setAttribute("aria-pressed","true");legend.append(toggle);
  }
  heading.append(legend);section.append(heading,elt("p","左轴为结构分，右轴为未复权收盘价（元）。缺数和评分版本变化处断线。","muted lr-history-note"),chart);
  function draw(){
    chart.replaceChildren();
    if(!model.days.length){chart.append(empty("所选日期之前暂无已保存的评分与行情"));return;}
    const scroll=elt("div",undefined,"lr-history-scroll"),content=elt("div",undefined,"lr-history-content");
    scroll.tabIndex=0;scroll.setAttribute("role","region");scroll.setAttribute("aria-label","近30个交易日趋势与明细，可横向滚动");
    const width=Math.max(980,model.days.length*70+240),right=width-80,x=index=>160+(index+0.5)*(width-240)/model.days.length;
    content.style.width=width+"px";
    const svg=panoramaSvg("svg",{viewBox:`0 0 ${width} 214`,width,height:214,role:"img","aria-label":"LEFT、RIGHT 盘后结构分与收盘价趋势"});
    const prices=model.values.PRICE.filter(point=>point.value!==null).map(point=>point.value);
    const low=prices.length?Math.min(...prices):0,high=prices.length?Math.max(...prices):1,padding=Math.max((high-low)*.15,high*.005,.01),priceMin=Math.max(0,low-padding),priceMax=high+padding;
    const y=(value,key)=>18+(key==="PRICE"?(priceMax-value)/(priceMax-priceMin):(100-value)/100)*152;
    for(let i=0;i<5;i++){
      const yy=18+i*38,line=panoramaSvg("line",{x1:58,x2:right,y1:yy,y2:yy,class:"lr-history-grid"}),label=panoramaSvg("text",{x:48,y:yy+4,"text-anchor":"end"});label.textContent=String(100-i*25);svg.append(line,label);
      if(visible.has("PRICE")&&prices.length){const price=panoramaSvg("text",{x:right+10,y:yy+4,class:"lr-price-axis"});price.textContent=researchNumber(priceMax-(priceMax-priceMin)*i/4);svg.append(price);}
    }
    for(const key of visible){
      for(const segment of model.segments[key]){
        if(segment.length>1)svg.append(panoramaSvg("polyline",{points:segment.map(point=>`${x(point.index)},${y(point.value,key)}`).join(" "),class:"lr-history-line lr-history-"+key.toLowerCase()}));
        for(const point of segment){
          const dot=panoramaSvg("circle",{cx:x(point.index),cy:y(point.value,key),r:3.5,class:"lr-history-point lr-history-"+key.toLowerCase()}),tooltip=panoramaSvg("title");
          tooltip.textContent=point.day+" · "+(key==="PRICE"?"收盘价 "+researchNumber(point.value)+" 元 · "+panoramaRatio(point.row?.return_ratio,true):key+" "+researchNumber(point.value,1)+" 分 · "+display(point.version));dot.append(tooltip);svg.append(dot);
        }
      }
    }
    model.days.forEach((day,index)=>{const label=panoramaSvg("text",{x:x(index),y:202,"text-anchor":"middle"});label.textContent=day.slice(5);svg.append(label);});
    if(!visible.size)content.append(empty("选择上方曲线即可显示趋势"));else content.append(svg);
    const rows=[];
    for(const family of ["LEFT","RIGHT"]){
      rows.push([family==="LEFT"?"LEFT 左侧结构":"RIGHT 右侧启动",...model.values[family].map(point=>{const cell=elt("span",researchNumber(point.value,1));cell.title=point.value===null?point.row?lrMissing(point.row):"该日暂无评分记录":display(point.version);return cell;})]);
    }
    rows.push(["收盘价（元）",...model.values.PRICE.map(point=>researchNumber(point.value))]);
    rows.push(["日涨跌幅",...model.values.PRICE.map(point=>elt("span",panoramaRatio(point.row?.return_ratio,true),panoramaTone(point.row?.return_ratio)))]);
    const detail=table(["盘后评分 / 行情",...model.days.map(day=>day.slice(5))],rows,"暂无历史明细");detail.classList.add("lr-history-table");content.append(detail);scroll.append(content);chart.append(scroll);
    if(!Object.values(model.values).some(points=>points.some(point=>point.value!==null)))chart.append(empty("此窗口的评分与行情资料待补"));
  }
  draw();return section;
}
function lrScore(row) {
  const wrap=elt("div",undefined,"lr-score"),score=panoramaNumber(row.setup_score);
  wrap.append(elt("strong",researchNumber(score,1),"numeric"));
  if(score!==null && score>=0 && score<=100){const bar=elt("progress");bar.max=100;bar.value=score;bar.setAttribute("aria-label",row.family+" 结构分 "+score);wrap.append(bar);}
  else {const missing=elt("span",lrStatus(row.status),"muted");missing.title=lrMissing(row);wrap.append(missing);}
  return wrap;
}
function showLrDetail(row,parent) {
  parent.querySelector(".lr-dialog")?.remove();
  const dialog=elt("dialog",undefined,"lr-dialog"),head=elt("header",undefined,"lr-dialog-head"),copy=elt("div");
  const heading=elt("h2",row.name || row.ts_code);heading.id="lr-detail-title";dialog.setAttribute("aria-labelledby",heading.id);
  copy.append(heading,elt("p",display(row.ts_code)+" · "+display(row.family)+" · "+display(row.trade_date),"muted"));
  const close=button("关闭详情",()=>dialog.close());head.append(copy,close);
  const body=elt("div",undefined,"lr-dialog-body");
  body.append(metrics([["结构分",researchNumber(row.setup_score,1)],["批次收盘价（元）",researchNumber(row.close)],["日涨跌幅",panoramaRatio(row.return_ratio,true)],["评分状态",lrStatus(row.status)]]));
  const history=card("近期趋势"),historyNotice=elt("p","正在读取近30个交易日…","muted");historyNotice.setAttribute("role","status");history.append(historyNotice);body.append(history);
  if(row.structure){
    const structure=row.structure,pattern=card((structure.label || "转强形态")+" · "+(structure.stage_label || display(structure.stage)));
    pattern.append(metrics([["LEFT",researchNumber(structure.left_score,1)],["RIGHT",researchNumber(structure.right_score,1)],["RIGHT两日净增",researchNumber(structure.right_gain_2d,1)],["LEFT前5日峰值",researchNumber(structure.left_peak,1)]]));
    const confirmed=elt("ul");for(const reason of researchStrings(structure.reasons))confirmed.append(elt("li",reason));pattern.append(confirmed);
    if(structure.pending_confirmations?.length){const pending=elt("div",undefined,"lr-pending");pending.append(elt("strong","待确认条件"));const list=elt("ul");for(const reason of researchStrings(structure.pending_confirmations))list.append(elt("li",reason));pending.append(list);pattern.append(pending);}
    body.append(pattern);
  }
  const parts=card("结构分构成"),source=row.subscores || {},values=source.parts || {},weights=source.weights || {};
  parts.append(table(["已保存分项","分项分","权重"],Object.entries(values).map(([key,value])=>[lrPartNames[key] || key,researchNumber(value,1),panoramaRatio(weights[key])]),"该批次未保存分项数值"));
  const reason=card("批次说明"),reasons=[...new Set(researchStrings(row.reasons).map(lrReason))];
  if(row.note)reason.append(elt("p",researchStrings(row.note).join("\n"),"details"));
  if(reasons.length){const list=elt("ul");for(const line of reasons)list.append(elt("li",line));reason.append(list);}
  if(!row.note&&!reasons.length)reason.append(empty("该批次未保存文字说明"));
  const evidence=card("指标与来源");
  evidence.append(table(["项目","已保存值"],[
    ["量比",researchNumber(row.volume_ratio)],["换手率",panoramaRatio(row.turnover_ratio)],
    ["评分状态",lrStatus(row.status)],["评分日期",row.trade_date],["策略版本",row.version_id],
    ["日线条数",panoramaCount(row.quality?.daily_count)],["完整周线",panoramaCount(row.quality?.completed_weeks)],
    ["板块五日收益",typeof row.quality?.sector_return_available==="boolean"?(row.quality.sector_return_available?"已有资料":"资料待补"):"—"],
    ["数据时间",time(row.observed_at)],["发布时间",time(row.published_at)],["来源",row.source],["记录编号",row.assessment_id],
  ],"暂无指标"));
  const sectors=card("已存板块关联");
  sectors.append(table(["板块","成员日期","来源 / 批次"],(row.sectors || []).map(item=>[item.name || item.id,item.membership_date,[item.source,item.source_batch].filter(Boolean).join(" · ") || "—"]),"该记录暂无已保存的板块关联"));
  body.append(parts,reason,evidence,sectors);dialog.append(head,body);parent.append(dialog);
  dialog.addEventListener("click",event=>{if(event.target===dialog){const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();}});
  dialog.showModal();close.focus();
  const token=state.token;
  async function loadHistory(){
    history.replaceChildren(elt("h2","近期趋势"),historyNotice);historyNotice.textContent="正在读取近30个交易日…";
    try{
      const data=await api(apiRoot+"/left-right-history?"+new URLSearchParams({ts_code:row.ts_code,trade_date:row.trade_date,days:"30"}));
      if(token!==state.token||!dialog.isConnected||!dialog.open)return;
      history.replaceChildren(lrHistoryChart(data,row.trade_date));
    }catch(error){
      if(token!==state.token||!dialog.isConnected||!dialog.open)return;
      historyNotice.textContent="近期趋势读取失败，请重试。";history.replaceChildren(elt("h2","近期趋势"),historyNotice,button("重试读取趋势",loadHistory));
    }
  }
  loadHistory();return dialog;
}
function lrRows(items,parent,mode="ALL") {
  const structureMode=items.some(row=>row.structure),columns=["排名","股票 / 代码","批次收盘（元）","日涨跌幅",structureMode?"LEFT / RIGHT":"结构分",structureMode?"形态进展":"量比 / 换手率","评分日期 / 版本","详情"];
  const rows=items.map((row,index)=>{
    const identity=elt("div",undefined,"sector-etf-identity");identity.append(elt("strong",row.name || row.ts_code),elt("span",row.ts_code,"muted numeric"));
    const activity=elt("div",undefined,"lr-cell-stack");activity.append(elt("span",researchNumber(row.volume_ratio)+" 倍"),elt("span",panoramaRatio(row.turnover_ratio),"muted"));
    const batch=elt("div",undefined,"lr-cell-stack");batch.append(elt("span",display(row.trade_date),"numeric"),elt("span",display(row.version_id),"muted"));
    const scores=structureMode?elt("div",undefined,"lr-dual-score"):lrScore(row),progress=structureMode?elt("div",undefined,"lr-cell-stack"):activity;
    if(structureMode){const structure=row.structure || {};scores.append(elt("span","L "+researchNumber(structure.left_score,1)+" · R "+researchNumber(structure.right_score,1),"numeric"),elt("small","RIGHT两日净增 "+researchNumber(structure.right_gain_2d,1),"muted"));progress.append(tag(structure.stage_label || (structure.stage==="CONFIRMED"?"条件齐备":"观察中")),elt("span",panoramaCount(structure.confirmed_count)+" / "+panoramaCount(structure.confirmation_total)+" 项确认","muted"));}
    return [elt("span",String((lrUI.page-1)*20+index+1),"numeric muted"),identity,elt("span",researchNumber(row.close),"numeric"),elt("span",panoramaRatio(row.return_ratio,true),"numeric "+panoramaTone(row.return_ratio)),scores,progress,batch,button("详情与趋势",()=>showLrDetail(row,parent))];
  });
  const result=table(columns,rows,mode==="ALL"?"所选日期、家族和搜索条件下暂无已保存评分":"暂无匹配结果");
  const t=result.querySelector("table");if(t){t.classList.add("lr-table","sector-etf-table");for(const row of t.tBodies[0].rows)Array.from(row.cells).forEach((cell,index)=>cell.dataset.label=columns[index]);}
  return result;
}
async function renderLeftRight() {
  const token=state.token,root=elt("div",undefined,"lr-page"),summary=elt("div",undefined,"lr-summary"),panel=card("盘后结构评分榜");
  const results=elt("div"),issues=elt("div"),notice=elt("p","正在读取已发布评分…","sector-etf-notice"),batch=elt("p","","research-batch");notice.setAttribute("role","status");
  const date=select("trade_date",[["","最新已发布日期"]]),search=input("search","q");date.value=lrUI.trade_date;search.value=lrUI.q;search.maxLength=80;search.placeholder="搜索股票名称或代码";
  const mode=select("structure_mode",[["ALL","原始结构分榜"],["STARTUP","整理后刚转强"],["PULLBACK","回调后再转强"]]);mode.value=lrUI.structure_mode;
  const form=elt("form",undefined,"sector-etf-filters lr-filters"),submit=elt("button","搜索","primary");submit.type="submit";form.append(field("股票名称或代码",search),field("评分日期",date),field("筛选方式",mode),submit);
  const parameters=elt("details",undefined,"lr-parameters"),parameterHeading=elt("summary","形态筛选参数"),parameterForm=elt("form",undefined,"lr-parameter-form"),parameterNotice=elt("p","","lr-parameter-note muted");parameterNotice.setAttribute("role","status");
  const parameterInputs={};
  for(const [key,label,min,max,scale] of [["min_left","LEFT前5日峰值优先参考",50,90,1],["min_right_gain","RIGHT近2日最低净增",1,15,1],["max_ma20_distance","距MA20最大偏离（%）",3,20,100]]){
    const node=input("number",key,true);node.min=String(min);node.max=String(max);node.step="1";node.value=String(lrUI.parameters[key]*scale);parameterInputs[key]=node;parameterForm.append(field(label,node));
  }
  const apply=elt("button","应用参数","primary");apply.type="submit";
  const reset=button("恢复默认",()=>{lrUI.parameters=lrDefaults();for(const [key,node] of Object.entries(parameterInputs))node.value=String(lrUI.parameters[key]*(key==="max_ma20_distance"?100:1));lrUI.page=1;return load();});
  const parameterActions=elt("div",undefined,"toolbar");parameterActions.append(apply,reset);parameterForm.append(parameterActions);
  parameters.append(parameterHeading,parameterForm,parameterNotice,elt("p","LEFT峰值用于优先参考；RIGHT需近2日净增达标且最新一天回升。形态结果按RIGHT结构分降序排列。","lr-parameter-note muted"));
  parameterForm.addEventListener("submit",event=>{event.preventDefault();if(!parameterForm.reportValidity())return;lrUI.parameters={min_left:Number(parameterInputs.min_left.value),min_right_gain:Number(parameterInputs.min_right_gain.value),max_ma20_distance:Number(parameterInputs.max_ma20_distance.value)/100};lrUI.page=1;action(load);});
  for(const node of Object.values(parameterInputs))node.addEventListener("input",()=>{parameterNotice.textContent="参数尚未应用，榜单仍使用已应用条件。";});
  const tabs=elt("div",undefined,"toolbar tabs");tabs.setAttribute("role","group");tabs.setAttribute("aria-label","评分家族");
  for(const [family,label] of [["LEFT","LEFT 左侧结构"],["RIGHT","RIGHT 右侧启动"]]){
    const tab=button(label,()=>{lrUI.family=family;lrUI.structure_mode="ALL";mode.value="ALL";lrUI.page=1;return load();});
    tab.dataset.family=family;tab.setAttribute("aria-pressed",String(lrUI.family===family&&lrUI.structure_mode==="ALL"));tabs.append(tab);
  }
  const pager=elt("div",undefined,"sector-etf-pagination lr-pagination"),pageLabel=elt("span","","muted"),previous=button("上一页",()=>{lrUI.page=Math.max(1,lrUI.page-1);return load();}),next=button("下一页",()=>{lrUI.page++;return load();});pager.append(pageLabel,previous,next);
  const reload=button("刷新评分",()=>load());
  const coverage=elt("p","","lr-coverage muted");
  panel.append(tabs,form,parameters,batch,coverage,notice,issues,results,pager);root.append(title("LEFT / RIGHT","盘后结构评分 · 转强形态筛选 · 近期趋势",reload),summary,panel);$("screen").replaceChildren(root);
  function active(request){return request===lrUI.request&&token===state.token&&state.page==="left-right"&&root.isConnected;}
  async function load(){
    clearTimeout(lrUI.timer);if(!root.isConnected||token!==state.token)return;const request=++lrUI.request;
    parameters.hidden=lrUI.structure_mode==="ALL";for(const child of tabs.children)child.setAttribute("aria-pressed",String(child.dataset.family===lrUI.family&&lrUI.structure_mode==="ALL"));
    parameterNotice.textContent="已应用：LEFT峰值参考 "+lrUI.parameters.min_left+" · RIGHT两日净增 ≥ "+lrUI.parameters.min_right_gain+" · MA20偏离 ≤ "+researchNumber(lrUI.parameters.max_ma20_distance*100,0)+"%";
    notice.textContent="正在读取已发布评分…";notice.hidden=false;notice.classList.remove("error");results.setAttribute("aria-busy","true");previous.disabled=next.disabled=reload.disabled=submit.disabled=true;
    try{
      const data=await api(apiRoot+"/left-right-info?"+new URLSearchParams({family:lrUI.structure_mode==="ALL"?lrUI.family:"RIGHT",structure_mode:lrUI.structure_mode,...lrUI.parameters,trade_date:lrUI.trade_date,q:lrUI.q,page:String(lrUI.page),page_size:"20"}));if(!active(request))return;
      const s=data.summary || {},upCount=panoramaNumber(s.up_count),returnCount=panoramaNumber(s.valid_return_count),ratio=returnCount>0&&upCount!==null?upCount/returnCount:null;
      summary.replaceChildren(metrics([["已保存候选",panoramaCount(s.total)],["最高结构分",researchNumber(s.max_score,1)],["上涨占比",panoramaRatio(ratio)],["涨幅 ≥ 9.5%",panoramaCount(s.high_return_count)]]));
      date.replaceChildren(...[["","最新已发布日期"],...(data.available_dates || []).map(day=>[day,day])].map(([value,label])=>{const item=elt("option",label);item.value=value;return item;}));date.value=lrUI.trade_date;
      batch.textContent="评分日期 "+display(data.selected_trade_date)+" · "+display(data.batch?.version_id)+" · 发布 "+time(data.batch?.published_at)+" · 有效评分 "+panoramaCount(s.valid_score_count)+" / "+panoramaCount(s.total)+" · "+(lrUI.structure_mode==="ALL"?lrUI.family:"RIGHT")+"结构分降序";
      const counts=data.structure_counts;coverage.hidden=!counts;
      coverage.textContent=counts?"底池 "+panoramaCount(counts.universe)+" · 可判定 "+panoramaCount(counts.evaluated)+" · 匹配 "+panoramaCount(counts.matched)+" · 资料待补 "+panoramaCount(counts.insufficient)+" · 观察中 "+panoramaCount(counts.watch)+" · 条件齐备 "+panoramaCount(counts.confirmed):"";
      lrUI.page=data.page;results.replaceChildren(lrRows(data.items || [],root,lrUI.structure_mode));issues.replaceChildren(researchIssues([...(data.issues || []),...(data.insufficient_reasons || []).map(item=>lrReason(item.reason)+" · "+panoramaCount(item.count)+"只")]));
      const pages=Math.max(1,data.pages || 0);pageLabel.textContent="共 "+panoramaCount(data.total)+" 项 · 第 "+data.page+" / "+pages+" 页";previous.disabled=data.page<=1;next.disabled=data.page>=pages;notice.hidden=true;
      $("freshness").textContent="盘后评分 "+display(data.selected_trade_date);
    }catch(error){if(!active(request))return;results.replaceChildren();summary.replaceChildren();issues.replaceChildren();batch.textContent="";coverage.textContent="";pageLabel.textContent="";notice.textContent="评分读取失败，请重新搜索或刷新。";notice.classList.add("error");}
    finally{if(active(request)){reload.disabled=submit.disabled=false;results.setAttribute("aria-busy","false");}}
  }
  researchSearch(search,form,lrUI,load);for(const event of ["input","compositionstart"])search.addEventListener(event,()=>{previous.disabled=next.disabled=true;});date.addEventListener("change",()=>{lrUI.trade_date=date.value;lrUI.page=1;action(load);});mode.addEventListener("change",()=>{lrUI.structure_mode=mode.value;lrUI.page=1;action(load);});await load();
}
function hotMetric(value,key) {
  const number=panoramaNumber(value);return number===null?"—":researchNumber(key==="net_cny"?number/1e8:number,key==="net_cny"?2:0);
}
function hotSectorChart(days,selected,key) {
  const width=days.length*280,svg=panoramaSvg("svg",{viewBox:"0 0 "+width+" 110",width,height:110,role:"img","aria-label":(key==="net_cny"?"主力净额（亿元）":"板块强度")+"历史曲线，缺数断开"});svg.classList.add("hot-curve");
  const points=days.map((day,index)=>{const sector=(day.top_sectors || []).find(item=>item.sector_id===selected);return {day,sector,index,value:panoramaNumber(sector?.[key])};});
  const values=points.filter(point=>point.value!==null).map(point=>point.value),lo=values.length?Math.min(...values):0,hi=values.length?Math.max(...values):0;
  const y=value=>hi===lo?56:88-(value-lo)/(hi-lo)*64;
  const baseline=hi>=0&&lo<=0?y(0):88;svg.append(panoramaSvg("line",{x1:30,x2:Math.max(30,width-30),y1:baseline,y2:baseline,class:"hot-chart-grid"}));
  let segment=[];function finish(){if(segment.length>1)svg.append(panoramaSvg("polyline",{points:segment.map(point=>point.join(",")).join(" "),class:"hot-chart-line"}));segment=[];}
  for(const point of points){if(point.value===null){finish();continue;}segment.push([point.index*280+140,y(point.value)]);}finish();
  for(const point of points){const x=point.index*280+140,label=panoramaSvg("text",{x,y:105,"text-anchor":"middle"});label.textContent=point.value===null?"缺数":hotMetric(point.value,key)+(key==="net_cny"?" 亿元":"");svg.append(label);
    if(point.value!==null){const dot=panoramaSvg("circle",{cx:x,cy:y(point.value),r:5,class:"hot-chart-dot"}),tip=panoramaSvg("title");tip.textContent=point.day.trade_date+" · "+display(point.sector?.name)+" · "+hotMetric(point.value,key);dot.append(tip);svg.append(dot);}}
  return svg;
}
function renderHotBoard(data,root,canvas,heading) {
  const days=[...(data.days || [])].sort((a,b)=>String(b.trade_date).localeCompare(String(a.trade_date)));
  const available=days.flatMap(day=>day.top_sectors || []);
  if(!available.some(item=>item.sector_id===hotUI.selected)){hotUI.selected=available[0]?.sector_id || "";hotUI.selectedDay=days[0]?.trade_date || "";}
  const selected=available.find(item=>item.sector_id===hotUI.selected);
  heading.textContent=(selected?.name || "暂无板块")+" · "+(hotUI.metric==="net_cny"?"主力净额（亿元）":"板块强度");
  if(!days.length){canvas.replaceChildren(empty("所选范围暂无已保存的热门板块快照"));return;}
  const track=elt("div",undefined,"hot-track"),columns=elt("div",undefined,"hot-columns");
  track.append(hotSectorChart(days,hotUI.selected,hotUI.metric));
  for(const day of days){
    const column=elt("section",undefined,"hot-day-column"),header=elt("header",undefined,"hot-day-heading");
    const latest=day.trade_date===data.latest_trade_date,today=day.trade_date===data.today_shanghai,boardOnly=latest||today||day.stock_mode!=="HISTORICAL_LR";
    header.append(elt("h3",day.trade_date),tag(boardOnly?(latest?"最新 · 仅板块":"当日 · 仅板块"):"历史归档"));
    const context=elt("details",undefined,"hot-day-source");context.append(elt("summary","采样 "+time(day.source_observed_at)),elt("p","来源 "+display(day.source)+" · 采集 "+time(day.collected_at)),elt("p","覆盖 "+panoramaCount(day.coverage?.loaded)+" / "+panoramaCount(day.coverage?.reported_total)),elt("p","源批次 "+researchStrings(day.source_batches).join(" · ")));
    header.append(context);column.append(header);
    const labels=elt("div",undefined,"hot-rank-labels");labels.append(elt("span","名次 / 板块"),elt("span","强度"),elt("span","净额（亿）"));column.append(labels);
    const ranking=elt("div",undefined,"hot-ranking");
    for(const sector of day.top_sectors || []){
      const row=button("",()=>{hotUI.selected=sector.sector_id;hotUI.selectedDay=day.trade_date;const scroll=canvas.scrollLeft;renderHotBoard(data,root,canvas,heading);canvas.scrollLeft=scroll;canvas.querySelector('[data-sector="'+CSS.escape(sector.sector_id)+'"][data-day="'+CSS.escape(day.trade_date)+'"]')?.focus({preventScroll:true});},"hot-sector-row");
      row.dataset.sector=sector.sector_id;row.dataset.day=day.trade_date;row.setAttribute("aria-pressed",String(sector.sector_id===hotUI.selected));row.classList.toggle("focused",sector.sector_id===hotUI.selected&&day.trade_date===hotUI.selectedDay);
      row.setAttribute("aria-label",day.trade_date+" "+sector.name+" 第 "+sector.rank+" 名");
      const name=elt("span",undefined,"hot-sector-name");name.append(elt("small",String(sector.rank ?? "—").padStart(2,"0"),"numeric"),elt("strong",sector.name || sector.sector_id));
      row.append(name,elt("span",hotMetric(sector.strength_raw,"strength_raw"),"numeric "+panoramaTone(sector.strength_raw)),elt("span",hotMetric(sector.net_cny,"net_cny"),"numeric "+panoramaTone(sector.net_cny)));ranking.append(row);
    }
    if(!(day.top_sectors || []).length)ranking.append(empty("该日暂无匹配板块"));column.append(ranking);
    const stocks=elt("section",undefined,"hot-stocks"),sector=(day.top_sectors || []).find(item=>item.sector_id===hotUI.selected);
    stocks.append(elt("h3",selected?.name || "板块历史"));
    if(boardOnly)stocks.append(elt("p","最新交易日与当天仅展示板块。","hot-stock-note"));
    else{
      stocks.append(elt("p","盘后评分 "+display(day.score_trade_date),"hot-stock-note"));
      for(const [family,threshold] of [["LEFT",75],["RIGHT",70]]){
        const group=elt("section",undefined,"hot-stock-group"),items=sector?.stock_groups?.[family] || [];
        group.append(elt("h4",family+" ≥ "+threshold+" 分 · "+panoramaCount(sector?.group_totals?.[family])+" 只"));
        for(const row of items){const item=button("",()=>showLrDetail(row,root),"hot-stock-card"),copy=elt("span",undefined,"hot-stock-identity"),scores=elt("span",undefined,"hot-stock-values");const memberDate=(row.sectors || []).filter(link=>link.id===sector.sector_id&&link.membership_date&&link.membership_date<=row.trade_date).map(link=>link.membership_date).sort().at(-1);copy.append(elt("strong",row.name || row.ts_code),elt("small",row.ts_code,"numeric muted"),elt("small","成员关系 "+display(memberDate),"muted"));scores.append(elt("b",researchNumber(row.setup_score,1)+" 分","numeric"),elt("small",panoramaRatio(row.return_ratio,true),"numeric "+panoramaTone(row.return_ratio)));item.append(copy,scores);group.append(item);}
        if(!items.length)group.append(elt("p",!sector?"该板块未在当日展示范围内":!day.score_trade_date?"该日暂无已保存评分":"暂无有效关联且达标的评分记录","hot-stock-note"));stocks.append(group);
      }
      stocks.append(elt("p","按评分日及之前最新已存成员关系筛选，结构分降序，每组最多 5 只。","hot-stock-note"));
    }
    if(sector)stocks.append(elt("p","板块涨跌 "+panoramaRatio(sector.return_ratio,true)+" · 数据时间 "+time(sector.observed_at),"hot-stock-note"));
    column.append(stocks);columns.append(column);
  }
  track.append(columns);canvas.replaceChildren(track);
}
async function renderHotSectors() {
  const token=state.token,root=elt("div",undefined,"hot-page"),panel=card("热门板块历史"),summary=elt("div",undefined,"research-batch"),issues=elt("div"),notice=elt("p","正在读取已保存板块…","sector-etf-notice");notice.setAttribute("role","status");
  const search=input("search","q");search.value=hotUI.q;search.maxLength=80;search.placeholder="搜索板块名称或代码";
  const days=select("days",[["5","近 5 个交易日"],["10","近 10 个交易日"],["20","近 20 个交易日"]]),limit=select("limit",[["10","每列前 10"],["20","每列前 20"],["50","每列前 50"]]);days.value=hotUI.days;limit.value=hotUI.limit;
  const form=elt("form",undefined,"hot-filters"),submit=elt("button","搜索","primary");submit.type="submit";form.append(field("板块名称或代码",search),field("历史范围",days),field("显示数量",limit),submit);
  const toolbar=elt("div",undefined,"toolbar tabs"),heading=elt("h3","","hot-selected-title"),canvas=elt("div",undefined,"hot-canvas");canvas.tabIndex=0;canvas.setAttribute("role","region");canvas.setAttribute("aria-label","热门板块多日对照，可横向滚动");
  for(const [key,label] of [["strength_raw","强度曲线"],["net_cny","净额曲线"]]){const tab=button(label,()=>{hotUI.metric=key;for(const child of toolbar.children)child.setAttribute("aria-pressed",String(child.dataset.metric===key));if(hotUI.data){const scroll=canvas.scrollLeft;renderHotBoard(hotUI.data,root,canvas,heading);canvas.scrollLeft=scroll;}});tab.dataset.metric=key;tab.setAttribute("aria-pressed",String(key===hotUI.metric));toolbar.append(tab);}
  const reload=button("刷新板块",()=>load());panel.append(form,summary,issues,notice,toolbar,heading,elt("p","最新日期在左；点击板块查看跨日强度及历史 LEFT / RIGHT 股票。左右滚动查看更多日期。","sector-etf-scope"),canvas);root.append(title("热门板块","多日板块强度与历史评分关联",reload),panel);$("screen").replaceChildren(root);
  function active(request){return request===hotUI.request&&token===state.token&&state.page==="hot-sectors"&&root.isConnected;}
  async function load(){
    clearTimeout(hotUI.timer);if(!root.isConnected||token!==state.token)return;const request=++hotUI.request;notice.hidden=false;notice.textContent="正在读取已保存板块…";notice.classList.remove("error");canvas.setAttribute("aria-busy","true");reload.disabled=submit.disabled=true;
    try{
      const data=await api(apiRoot+"/hot-sectors-info?"+new URLSearchParams({days:hotUI.days,limit:hotUI.limit,q:hotUI.q}));if(!active(request))return;
      hotUI.data=data;const scroll=canvas.scrollLeft;renderHotBoard(data,root,canvas,heading);canvas.scrollLeft=scroll;issues.replaceChildren(researchIssues(data.issues));
      summary.textContent="最新交易日 "+display(data.latest_trade_date)+" · 已保存 "+panoramaCount(data.days?.length)+" 个交易日 · 搜索保留原始排名";notice.hidden=true;$("freshness").textContent="板块数据时间见各日期";
    }catch(error){if(!active(request))return;hotUI.data=null;canvas.replaceChildren();summary.textContent="";heading.textContent="";issues.replaceChildren();notice.textContent="板块读取失败，请重新搜索或刷新。";notice.classList.add("error");}
    finally{if(active(request)){reload.disabled=submit.disabled=false;canvas.setAttribute("aria-busy","false");}}
  }
  const composing=researchSearch(search,form,hotUI,load);for(const control of [days,limit])control.addEventListener("change",()=>{hotUI[control.name]=control.value;action(load);});hotUI.reload=()=>composing()?undefined:load();await load();
}
function backtestRulesNotice() {
  const notice=elt("aside",undefined,"backtest-rules");
  notice.setAttribute("aria-labelledby","backtest-rules-title");
  const heading=elt("div",undefined,"backtest-rules-heading"),name=elt("h2","板块穿透 ETF · 自动模拟规则");
  name.id="backtest-rules-title";
  heading.append(name,elt("span","规则说明","tag"));
  notice.append(heading,elt("p","适用于自动周期；手动组合按您设置的节点执行。","backtest-rules-intro"));
  const columns=elt("div",undefined,"backtest-rules-grid");
  const rules=[
    ["准入条件","新增 / 加强 · AI 明确选中",[
      "板块方向有效，AI选中具有成分穿透或已核验板块—指数关联的ETF。",
      "境内被动行业或主题ETF的身份、实际交易规则、交易单位及当日可用报价通过核验；规则待复核时暂停新入场。",
      "同一ETF只保留一个活动周期；“维持”用于观察或继续持仓，自动模式不加仓。",
    ]],
    ["减仓条件","首次转弱 · 目标 50%",[
      "本周期绑定的板块首次明确转弱，登记当时持仓50%的减仓目标。",
      "按该ETF交易单位向下取整，同一周期只登记一次减仓目标。",
      "未成交份额继续等待执行，后续转强时保留已登记的待卖目标。",
    ]],
    ["清仓条件","明确退潮 · 全部剩余持仓",[
      "本周期绑定的板块明确退潮，登记卖出全部剩余份额。",
      "清仓可直接触发，并覆盖尚未完成的减仓目标。",
      "卖完后结束周期；待买尚未成交时出现转弱或退潮，则撤销买入。",
    ]],
  ];
  for (const [index,[label,trigger,items]] of rules.entries()) {
    const section=elt("section",undefined,"backtest-rule"),title=elt("h3"),number=elt("span",String(index+1).padStart(2,"0"),"backtest-rule-number");
    number.setAttribute("aria-hidden","true");title.append(number,document.createTextNode(label));
    const list=elt("ul");for (const item of items) list.append(elt("li",item));
    section.append(title,elt("p",trigger,"backtest-rule-trigger"),list);columns.append(section);
  }
  const foot=elt("div",undefined,"backtest-rules-foot");
  for (const [label,text] of [
    ["研判参考","主板占比、板块暴露、核心股、当日报价、规模和日均成交额供AI综合研判；规模与流动性保留资料日期、覆盖和缺失状态，不设统一数值准入线。"],
    ["成交与等待","节点登记后，使用交易时段内、节点之后的有效新报价撮合。买入资金须足够一手及已配置费用；卖出遵守T+1，未解锁份额继续等待。"],
    ["历史周期","已有持仓退出沿用创建时冻结的规则；当前产品规则待复核时仍按可卖份额与有效报价检查退出。实际进度见下方节点与成交记录。"],
  ]) {
    const line=elt("p");line.append(elt("strong",label),document.createTextNode(text));foot.append(line);
  }
  notice.append(columns,foot);
  return notice;
}

async function renderBacktests() {
  if (!await refresh() || state.page!=="backtest") return;
  const tabs=elt("div",undefined,"toolbar tabs");
  for (const [mode,name] of [["auto","自动周期"],["manual","手动组合"]]) {
    const b=button(name,()=>{state.mode=mode;return renderBacktests();});
    b.setAttribute("aria-pressed",String(state.mode===mode)); tabs.append(b);
  }
  const screen=$("screen");
  screen.replaceChildren(title("回测工作台","查阅自动周期，或独立重放手动组合"),backtestRulesNotice(),tabs);
  if (state.mode==="auto") {
    stopPoll();
    renderAutomaticBacktests(screen,state.view);
    return;
  }
  const saved=await api(apiRoot+"/drafts");
  if (state.page!=="backtest" || state.mode!=="manual") return;
  const pane=elt("div",undefined,"split"), editor=card("组合编辑");
  const drafts=renderSavedDrafts(saved.drafts);
  const pick=input("file","bundle"); pick.accept=".json,application/json";
  pick.addEventListener("change",()=>action(async()=>{
    const file=pick.files[0]; if(!file)return;
    if(file.size>8*1024*1024) throw new Error("证据包超过 8 MiB");
    const bundle=JSON.parse(await file.text());
    if(bundle.schema_version!=="stock-v4-manual-v1" || !Array.isArray(bundle.nodes) || !bundle.instruments || !Array.isArray(bundle.quotes)) {
      throw new Error("请选择 stock-v4-manual-v1 证据包");
    }
    state.editor=bundle; state.draft=null; state.parent=null; await renderBacktests();
  }));
  editor.append(field("导入本地历史证据包",pick));
  const history=elt("details");
  history.append(elt("summary","从已保存行情选取报价"));
  const historyForm=elt("form",undefined,"fields");
  const historyCode=input("text","etf",true); historyCode.placeholder="512480.SH";
  const historyFrom=input("datetime-local","from",true);
  const historyCutoff=input("datetime-local","cutoff",true);
  const merge=input("checkbox","merge");
  const historySubmit=elt("button","读取报价"); historySubmit.type="submit";
  historyForm.append(field("ETF 代码",historyCode),field("开始时间（北京时间）",historyFrom),
    field("截止时间（北京时间）",historyCutoff),field("加入当前组合",merge),historySubmit);
  historyForm.addEventListener("submit",e=>{e.preventDefault();action(async()=>{
    const bundle=await api(apiRoot+"/history","POST",{etf:historyCode.value.trim().toUpperCase(),
      from:historyFrom.value+":00+08:00",cutoff:historyCutoff.value+":00+08:00"});
    if (merge.checked && state.editor) {
      if (bundle.cutoff!==state.editor.cutoff || bundle.price_mode!==state.editor.price_mode ||
          JSON.stringify(bundle.calendar)!==JSON.stringify(state.editor.calendar)) {
        throw new Error("合并报价时，截止时间、价格模式和交易日历须一致");
      }
      const savedQuotes=new Map(state.editor.quotes.map(q=>[q.id,q]));
      for (const quote of bundle.quotes) savedQuotes.set(quote.id,quote);
      Object.assign(state.editor.instruments,bundle.instruments);
      state.editor.quotes=[...savedQuotes.values()];
    } else {
      state.editor=bundle; state.draft=null; state.parent=null;
    }
    await renderBacktests();
  });});
  history.append(historyForm); editor.append(history);
  const nodePreview=state.editor?renderEditor(editor):null;
  if (!state.editor) editor.append(empty("从已保存场景、自动周期或本地证据包开始"));
  pane.append(editor,drafts.panel); screen.append(pane);
  if (nodePreview) screen.append(nodePreview);
  if (drafts.comparisonCard) screen.append(drafts.comparisonCard);
  const result=card("回测结果"); result.id="job-result"; screen.append(result);
  renderResult(state.draft?.job);
  if (state.draft) await poll();
  const touch=await api(apiRoot+"/touch");
  if (state.page!=="backtest" || state.mode!=="manual") return;
  const section=card("三日历史触达");
  const groups=Object.entries(touch.result?.by_original_signal||{});
  if (touch.result) section.append(elt("p",(touch.frozen?"冻结样本 · ":"")+"截止 "+time(touch.result.as_of),"muted"));
  section.append(table(["原提示","有效样本","触达","触达率","待满窗","缺价格"],groups.map(([key,r])=>[
    key,r.valid,r.touched,pct(r.touch_pct),r.wait_window,r.missing_price
  ]),"暂无已接入的三日触达结果"));
  screen.append(section);
}
function renderAutomaticBacktests(screen,view) {
  const cycles=view.cycles;
  const entered=cycles.filter(cycle=>cycle.book.fills.length>0);
  const closed=entered.filter(cycle=>cycle.status==="CLOSED").length;
  const archived={...view.evaluation,entered:view.evaluation.entered??entered.length,closed:view.evaluation.closed??closed};
  const recent=view.evaluation_recent;
  const summary=card("周期统计"), statistics=elt("div");
  const period=select("period",[
    ...(recent?[["recent","最近 20 个交易日入场"]]:[]),["all","全部已归档周期"],
  ]);
  const renderSummary=()=>{
    const evaluation=period.value==="recent"?recent:archived;
    const gross={...evaluation.GROSS,...evaluation.profit_statistics?.GROSS}, net={...evaluation.NET,...evaluation.profit_statistics?.NET};
    const closedRate=evaluation.entered ? pct(evaluation.closed/evaluation.entered,100) : "—";
    statistics.replaceChildren(
      metrics([
        ["已入场",evaluation.entered],["已闭合",evaluation.closed],
        ["闭合率",closedRate],["已入场未退出",evaluation.entered-evaluation.closed],
      ]),
      table(["收益口径","可计胜率的闭合周期","盈利周期","胜率","平均盈利（元）","平均亏损（元）","盈亏比"],[
        ["毛收益",gross?.closed??0,gross?.wins??0,pct(gross?.win_pct),gross?.average_profit,gross?.average_loss,gross?.payoff_ratio],
        ["净收益",net?.closed??0,net?.wins??0,pct(net?.win_pct),net?.average_profit,net?.average_loss,net?.payoff_ratio],
      ],"暂无已闭合周期"),
    );
    if (evaluation.start && evaluation.end) {
      statistics.prepend(elt("p",evaluation.start+" 至 "+evaluation.end+" 入场 · 截至 "+time(evaluation.as_of),"muted"));
    }
  };
  period.addEventListener("change",renderSummary);
  renderSummary();
  summary.append(field("统计区间",period),statistics);
  const auto=card("自动模拟周期");
  auto.append(table([
    "ETF","状态","持有时长","估值时间（北京）","收益口径","收益率",
    "已实现（元）","未退出浮盈亏（元）","执行进度","操作",
  ],cycles.map(cycle=>{
    const valuation=cycle.valuation;
    return [
      cycle.etf,statusName(cycle.status),cycleHoldingTime(cycle),
      time(cycle.valuation_price_at),resultBasisName(valuation.basis),pct(valuation.return_pct),
      valuation.realized,valuation.shares ? valuation.unrealized : null,statusName(cycle.pending_reason),
      button("复制到手动",async()=>{
        const result=await api(apiRoot+"/copy-auto","POST",{cycle_id:cycle.id});
        state.mode="manual"; await openDraft(result.id);
      }),
    ];
  }),"暂无自动周期"));
  const ratio=value=>value==null ? null : pct(value,100);
  const exits=card("退出计划");
  exits.append(elt("p","v2 周期在买入成交时按大盘档位和 ETF 的 ATR20 冻结各条线；浮盈与峰值只取轮次采样报价。v1 周期没有价格和时间退出。","muted"));
  exits.append(table(["ETF","规则版本","大盘档位","ATR20","止损线","移动止盈（启动 / 回撤）","目标止盈减半","采样浮盈","采样峰值","余仓保本","持有日 / 上限","标注"],
    cycles.filter(cycle=>cycle.book.fills.length).map(cycle=>{
      const plan=cycle.exit_plan,seen=cycle.exit_view;
      if (!plan) return [cycle.etf,cycle.rule.version,"—","—","—","—","—","—","—","—","—","无价格退出"];
      return [cycle.etf,cycle.rule.version,regimes[plan.market_regime]?.[0] || plan.market_regime,ratio(plan.atr20),
        "-"+ratio(plan.stop_loss),ratio(plan.trail_start)+" / "+ratio(plan.trail_drawdown),ratio(plan.target_reduce),
        ratio(seen?.return_ratio),ratio(seen?.peak_return_ratio),
        seen?.breakeven_armed ? (seen.remainder?"已武装 · 余仓生效":"已武装 · 待减仓") : "未武装",
        seen?.held_days==null ? "— / T+"+plan.max_days : "T+"+seen.held_days+" / T+"+plan.max_days,
        plan.flags.map(flag=>exitFlags[flag] || flag).join("；") || null];
    }),"暂无已成交的自动周期"));
  const fillPrices=cycle=>Object.fromEntries(cycle.book.fills.map(fill=>[fill.order_id,fill.price]));
  const rows=cycles.flatMap(cycle=>cycle.orders.map(order=>[
    cycle.etf,actions[order.action],time(order.requested_at),order.theoretical,order.target,order.remaining,
    order.action==="REDUCE" && Number(order.theoretical)>0 ? pct(Number(order.target)*Number(cycle.rule.reduce_ratio)/Number(order.theoretical),100) : null,
    statusName(order.status),order.trigger_reason ? exitReasons[order.trigger_reason] || order.trigger_reason : null,
    order.trigger_price,fillPrices(cycle)[order.id],
  ]));
  const orders=card("自动节点");
  orders.append(table(["ETF","动作","节点时间","理论份额","取整后目标","剩余份额","取整后减仓比例","状态","触发原因","触发价","成交价"],rows,"暂无自动节点"));
  const execution=card("自动成交与 T+1 批次");
  execution.append(table(["ETF","动作","成交时间","参考价","成交价","成交份额"],cycles.flatMap(c=>c.book.fills.map(f=>[
    c.etf,actions[f.action],time(f.filled_at),f.raw_price,f.price,f.quantity,
  ])),"暂无成交"));
  execution.append(table(["ETF","买入日","可卖日期","剩余份额"],cycles.flatMap(c=>c.book.lots.filter(l=>l.remaining).map(l=>[
    c.etf,l.bought_on,l.sellable_from,l.remaining,
  ])),"暂无持仓批次"));
  const historyDetail=card("历史周期详情");historyDetail.hidden=true;
  screen.append(summary,auto,exits,orders,execution,archiveSearch("cycles",async id=>{
    const c=await api(apiRoot+"/cycle?id="+encodeURIComponent(id));
    historyDetail.hidden=false;
    historyDetail.replaceChildren(elt("h2",c.etf+" · "+statusName(c.status)),
      metrics([["收益率",pct(c.valuation.return_pct)],["持有时长",cycleHoldingTime(c)],["收益口径",resultBasisName(c.valuation.basis)]]),
      table(["动作","成交时间","价格","份额"],c.book.fills.map(f=>[actions[f.action],time(f.filled_at),f.price,f.quantity])),
      button("复制到手动",async()=>{const result=await api(apiRoot+"/copy-auto","POST",{cycle_id:c.id});state.mode="manual";await openDraft(result.id);}));
  }),historyDetail);
}
function cycleHoldingTime(cycle) {
  const first=cycle.book.fills[0]?.filled_at;
  if (!first) return "—";
  const end=cycle.closed_at;
  const duration=end?(Date.parse(end)-Date.parse(first))/1000:cycle.position_duration_seconds;
  if (!Number.isFinite(duration) || duration<0) return "—";
  const days=Math.floor(duration/86400), hours=Math.floor(duration%86400/3600);
  const minutes=Math.floor(duration%3600/60);
  const formatted=days?days+" 天 "+hours+" 小时":hours?hours+" 小时 "+minutes+" 分钟":
    minutes?minutes+" 分钟":Math.floor(duration)+" 秒";
  return formatted+(end?"":" · 截至账本");
}
function priceModeName(mode) {
  return mode === "ASSUMPTION" ? "假设价格" : "历史报价";
}
function resultBasisName(basis) {
  return basis === "NET" ? "净收益" : basis === "GROSS" ? "毛收益" : "—";
}
function renderSavedDrafts(drafts) {
  const panel=card("已保存的场景");
  if (!drafts.length) {
    panel.append(empty("暂无手动回测修订"));
    return {panel};
  }
  const draftIds=new Set(drafts.map(draft=>draft.id));
  const selected=new Set(state.compareDraftIds.filter(id=>draftIds.has(id)));
  const comparisonCard=card("场景比较"), comparisons=elt("div");
  comparisonCard.hidden=true;
  comparisons.setAttribute("aria-live","polite");
  comparisonCard.append(comparisons);
  const choices=[];
  let loading=false;
  const updateControls=()=>{
    compare.disabled=loading || selected.size<2;
    for (const choice of choices) {
      choice.disabled=loading || (selected.size>=3 && !choice.checked);
    }
  };
  const compare=button("比较所选",async()=>{
    const ids=[...selected], token=state.token, rows=[];
    loading=true;
    updateControls();
    comparisonCard.hidden=false;
    comparisons.replaceChildren(elt("p","正在读取已保存的结果…","muted"));
    try {
      for (const id of ids) {
        const draft=await api(backtests+"/"+encodeURIComponent(id));
        if (state.token!==token || !panel.isConnected) return;
        const result=draft.job?.result;
        rows.push([
          id.slice(-10), statusName(draft.job?.status||"DRAFT"),
          result?.initial_cash??draft.bundle.initial_cash??"10000.00",
          time(result?.cutoff??draft.bundle.cutoff),priceModeName(result?.price_mode??draft.bundle.price_mode),
          resultBasisName(result?.basis),pct(result?.return_pct),result?.realized,result?.unrealized,
          result ? statusName(result.status) : "待计算",
          time(draft.bundle.nodes.map(n=>n.at).sort()[0]),
          JSON.stringify(draft.bundle.costs??{}),result?.price_evidence_sha256??"旧版未记录",
        ]);
      }
      comparisons.replaceChildren(table(
        ["修订","任务状态","初始资金（元）","截止（北京）","价格模式","收益口径","收益率",
          "已实现（元）","未实现（元）","结果状态","首节点时间","成本参数","报价证据版本"],rows,"暂无可比较的结果"
      ));
    } catch (error) {
      comparisonCard.hidden=true;
      comparisons.replaceChildren();
      throw error;
    } finally {
      loading=false;
      updateControls();
    }
  });
  const tableRows=drafts.map(draft=>{
    const choice=input("checkbox","compare");
    choice.checked=selected.has(draft.id);
    choice.setAttribute("aria-label","比较修订 "+draft.id.slice(-10));
    choice.addEventListener("change",()=>{
      if (choice.checked) selected.add(draft.id); else selected.delete(draft.id);
      state.compareDraftIds=[...selected];
      comparisonCard.hidden=true;
      comparisons.replaceChildren();
      updateControls();
    });
    choices.push(choice);
    return [choice,draft.id.slice(-10),statusName(draft.status),button("打开",()=>openDraft(draft.id))];
  });
  const toolbar=elt("div",undefined,"toolbar");
  toolbar.append(compare,elt("span","选择 2 至 3 个已保存场景","muted"));
  panel.append(table(["比较","修订","状态",""],tableRows,"暂无手动回测修订"),toolbar,archiveSearch("drafts",openDraft));
  updateControls();
  // The comparison table sits below the editor and spans the workspace.
  return {panel,comparisonCard};
}
async function openDraft(id) {
  const data=await api(backtests+"/"+encodeURIComponent(id));
  state.draft=data; state.editor=structuredClone(data.bundle); state.parent=id;
  await renderBacktests();
}
function archiveSearch(kind,open) {
  const panel=card(kind==="reports"?"历史报告检索":kind==="cycles"?"历史周期检索":"历史场景检索");
  const day=input("date","date",false), identifier=input("text","id",false), etf=input("text","etf",false), results=elt("div");
  let cursor=null;
  const load=async(reset)=>{
    if (reset) cursor=null;
    const params=new URLSearchParams({kind});
    if(day.value)params.set("date",day.value);
    if(identifier.value.trim())params.set("id",identifier.value.trim());
    if(kind==="cycles" && etf.value.trim())params.set("etf",etf.value.trim());
    if(cursor)params.set("cursor",cursor);
    const data=await api(apiRoot+"/archive?"+params);
    cursor=data.next_cursor;
    results.replaceChildren(table(["时间","标识",""],data.items.map(r=>[time(r.at),r.id,button("打开",()=>open(r.id))]),"没有匹配记录"));
    next.disabled=!cursor;
  };
  const next=button("下一页",()=>load(false));next.disabled=true;
  panel.append(field("日期（可空）",day),field("完整报告 / 修订标识（可空）",identifier),button("检索",()=>load(true)),next,results);
  if(kind==="cycles")panel.prepend(field("ETF（可空，如 512800.SH）",etf));
  return panel;
}
const timelineClock=new Intl.DateTimeFormat("sv-SE",{
  timeZone:"Asia/Shanghai",dateStyle:"short",timeStyle:"medium",
});
function timelineInput(at) {
  return timelineClock.format(new Date(at)).replace(" ","T");
}
function savedNodeTimes(bundle,code,reports) {
  const cutoff=Date.parse(bundle.cutoff), days=new Set(bundle.calendar??[]);
  const valid=at=>Date.parse(at)<=cutoff && days.has(timelineInput(at).slice(0,10));
  const quotes=bundle.quotes
    .filter(q=>q.etf===code && q.tradeable && Date.parse(q.received_at)<=cutoff)
    .sort((a,b)=>Date.parse(b.observed_at)-Date.parse(a.observed_at)||(b.revision??0)-(a.revision??0));
  const used=new Set(), choices=[];
  for (const q of quotes) {
    if (!valid(q.observed_at) || used.has(q.observed_at)) continue;
    used.add(q.observed_at);
    choices.push([q.observed_at,"报价 "+timelineInput(q.observed_at).replace("T"," ")+" · "+(q.price_cny??q.price)]);
    if (choices.length===50) break;
  }
  for (const report of reports) {
    if (valid(report.at)) choices.push([
      report.at,"报告 "+timelineInput(report.at).replace("T"," ")+(report.slot?" · "+report.slot+" 轮":""),
    ]);
  }
  return choices.sort((a,b)=>Date.parse(b[0])-Date.parse(a[0]));
}
function renderEditor(section) {
  const bundle=state.editor;
  const money=input("text","initial_cash",true);
  money.inputMode="decimal"; money.value=bundle.initial_cash||"10000.00";
  money.addEventListener("input",()=>{bundle.initial_cash=money.value;});
  const initial=field("初始资金（元）",money);
  const row=elt("div",undefined,"toolbar");
  const caption=elt("p","报价 "+bundle.quotes.length+" 条 · 截止 "+time(bundle.cutoff),"muted");
  row.append(initial,caption); section.append(row);
  const form=elt("form",undefined,"fields");
  const code=select("etf",Object.keys(bundle.instruments).map(k=>[k,k]));
  const at=input("datetime-local","at",true); at.step="1";
  const savedTimes=select("saved_at",[]);
  const fillTimes=()=>{
    const choices=[["","手动输入时间"],...savedNodeTimes(bundle,code.value,state.view?.reports??[])];
    savedTimes.replaceChildren(...choices.map(([value,label])=>{
      const option=elt("option",label);
      option.value=value;
      return option;
    }));
  };
  code.addEventListener("change",fillTimes); fillTimes();
  savedTimes.addEventListener("change",()=>{
    if (savedTimes.value) at.value=timelineInput(savedTimes.value);
  });
  at.addEventListener("input",()=>{savedTimes.value="";});
  const act=select("action",Object.entries(actions).filter(([key])=>key!=="EXIT"));
  const size=input("text","size",true); size.inputMode="decimal";
  const mode=select("size_mode",[]);
  const update=()=>{
    mode.replaceChildren(...modes[act.value].map(k=>{const opt=elt("option",sizes[k]);opt.value=k;return opt;}));
    size.value=act.value==="REDUCE"?"0.5":"";
    size.required=mode.value!=="ALL"; size.disabled=!size.required;
  };
  act.addEventListener("change",update);
  mode.addEventListener("change",()=>{size.required=mode.value!=="ALL";size.disabled=!size.required;});
  update();
  const submit=elt("button","添加节点"); submit.type="submit";
  let editing=null;
  form.append(field("ETF",code),field("报告 / 报价时间线",savedTimes),field("节点时间（北京时间）",at),field("动作",act),field("数量基准",mode),field("数值",size),submit);
  form.addEventListener("submit",e=>{e.preventDefault();action(async()=>{
    const raw=size.value.trim(), value=mode.value==="ALL"?null:mode.value==="SHARES"?Number(raw):raw;
    if (mode.value==="SHARES" && (!Number.isSafeInteger(value)||value<=0)) throw new Error("份额须为正整数");
    const original=editing===null?null:bundle.nodes[editing];
    const atValue=savedTimes.value || (original && timelineInput(original.at)===at.value
      ? original.at : chinaTimestamp(at.value));
    const node={id:original?.id??"node-"+newRequestId(),
      etf:code.value,at:atValue,action:act.value,size_mode:mode.value,size:value};
    if (editing===null) bundle.nodes.push(node); else bundle.nodes[editing]=node;
    await renderBacktests();
  });});
  section.append(form);
  section.append(table(["时间","ETF","动作","数量",""],bundle.nodes.map((n,index)=>{
    const controls=elt("div",undefined,"toolbar");
    controls.append(button("编辑",()=>{
      editing=index; code.value=n.etf; fillTimes(); at.value=timelineInput(n.at);
      act.value=n.action; update(); mode.value=n.size_mode; size.value=n.size??"";
      size.required=mode.value!=="ALL"; size.disabled=!size.required;
      submit.textContent="更新节点"; at.focus();
    }),button("移除",async()=>{bundle.nodes.splice(index,1);await renderBacktests();}));
    return [time(n.at),n.etf,actions[n.action],sizes[n.size_mode]+" "+(n.size ?? ""),controls];
  }),"暂无节点"));
  const costs=card("费用与滑点");
  for (const [key,label] of [["fee_rate","手续费率（小数）"],["minimum_fee","最低手续费（元）"],["slippage_bps","滑点（基点）"]]) {
    const control=input("text",key,false); control.inputMode="decimal"; control.value=bundle.costs?.[key]??"";
    control.addEventListener("change",()=>{
      const value=control.value.trim(); bundle.costs??={};
      if (!value) delete bundle.costs[key];
      else if (!Number.isFinite(Number(value)) || Number(value)<0) {control.setCustomValidity("请输入非负数");return;}
      else bundle.costs[key]=value;
      control.setCustomValidity("");
    });
    costs.append(field(label,control));
  }
  costs.append(elt("p","三项均留空时计算毛收益；计算净收益请完整填写三项，可填写 0。","muted"));
  section.append(costs);
  const controls=elt("div",undefined,"toolbar");
  const preview=card("节点预览");
  preview.classList.add("node-preview");
  let previewRevision=0;
  const showPreview=async()=>{
    if (![...costs.querySelectorAll("input")].every(control=>control.reportValidity())) return;
    const revision=++previewRevision;
    if (!bundle.nodes.length) return;
    const result=await api(apiRoot+"/preview","POST",{bundle});
    if (revision!==previewRevision || !preview.isConnected) return;
    preview.replaceChildren(elt("h2","节点预览 · 已保存报价"));
    preview.append(table(["意图时间","ETF","参考价","目标份额","当时可卖","预计成交时间","成交后现金","状态"],result.nodes.map(n=>{
      const fills=result.fills.filter(f=>f.node_id===n.id), last=fills.at(-1);
      return [time(n.at),n.etf,n.reference_quote?.price_cny,n.target_quantity,n.sellable_at_intent,
        last?time(last.filled_at):"待价格 / T+1",last?.cash_after,statusName(n.reason||n.status)];
    })));
    preview.append(table(["ETF","成交后持仓","可卖"],result.positions.map(p=>[p.etf,p.shares,p.sellable])));
  };
  controls.append(button("更新预览",showPreview));
  if (bundle.nodes.length<=64 && bundle.quotes.length<=4000) setTimeout(()=>{
    if (preview.isConnected) action(showPreview);
  },0);
  controls.append(button("保存并计算",async()=>{
    if (![...costs.querySelectorAll("input")].every(control=>control.reportValidity())) return;
    if (!bundle.nodes.length) throw new Error("请先添加交易节点");
    const changed = !state.draft || JSON.stringify(bundle)!==JSON.stringify(state.draft.bundle);
    const id=changed ? (await api(backtests+"/manual","POST",{bundle,parent_id:state.parent})).id : state.draft.id;
    const retry=!changed && ["FAILED","CANCELLED"].includes(state.draft?.job?.status);
    await api(backtests+"/"+id+"/run","POST",retry?{retry:true}:{});
    await openDraft(id);
  },"primary"));
  if (state.draft) {
    const sourceId=state.draft.id;
    let cloneRequestId=null;
    const clone=button("复制草稿",async()=>{
      if(clone.disabled)return;
      clone.disabled=true;
      try {
        cloneRequestId ||= newRequestId();
        const id=(await api(backtests+"/"+sourceId+"/clone","POST",{request_id:cloneRequestId})).id;
        await openDraft(id);
        cloneRequestId=null;
      } finally { clone.disabled=false; }
    });
    controls.append(clone);
  }
  section.append(controls);
  return preview;
}
function renderResult(job) {
  const panel=$("job-result");
  if (!panel) return;
  panel.replaceChildren(elt("h2","回测结果"));
  if (!job) {panel.append(empty("保存并提交后在此查看"));return;}
  panel.append(tag(statusName(job.status)));
  if (["QUEUED","RUNNING"].includes(job.status)) {
    const b=button(job.cancel_requested?"取消处理中":"取消回测",async()=>{
      await api(backtests+"/"+state.draft.id+"/cancel","POST",{});
      await poll();
    });
    b.disabled=job.cancel_requested; panel.append(b);
  }
  if(job.error_code)panel.append(elt("p",statusName(job.error_code),"muted"));
  const result=job.result;
  if(!result)return;
  if (state.draft) {
    const id = state.draft.id;
    panel.append(button("下载结果",async()=>{
      const data = await api(backtests+"/"+id+"/download");
      const url = URL.createObjectURL(new Blob([JSON.stringify(data)],{type:"application/json"}));
      const link = elt("a");
      link.href=url; link.download=id+".json"; link.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    }));
  }
  panel.append(metrics([
    ["组合收益率",pct(result.return_pct)],["初始资金（元）",result.initial_cash],["现金（元）",result.cash],
    ["已实现（元）",result.realized],["未实现（元）",result.unrealized],
  ]));
  panel.append(elt("p",[
    "截止 "+time(result.cutoff)+"（北京时间）",priceModeName(result.price_mode),
    resultBasisName(result.basis),statusName(result.status),
    result.positions.length?"含期末持仓估值":null,
  ].filter(Boolean).join(" · "),"muted"));
  panel.append(elt("p","成本参数：手续费率 "+(result.rules?.fee_rate??"未提供")+" · 最低手续费 "+(result.rules?.minimum_fee??"未提供")+" 元 · 滑点 "+(result.rules?.slippage_bps??"未提供")+" 基点","muted"));
  if (result.limitations?.includes("CORPORATE_ACTIONS_NOT_INCLUDED")) {
    panel.append(elt("p","未完整计入分红和份额事件","muted"));
  } else if (result.limitations?.includes("CORPORATE_ACTIONS_SUPPLIED_ONLY")) {
    panel.append(elt("p","按导入的事件登记 · 应收分红 "+result.distribution_receivable_cny+" 元","muted"));
    const statuses={RECORDED:"已登记",RECEIVABLE:"应收",PAID:"已到账",APPLIED:"已调整"};
    panel.append(table(["ETF","事件","生效时间","状态","现金或份额","来源"],result.corporate_actions.map(event=>[
      event.etf,event.kind==="CASH_DIVIDEND"?"现金分红":"份额调整",event.ex_date || event.effective_date,
      statuses[event.status],event.cash_cny==null?event.shares_before+" → "+event.shares_after:event.cash_cny+" 元",
      event.source,
    ]),"截止时间前暂无已登记事件"));
  }
  panel.append(table(["节点时间","ETF","动作","数量基准","参考报价时间","目标份额","当时可卖","状态"],result.nodes.map(n=>[
    time(n.at),n.etf,actions[n.action],sizes[n.size_mode]+" "+(n.size??""),time(n.reference_quote?.observed_at),
    n.target_quantity,n.sellable_at_intent,statusName(n.reason||n.status),
  ]),"暂无节点"));
  panel.append(tradeTimeline(result));
  panel.append(table(["节点时间","成交时间","ETF","成交量","价格","顺延"],result.fills.map(f=>[
    time(f.intent_at),time(f.filled_at),f.etf,f.quantity,f.price,f.deferred_t1?"T+1":"—",
  ]),"暂无成交"));
  panel.append(table(["ETF","剩余份额","可卖","估值价格时间"],result.positions.map(p=>[
    p.etf,p.shares,p.sellable,time(p.price_quote?.observed_at)
  ]),"期末暂无剩余持仓"));
}
function tradeTimeline(result) {
  const section=card("意图与成交时间线");
  section.append(elt("p","圆点：节点意图 · 方点：实际模拟成交 · 橙色连线：T+1 顺延。图示前 50 个节点，完整明细见下表。","muted"));
  const nodes=result.nodes.slice(0,50);
  if(!nodes.length)return section;
  const first=Math.min(...nodes.map(n=>Date.parse(n.at))), last=Date.parse(result.cutoff);
  const x=at=>180+580*(Date.parse(at)-first)/Math.max(1,last-first);
  const ns="http://www.w3.org/2000/svg", svg=document.createElementNS(ns,"svg");
  svg.setAttribute("viewBox",`0 0 800 ${nodes.length*32+50}`);svg.setAttribute("width","100%");
  svg.setAttribute("role","img");svg.setAttribute("aria-label","交易意图与模拟成交时间线");
  const shape=(name,attrs,label)=>{
    const item=document.createElementNS(ns,name);
    for(const [key,value] of Object.entries(attrs))item.setAttribute(key,value);
    if(label){const title=document.createElementNS(ns,"title");title.textContent=label;item.append(title);}
    svg.append(item);return item;
  };
  nodes.forEach((node,index)=>{
    const y=30+index*32;
    const label=shape("text",{x:4,y:y+4,fill:"currentColor","font-size":12});label.textContent=node.etf+" "+actions[node.action];
    shape("line",{x1:180,y1:y,x2:760,y2:y,stroke:"#8b93a5","stroke-opacity":.25});
    shape("circle",{cx:x(node.at),cy:y,r:5,fill:"#517cc5"},"意图 "+time(node.at));
    for(const fill of result.fills.filter(f=>f.node_id===node.id)){
      const color=fill.deferred_t1?"#bc782e":"#37846a";
      shape("line",{x1:x(node.at),y1:y,x2:x(fill.filled_at),y2:y,stroke:color,"stroke-width":2});
      shape("rect",{x:x(fill.filled_at)-4,y:y-4,width:8,height:8,fill:color},time(fill.filled_at)+" · "+fill.quantity+" 份"+(fill.deferred_t1?" · T+1":""));
    }
  });
  section.append(svg);return section;
}
async function poll() {
  stopPoll();
  if (!state.draft || state.page!=="backtest" || state.mode!=="manual") return;
  const id=state.draft.id, next=await api(backtests+"/"+id);
  if (state.draft?.id!==id || state.page!=="backtest" || state.mode!=="manual") return;
  state.draft=next; renderResult(next.job);
  if (["QUEUED","RUNNING"].includes(next.job?.status)) state.timer=setTimeout(()=>action(poll),1250);
}
async function renderTrades() {
  const screen=$("screen");
  const [data, eod] = await Promise.all([api(apiRoot+"/trades"), api(apiRoot+"/left-right").catch(()=>null)]);
  if (state.page!=="trades") return;
  const entry=card("录入真实成交"), form=elt("form",undefined,"fields");
  const code=input("text","etf",true); code.placeholder="512480.SH";
  const side=select("side",[["BUY","买入"],["SELL","卖出"]]);
  const at=input("datetime-local","executed_at",true); at.value=chinaInput();
  const price=input("text","price_cny",true); price.inputMode="decimal";
  const qty=input("number","quantity"); qty.min="1"; qty.step="1";
  const fees=input("text","fees_cny"); fees.inputMode="decimal";
  const submit=elt("button","保存成交","primary"); submit.type="submit";
  form.append(field("ETF 代码",code),field("方向",side),field("成交时间（北京时间）",at),field("价格（元）",price),field("份额（可留空）",qty),field("费用（可留空）",fees),submit);
  let requestId=null;
  form.addEventListener("input",()=>{requestId=null;});
  form.addEventListener("submit",e=>{e.preventDefault();action(async()=>{
    if(submit.disabled)return; submit.disabled=true;
    try {
      requestId ||= newRequestId();
      await api(apiRoot+"/trades","POST",{request_id:requestId,etf:code.value.trim().toUpperCase(),side:side.value,
        executed_at:chinaTimestamp(at.value),price_cny:price.value.trim(),quantity:qty.value?Number(qty.value):null,fees_cny:fees.value.trim()||null});
      await renderTrades(); note("成交已保存");
    } finally { submit.disabled=false; }
  });});
  entry.append(form);
  const positions=card("持仓与可卖份额"), fills=card("已录入成交");
  positions.append(table(["ETF","持仓","可卖","锁定","成本"],data.positions.map(p=>[
    p.etf,p.quantity??"未知",p.sellable??"未知",p.locked??"未知",p.basis_cny??"未知"
  ]),"暂无已录入持仓"));
  fills.append(table(["时间","ETF","方向","价格","份额","费用"],data.fills.map(r=>[
    time(r.executed_at),r.etf,actions[r.side],r.price_cny,r.quantity??"未知",r.fees_cny??"未知"
  ]),"暂无成交记录"));
  const reference = elt("details",undefined,"card");
  const archive = eod?.reference;
  reference.append(elt("summary","LEFT/RIGHT 盘后参考"));
  if (archive) {
    reference.append(elt("p",archive.trade_date+" · "+archive.source,"muted"),
      table(["标的","方向","记录"],archive.items.map(row=>[row.code,row.side,row.note]),"该交易日暂无记录"));
  } else reference.append(empty("暂无已保存的盘后档案"));
  screen.replaceChildren(title("实盘记账","录入已实际发生的 ETF 成交"),entry,positions,fills,reference);
}

async function renderRuntimeStatus() {
  const data = await api(apiRoot+"/status");
  if (!state.token || state.page!=="status") return;
  const disk = data.temporary;
  const worker = data.worker;
  const storage = card("临时目录");
  storage.append(table(["范围","受管用量","条目","上次回收","回收条目","失败"],[["应用全局",disk]].map(([name,info])=>[
    name,info?.managed_bytes==null?null:(info.managed_bytes/(1024**2)).toFixed(2)+" MiB",
    info?.managed_entries,time(info?.last_cleanup?.at),info?.last_cleanup?.removed,
    info?.last_cleanup?.error || info?.usage_error || info?.last_cleanup?.failed,
  ])));
  storage.append(table(["服务","占用（MiB）","条目"],Object.entries(disk?.services??{}).map(([role,info])=>[
    role,(info.bytes/(1024**2)).toFixed(2),info.entries,
  ])));
  const gib = n => n == null ? "—" : (n / (1024**3)).toFixed(2)+" GiB";
  const enabled = value => value == null ? "未收到运行状态" : value ? "启用" : "关闭";
  const rounds = card("近期轮次"), delivery = card("最近报告的投递"), schedule = card("定时任务");
  const octop = card("OCTOP 研判与排障"), latest = data.octop;
  octop.append(table(["项目","当前记录"],[
    ["模型配置来源",latest?.model_expert ?? "最近报告未记录专家"],
    ["最近研判模型",latest?.configured_model],
    ["报告发布时间",time(latest?.published_at)],
    ["报告编号",latest?.report_id],
    ["最近轮次失败",latest?.last_failure
      ? [latest.last_failure.trade_date,latest.last_failure.slot,latest.last_failure.error].join(" · ") : "暂无"],
    ["QQ 通道","my-stock 私聊"],
  ],"暂无 OCTOP 记录"));
  octop.append(elt("p","默认模型在 OCTOP 的 my-stock 专家设置中修改。工作台显示最近实际用于研判的模型。","muted"));
  schedule.append(elt("p","Stock V4 worker 调度五轮，调用 OCTOP 研判并保存报告。","muted"));
  schedule.append(elt("p","常规轮次 "+(data.schedule?.slots || []).join(" · ")+"（北京时间）","muted"));
  if (worker) schedule.append(elt("p","Worker 状态更新 "+time(worker.observed_at),"muted"));
  schedule.append(table(["交易日","轮次","计划时间"],(data.schedule?.upcoming || []).map(r=>[
    r.trade_date,r.slot,time(r.planned_at)
  ]),"日历尚无后续轮次"));
  rounds.append(table(["交易日","轮次","状态","错误"],data.rounds.map(r=>[
    r.trade_date,r.slot,roundStates[r.state] || statusName(r.state),r.error
  ]),"暂无轮次记录"));
  delivery.append(table(["目标","最近报告","排版","本次投递","本次接收","最近一次接收","错误"],(data.deliveries || []).map(r=>{
    const qq = r.target==="octop";
    return [qq?"QQ":r.target==="redis"?"Redis":r.target,r.report_id,
      qq && r.state!=="NOT_QUEUED" ? qqFormatStates[r.format_state] : null,
      r.report_id ? qqDeliveryLabel(r) : "暂无报告",time(r.delivered_at),time(r.last_sent_at),deliveryErrors[r.error] || r.error];
  }),"暂无报告投递记录"));
  delivery.append(table(["待处理目标","状态","条数"],data.notifications.map(r=>[
    r.target==="octop"?"QQ":r.target,deliveryStates[r.state] || statusName(r.state),r.count
  ]),"暂无待投递或失败记录"));
  $("screen").replaceChildren(title("运行状态","调度、投递和临时目录",button("刷新",renderRuntimeStatus)),
    metrics([["维护状态",data.paused?"已暂停":"正常"],["Hermes 五轮",enabled(worker?.hermes_schedule)],
      ["共享行情采集",enabled(worker?.official_collection)],["个股研究报价",enabled(worker?.stock_research)],
      ["QQ 项目通知",enabled(worker?.qq_push)],["Findesk IOPV",enabled(worker?.findesk_iopv)],
      ["临时盘可用",gib(disk?.filesystem_free_bytes)],["应用临时预算",gib(disk?.budget_bytes)],
      ["文件系统硬配额",disk?disk.hard_limit?"已确认":"按应用预算":"—"]]),
    schedule,octop,rounds,delivery,storage);
}

const marketStream = {socket:null, timer:null, updateTimer:null, cursor:"0-0", retry:2000, generation:0};
function stopMarketStream() {
  marketStream.generation++;
  clearTimeout(marketStream.timer);
  clearTimeout(marketStream.updateTimer);
  marketStream.socket?.close();
  Object.assign(marketStream,{socket:null,timer:null,updateTimer:null,cursor:"0-0",retry:2000});
}
function refreshFromStream() {
  if (marketStream.updateTimer) return;
  marketStream.updateTimer = setTimeout(()=>{
    marketStream.updateTimer = null;
    const page = state.page;
    if(page==="sector-etf"){
      action(()=>sectorEtfUI.reload?.());
      return;
    }
    if(page==="hot-sectors"){
      action(()=>hotUI.reload?.());
      return;
    }
    if (page==="overview" || page==="market") {
      action(async()=>{
        if (!await refresh() || state.page!==page) return;
        if (page==="market") renderMarket();
        else $("freshness").textContent += " · 可刷新查看";
      });
    }
  },750);
}
async function connectMarketStream(generation = marketStream.generation) {
  const token = state.token;
  if (!token || generation!==marketStream.generation) return;
  const retry = () => {
    if (!state.token || generation!==marketStream.generation) return;
    clearTimeout(marketStream.timer);
    marketStream.timer = setTimeout(()=>connectMarketStream(generation),marketStream.retry);
    marketStream.retry = Math.min(30000,marketStream.retry*2);
  };
  try {
    const result = await api("/api/v2/market/ws-ticket","POST");
    if (token!==state.token || generation!==marketStream.generation) return;
    const url = new URL("/api/v2/market/ws",location.href);
    url.protocol = location.protocol==="https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url);
    marketStream.socket = socket;
    socket.addEventListener("open",()=>{
      if (generation!==marketStream.generation) return;
      socket.send(JSON.stringify({ticket:result.ticket,after_cursor:marketStream.cursor,topics:["market","report"]}));
    });
    socket.addEventListener("message",event=>{
      if (generation!==marketStream.generation) return;
      let value;
      try { value=JSON.parse(event.data); } catch { return; }
      if (!["snapshot","heartbeat","update"].includes(value?.type)) return;
      marketStream.retry=2000;
      if (typeof value.cursor==="string") marketStream.cursor=value.cursor;
      if (value.type==="update" || value.type==="snapshot") refreshFromStream();
    });
    socket.addEventListener("close",()=>{
      if (marketStream.socket!==socket) return;
      marketStream.socket=null;
      retry();
    });
  } catch { retry(); }
}

$("login-form").addEventListener("submit",async e=>{
  e.preventDefault();
  const form=e.currentTarget, submit=form.querySelector("button");
  state.token=form.elements.token.value.trim();
  submit.disabled=true;
  $("login-status").textContent="正在验证…";
  try {
    if(!await refresh())return;
    form.reset(); $("login").hidden=true; $("workspace").hidden=false; $("logout").hidden=false;
    await navigate("overview");
    connectMarketStream();
  } catch(error) {
    state.token=""; $("login-status").textContent=error.message;
  } finally { submit.disabled=false; }
});
$("logout").addEventListener("click",logout);
document.querySelectorAll("[data-page]").forEach(b=>b.addEventListener("click",()=>action(()=>navigate(b.dataset.page))));
