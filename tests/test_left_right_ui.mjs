import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import {test} from "node:test";

class Element {
  constructor(tag,document){this.tagName=tag;this.ownerDocument=document;this.children=[];this.attributes={};this.dataset={};this.style={};this.events={};this.className="";this.value="";this._text="";this.open=false;
    this.classList={add:(...names)=>names.forEach(name=>this.toggleClass(name,true)),remove:(name)=>this.toggleClass(name,false),toggle:(name,value)=>this.toggleClass(name,value)};
  }
  toggleClass(name,value){const values=new Set(this.className.split(/\s+/).filter(Boolean));if(value??!values.has(name))values.add(name);else values.delete(name);this.className=[...values].join(" ");}
  append(...nodes){for(let node of nodes){if(typeof node==="string")node=this.ownerDocument.createTextNode(node);node.parent=this;this.children.push(node);if(this.tagName==="select"&&!this.value)this.value=node.value;}}
  appendElement(tag){const node=this.ownerDocument.createElement(tag);this.append(node);return node;}
  createTHead(){return this.appendElement("thead");} createTBody(){return this.appendElement("tbody");}
  insertRow(){return this.appendElement("tr");} insertCell(){return this.appendElement("td");}
  get tBodies(){return this.children.filter(node=>node.tagName==="tbody");} get rows(){return this.children.filter(node=>node.tagName==="tr");} get cells(){return this.children.filter(node=>["td","th"].includes(node.tagName));}
  replaceChildren(...nodes){for(const child of this.children)child.parent=null;this.children=[];this._text="";this.append(...nodes);}
  set textContent(value){this.replaceChildren();this._text=String(value);} get textContent(){return this._text+this.children.map(child=>child.textContent).join("");}
  get isConnected(){return this===this.ownerDocument.body||Boolean(this.parent?.isConnected);}
  setAttribute(name,value){this.attributes[name]=String(value);if(name==="class")this.className=String(value);}
  addEventListener(name,listener){(this.events[name]??=[]).push(listener);}
  querySelector(selector){return walk(this,node=>selector.startsWith(".")?node.className.split(/\s+/).includes(selector.slice(1)):node.tagName===selector)[0]||null;}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);this.parent=null;}
  reportValidity(){return walk(this,node=>node.tagName==="input"&&node.type==="number").every(node=>node.value!==""&&Number.isFinite(Number(node.value))&&Number(node.value)>=Number(node.min)&&Number(node.value)<=Number(node.max));}
  focus(){this.ownerDocument.activeElement=this;} showModal(){this.open=true;} close(){this.open=false;}
  async dispatch(name,extra={}){await Promise.all((this.events[name]||[]).map(listener=>listener({preventDefault(){},...extra})));}
}
function walk(root,predicate){return [...(predicate(root)?[root]:[]),...root.children.flatMap(child=>walk(child,predicate))];}
const css=(root,name)=>walk(root,node=>node.className.split(/\s+/).includes(name));
const button=(root,label)=>walk(root,node=>node.tagName==="button"&&node.textContent===label)[0];
const named=(root,name)=>walk(root,node=>node.name===name)[0];
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};

function harness(respond){
  const nodes=new Map(),calls=[];
  const document={createElement(tag){return new Element(tag,this);},createElementNS(ns,tag){return this.createElement(tag);},
    createTextNode(text){const node=this.createElement("#text");node.textContent=text;return node;},
    getElementById(id){if(!nodes.has(id)){const node=this.createElement("div");this.body.append(node);nodes.set(id,node);}return nodes.get(id);},
    querySelectorAll(){return [];},querySelector(selector){return this.body.querySelector(selector);}};
  document.body=document.createElement("body");
  const context=vm.createContext({window:{},document,Node:Element,Intl,Date,URLSearchParams,setTimeout,clearTimeout,
    async fetch(url,options){calls.push({url,options});return {ok:true,json:()=>respond(url)};}});
  vm.runInContext(readFileSync(new URL("../web/app.js",import.meta.url),"utf8"),context);
  vm.runInContext('state.token="test-token";state.page="left-right"',context);
  return {context,document,calls,root:document.getElementById("screen")};
}
const row=(code="000001.SZ",score="80")=>({ts_code:code,name:code==="000001.SZ"?"平安银行":"第二股票",family:"RIGHT",trade_date:"2026-11-17",status:"READY",setup_score:score,close:"10.10",return_ratio:"0.01",version_id:"right-v1",quality:{daily_count:80,completed_weeks:28},subscores:{parts:{trend:80},weights:{trend:.2}}});
const info=(items=[row()])=>({items,available_dates:["2026-11-17"],selected_trade_date:"2026-11-17",family:"RIGHT",page:1,page_size:20,pages:1,total:items.length,summary:{total:items.length,valid_score_count:items.length,max_score:"80",valid_return_count:1,up_count:1,high_return_count:0},batch:{version_id:"right-v1",published_at:"2026-11-17T16:00:00+08:00"}});
const days=["2026-11-10","2026-11-11","2026-11-12","2026-11-13","2026-11-16","2026-11-17"];

test("backtest rules draw the screening and exit topology from the served rule snapshot",()=>{
  const {context,calls}=harness(()=>{throw new Error("the rule notice must not request data");});
  const row=(multiple,days)=>({stop_loss:[multiple,"0.011","0.022"],trail_start:["0.41","0.021","0.031"],
    trail_drawdown:["0.36","0.013","0.023"],target_reduce:["0.71","0.026","0.036"],
    stagnant_days:days,stagnant_return:"0.012",max_days:days+7});
  const v2={version:"hermes_signal_cycle_v2",reduce_ratio:"0.50",regime_mode:"DUAL",time_exit_enabled:true,
    bear_early_weak_exit:false,weak_exit_trade_days:4,breakeven_arm_return:"0.017",breakeven_buffer:"1.007",
    regime_bull_volume_ratio:"1.13",regime_bear_volume_ratio:"0.87",
    exit_table:{BEAR_DEFENSE:row("0.7",2),RANGE_ROTATION:row("0.8",3),BULL_OFFENSE:row("0.95",4)}};
  const notice=context.backtestRulesNotice(v2),text=notice.textContent,sections=walk(notice,node=>node.tagName==="details");
  assert.deepEqual(sections.map(node=>node.open),[false,false,false]);
  sections[1].open=true;sections[1].dispatch("toggle");
  assert.deepEqual(walk(context.backtestRulesNotice(v2),node=>node.tagName==="details").map(node=>node.open),[false,true,false]);
  for (const part of ["新周期规则 hermes_signal_cycle_v2","筛选逻辑","交易逻辑","成员不少于 300 只","沪深300 + 中证1000",
    "0.7 × ATR20，限 1.1%–2.2%","0.95 × ATR20，限 1.1%–2.2%","0.71 × ATR20，限 2.6%–3.6%","T+2 起浮盈低于 +1.2%","T+11",
    "峰值浮盈到过 +1.7%","成本 × 1.007","跨交易日第 4 次转弱","量能比 ≥ 1.13","量能比 ≤ 0.87","减50%，并启用保本",
    "到期退出","轮次失败时也为卖单补价"]) assert.ok(text.includes(part),part);
  assert.ok(!text.includes("弱势档早衰"));
  assert.ok(!context.backtestRulesNotice({...v2,time_exit_enabled:false}).textContent.includes("到期退出"));
  assert.ok(context.backtestRulesNotice({...v2,bear_early_weak_exit:true,regime_mode:"SINGLE"}).textContent.includes("弱势档早衰"));
  const v1=context.backtestRulesNotice({version:"hermes_signal_cycle_v1",reduce_ratio:"0.50"}).textContent;
  for (const part of ["新周期规则 hermes_signal_cycle_v1","首次转弱","明确退潮","没有按价格或持有天数的退出"]) assert.ok(v1.includes(part),part);
  for (const part of ["成本止损","移动止盈","ATR20 ×","各线取值"]) assert.ok(!v1.includes(part),part);
  assert.equal(calls.length,0);
});

test("ETF report renders the five saved counting dimensions and their selectable tiers",()=>{
  const {context,calls,root}=harness(()=>{throw new Error("saved report must not recollect data");});
  const fixtures=[
    ["A",3,6,1,2,6,0], ["B",1,1,1,1,1,0], ["C",1,1,0,1,0,1],
  ];
  const log=process.env.ETF_ACCEPTANCE_LOG;
  const saved=log && Object.fromEntries(Array.from(readFileSync(log,"utf8")
    .matchAll(/T24_WORKBENCH ([ABC]) (\{[^\r\n]+\})/g),m=>[m[1],JSON.parse(m[2])]));
  for (const [name,sectors,pairs,indexes,products,core,unknown] of fixtures) {
    const statistics=saved ? saved[name] : {
      sectors:{total:sectors,with_selectable:sectors},
      pairs:{total:pairs,selectable:pairs,selectable_by_tier:{core,below_core:0,unknown}},
      indexes:{total:1,weights_available:indexes}, products:{total:products,selectable:products},
      orders:{total:1},
    };
    const report={raw:JSON.stringify({sectors:[]}),round_slot:"09:35",status:"PARTIAL",
      input_meta:{representation:{statistics}}};
    context.reportDetail(root,report,()=>{});
    for (const text of ["可选板块"+sectors+"/"+sectors,"可选配对"+pairs+"/"+pairs,
                       "有权重指数"+indexes+"/1","可选 ETF"+products+"/"+products,
                       "本轮节点1","主干配对"+core,"低暴露配对0","未知暴露配对"+unknown]) {
      assert.ok(root.textContent.includes(text),name+" "+text);
    }
  }
  assert.equal(calls.length,0);
});

test("report names a sector whose saved pair group is empty as not found in the current material scope",()=>{
  const {context,calls,root}=harness(()=>{throw new Error("saved report must not recollect data");});
  const statistics={sectors:{total:2,with_selectable:1},indexes:{total:1,weights_available:1},
    pairs:{total:1,selectable:1,selectable_by_tier:{core:1,below_core:0,unknown:0}},
    products:{total:1,selectable:1},orders:{total:0}};
  const report={raw:JSON.stringify({sectors:[{kaipanla_plate_id:"801027",sector_name:"银行"},
      {kaipanla_plate_id:"801001",sector_name:"芯片"}]}),round_slot:"09:35",status:"PARTIAL",
    input_meta:{representation:{statistics,pairs:{"kaipanla:801027":{"512800.SH":{}},"kaipanla:801001":{}}}}};
  context.reportDetail(root,report,()=>{});
  assert.ok(root.textContent.includes("当前资料范围未找到：芯片"),root.textContent);
  assert.ok(!root.textContent.includes("全市场无") && !root.textContent.includes("纯游资"));
  delete report.input_meta.representation.pairs;
  context.reportDetail(root,report,()=>{});
  assert.ok(!root.textContent.includes("当前资料范围未找到"));
  assert.equal(calls.length,0);
});

test("history keeps zero scores, breaks at missing days and version changes, and respects cutoff",()=>{
  const {context}=harness(()=>{});
  const data={trade_dates:[...days,"2026-11-18"],items:[
    {trade_date:days[0],family:"LEFT",setup_score:0,status:"READY",version_id:"v1"},
    {trade_date:days[1],family:"LEFT",setup_score:70,status:"READY",version_id:"v1"},
    {trade_date:days[2],family:"LEFT",setup_score:99,status:"MISSING",version_id:"v1"},
    {trade_date:days[3],family:"LEFT",setup_score:75,status:"READY",version_id:"v1"},
    {trade_date:days[4],family:"LEFT",setup_score:77,status:"READY",version_id:"v2"},
    {trade_date:days[5],family:"LEFT",setup_score:80,status:"READY",version_id:"v2"}],
    bars:[{trade_date:days[0],close:"10"},{trade_date:days[2],close:"11"}]};
  const before=JSON.stringify(data),model=context.lrHistoryModel(data,"2026-11-17");
  assert.deepEqual(Array.from(model.days),days);
  assert.deepEqual(Array.from(model.segments.LEFT,segment=>Array.from(segment,point=>point.value)),[[0,70],[75],[77,80]]);
  assert.deepEqual(Array.from(model.segments.PRICE,segment=>segment.length),[1,1]);
  assert.equal(model.values.RIGHT[0].value,null);
  assert.equal(JSON.stringify(data),before);
});

test("history plots three series and aligned price/return rows with independent toggles",async()=>{
  const {context,calls}=harness(()=>{throw new Error("chart rendering must not request data");});
  const data={trade_dates:days.slice(0,2),items:days.slice(0,2).flatMap(day=>["LEFT","RIGHT"].map(family=>({trade_date:day,family,setup_score:family==="LEFT"?0:80,status:"READY",version_id:family+"v1"}))),bars:days.slice(0,2).map(day=>({trade_date:day,close:10,return_ratio:0}))};
  const chart=context.lrHistoryChart(data,"2026-11-17");
  assert.equal(walk(chart,node=>node.tagName==="polyline").length,3);
  assert.ok(chart.textContent.includes("0.0"));assert.ok(chart.textContent.includes("收盘价（元）"));assert.ok(chart.textContent.includes("日涨跌幅"));
  await button(chart,"LEFT").dispatch("click");await settle();
  assert.equal(walk(chart,node=>node.tagName==="polyline").length,2);
  assert.equal(button(chart,"LEFT").attributes["aria-pressed"],"false");
  await button(chart,"RIGHT").dispatch("click");await button(chart,"收盘价").dispatch("click");await settle();
  assert.ok(chart.textContent.includes("选择上方曲线即可显示趋势"));assert.equal(calls.length,0);
});

test("shape mode applies original thresholds, resets page and preserves server ranking",async()=>{
  const {context,root,calls}=harness(()=>info([row("000002.SZ","90"),row("000001.SZ","80")]));
  await context.renderLeftRight();
  const mode=named(root,"structure_mode");mode.value="PULLBACK";await mode.dispatch("change");await settle();
  let query=new URL(calls.at(-1).url,"https://test.invalid").searchParams;
  assert.equal(query.get("structure_mode"),"PULLBACK");assert.equal(query.get("family"),"RIGHT");
  assert.equal(query.get("min_left"),"70");assert.equal(query.get("min_right_gain"),"3");assert.equal(query.get("max_ma20_distance"),"0.1");
  named(root,"min_left").value="75";named(root,"min_right_gain").value="5";named(root,"max_ma20_distance").value="8";
  await named(root,"min_left").dispatch("input");assert.ok(root.textContent.includes("参数尚未应用"));
  await css(root,"lr-parameter-form")[0].dispatch("submit");await settle();
  query=new URL(calls.at(-1).url,"https://test.invalid").searchParams;
  assert.equal(query.get("min_left"),"75");assert.equal(query.get("min_right_gain"),"5");assert.equal(query.get("max_ma20_distance"),"0.08");assert.equal(query.get("page"),"1");
  const table=css(root,"lr-table")[0];assert.equal(table.tBodies[0].rows[0].cells[1].textContent,"第二股票000002.SZ");
  await button(root,"LEFT 左侧结构").dispatch("click");await settle();
  query=new URL(calls.at(-1).url,"https://test.invalid").searchParams;
  assert.equal(query.get("structure_mode"),"ALL");assert.equal(query.get("family"),"LEFT");
  assert.ok(calls.every(call=>call.options.method==="GET"));
});

test("rapid mode changes ignore stale responses",async()=>{
  const old=deferred(),{context,root}=harness(url=>new URL(url,"https://test.invalid").searchParams.get("structure_mode")==="STARTUP"?old.promise:info([row("000002.SZ","91")]));
  await context.renderLeftRight();const mode=named(root,"structure_mode");
  mode.value="STARTUP";await mode.dispatch("change");mode.value="PULLBACK";await mode.dispatch("change");await settle();
  old.resolve(info([row("000001.SZ","10")]));await settle();
  const table=css(root,"lr-table")[0];assert.ok(table.textContent.includes("第二股票"));assert.ok(!table.textContent.includes("平安银行"));
});

test("shape results show both scores, progress and complete-universe counts",async()=>{
  const structure={pattern:"STARTUP",label:"整理后刚转强",left_score:72,right_score:81,right_gain_2d:5,left_peak:77,stage:"WATCH",stage_label:"观察中",confirmed_count:3,confirmation_total:4,reasons:["最新收盘上涨"],pending_confirmations:["成交量收敛待确认"]};
  const data={...info([{...row(),structure}]),structure_counts:{universe:5000,evaluated:4500,matched:25,insufficient:500,watch:20,confirmed:5},insufficient_reasons:[{reason:"完整周线不足",count:500}]};
  const {context,root}=harness(()=>data);await context.renderLeftRight();
  assert.ok(root.textContent.includes("L 72.0 · R 81.0"));assert.ok(root.textContent.includes("RIGHT两日净增 5.0"));
  assert.ok(root.textContent.includes("3 / 4 项确认"));assert.ok(root.textContent.includes("底池 5,000"));
  assert.ok(root.textContent.includes("资料待补 500"));assert.ok(root.textContent.includes("完整周线不足 · 500只"));
  const dialog=context.showLrDetail({...row(),structure},root);assert.ok(dialog.textContent.includes("成交量收敛待确认"));
});

test("invalid threshold input does not issue a new request",async()=>{
  const {context,root,calls}=harness(()=>info());await context.renderLeftRight();
  named(root,"min_left").value="91";await css(root,"lr-parameter-form")[0].dispatch("submit");await settle();
  assert.equal(calls.length,1);
});

test("empty shape results retain coverage facts and differ from an unsaved score board",async()=>{
  const {context,root}=harness(url=>{
    const mode=new URL(url,"https://test.invalid").searchParams.get("structure_mode");
    return mode==="ALL"?info([]):{...info([]),structure_counts:{universe:5000,evaluated:4800,matched:0,insufficient:200,watch:0,confirmed:0},insufficient_reasons:[{reason:"完整周线不足",count:200}]};
  });
  await context.renderLeftRight();
  assert.ok(root.textContent.includes("所选日期、家族和搜索条件下暂无已保存评分"));
  const mode=named(root,"structure_mode");
  for(const value of ["STARTUP","PULLBACK"]){
    mode.value=value;await mode.dispatch("change");await settle();
    assert.ok(root.textContent.includes("暂无匹配结果"));
    assert.ok(!root.textContent.includes("暂无已保存评分"));
    assert.ok(root.textContent.includes("资料待补 200"));
    assert.ok(root.textContent.includes("完整周线不足 · 200只"));
  }
  mode.value="ALL";await mode.dispatch("change");await settle();
  assert.ok(root.textContent.includes("所选日期、家族和搜索条件下暂无已保存评分"));
  assert.ok(!root.textContent.includes("暂无匹配结果"));
});

test("detail requests dated 30-day history and explains missing scores in Chinese",async()=>{
  const {context,root,calls}=harness(()=>({trade_dates:days,items:[],bars:[]}));
  const missing={...row(),status:"MISSING",setup_score:null,reasons:["COST85_UNAVAILABLE","weekly"]};
  const dialog=context.showLrDetail(missing,root);await settle();
  const query=new URL(calls[0].url,"https://test.invalid");assert.equal(query.pathname,"/api/v2/workspace/left-right-history");assert.equal(query.searchParams.get("ts_code"),"000001.SZ");assert.equal(query.searchParams.get("trade_date"),"2026-11-17");assert.equal(query.searchParams.get("days"),"30");
  assert.ok(dialog.textContent.includes("资料待补"));assert.ok(dialog.textContent.includes("筹码成本缺失"));assert.ok(dialog.textContent.includes("周线状态资料待补"));assert.ok(dialog.textContent.includes("日线条数80"));assert.ok(!dialog.textContent.includes("MISSING"));
});

test("history result arriving after dialog close does not update it",async()=>{
  const pending=deferred(),{context,root}=harness(()=>pending.promise),dialog=context.showLrDetail(row(),root);
  dialog.close();pending.resolve({trade_dates:days,items:[],bars:[]});await settle();
  assert.equal(css(dialog,"lr-history").length,0);
});

const roundSlots=["09:35","10:35","13:05","14:05","14:30"];
const roundCellOf=(fields={})=>({active:true,phase:"WEAKEN",streak:1,covered:true,was_active:true,judged:"WEAKEN",advanced:"WEAKEN",
  skipped:null,reasons:["板块涨跌 -1.23%"],change:"-0.0123",...fields});
const outsideCell=(fields={})=>roundCellOf({active:false,phase:null,streak:0,was_active:false,advanced:null,...fields});
function overview(extra={}) {
  const report=slot=>({id:"report-"+slot,at:"2026-11-20T"+slot+":40+08:00",status:"PARTIAL",slot,summary:slot+" 的原文摘要"});
  return {slots:roundSlots,reports:["14:05","13:05","10:35","09:35"].map(report),rotation:{},rotation_window:[],cycles:[],
    evaluation:{open_or_pending:0},market:null,round_sectors:[
      {id:"kaipanla:1",name:"连续",rounds:{"09:35":roundCellOf({streak:2}),"10:35":roundCellOf({streak:3}),
        "13:05":roundCellOf({phase:"MAINTAIN",judged:"MAINTAIN",advanced:"MAINTAIN",change:"0.004"}),
        "14:05":roundCellOf({phase:"STRENGTHEN",judged:"STRENGTHEN",advanced:"STRENGTHEN",streak:5,change:null})}},
      {id:"kaipanla:2",name:"入场后离场",rounds:{"09:35":outsideCell({covered:false,judged:null,reasons:[],change:null}),
        "10:35":roundCellOf({phase:"NEW",judged:"NEW",advanced:"NEW",streak:0,was_active:false}),
        "13:05":roundCellOf({active:false,phase:null,streak:0,judged:"RETREAT",advanced:"RETREAT"}),
        "14:05":outsideCell({judged:"WEAKEN",skipped:"NO_ACTIVE_DIRECTION"})}},
      {id:"kaipanla:3",name:"沿用",rounds:{"09:35":roundCellOf({streak:4}),
        "10:35":roundCellOf({streak:4,judged:"UNKNOWN",advanced:null}),
        "13:05":roundCellOf({streak:4,advanced:null,skipped:"DUPLICATE_EVIDENCE"}),
        "14:05":roundCellOf({streak:4,covered:false,judged:null,advanced:null,reasons:[],change:null})}},
      {id:"kaipanla:4",name:null,rounds:{"09:35":outsideCell({judged:"MAINTAIN",skipped:"NO_ACTIVE_DIRECTION"})}},
    ],...extra};
}
function openOverview(view) {
  const page=harness(url=>url.includes("/report?")?{id:"report-10:35",at:"2026-11-20T10:35:40+08:00",status:"PARTIAL",sectors:[]}:{items:[]});
  page.context.overviewFixture=view;
  vm.runInContext("state.page='overview';state.view=overviewFixture;roundsUI.source=false;roundsUI.idle=false;renderOverview()",page.context);
  const card=()=>css(page.root,"round-card")[0];
  const grid=()=>Object.fromEntries(css(card(),"round-matrix")[0].tBodies[0].rows.map(row=>[row.children[0].textContent,
    row.children.slice(1).map(cell=>cell.children[0])]));
  return {...page,card,grid,heads:()=>css(card(),"round-matrix")[0].children[0].rows[0].children};
}

test("five-round card lays sectors against rounds with entry, streaks, exit and carried state",()=>{
  const {card,grid,heads}=openOverview(overview());
  assert.deepEqual(heads().map(th=>th.textContent),["板块","09:35查看报告","10:35查看报告","13:05查看报告","14:05查看报告","14:30待报告"]);
  const rows=grid(), text=name=>rows[name].map(cell=>cell.textContent), kinds=name=>rows[name].map(cell=>cell.children[0]?.className ?? cell.className);
  // Sectors that never were an active direction today stay folded away until asked for.
  assert.deepEqual(Object.keys(rows),["连续","沿用","入场后离场"]);
  assert.deepEqual(text("连续"),["▼减弱×2-1.23%","▼减弱×3-1.23%","—维持+0.40%","▲加强×5","·"]);
  assert.deepEqual(kinds("连续"),["rm-chip rm-weaken","rm-chip rm-weaken","rm-chip rm-maintain","rm-chip rm-strengthen","rm-wait"]);
  assert.deepEqual(text("入场后离场"),["·","＋新增-1.23%","✕退潮-1.23%","减弱","·"]);
  assert.deepEqual(kinds("入场后离场").slice(0,4),["rm-chip rm-idle","rm-chip rm-new","rm-chip rm-retreat","rm-chip rm-idle"]);
  assert.deepEqual(text("沿用"),["▼减弱×4-1.23%","=▼减弱×4-1.23%","=▼减弱×4-1.23%","=▼减弱×4","·"]);
  assert.deepEqual(kinds("沿用").slice(1,4),Array(3).fill("rm-chip rm-weaken rm-carried"));
  assert.equal(rows["沿用"][1].title,"沿用 · 10:35\n状态沿用：减弱×4\nAI 本轮判断：待确认\n板块涨跌 -1.23%");
  assert.match(rows["沿用"][2].title,/证据与上一轮相同，状态沿用/);
  assert.match(rows["沿用"][3].title,/本轮报告未覆盖该板块/);
  assert.match(rows["入场后离场"][2].title,/本轮退潮，退出活动方向/);
  assert.match(rows["入场后离场"][3].title,/不在活动方向内[\s\S]*未判新增不入场/);
  assert.equal(css(card(),"slot").length,0);
});

test("five-round card unfolds idle sectors, falls back to the source text and still opens a report",async()=>{
  const {card,grid,heads,calls,root}=openOverview(overview());
  const idle=button(card(),"非活动板块 1");
  assert.equal(idle.attributes["aria-pressed"],"false");
  await idle.dispatch("click");
  assert.deepEqual(Object.keys(grid()).at(-1),"kaipanla:4");
  assert.equal(grid()["kaipanla:4"][0].textContent,"维持");
  assert.equal(button(card(),"非活动板块 1").attributes["aria-pressed"],"true");
  await heads()[2].children[1].dispatch("click");
  await settle();
  assert.ok(calls.some(call=>call.url.endsWith("/report?id=report-10%3A35")));
  await button(card(),"看原文").dispatch("click");
  assert.equal(css(card(),"round-matrix").length,0);
  assert.deepEqual(css(card(),"slot").map(row=>row.textContent),["09:3509:35 的原文摘要查看报告","10:3510:35 的原文摘要查看报告",
    "13:0513:05 的原文摘要查看报告","14:0514:05 的原文摘要查看报告","14:30待报告"]);
  await button(card(),"看原文").dispatch("click");
  assert.equal(css(card(),"round-matrix").length,1);
  assert.ok(root.textContent.includes("活动轮动方向"));
});

test("five-round card tolerates a day without reports, missing fields and a failed round",()=>{
  const empty=openOverview(overview({reports:[],round_sectors:undefined}));
  assert.deepEqual(empty.heads().slice(1).map(th=>th.textContent),roundSlots.map(slot=>slot+"待报告"));
  assert.ok(empty.card().textContent.includes("暂无已落库的板块判断"));
  const sparse=openOverview(overview({rotation_window:[{trade_date:"2026-11-20",slot:"14:30",status:"FAILED"}],
    round_sectors:[{id:"kaipanla:9",rounds:{"09:35":{active:true,was_active:true,covered:true}}},{id:"kaipanla:8"}]}));
  assert.notEqual(sparse.heads()[5].textContent,"14:30待报告");
  assert.deepEqual(sparse.grid()["kaipanla:9"].map(cell=>cell.textContent),["=沿用","·","·","·","·"]);
  assert.equal(button(sparse.card(),"非活动板块 1").textContent,"非活动板块 1");
  // A retreat whose earlier membership lies before the loaded reports is still an exit worth showing.
  const edge=openOverview(overview({round_sectors:[{id:"kaipanla:7",name:"窗口外退潮",rounds:{
    "09:35":roundCellOf({active:false,phase:null,streak:0,was_active:null,judged:"RETREAT",advanced:"RETREAT"}),
    "10:35":outsideCell({judged:"RETREAT",advanced:"RETREAT"})}}]}));
  assert.deepEqual(edge.grid()["窗口外退潮"].slice(0,2).map(cell=>cell.textContent),["✕退潮-1.23%","退潮"]);
  assert.match(edge.grid()["窗口外退潮"][0].title,/超出已加载的报告范围/);
  assert.equal(button(edge.card(),"非活动板块 1"),undefined);
});
