// Exercise the shipped page without a browser dependency or external requests.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
import {test} from "node:test";

class Element {
  constructor(tag,document){this.tagName=tag;this.ownerDocument=document;this.children=[];this.attributes={};this.dataset={};this.style={};this.events={};this.className="";this.value="";this.hidden=false;this._text="";
    this.classList={add:(name)=>this.toggleClass(name,true),toggle:(name,value)=>this.toggleClass(name,value)};
  }
  toggleClass(name,value){const values=new Set(this.className.split(/\s+/).filter(Boolean));if(value??!values.has(name))values.add(name);else values.delete(name);this.className=[...values].join(" ");}
  append(...nodes){for(let node of nodes){if(typeof node==="string")node=this.ownerDocument.createTextNode(node);node.parent=this;this.children.push(node);if(this.tagName==="select"&&!this.value)this.value=node.value;}}
  appendElement(tag){const node=this.ownerDocument.createElement(tag);this.append(node);return node;}
  createTHead(){return this.appendElement("thead");}
  createTBody(){return this.appendElement("tbody");}
  insertRow(){return this.appendElement("tr");}
  insertCell(){return this.appendElement("td");}
  replaceChildren(...nodes){for(const child of this.children)child.parent=null;this.children=[];this._text="";this.append(...nodes);}
  insertBefore(node,target){node.parent=this;this.children.splice(this.children.indexOf(target),0,node);}
  set textContent(value){this.replaceChildren();this._text=String(value);}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join("");}
  get childElementCount(){return this.children.length;}
  get firstElementChild(){return this.children[0];}
  get isConnected(){return this===this.ownerDocument.body||Boolean(this.parent?.isConnected);}
  setAttribute(name,value){this.attributes[name]=String(value);if(name==="class")this.className=String(value);}
  addEventListener(name,listener){(this.events[name]??=[]).push(listener);}
  checkValidity(){return this.type!=="month"||/^\d{4}-(0[1-9]|1[0-2])$/.test(this.value)&&this.value>=this.min&&this.value<=this.max;}
  reportValidity(){return true;}
  focus(){this.ownerDocument.activeElement=this;}
  async dispatch(name,extra={}){await Promise.all((this.events[name]||[]).map(listener=>listener({preventDefault(){},...extra})));}
}

function walk(root,predicate){return [...(predicate(root)?[root]:[]),...root.children.flatMap(child=>walk(child,predicate))];}
function css(root,name){return walk(root,node=>node.className.split(/\s+/).includes(name));}
function button(root,label){return walk(root,node=>node.tagName==="button"&&node.textContent===label)[0];}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const pending=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};

function reportPage(entry){
  const nodes=new Map();
  const document={createElement(tag){return new Element(tag,this);},
    createTextNode(text){const node=this.createElement("#text");node.textContent=text;return node;},
    getElementById(id){if(!nodes.has(id))nodes.set(id,this.createElement("div"));return nodes.get(id);},
    querySelectorAll(){return [];}};
  document.body=document.createElement("body");
  const context=vm.createContext({window:{},document,Node:Element,Intl,Date,URLSearchParams});
  vm.runInContext(readFileSync(new URL("../web/app.js",import.meta.url),"utf8"),context);
  const panel=document.createElement("section");
  context.reportDetail(panel,entry,()=>{throw new Error("render must not request data");});
  return panel;
}

test("report shows one catalog gap and separates recovered facts from publication time",()=>{
  const gap={products:[],coverage_gaps:{weights:{reason:"WEIGHTS_UNVERIFIED",products:1351,indexes:100}}};
  const entry={round_slot:"10:35",status:"PARTIAL",at:"2026-11-17T11:15:25+08:00",
    data_as_of:"2026-11-17T10:35:11+08:00",raw:JSON.stringify({summary:"已保存的研判",sectors:[]}),sectors:{},
    input_meta:{manual_recovery:{retry_of:"failed",source_cutoff:"2026-11-17T10:35:14+08:00",
      executed_at:"2026-11-17T11:15:25+08:00",decision_hash:"private-ledger-reference"},
      etf_recommendations:{sectors:{bank:gap,chip:gap}}}};
  const original=JSON.stringify(entry),text=reportPage(entry).textContent;
  assert.ok(text.includes("10:35轮·手动恢复"));
  assert.ok(text.includes("事实截止 2026-11-17 10:35:14 · 恢复发布 2026-11-17 11:15:25"));
  assert.equal(text.split("资料待补：目录1351只ETF缺可用成分权重").length-1,1);
  assert.ok(!text.includes("2702只ETF"));
  assert.ok(!text.includes("private-ledger-reference"));
  assert.equal(JSON.stringify(entry),original);
});

test("ordinary report retains its existing title and data clock",()=>{
  const entry={round_slot:"10:35",status:"PARTIAL",at:"2026-11-17T10:35:30+08:00",
    data_as_of:"2026-11-17T10:35:11+08:00",raw:JSON.stringify({sectors:[]}),sectors:{},input_meta:{}};
  const text=reportPage(entry).textContent;
  assert.ok(text.includes("10:35 轮报告"));
  assert.ok(text.includes("发布 2026-11-17 10:35:30 · 数据 2026-11-17 10:35:11"));
  assert.ok(!text.includes("手动恢复"));
});

test("direction report separates reference checks from selected ETF execution gaps",()=>{
  const entry={round_slot:"13:05",status:"PARTIAL",at:"2026-11-17T13:06:00+08:00",
    raw:JSON.stringify({sectors:[]}),sectors:{},input_meta:{etf_recommendations:{method:"etf-direction-first-v2",sectors:{chip:{products:[{
      etf:"588100.SH",ai_selected:true,recommendation_status:"PENDING",association_status:"VERIFIED",execution_eligibility:"PENDING",
      execution_reasons:["PRODUCT_RULE_UNVERIFIED","TRADE_UNIT_UNVERIFIED"],checks:{mainboard:{effect:"REFERENCE",required:false,result:"FAIL",value:0}}
    }]}}}}};
  const original=JSON.stringify(entry),text=reportPage(entry).textContent;
  assert.ok(text.includes("AI已选·执行资料待补 · 交易规则待核验 · 交易单位待核验"));
  assert.ok(text.includes("研判参考"));
  assert.ok(!text.includes("静态未通过"));
  assert.ok(!text.includes("PRODUCT_RULE_UNVERIFIED"));
  assert.equal(JSON.stringify(entry),original);
});

function coverage(month="2026-11",view="daily"){
  const days=Array.from({length:new Date(Number(month.slice(0,4)),Number(month.slice(5)),0).getDate()},(_,index)=>{
    const date=month+"-"+String(index+1).padStart(2,"0");
    const status=index===0?"CLOSED":index===2?"PARTIAL":index<13?"AVAILABLE":"PENDING";
    return {date,is_open:status!=="CLOSED",status,available_count:status==="AVAILABLE"?4:status==="PARTIAL"?2:0,expected_count:4,datasets:[
      {dataset:"daily",label:"日线行情",status:status==="AVAILABLE"?"AVAILABLE":"MISSING",row_count:status==="AVAILABLE"?5571:null,ready_count:null,source_at:null,updated_at:null},
      {dataset:"daily_assessment",label:"盘后策略评估",status:"AVAILABLE",row_count:11142,ready_count:0,source_at:"2026-11-13T15:00:00+08:00",updated_at:"2026-11-16T09:00:00+08:00"},
    ]};
  });
  return {month,view,latest_business_date:"2026-11-13",summary:{available:11,partial:1,missing:0},days};
}

async function page(){
  const document={createElement(tag){return new Element(tag,this);},createElementNS(_ns,tag){return this.createElement(tag);},createTextNode(text){const node=this.createElement("#text");node.textContent=text;return node;}};
  document.body=document.createElement("body");const screen=document.createElement("main");document.body.append(screen);
  const calls=[];let handler=null,current=true;
  const api=async(path,method="GET")=>{calls.push({path,method});if(path.endsWith("/data-updates"))return {latest_business_date:"2026-11-13",datasets:[],recent_jobs:[]};
    const query=new URL(path,"http://fixtures.invalid").searchParams;
    return handler?handler(query.get("month"),query.get("view")):coverage(query.get("month"),query.get("view"));};
  const context=vm.createContext({window:{},document,Node:Element,Intl,Date,URLSearchParams});
  vm.runInContext(readFileSync(new URL("../web/data-updates.js",import.meta.url),"utf8"),context);
  await context.window.renderDataUpdates({screen,api,apiRoot:"/api/v2/workspace",newRequestId:()=>"test",isCurrent:()=>current,navigate(){}});
  return {screen,document,calls,setHandler(value){handler=value;},leave(){current=false;}};
}

test("coverage dates show records and zero score readiness; inspecting dates only reads loaded data",async()=>{
  const app=await page(),{screen,calls}=app;
  assert.equal(css(screen,"update-option").length,5);
  assert.equal(css(screen,"coverage-day").length,30);
  assert.equal(css(screen,"coverage-blank").length,12); // November begins on Sunday.
  assert.ok(css(screen,"coverage-detail")[0].textContent.includes("归档 11,142 条 · 可评分 0 条"));
  const selected=css(screen,"coverage-day").find(node=>node.dataset.date==="2026-11-02");
  const before=calls.length;await selected.dispatch("click");
  assert.equal(calls.length,before);
  assert.equal(selected.attributes["aria-pressed"],"true");
  assert.ok(css(screen,"coverage-detail")[0].textContent.includes("2026 / 11 / 02"));
  await selected.dispatch("keydown",{key:"ArrowRight"});
  assert.equal(app.document.activeElement.dataset.date,"2026-11-03");
  assert.ok(css(screen,"coverage-detail")[0].textContent.includes("部分缺项"));
  assert.ok(calls.every(call=>call.method==="GET"));
});

test("month and view responses are fenced; retry restores the calendar after a failed read",async()=>{
  const app=await page(),{screen,calls}=app;
  const earlier=pending(),latest=pending();app.setHandler(month=>month==="2026-10"?earlier.promise:latest.promise);
  const previous=walk(screen,node=>node.attributes["aria-label"]==="上个月")[0];
  await previous.dispatch("click");await previous.dispatch("click");
  latest.resolve(coverage("2026-09"));await settle();
  earlier.resolve(coverage("2026-10"));await settle();
  assert.ok(css(screen,"coverage-day").every(node=>node.dataset.date.startsWith("2026-09")));
  app.setHandler(async()=>{throw new Error("读取失败示例");});await button(screen,"刷新日历").dispatch("click");await settle();
  assert.equal(css(screen,"coverage-layout")[0].hidden,true);
  assert.ok(screen.textContent.includes("读取失败示例"));
  app.setHandler((month,view)=>coverage(month,view));await button(screen,"重试").dispatch("click");await settle();
  assert.equal(css(screen,"coverage-layout")[0].hidden,false);
  const view=walk(screen,node=>node.tagName==="select")[0];view.value="assessment";await view.dispatch("change");await settle();
  assert.ok(css(screen,"coverage-day").some(node=>node.textContent.includes("已归档")));
  assert.ok(calls.at(-1).path.endsWith("month=2026-09&view=assessment"));
  const archived=css(screen,"coverage-day").find(node=>node.dataset.date.endsWith("-13"));
  assert.equal(css(archived,"coverage-day__count")[0].textContent,"1.1万");
  assert.equal(css(archived,"coverage-day__count")[0].title,"11,142 条归档");
  assert.ok(archived.attributes["aria-label"].includes("11,142 条归档"));
  const before=calls.length;await button(screen,"刷新状态").dispatch("click");
  assert.equal(calls.length,before+2);
  assert.ok(calls.every(call=>call.method==="GET"));
});

test("partially saved assessments show archived rows instead of a complete-looking dataset ratio",async()=>{
  const app=await page(),{screen}=app;
  app.setHandler((month,view)=>{
    const result=coverage(month,view),day=result.days.find(item=>item.date.endsWith("-13"));
    day.status="PARTIAL";day.available_count=day.expected_count=1;
    day.datasets.find(item=>item.dataset==="daily_assessment").row_count=5000;
    return result;
  });
  const view=walk(screen,node=>node.tagName==="select")[0];view.value="assessment";await view.dispatch("change");await settle();
  const day=css(screen,"coverage-day").find(node=>node.dataset.date==="2026-11-13");
  assert.ok(day.textContent.includes("归档未齐"));
  assert.ok(day.textContent.includes("5,000"));
  assert.ok(!day.textContent.includes("1 / 1"));
  assert.ok(day.attributes["aria-label"].includes("5,000 条归档"));
  assert.ok(css(screen,"coverage-detail")[0].textContent.includes("归档 5,000 条 · 可评分 0 条"));
  assert.ok(css(screen,"coverage-detail")[0].textContent.includes("应归档 11,142 条"));
  assert.equal(css(screen,"coverage-record__state--partial").length,1);
});
