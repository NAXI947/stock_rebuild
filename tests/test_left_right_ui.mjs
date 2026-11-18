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
