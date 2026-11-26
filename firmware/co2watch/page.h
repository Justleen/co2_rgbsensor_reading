// GENERATED from page.html by firmware/gen_page.py. Do not edit.
#pragma once
static const char PAGE[] PROGMEM = R"HTML(<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>co2watch</title><style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--bd:#ddd}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--fg:#eee;--mut:#9a9a94;--card:#222220;--bd:#3a3a37}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:480px;margin:0 auto;padding:16px}
section{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:12px}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:15px;margin:0 0 10px}
#sw{flex:1 1 40px;min-width:40px;height:28px;border-radius:6px;border:1px solid var(--bd);background:#000}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.big{font-size:22px;font-variant-numeric:tabular-nums}
small,.mut{color:var(--mut)}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--bd);background:var(--bg);color:var(--fg);cursor:pointer;transition:transform .08s,filter .08s,background .2s}
button:hover{filter:brightness(.95)}button:active{transform:scale(.94);filter:brightness(.8)}button:disabled{opacity:.6;cursor:wait}
.b3{flex-wrap:nowrap;gap:6px;align-items:stretch}.ends{flex-wrap:nowrap}.ends label{display:flex;align-items:center;gap:6px;flex:1 1 0;min-width:0}.ends input{flex:1 1 0;min-width:0;width:100%;font:inherit;padding:5px 6px}.b3 button{flex:1 1 0;min-width:0;padding:6px 4px;font-size:13px;line-height:1.2}
.rl{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.rl label{display:flex;align-items:center;gap:8px;cursor:pointer}
.sw{position:relative;width:46px;height:26px;flex:none}.sw input{opacity:0;width:0;height:0;position:absolute}
.sw i{position:absolute;inset:0;border-radius:13px;background:var(--bd);transition:background .25s}
.sw i:before{content:'';position:absolute;left:3px;top:3px;width:20px;height:20px;border-radius:50%;background:#fff;transition:transform .25s;box-shadow:0 1px 2px rgba(0,0,0,.3)}
.sw input:checked+i{background:#2e8b57}.sw input:checked+i:before{transform:translateX(20px)}
.sw input:disabled+i{opacity:.6}.badge{font-size:11px;font-weight:normal;padding:1px 6px;border-radius:9px;background:#e0a020;color:#000;margin-left:6px}
#toast{position:fixed;top:10px;left:50%;transform:translate(-50%,-8px);max-width:90vw;padding:7px 14px;border-radius:8px;font-size:14px;pointer-events:none;opacity:0;transition:opacity .3s,transform .3s;z-index:9;white-space:pre-line;text-align:center}
#toast.show{opacity:1;transform:translate(-50%,0)}#toast.ok{background:#dcefdc;color:#1f5223;border:1px solid #a9d3a9}#toast.err{background:#f6dcdc;color:#7a1f1f;border:1px solid #e0a9a9}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{text-align:right;padding:2px 6px}th:first-child,td:first-child{text-align:left}
</style></head><body><div id="toast" role="status" aria-live="polite"></div><main>
<h1>co2watch <small id="ver"></small></h1>
<section><h2>Drop checker colour</h2>
<div class="row" style="margin-top:10px"><span class="big" id="hex">-</span><span id="sw"></span></div>
<small id="info">loading...</small>
<div id="stale" style="display:none;margin-top:6px;padding:6px 10px;border-radius:6px;background:#f6dcdc;color:#7a1f1f;font-size:13px"></div>
<div class="row b3" style="margin-top:10px"><button onclick="act('/read')">Take reading now</button>
<button onclick="if(confirm('Use the current reading as white? Do this with the sensor on the white backing.'))act('/white?set=1')">Use as white reference</button>
<button onclick="act('/white?reset=1')">Reset white</button></div>
<div id="idxbox" style="display:none;margin-top:14px;border-top:1px solid var(--bd);padding-top:10px">
<div class="row"><b>CO2 index</b><small id="idxwarn" style="color:#c62828"></small></div>
<div style="position:relative;height:16px;margin:4px 0 0"><small class="mut" id="idxv" style="position:absolute;bottom:0;left:0;white-space:nowrap">-</small></div>
<div style="position:relative;height:16px;border-radius:8px;margin:4px 0 6px;background:linear-gradient(90deg,#2f6fd6,#2bb3a0,#3cb043,#c8d630,#e8d020)">
<div id="idxm" style="position:absolute;top:-3px;width:4px;height:22px;margin-left:-2px;background:var(--fg);border-radius:2px;left:0"></div></div>
<div class="row mut" style="justify-content:space-between;font-size:12px"><span>0%</span><span>50%</span><span>100%</span></div>
<div class="row ends" style="margin-top:8px"><label>Low <input type="number" id="idxlo" step="0.1"></label>
<label>High <input type="number" id="idxhi" step="0.1"></label><button onclick="idxSet()">Set</button></div>
<small class="mut" id="idxcur"></small>
<canvas id="hist" height="80" style="width:100%;margin-top:8px"></canvas><small class="mut" id="histinfo"></small></div>
</section>
<section><h2>Light</h2>
<div class="row"><button onclick="act('/dimmer?level=100')">Set to max</button><button onclick="sch('resume=1')">Run schedule</button></div>
<small id="dinfo"></small></section>
<section><h2>Light schedule</h2>
<div class="row">
<span class="mut" id="clk"></span></div>
<canvas id="cv" height="150" style="width:100%;margin-top:8px"></canvas>
<div id="dragHint" style="display:none"><div class="row" style="margin-top:6px"><label>Hours at 100 %
<input type="number" id="maxh" min="0.5" max="16" step="0.5" value="7" style="width:4.5em"></label></div></div>
<table id="tb" style="margin-top:6px"><tr><th>#</th><th>time</th><th>level %</th><th>then</th></tr></table>
<div class="row" style="margin-top:10px"><button onclick="saveSch()">Save schedule</button>
<button onclick="if(confirm('Reset the schedule to the default sunrise/sunset?'))sch('reset=1')">Reset to default</button></div></section>
</main><script>
const $=id=>document.getElementById(id);let busy=0,S=null,schDirty=0,nowMin=null,lastBtn=null;
document.addEventListener('click',e=>{lastBtn=e.target.closest('button')},true);
function btnStart(){const b=lastBtn;lastBtn=null;if(!b)return null;b.dataset.t=b.dataset.t||b.textContent;b.disabled=true;b.textContent='working…';return b}
function btnEnd(b){if(!b)return;b.disabled=false;b.textContent=b.dataset.t}
let toastT;function toast(t,ok){const e=$('toast');e.textContent=t;e.className=(ok?'ok':'err')+' show';clearTimeout(toastT);toastT=setTimeout(()=>e.classList.remove('show'),ok?2500:5000)}
function bad(t){toast('Error! Bad!\n'+t,false)}
function done(b){if(b)toast(b.dataset.t=='Take reading now'?'Reading taken!':'Saved!',true)}
const RN=['smooth','linear','step'];
const hm=m=>String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0');
function lvAt(p,m){let i=7;for(let k=0;k<8;k++)if(p[k][0]<=m)i=k;const j=(i+1)%8;
 let sp=p[j][0]-p[i][0];if(sp<=0)sp+=1440;let d=m-p[i][0];if(d<0)d+=1440;let f=Math.min(d/sp,1);
 const a=p[i][1],b=p[j][1];if(p[i][2]==2)return a;if(p[i][2]==0)f=(1-Math.cos(Math.PI*f))/2;return a+(b-a)*f}
function readTb(){const p=[];for(let k=0;k<8;k++){const v=$('t'+k).value.trim(),t=/^([01]?[0-9]|2[0-3]):([0-5][0-9])$/.exec(v);
 if(!t)throw 'Point '+(k+1)+': time "'+v+'" must be 24-hour HH:MM';
 p.push([(+t[1])*60+(+t[2]),Math.max(0,Math.min(100,+$('l'+k).value||0)),+$('r'+k).value])}return p}
// Relay rules derived from the 100 % bar of the light schedule (mirrored in the emulator):
// CO2 on 2 h before the bar starts, off 2 h before the bar ends.
// Air off from when CO2 comes on until the bar ends (light drops from 100 %), on otherwise.
// Air wins: CO2 is never on while air is on. Without a 100 % bar: CO2 off, air on.
const inWin=(m,a,b)=>a<=b?(m>=a&&m<b):(m>=a||m<b);
function rules(p){const pl=plateauIdx(p);
 if(pl<0)return{co2on:-1,co2off:-1,co2:m=>false,air:m=>true};
 const st=p[pl][0],en=p[pl+1][0],on=(st-120+1440)%1440,off=(en-120+1440)%1440;
 return{co2on:on,co2off:off,co2:m=>inWin(m,on,off),air:m=>!inWin(m,on,en)}}
let hasRelays=0,dragOn=0;
// --- Drag-to-edit (firmware >= 0.5.0 sends drag:true; the emulator sends preview) ---------------------------------
// Time snaps to 5 min, level to 1 %. Points stay in time order. The 100 % plateau
// (first point at 100 % and the next one) is a bar of exactly maxh hours: drag it
// anywhere along its length to move the whole day (all points keep their relative
// times). Other points drag one by one; before the bar they are capped at 99 %,
// after it at 40 %.
const AFTER_CAP=40;
const maxMin=()=>Math.round(Math.max(0.5,Math.min(16,+$('maxh').value||7))*60);
function constrain(p){const pl=plateauIdx(p),mx=maxMin();let changed=false;
 if(pl<0)return false;
 const d=p[pl][0]+mx-p[pl+1][0];               // fixed length: resize, evening keeps its fade durations
 if(d){for(let k=pl+1;k<8;k++)p[k][0]+=d;changed=true}
 const over=p[7][0]-1435;if(over>0){for(const q of p)q[0]-=over}  // keep the day inside 00:00-23:55
 const under=-p[0][0];if(under>0){for(const q of p)q[0]+=under}
 p.forEach((q,k)=>{const cap=k<pl?99:k>pl+1?AFTER_CAP:100;if(q[1]>cap){q[1]=cap;changed=true}});if(fixSteps(p))changed=true;return changed}
function writeTb(p){p.forEach((q,k)=>{$('t'+k).value=hm(q[0]);$('l'+k).value=q[1]});fixSteps(p)}
// Only switching on from 0 % or off to 0 % may be instant (step); every other change fades.
function fixSteps(p){let ch=false;p.forEach((q,k)=>{const n=p[(k+1)%8],ok=q[1]==0||n[1]==0,sel=$('r'+k);
  if(q[2]==2&&!ok){q[2]=0;ch=true}if(sel){sel.options[2].disabled=!ok;sel.value=q[2]}});return ch}
function plateauIdx(p){for(let k=0;k<7;k++)if(p[k][1]>=100&&p[k+1][1]>=100)return k;return -1}
// Schedule graph scale, shared by draw() and the drag handling (graph area 150 px high).
const GH=150,gX=(w,m)=>m/1440*(w-30)+25,gY=v=>GH-18-v/100*(GH-28);
function fixTb(){try{fixSteps(readTb())}catch(e){}}
function initDrag(){dragOn=1;const c=$('cv');c.style.touchAction='none';$('dragHint').style.display='';let drag=-1;
 if(S&&S.maxh)$('maxh').value=S.maxh;
 if(S)fixTb();
 $('maxh').oninput=()=>{let p;try{p=readTb()}catch(e){return}constrain(p);writeTb(p);schDirty=1;draw()};
 const geo=()=>{const w=c.clientWidth,r=c.getBoundingClientRect();return{r,X:m=>gX(w,m),Y:gY,
  iX:x=>(x-25)/(w-30)*1440,iY:y=>(GH-18-y)/(GH-28)*100}};
 const pts=()=>{try{return readTb()}catch(e){return S.pts.map(q=>q.slice())}};
 let grab=0,orig=null;
 const hit=e=>{const g=geo(),x=e.clientX-g.r.left,y=e.clientY-g.r.top,p=pts(),pl=plateauIdx(p);
  if(pl>=0&&Math.abs(y-g.Y(100))<10&&x>=g.X(p[pl][0])-10&&x<=g.X(p[pl+1][0])+10)return 'bar';
  let best=-1,bd=14;p.forEach((q,k)=>{if(pl>=0&&(k==pl||k==pl+1))return;const d=Math.hypot(g.X(q[0])-x,g.Y(q[1])-y);if(d<bd){bd=d;best=k}});return best};
 c.addEventListener('pointerdown',e=>{drag=hit(e);if(drag==='bar'){const g=geo();grab=g.iX(e.clientX-g.r.left);orig=pts()}
  if(drag==='bar'||drag>=0){c.setPointerCapture(e.pointerId);e.preventDefault()}else drag=-1});
 c.addEventListener('pointermove',e=>{if(drag===-1){const h=hit(e);c.style.cursor=h==='bar'?'ew-resize':h>=0?'grab':'';return}c.style.cursor=drag==='bar'?'ew-resize':'grabbing';
  if(drag==='bar'){const g=geo();let d=Math.round((g.iX(e.clientX-g.r.left)-grab)/5)*5;
   d=Math.max(-orig[0][0],Math.min(1435-orig[7][0],d));const p=orig.map(q=>[q[0]+d,q[1],q[2]]);writeTb(p);schDirty=1;draw();return}
  const g=geo(),p=pts(),pl=plateauIdx(p),snap=v=>Math.round(v/5)*5;
  let m=snap(g.iX(e.clientX-g.r.left)),v=Math.round(g.iY(e.clientY-g.r.top));
  const lo=k=>k>0?p[k-1][0]+5:0,hi=k=>k<7?p[k+1][0]-5:1435;
  {m=Math.max(lo(drag),Math.min(hi(drag),m));const cap=pl<0?100:drag<pl?99:AFTER_CAP;v=Math.max(0,Math.min(cap,v));p[drag]=[m,v,p[drag][2]]}
  writeTb(p);schDirty=1;draw()});
 const end=()=>{drag=-1;c.style.cursor=''};c.addEventListener('pointerup',end);c.addEventListener('pointercancel',end)}
function draw(){const c=$('cv'),w=c.clientWidth,h=GH,H=192,x=c.getContext('2d');c.width=w;c.height=H;
 let p=S.pts;if(schDirty)try{p=readTb().sort((a,b)=>a[0]-b[0])}catch(e){}
 const cs=getComputedStyle(document.body),fg=cs.color,X=m=>gX(w,m),Y=gY;
 x.clearRect(0,0,w,h);x.font='10px system-ui';x.fillStyle=fg;x.strokeStyle='rgba(128,128,128,.3)';
 for(let hh=0;hh<=24;hh+=3){x.beginPath();x.moveTo(X(hh*60),Y(0));x.lineTo(X(hh*60),Y(100));x.stroke();x.fillText(hh,X(hh*60)-4,h-4)}
 for(const v of[0,50,100]){x.fillText(v,0,Y(v)+3)}
 x.strokeStyle='#e0a020';x.lineWidth=2;x.beginPath();for(let m=0;m<=1440;m+=5){const v=lvAt(p,m%1440);m?x.lineTo(X(m),Y(v)):x.moveTo(X(m),Y(v))}x.stroke();
 x.fillStyle='#e0a020';for(const q of p){x.beginPath();x.arc(X(q[0]),Y(q[1]),3,0,7);x.fill()}
 if(dragOn){const pl=plateauIdx(p);if(pl>=0){const xa=X(p[pl][0]),xb=X(p[pl+1][0]);x.strokeStyle='#e0a020';x.lineWidth=8;x.lineCap='round';
  x.beginPath();x.moveTo(xa,Y(100));x.lineTo(xb,Y(100));x.stroke();x.lineWidth=1;x.lineCap='butt';x.fillStyle='#000';x.font='bold 10px system-ui';
  x.fillText('⟷ '+(maxMin()/60)+' h',(xa+xb)/2-16,Y(100)+3.5);x.font='10px system-ui'}}
 {const R=rules(p),lanes=[['CO2','#2e8b57',m=>R.co2(m)&&!R.air(m),158],['Air','#3b7dd8',R.air,176]];
  for(const[nm,col,f,y]of lanes){x.fillStyle='rgba(128,128,128,.15)';x.fillRect(X(0),y,X(1440)-X(0),12);x.fillStyle=col;
   let st=null;for(let m=0;m<=1440;m++){const on=m<1440&&f(m);if(on&&st==null)st=m;if(!on&&st!=null){x.fillRect(X(st),y,X(m)-X(st),12);st=null}}
   x.fillStyle=fg;x.fillText(nm,0,y+10)}}
 if(nowMin!=null){x.strokeStyle='#d33';x.lineWidth=1;x.beginPath();x.moveTo(X(nowMin),Y(100));x.lineTo(X(nowMin),188);x.stroke()}}
function fillTb(){const tb=$('tb');while(tb.rows.length>1)tb.deleteRow(1);
 S.pts.forEach((q,k)=>{const r=tb.insertRow();r.innerHTML='<td>'+(k+1)+'</td><td><input type="text" id="t'+k+'" value="'+hm(q[0])+'" maxlength="5" inputmode="numeric" placeholder="HH:MM" style="width:4.5em"></td><td><input type="number" id="l'+k+'" min="0" max="100" style="width:4em" value="'+q[1]+'"></td><td><select id="r'+k+'">'+RN.map((n,i)=>'<option value="'+i+'"'+(i==q[2]?' selected':'')+'>'+n+'</option>').join('')+'</select></td>'});
 tb.oninput=tb.onchange=()=>{schDirty=1;if(dragOn)fixTb();draw()};if(S.maxh)$('maxh').value=S.maxh;if(dragOn)fixTb();schDirty=0;draw()}
async function loadSch(){S=await (await fetch('/api/schedule')).json();fillTb()}
async function sch(a){busy=1;const b=btnStart();let ok=false;try{const r=await fetch('/api/schedule?'+a,{method:'POST'});if(!r.ok)bad(await r.text());else{S=await r.json();fillTb();ok=true}}catch(e){bad('No answer from the board')}finally{busy=0;btnEnd(b);if(ok)done(b);load()}}
function saveSch(){let p;try{p=readTb().sort((a,b)=>a[0]-b[0])}catch(e){bad(e);return}
 let extra='';if(dragOn){if(constrain(p)){writeTb(p);draw()}extra='&maxh='+(maxMin()/60)}for(let k=1;k<8;k++)if(p[k][0]<=p[k-1][0]){bad('Two points have the same time');return}
 sch('pts='+encodeURIComponent(p.map(q=>q.join(',')).join(';'))+extra)}
const RL={co2:'CO2',air:'Air',heat:'Heat',pump:'Pump'},rlBusy={};
function showRelays(r,src){const sec=$('relays');hasRelays=r?1:0;if(!sec||!r){if(sec)sec.style.display='none';return}sec.style.display='';src=src||{};const box=$('rl');
 for(const k in RL){if(!(k in r))continue;let c=$('rl_'+k);
  if(!c){const l=document.createElement('label');l.innerHTML='<span class="sw"><input type="checkbox" id="rl_'+k+'"><i></i></span>'+RL[k]+' <small class="mut" id="rs_'+k+'"></small>';box.appendChild(l);c=$('rl_'+k);c.onchange=()=>setRelay(k,c.checked)}
  if(!rlBusy[k])c.checked=!!r[k];$('rs_'+k).textContent={schedule:'auto',override:'manual 1 h',safe:'safe',interlock:'off: air is on'}[src[k]]||''}}
async function setRelay(k,on){const c=$('rl_'+k);rlBusy[k]=1;c.disabled=true;
 try{const r=await fetch('/relay?name='+k+'&on='+(on?1:0),{method:'POST'});if(!r.ok){bad(await r.text());c.checked=!on}else toast(RL[k]+(on?' on':' off'),true)}
 catch(e){bad('No answer from the board');c.checked=!on}finally{rlBusy[k]=0;c.disabled=false}}
function showIdx(x){const box=$('idxbox');if(!x){box.style.display='none';return}box.style.display='';
 {const e=$('idxv'),p=Math.max(0,Math.min(100,x.ok?x.value:0));e.textContent=x.ok?x.value.toFixed(1)+' %':'-';e.style.left=p+'%';e.style.transform='translateX('+(p<8?'0':p>92?'-100%':'-50%')+')'}$('idxwarn').textContent=x.ok&&(x.value<0||x.value>100)?'outside the range':'';$('idxm').style.left=Math.max(0,Math.min(100,x.ok?x.value:0))+'%';
 $('idxcur').textContent=x.ok?'Current: '+x.hue.toFixed(1)+'° hue':'Current: waiting for a reading';
 for(const[k,v]of[['idxlo',x.low],['idxhi',x.high]]){const e=$(k);if(document.activeElement!==e&&e.dataset.v!=String(v)){e.value=v.toFixed(1);e.dataset.v=v}}
}
function idxSet(){act('/api/co2index?low='+encodeURIComponent($('idxlo').value)+'&high='+encodeURIComponent($('idxhi').value))}
function show(s){
 showIdx(s.co2idx);
 showRelays(s.relays,s.relay_src);
 if((s.preview||s.drag)&&!dragOn)initDrag();
 $('ver').textContent='v'+s.version;
 $('sw').style.background=s.colour.ok?s.colour.hex:'repeating-linear-gradient(45deg,#888 0 8px,#aaa 8px 16px)';
 $('hex').textContent=s.colour.ok?s.colour.hex:'no colour';
 {const w=staleMsg(s),e=$('stale');e.textContent=w;e.style.display=w?'':'none'}
 $('info').textContent=s.seq?('reading #'+s.seq+', '+s.age+' s ago'+(s.saturated?', SATURATED':'')+', i2c errors '+s.i2c_errors):'no reading yet';

 const src={manual:'manual',schedule:'following the schedule',override:'max, schedule resumes in '+Math.ceil(s.dim.override_left/60)+' min',safe:'no time yet: safe level'}[s.dim.source];
 $('dinfo').textContent='Current output: '+s.dim.level.toFixed(1)+' %, '+src;
 $('clk').textContent=s.time_valid?s.time+' (Amsterdam)':'waiting for NTP time';
 nowMin=s.time_valid?s.minute:null;if(S)draw();
}
// CO2 index history (last 12 h), read by the browser straight from VictoriaMetrics
// (VM allows CORS), so the D1 does no extra work.
const VM='http://monitoring.fritz.box',HIST_H=12;
async function loadHist(){const c=$('hist');if(!c)return;const end=Math.floor(Date.now()/1000),start=end-HIST_H*3600;
 try{const r=await fetch(VM+'/api/v1/query_range?query=co2watch_co2_index_percent&start='+start+'&end='+end+'&step=300');
  const j=await r.json(),v=(j.data.result[0]||{values:[]}).values.map(q=>[+q[0],+q[1]]);drawHist(v,start,end);
  $('histinfo').textContent=v.length?'':'no CO2 index data in the last 12 h'}
 catch(e){drawHist([],start,end);$('histinfo').textContent='history unavailable (VictoriaMetrics at '+VM+' not reachable)'}}
function drawHist(v,start,end){const c=$('hist'),w=c.clientWidth,h=80,x=c.getContext('2d');c.width=w;c.height=h;
 const fg=getComputedStyle(document.body).color,X=t=>(t-start)/(end-start)*(w-30)+25,Y=p=>h-14-Math.max(0,Math.min(100,p))/100*(h-22);
 x.clearRect(0,0,w,h);x.font='10px system-ui';x.fillStyle=fg;x.strokeStyle='rgba(128,128,128,.3)';x.lineWidth=1;
 for(const p of[0,50,100]){x.beginPath();x.moveTo(X(start),Y(p));x.lineTo(X(end),Y(p));x.stroke();x.fillText(p,0,Y(p)+3)}
 for(let t=Math.ceil(start/10800)*10800;t<=end;t+=10800){const d=new Date(t*1000);x.fillText(String(d.getHours()).padStart(2,'0')+':00',X(t)-12,h-2)}
 x.strokeStyle='#3cb043';x.lineWidth=2;x.beginPath();let prev=null;
 for(const[t,p]of v){if(prev==null||t-prev>900)x.moveTo(X(t),Y(p));else x.lineTo(X(t),Y(p));prev=t}x.stroke()}
// Stale-data warning: the last reading failed (sensor not answering) or no reading for > 11 min (readings come every 5 min).
function staleMsg(s){if(!s.seq)return '';if(!s.colour.ok)return 'Sensor not answering: the last reading failed ('+s.i2c_errors+' I2C errors). No fresh colour data; check the sensor wiring.';
 if(s.age>660)return 'Data is stale: the last reading was '+Math.round(s.age/60)+' min ago.';return ''}
async function load(){if(busy)return;try{show(await (await fetch('/api/state')).json())}catch(e){$('info').textContent='offline?';const st=$('stale');st.textContent='The board is not answering: the values on this page are stale.';st.style.display=''}}
async function act(u){busy=1;const b=btnStart();let ok=false;try{const r=await fetch(u,{method:'POST'});if(!r.ok)bad(await r.text());else ok=true}catch(e){bad('No answer from the board')}finally{busy=0;btnEnd(b);if(ok)done(b);load()}}
load();loadSch();loadHist();setInterval(load,5000);setInterval(loadHist,300000);
</script></body></html>)HTML";
