"use strict";

// Port of the retained AdminDataUpdates.vue. Presentation and controls follow
// the original template; the host supplies the authenticated API adapter.
window.renderDataUpdates = async function ({screen, api, apiRoot, newRequestId, isCurrent, navigate}) {
  const options = [
    {value:"daily",label:"日线与指标",note:"按日期补日线、复权、指标和筹码",icon:"M3 17l5-5 4 4 8-8M14 8h6v6"},
    {value:"weekly",label:"周线补采",note:"只更新已经完成的交易周",icon:"M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"},
    {value:"basic",label:"基础资料",note:"股票资料、交易日历、申万行业",icon:"M4 6h16M4 12h16M4 18h12"},
    {value:"monthly",label:"月更",note:"指定月份的概念成分和解禁",icon:"M12 8v4l3 3m6-3a9 9 0 1 1-18 0 9 9 0 0 1 18 0z"},
    {value:"yearly",label:"年更",note:"年度财务与最新业务日策略重算",icon:"M9 17v-6m3 6V7m3 10v-4M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"},
  ];
  const labels = {OK:"已更新",NOT_SYNCED:"尚未更新",STALE:"建议更新",EMPTY:"本范围无记录",PENDING:"排队中",RUNNING:"执行中",SUCCEEDED:"已完成",FAILED:"失败",CANCELLED:"已取消"};
  const errors = {USE_COMPLETED_DATES_MAX_31_DAYS:"请选择已完成的日期，每次最多31个自然日。",NO_COMPLETED_TRADING_DAY_IN_RANGE:"所选范围没有适用的已完成交易日或完整周。",TRADE_CALENDAR_RANGE_MISSING:"所选日期的交易日历不完整，请先更新基础资料。",USE_COMPLETED_REPORT_YEAR:"请选择已经结束的财务报告年度。",INVALID_MONTH:"请选择有效月份。"};
  const currentYear = new Date().getFullYear();
  const model = {mode:"daily",date_from:"",date_to:"",month:"",year:currentYear-1};
  let snapshot=null,loading=false,submitting=false,request=0,attempt=null;
  const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;};
  const root=el("div",undefined,"admin-page data-updates-page");
  const live=()=>root.isConnected && isCurrent();
  const tone=status=>["OK","SUCCEEDED","ACTIVE","FRESH","VERIFIED"].includes(status)?"ok":["WARNING","ATTENTION","PARTIAL","RUNNING","PENDING","WAITING","PROPOSED"].includes(status)?"warning":["FAILED","ERROR","STALE","QUARANTINED","EXPIRED","LEASE_EXPIRED"].includes(status)?"danger":"unknown";
  const integer=value=>Number(value).toLocaleString("zh-CN");
  const formatTime=(value,empty="未记录")=>{if(!value)return empty;const date=new Date(value);return Number.isNaN(date.getTime())?String(value):new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false,timeZone:"Asia/Shanghai"}).format(date);};
  const badge=(status,text)=>{const node=el("span",undefined,"admin-badge admin-badge--"+tone(status));const dot=el("span",undefined,"admin-badge__dot");dot.setAttribute("aria-hidden","true");node.append(dot,document.createTextNode(text || labels[status] || status));return node;};
  const card=(heading,note)=>{const section=el("section",undefined,"admin-card"),head=el("div",undefined,"admin-card__header"),copy=el("div");copy.append(el("h2",heading),el("p",note));head.append(copy);section.append(head);return {section,head};};
  const table=(columns,rows)=>{const wrap=el("div",undefined,"admin-table-wrap"),table=el("table",undefined,"admin-table"),head=el("thead"),headRow=el("tr"),body=el("tbody");for(const label of columns)headRow.append(el("th",label));head.append(headRow);for(const cells of rows){const row=el("tr");for(const item of cells){const cell=el("td");if(item instanceof Node)cell.append(item);else cell.textContent=item??"—";row.append(cell);}body.append(row);}table.append(head,body);wrap.append(table);return wrap;};
  const primary=(value,secondary)=>{const block=el("div");block.append(el("span",value,"admin-table__primary"));if(secondary)block.append(el("span",secondary,"admin-table__secondary"));return block;};

  const header=el("header",undefined,"admin-page__header"),intro=el("div"),actions=el("div",undefined,"admin-page__actions"),latest=badge("UNKNOWN",""),refresh=el("button","刷新状态","ui-button admin-button");
  latest.hidden=true;refresh.type="button";
  intro.append(el("p","TUSHARE / BATCH UPDATES","admin-page__eyebrow"),el("h1","数据更新与补采"),el("p","日线与每日指标次晨自动同步；基础资料、周线、月更和年更支持按需排队补采。"));
  actions.append(latest,refresh);header.append(intro,actions);
  const controls=card("选择补采周期与范围","提交后由后台 collector 异步排队执行，可随时离开页面。"),active=el("span","日线与指标","admin-pill admin-pill--active"),controlsBody=el("div",undefined,"admin-card__body");
  controls.head.append(active);
  const optionGrid=el("div",undefined,"update-options"),form=el("form",undefined,"admin-toolbar update-form"),message=el("p",undefined,"shell-note"),submitError=el("p",undefined,"update-error");
  message.hidden=submitError.hidden=true;message.setAttribute("role","status");submitError.setAttribute("role","alert");
  const submit=el("button","提交更新任务","ui-button ui-button--primary admin-button");submit.type="submit";
  for(const option of options){
    const button=el("button",undefined,"update-option"),head=el("div",undefined,"update-option__head");button.type="button";button.dataset.mode=option.value;
    const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
    for(const [key,value] of Object.entries({viewBox:"0 0 24 24",width:"18",height:"18",fill:"none",stroke:"currentColor","stroke-width":"1.9","stroke-linecap":"round","stroke-linejoin":"round","aria-hidden":"true",class:"update-option__icon"}))svg.setAttribute(key,value);
    const path=document.createElementNS(svg.namespaceURI,"path");path.setAttribute("d",option.icon);svg.append(path);head.append(el("strong",option.label),svg);button.append(head,el("span",option.note));
    button.addEventListener("click",()=>{model.mode=option.value;renderForm();});optionGrid.append(button);
  }
  controlsBody.append(optionGrid,form,message,submitError);controls.section.append(controlsBody);
  const asyncState=el("div"),datasetsView=el("div"),jobsView=el("div");asyncState.hidden=datasetsView.hidden=jobsView.hidden=true;root.append(header,controls.section,asyncState,datasetsView,jobsView);screen.replaceChildren(root);
  const coverage=mountCoverage();

  function mountCoverage(){
    const section=card("数据覆盖日历","历史覆盖记录长期保存；选择日期查看已入库条数与缺失项。"),body=el("div",undefined,"coverage-body"),toolbar=el("div",undefined,"coverage-toolbar"),navigation=el("div",undefined,"coverage-navigation");
    const previous=el("button","‹","ui-button admin-button admin-button--quiet"),next=el("button","›","ui-button admin-button admin-button--quiet"),monthLabel=el("label","查看月份","admin-field"),month=el("input"),viewLabel=el("label","数据范围","admin-field"),view=el("select"),reload=el("button","刷新日历","ui-button admin-button admin-button--quiet");
    const monthParts=Object.fromEntries(new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit"}).formatToParts(new Date()).map(part=>[part.type,part.value]));
    month.type="month";month.min="0002-01";month.max="9998-12";month.value=monthParts.year+"-"+monthParts.month;month.required=true;
    for(const [value,label] of [["daily","日线与指标"],["weekly","周线"],["assessment","盘后评估"]]){const option=el("option",label);option.value=value;view.append(option);}
    previous.type=next.type=reload.type="button";previous.setAttribute("aria-label","上个月");next.setAttribute("aria-label","下个月");
    monthLabel.append(month);viewLabel.append(view);navigation.append(previous,monthLabel,next);toolbar.append(navigation,viewLabel,reload);
    const summary=el("p",undefined,"coverage-summary"),hint=el("p",undefined,"coverage-hint"),legend=el("div",undefined,"coverage-legend"),notice=el("div"),layout=el("div",undefined,"coverage-layout"),calendar=el("div",undefined,"coverage-calendar"),detail=el("div",undefined,"coverage-detail");
    const statusLabels={AVAILABLE:"有数据",PARTIAL:"部分缺项",MISSING:"无数据",CLOSED:"休市",PENDING:"待更新",UNKNOWN:"日历未知",NOT_DUE:"非周结日"};
    const rowLabels={AVAILABLE:"有数据",EMPTY:"有效空记录",MISSING:"无数据",NOT_DUE:"本日不适用"};
    summary.setAttribute("role","status");summary.setAttribute("aria-live","polite");detail.setAttribute("aria-label","所选日期数据明细");
    for(const status of ["AVAILABLE","PARTIAL","MISSING","CLOSED"]){const item=el("span",undefined,"coverage-legend__item"),dot=el("i",undefined,"coverage-dot coverage-dot--"+status.toLowerCase());dot.setAttribute("aria-hidden","true");item.append(dot,document.createTextNode(statusLabels[status]));legend.append(item);}
    layout.append(calendar,detail);body.append(toolbar,summary,legend,hint,notice,layout);section.section.append(body);root.insertBefore(section.section,datasetsView);
    let data=null,selected="",sequence=0,busy=false,dayButtons=[];
    const dateLabel=value=>value.replace(/-/g," / ");
    const dayStatus=day=>data.view==="assessment"&&day.status==="PARTIAL"?"归档未齐":data.view==="assessment"&&day.status==="AVAILABLE"?"已归档":statusLabels[day.status] || day.status;
    const fullTime=value=>value?new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(value)):"—";
    function state(text,error=false){
      const box=el("div",undefined,"admin-state"+(error?" admin-state--error":" admin-state--loading")),mark=el("span",error?"!":"…",error?"admin-state__mark":"admin-state__pulse");box.setAttribute("role",error?"alert":"status");mark.setAttribute("aria-hidden","true");box.append(mark,el("strong",text));
      if(error){const retry=el("button","重试","ui-button admin-button admin-button--quiet");retry.type="button";retry.addEventListener("click",()=>load());box.append(retry);}notice.replaceChildren(box);notice.hidden=false;
    }
    function renderBusy(){reload.disabled=busy;reload.textContent=busy?"读取中…":"刷新日历";previous.disabled=month.value==="0002-01";next.disabled=month.value==="9998-12";calendar.setAttribute("aria-busy",String(busy));}
    function selectDay(value,focus=false){
      if(!data)return;const day=data.days.find(item=>item.date===value);if(!day)return;selected=value;
      for(const button of dayButtons){const active=button.dataset.date===value;button.classList.toggle("selected",active);button.setAttribute("aria-pressed",String(active));button.tabIndex=active?0:-1;if(active&&focus)button.focus();}
      const head=el("div",undefined,"coverage-detail__head"),title=el("h3",dateLabel(day.date)),status=el("span",dayStatus(day),"coverage-status coverage-status--"+day.status.toLowerCase());head.append(title,status);detail.replaceChildren(head);
      if(day.status==="UNKNOWN")detail.append(el("p","该日期的交易日历尚未确认，可先更新基础资料。","coverage-detail__note"));
      else if(day.status==="PENDING")detail.append(el("p","该日期尚未完成盘后更新。","coverage-detail__note"));
      else if(day.status==="CLOSED")detail.append(el("p","非交易日。当天已有的资料记录仍可在下方查看。","coverage-detail__note"));
      const list=el("ul",undefined,"coverage-records");
      const expectedAssessments=Number(day.datasets.find(item=>item.dataset==="daily")?.row_count || 0)*2;
      for(const item of day.datasets){
        const row=el("li",undefined,"coverage-record"),heading=el("div",undefined,"coverage-record__head"),copy=el("div"),count=el("strong",item.row_count==null?"—":integer(item.row_count),"coverage-record__count tabular");
        const partialArchive=item.dataset==="daily_assessment"&&item.row_count>0&&item.row_count<expectedAssessments;
        copy.append(el("strong",item.label),el("span",partialArchive?"归档未齐":item.dataset==="daily_assessment"&&item.status==="AVAILABLE"?"已归档":rowLabels[item.status] || item.status,"coverage-record__state coverage-record__state--"+(partialArchive?"partial":item.status.toLowerCase())));heading.append(copy,count);row.append(heading);
        if(item.dataset==="daily_assessment"&&item.row_count!=null)row.append(el("p","归档 "+integer(item.row_count)+" 条 · 可评分 "+(item.ready_count==null?"未统计":integer(item.ready_count)+" 条"),"coverage-record__assessment"));
        if(partialArchive)row.append(el("p","按当日日线计算应归档 "+integer(expectedAssessments)+" 条（每只股票 LEFT / RIGHT 各一条）。","coverage-record__time"));
        if(item.source_at){const time=el("time",fullTime(item.source_at));time.dateTime=item.source_at;const line=el("p","数据来源时间 ","coverage-record__time");line.append(time);row.append(line);}
        if(item.updated_at){const time=el("time",fullTime(item.updated_at));time.dateTime=item.updated_at;const line=el("p","覆盖记录更新 ","coverage-record__time");line.append(time);row.append(line);}
        list.append(row);
      }
      if(list.childElementCount)detail.append(list);else detail.append(el("p","该日期尚无覆盖记录。","coverage-detail__note"));
    }
    function render(){
      const counts=data.summary;summary.textContent="本月 · 有数据 "+integer(counts.available)+" 天 · 部分缺项 "+integer(counts.partial)+" 天 · 无数据 "+integer(counts.missing)+" 天";
      hint.textContent={daily:"有数据：日线、复权因子、每日指标和筹码均有入库记录。",weekly:"周线按每周最后一个交易日记录。",assessment:"评估明细分别列出归档条数和可评分条数。"}[data.view];
      calendar.replaceChildren();calendar.setAttribute("role","group");calendar.setAttribute("aria-label",data.month+" 数据覆盖日历");dayButtons=[];
      const grid=el("div",undefined,"coverage-grid");
      for(const label of ["一","二","三","四","五","六","日"])grid.append(el("span",label,"coverage-weekday"));
      const offset=(new Date(data.month+"-01T00:00:00Z").getUTCDay()+6)%7;
      for(let index=0;index<offset;index++){const blank=el("span",undefined,"coverage-blank");blank.setAttribute("aria-hidden","true");grid.append(blank);}
      for(const day of data.days){
        const button=el("button",undefined,"coverage-day coverage-day--"+day.status.toLowerCase());button.type="button";button.dataset.date=day.date;
        const label=dayStatus(day),archive=day.datasets.find(item=>item.dataset==="daily_assessment");
        const hasArchive=data.view==="assessment"&&archive?.row_count>0,countText=hasArchive?(archive.row_count<10000?integer(archive.row_count):new Intl.NumberFormat("zh-CN",{notation:"compact",maximumFractionDigits:1}).format(archive.row_count)):day.expected_count?day.available_count+" / "+day.expected_count+" 项":"";
        const exactCount=hasArchive?integer(archive.row_count)+" 条归档":countText;
        button.setAttribute("aria-label",day.date+"，"+label+(exactCount?"，"+exactCount:""));button.setAttribute("aria-controls","coverage-day-detail");
        button.append(el("strong",String(Number(day.date.slice(-2))),"coverage-day__number"),el("span",label,"coverage-day__state"));
        if(countText){const count=el("span",countText,"coverage-day__count");count.title=exactCount;button.append(count);}
        button.addEventListener("click",()=>selectDay(day.date));button.addEventListener("keydown",event=>{
          const index=dayButtons.indexOf(button),steps={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7};let target=index;
          if(event.key in steps)target=index+steps[event.key];else if(event.key==="Home")target=index-(index+offset)%7;else if(event.key==="End")target=index+6-(index+offset)%7;else return;
          event.preventDefault();selectDay(data.days[Math.max(0,Math.min(data.days.length-1,target))].date,true);
        });dayButtons.push(button);grid.append(button);
      }
      for(let index=(offset+data.days.length)%7;index>0&&index<7;index++){const blank=el("span",undefined,"coverage-blank");blank.setAttribute("aria-hidden","true");grid.append(blank);}
      calendar.append(grid);detail.id="coverage-day-detail";
      const filled=data.days.filter(day=>["AVAILABLE","PARTIAL"].includes(day.status)),closed=data.days.filter(day=>day.date<=data.latest_business_date);
      if(!data.days.some(day=>day.date===selected))selected=data.days.find(day=>day.date===data.latest_business_date)?.date || filled.at(-1)?.date || closed.at(-1)?.date || data.days[0]?.date;
      if(selected)selectDay(selected);layout.hidden=false;
    }
    async function load(initialMonth){
      if(!live())return;if(initialMonth&&!data&&sequence===0)month.value=initialMonth;
      if(!month.value||!month.checkValidity())return;
      const current=++sequence,requestedMonth=month.value,requestedView=view.value;busy=true;data=null;layout.hidden=true;summary.textContent=hint.textContent="";renderBusy();state("正在读取数据覆盖记录");
      try{
        const value=await api(apiRoot+"/data-coverage?"+new URLSearchParams({month:requestedMonth,view:requestedView}));
        if(!live()||sequence!==current)return;
        if(value.month!==requestedMonth||value.view!==requestedView||!Array.isArray(value.days)||!value.summary)throw new Error("日历数据格式无效，请重新读取。");
        data=value;notice.replaceChildren();notice.hidden=true;render();
      }catch(error){if(live()&&sequence===current){data=null;layout.hidden=true;state(error.message==="INVALID_COVERAGE_QUERY"?"请选择有效的日历月份。":error.message || "日历读取失败，请稍后重试。",true);}}
      finally{if(live()&&sequence===current){busy=false;renderBusy();}}
    }
    function shiftMonth(offset){const [year,number]=month.value.split("-").map(Number);if(!year||!number)return;const serial=year*12+number-1+offset;if(serial<24||serial>9998*12+11)return;month.value=String(Math.floor(serial/12)).padStart(4,"0")+"-"+String(serial%12+1).padStart(2,"0");selected="";load();}
    previous.addEventListener("click",()=>shiftMonth(-1));next.addEventListener("click",()=>shiftMonth(1));month.addEventListener("change",()=>{selected="";load();});view.addEventListener("change",()=>load());reload.addEventListener("click",()=>load());layout.hidden=true;notice.hidden=true;
    return {load};
  }

  function field(label,type,name){const wrapper=el("label",label,"admin-field"),input=el("input");input.type=type;input.name=name;input.required=true;input.value=model[name];if(name==="year"){input.min="2000";input.max=String(currentYear-1);}input.addEventListener("input",()=>{model[name]=input.value;});wrapper.append(input);return wrapper;}
  function renderForm(){
    for(const button of optionGrid.children){const selected=button.dataset.mode===model.mode;button.classList.toggle("selected",selected);button.setAttribute("aria-pressed",String(selected));}
    active.textContent=options.find(option=>option.value===model.mode).label;
    const children=[];
    if(["daily","weekly"].includes(model.mode))children.push(field("开始日期","date","date_from"),field("结束日期","date","date_to"),el("span","已完成交易日 · 最多31天","admin-pill"));
    if(model.mode==="monthly")children.push(field("更新月份","month","month"));
    if(model.mode==="yearly")children.push(field("财务报告年度","number","year"),el("span","含最新业务日策略重算","admin-pill"));
    if(model.mode==="basic")children.push(el("span","股票资料 · 交易日历 · 申万行业","admin-pill"));
    form.replaceChildren(...children,submit);renderBusy();
  }
  function renderBusy(){refresh.disabled=loading;refresh.textContent=loading?"读取中…":"刷新状态";submit.disabled=submitting||loading;submit.textContent=submitting?"正在提交…":"提交更新任务";}
  function showAsync(error=""){
    asyncState.replaceChildren();
    if(loading&&!snapshot){const box=el("div",undefined,"admin-state admin-state--loading"),pulse=el("span",undefined,"admin-state__pulse"),copy=el("div");pulse.setAttribute("aria-hidden","true");box.setAttribute("role","status");box.setAttribute("aria-live","polite");copy.append(el("strong","正在读取管理数据"),el("p","正在读取后端已落库结果，不会触发供应商采集。"));box.append(pulse,copy);asyncState.append(box);}
    else if(error){const box=el("div",undefined,"admin-state admin-state--error"),mark=el("span","!","admin-state__mark"),copy=el("div"),retry=el("button","重试","ui-button admin-button admin-button--quiet");box.setAttribute("role","alert");mark.setAttribute("aria-hidden","true");copy.append(el("strong","读取失败"),el("p",error));retry.type="button";retry.addEventListener("click",load);box.append(mark,copy,retry);asyncState.append(box);}
    asyncState.hidden=!asyncState.childElementCount;
  }
  function renderSnapshot(){
    datasetsView.replaceChildren();jobsView.replaceChildren();datasetsView.hidden=!snapshot;jobsView.hidden=!snapshot?.recent_jobs?.length;latest.hidden=!snapshot?.latest_business_date;
    if(snapshot?.latest_business_date)latest.replaceChildren(el("span",undefined,"admin-badge__dot"),document.createTextNode("最新业务日 "+snapshot.latest_business_date));
    if(!snapshot)return;
    const datasets=snapshot.datasets || [],section=card("数据集同步状态分布","区分已更新、建议更新、有效空记录与尚未更新；更新成功不代表全市场完整率。"),legend=el("div",undefined,"admin-legend");
    const segments=[
      {key:"ok",label:"已更新",tone:"ok",count:datasets.filter(item=>item.status==="OK").length},
      {key:"working",label:"执行中",tone:"info",count:datasets.filter(item=>["PENDING","RUNNING"].includes(item.status)).length},
      {key:"stale",label:"建议更新",tone:"warning",count:datasets.filter(item=>["STALE","EMPTY"].includes(item.status)).length},
      {key:"failed",label:"失败",tone:"danger",count:datasets.filter(item=>item.status==="FAILED").length},
      {key:"unsynced",label:"尚未更新",tone:"unknown",count:datasets.filter(item=>item.status==="NOT_SYNCED").length},
    ].filter(segment=>segment.count>0),total=segments.reduce((sum,segment)=>sum+segment.count,0);
    for(const segment of segments){const item=el("span",undefined,"admin-legend__item");item.append(el("i",undefined,"admin-legend__dot admin-stack__tone--"+segment.tone),document.createTextNode(segment.label+" "+segment.count));legend.append(item);}section.head.append(legend);
    if(segments.length){const body=el("div",undefined,"admin-card__body"),stack=el("div",undefined,"admin-stack");body.style.paddingBottom="0";stack.setAttribute("aria-label","数据集同步状态占比");for(const segment of segments){const part=el("span",undefined,"admin-stack__tone--"+segment.tone);part.style.width=(Math.round(segment.count/total*1000)/10)+"%";stack.append(part);}body.append(stack);section.section.append(body);}
    const maxRows=Math.max(1,...datasets.map(item=>Number(item.row_count)||0));
    const rows=datasets.map(item=>{
      const state=el("div");state.append(badge(item.status));if(item.error_code)state.append(el("span",item.error_code,"admin-table__secondary"));
      const range=primary(item.business_date || "尚未更新");range.classList.add("tabular");
      if(item.details?.coverage_end)range.append(el("span","日历至 "+item.details.coverage_end,"admin-table__secondary"));
      if(item.details?.range_start)range.append(el("span",item.details.range_start+" → "+item.details.range_end,"admin-table__secondary"));
      const count=primary(item.row_count==null?"尚无成功记录":integer(item.row_count));count.classList.add("update-row-count");count.firstElementChild.classList.add("tabular");
      if(item.row_count!=null){const meter=el("div",undefined,"admin-meter admin-meter--"+tone(item.status)),bar=el("span"),number=Number(item.row_count);bar.style.width=(number>0?Math.max(2,Math.min(100,Math.round(number/maxRows*1000)/10)):0)+"%";meter.append(bar);count.append(meter);}
      return [primary(item.label,item.cadence),state,range,el("span",formatTime(item.last_success_at,"尚无成功记录"),"tabular"),count];
    });
    section.section.append(table(["数据集 / 周期","状态","业务日期 / 覆盖范围","最近成功","本次返回行数与量级"],rows));datasetsView.append(section.section);
    if(snapshot.recent_jobs?.length){
      const jobs=card("最近手动任务","提交、执行进度与完成结果。"),all=el("a","全部采集与任务 →","ui-button admin-button admin-button--quiet");all.href="#collections";all.addEventListener("click",event=>{event.preventDefault();navigate("collections");});jobs.head.append(all);
      const jobRows=snapshot.recent_jobs.map(job=>[el("strong",job.dataset || (job.task==="daily_assessment"?(job.mode==="yearly"?"年更：最新业务日策略":"盘后策略评估"):options.find(option=>option.value===job.mode)?.label || job.mode)),badge(job.status),el("span",job.business_date,"tabular"),el("span",formatTime(job.created_at)+" → "+formatTime(job.finished_at,"尚未结束"),"admin-table__primary tabular"),job.status==="SUCCEEDED"?"无当前错误":job.error_code || "—"]);
      jobs.section.append(table(["任务","状态","业务日期","提交 → 完成时间轴","错误"],jobRows));jobsView.append(jobs.section);
    }
  }
  async function load(){
    if(!live())return;const current=++request;loading=true;renderBusy();showAsync();
    try{const value=await api(apiRoot+"/data-updates");if(!live()||request!==current)return;snapshot=value;if(!model.date_from&&snapshot.latest_business_date){model.date_from=model.date_to=snapshot.latest_business_date;model.month=model.date_from.slice(0,7);renderForm();}renderSnapshot();loading=false;showAsync();}
    catch(error){if(!live()||request!==current)return;snapshot=null;loading=false;renderSnapshot();showAsync(error.message || "更新状态读取失败。");}
    finally{if(live()&&request===current){loading=false;renderBusy();}}
  }
  refresh.addEventListener("click",()=>Promise.all([load(),coverage.load()]));
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(submitting||loading||!live()||!form.reportValidity())return;
    const payload={mode:model.mode};
    if(["daily","weekly"].includes(model.mode)){payload.date_from=model.date_from;payload.date_to=model.date_to;}
    if(model.mode==="monthly")payload.month=model.month;
    if(model.mode==="yearly")payload.year=Number(model.year);
    const fingerprint=JSON.stringify(payload);if(attempt?.fingerprint!==fingerprint)attempt={fingerprint,request_id:newRequestId()};
    submitting=true;message.hidden=submitError.hidden=true;renderBusy();
    try{const result=await api(apiRoot+"/data-updates","POST",{...payload,request_id:attempt.request_id});if(!live())return;attempt=null;message.textContent="已提交 "+result.job_count+" 个任务，请稍后刷新查看执行结果。";message.hidden=false;await Promise.all([load(),coverage.load()]);}
    catch(error){if(!live())return;submitError.textContent=errors[error.message] || error.message || "任务提交失败，请稍后重试。";submitError.hidden=false;}
    finally{submitting=false;if(live())renderBusy();}
  });
  renderForm();await load();await coverage.load(snapshot?.latest_business_date?.slice(0,7));
};

window.renderDataUpdateJobs = async function ({screen, api, apiRoot, isCurrent, navigate}) {
  const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;};
  const root=el("div",undefined,"admin-page data-updates-page data-update-jobs-page"),header=el("header",undefined,"admin-page__header"),intro=el("div"),actions=el("div",undefined,"admin-page__actions");
  intro.append(el("p","TUSHARE / BATCH UPDATES","admin-page__eyebrow"),el("h1","全部采集与任务"),el("p","数据更新任务"));
  const back=el("button","数据更新与补采","ui-button admin-button admin-button--quiet"),refresh=el("button","刷新状态","ui-button admin-button");back.type=refresh.type="button";back.addEventListener("click",()=>navigate("data-updates"));actions.append(back,refresh);header.append(intro,actions);
  const section=el("section",undefined,"admin-card"),head=el("div",undefined,"admin-card__header"),introJobs=el("div"),content=el("div"),notice=el("div");introJobs.append(el("h2","数据更新任务"),el("p","提交、执行进度与完成结果。"));head.append(introJobs);section.append(head,notice,content);
  const pager=el("div",undefined,"jobs-pagination"),pageLabel=el("span",undefined,"tabular"),previous=el("button","上一页","ui-button admin-button admin-button--quiet"),next=el("button","下一页","ui-button admin-button admin-button--quiet");previous.type=next.type="button";pager.append(pageLabel,previous,next);section.append(pager);root.append(header,section);screen.replaceChildren(root);
  const modes={daily:"日线与指标",weekly:"周线补采",basic:"基础资料",monthly:"月更",yearly:"年更"},labels={PENDING:"排队中",RUNNING:"执行中",SUCCEEDED:"已完成",FAILED:"失败",CANCELLED:"已取消"};
  const formatTime=(value,empty="未记录")=>{if(!value)return empty;const date=new Date(value);return Number.isNaN(date.getTime())?String(value):new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false,timeZone:"Asia/Shanghai"}).format(date);};
  const live=()=>root.isConnected && isCurrent();
  let page=1,pages=1,request=0;
  function state(text,error=false){const box=el("div",undefined,"admin-state"+(error?" admin-state--error":""));box.setAttribute("role",error?"alert":"status");box.append(el("span",error?"!":"—","admin-state__mark"),el("strong",text));if(error){const retry=el("button","重试","ui-button admin-button admin-button--quiet");retry.type="button";retry.addEventListener("click",load);box.append(retry);}notice.replaceChildren(box);}
  async function load(){
    if(!live())return;const current=++request;refresh.disabled=previous.disabled=next.disabled=true;refresh.textContent="读取中…";state("正在读取管理数据");
    try{
      const data=await api(apiRoot+"/data-update-jobs?"+new URLSearchParams({page:String(page),page_size:"50"}));if(!live()||current!==request)return;
      page=data.page;pages=Math.max(1,data.pages);content.replaceChildren();notice.replaceChildren();
      if(data.items.length){const wrap=el("div",undefined,"admin-table-wrap"),table=el("table",undefined,"admin-table"),thead=el("thead"),tr=el("tr"),tbody=el("tbody");for(const title of ["任务","状态","业务日期","提交 → 完成时间轴","错误"])tr.append(el("th",title));thead.append(tr);
        for(const job of data.items){const row=el("tr"),status=el("span",undefined,"admin-badge admin-badge--"+(job.status==="SUCCEEDED"?"ok":job.status==="FAILED"?"danger":["RUNNING","PENDING"].includes(job.status)?"warning":"unknown"));status.append(el("span",undefined,"admin-badge__dot"),document.createTextNode(labels[job.status] || job.status));
          for(const value of [el("strong",job.dataset || (job.task==="daily_assessment"?(job.mode==="yearly"?"年更：最新业务日策略":"盘后策略评估"):modes[job.mode] || job.mode)),status,el("span",job.business_date,"tabular"),el("span",formatTime(job.created_at)+" → "+formatTime(job.finished_at,"尚未结束"),"admin-table__primary tabular"),job.status==="SUCCEEDED"?"无当前错误":job.error_code || "—"]){const cell=el("td");if(value instanceof Node)cell.append(value);else cell.textContent=value;row.append(cell);}tbody.append(row);}
        table.append(thead,tbody);wrap.append(table);content.append(wrap);
      }else state("暂无记录");
      pageLabel.textContent="共 "+data.total+" 项 · 第 "+page+" / "+pages+" 页";previous.disabled=page<=1;next.disabled=page>=pages;
    }catch(error){if(!live()||current!==request)return;content.replaceChildren();pageLabel.textContent="";state(error.message || "管理数据读取失败，请稍后重试。",true);}
    finally{if(live()&&current===request){refresh.disabled=false;refresh.textContent="刷新状态";}}
  }
  refresh.addEventListener("click",load);previous.addEventListener("click",()=>{page=Math.max(1,page-1);load();});next.addEventListener("click",()=>{page++;load();});await load();
};
