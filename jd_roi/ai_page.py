# -*- coding: utf-8 -*-
"""独立 AI 分析台页面：左侧=每日详细 AI 报告，右侧=可追问的对话框。

三点体验（用户明确要求）：
  1. **思考过程流式展示**：推理型模型会同时吐 `reasoning_content`（思考）与 `content`（回答），
     页面把「思考过程」放进可折叠块里实时滚动，正式回答边生成边显示，用户不空等。
  2. **离开页面不打断**：生成跑在服务端后台线程（`jd_roi/aijobs.py`），
     浏览器只是「挂」在事件流上；切走/刷新/断网都不影响生成。
  3. **回来能续看**：job 事件留在服务端，页面重新挂载即可回放（含已生成的思考与回答）。

页面自包含（内联 CSS/JS），报告区复用 report2._ai_block，保证与 /report 口径一致。
"""
from __future__ import annotations

import json

from . import report2

_AI = report2._e


AI_PAGE_CSS = report2.CSS + report2.UI_CSS + report2.AI_CSS + """
.aitop{background:linear-gradient(120deg,#0f172a,#1e3a8a);color:#fff;padding:22px 0 18px;margin-bottom:16px}
.aitop h1{margin:0 0 6px;font-size:22px}
.aitop .metaline{color:#c7d2fe;font-size:13px}
.aitop .navlinks{margin-top:10px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.aitop .navlinks a{color:#dbeafe;font-size:13px;text-decoration:none;border-bottom:1px dashed rgba(219,234,254,.5)}
.aitop .navlinks a:hover{color:#fff}
.aigrid{display:grid;grid-template-columns:minmax(0,1fr) 430px;gap:16px;align-items:start}
@media(max-width:1180px){.aigrid{grid-template-columns:1fr}}
.chatcard{position:sticky;top:14px;background:#fff;border:1px solid #e5e7eb;border-radius:14px;
  box-shadow:0 1px 3px rgba(16,24,40,.06);display:flex;flex-direction:column;max-height:calc(100vh - 30px)}
.chathd{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid #eef2f7}
.chathd h3{margin:0;font-size:15px;color:#0c447c}
.chathd .st{font-size:11.5px;color:#94a3b8;margin-left:auto}
.chatlog{flex:1;overflow:auto;padding:12px 14px;min-height:220px}
.msg{margin:0 0 12px;display:flex;flex-direction:column}
.msg .who{font-size:11px;color:#94a3b8;margin-bottom:3px}
.msg .bub{border-radius:10px;padding:9px 12px;font-size:13px;line-height:1.7;white-space:pre-wrap;
  word-break:break-word;border:1px solid #eef2f7;background:#f8fafc;color:#1f2937}
.msg.user{align-items:flex-end}
.msg.user .bub{background:#e8f0fe;border-color:#d5e3fb;color:#12335e}
.msg.ai .bub{background:#fff}
.msg .bub h4{margin:8px 0 4px;font-size:13.5px;color:#0b3d6b}
.msg .bub ul{margin:4px 0;padding-left:18px}
.msg .bub code{background:#f1f5f9;padding:0 5px;border-radius:4px;font-size:12px}
.bub.streaming:empty{display:none}
.bub.streaming::after{content:'▌';color:#185fa5;margin-left:1px;animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
.think{margin:0 0 7px;border:1px dashed #d7e2f2;border-radius:9px;background:#fbfcff;overflow:hidden}
.think>summary{cursor:pointer;font-size:11.5px;color:#64748b;padding:5px 10px;user-select:none;list-style:none}
.think>summary::-webkit-details-marker{display:none}
.think[open]>summary{color:#185fa5;border-bottom:1px solid #eef2f7}
.think .tbody{padding:7px 11px 9px;font-size:11.8px;line-height:1.65;color:#64748b;white-space:pre-wrap;
  word-break:break-word;max-height:230px;overflow:auto;font-family:ui-monospace,Menlo,Consolas,monospace}
.think .tst{color:#94a3b8;font-weight:400}
.thinkingbar{display:flex;align-items:center;gap:8px;font-size:12px;color:#185fa5;
  background:#f2f7ff;border:1px solid #dbe8fb;border-radius:8px;padding:6px 10px;margin-bottom:8px}
.thinkingbar .el{margin-left:auto;color:#64748b;font-size:11.5px}
.spin{width:12px;height:12px;flex:0 0 12px;border:2px solid #bfd7f5;border-top-color:#185fa5;
  border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.chatbox{border-top:1px solid #eef2f7;padding:10px 12px}
.chatbox textarea{width:100%;min-height:62px;max-height:160px;resize:vertical;border:1px solid #d8e0ea;
  border-radius:9px;padding:9px 11px;font-size:13px;font-family:inherit;line-height:1.6}
.chatbox textarea:focus{outline:none;border-color:#93c5fd;box-shadow:0 0 0 3px rgba(37,99,235,.1)}
.chatrow{display:flex;align-items:center;gap:8px;margin-top:8px}
.chatrow .hintline{font-size:11.5px;color:#94a3b8;margin-right:auto}
.chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 10px}
.chips button{border:1px solid #dbe4f0;background:#f8fbff;color:#185fa5;border-radius:20px;
  padding:3px 11px;font-size:12px;cursor:pointer}
.chips button:hover{background:#185fa5;color:#fff}
.aibig{width:100%;border:none;background:#1e3a8a;color:#fff;border-radius:10px;padding:11px;
  font-weight:700;font-size:14px;cursor:pointer}
.aibig:disabled{opacity:.55;cursor:default}
.chatempty{color:#94a3b8;font-size:12.8px;line-height:1.9}
.rptprog{margin:0 0 12px;border:1px solid #dbe4f0;border-radius:12px;background:#f8fbff;padding:10px 12px}
.rptprog .hd{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:#0c447c}
.rptprog .hd .el{margin-left:auto;font-weight:400;font-size:11.5px;color:#64748b}
.rptprog .pg{font-size:12.3px;color:#475569;margin-top:5px}
.rptprog .tbody{font-size:11.8px;color:#64748b;white-space:pre-wrap;word-break:break-word;
  max-height:190px;overflow:auto;font-family:ui-monospace,Menlo,Consolas,monospace;margin-top:6px}
.modesw{display:flex;align-items:center;gap:6px;font-size:12px;color:#475569;margin-bottom:8px}
.modesw label{cursor:pointer;user-select:none;display:flex;align-items:center;gap:5px}
.modesw input{accent-color:#185fa5;width:14px;height:14px}
.modesw .tag{font-size:11px;color:#94a3b8}
.steps{margin:0 0 7px}
.step{display:flex;gap:7px;align-items:flex-start;font-size:12.2px;line-height:1.6;
  padding:4px 8px;border-radius:7px;margin-bottom:3px;background:#f8fafc;border:1px solid #eef2f7}
.step .ic{flex:0 0 auto;font-size:12px;line-height:1.6}
.step.tool{background:#f2f7ff;border-color:#dbe8fb;color:#12335e}
.step.plan{background:#fbfcff;border-color:#e6eef8;color:#475569}
.step.obs{background:#fbfdfb;border-color:#e8f2ea;color:#475569;display:block}
.step.err{background:#fdf3f3;border-color:#f5dede;color:#9f1d1d}
.step .nm{font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:600;color:#185fa5}
.step .ar{color:#64748b;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.4px;
  word-break:break-all}
.step.obs details{margin-top:2px}
.step.obs summary{cursor:pointer;color:#3f6212;font-size:11.6px;list-style:none}
.step.obs summary::-webkit-details-marker{display:none}
.step.obs pre{margin:4px 0 0;font-size:11.3px;white-space:pre-wrap;word-break:break-word;
  max-height:180px;overflow:auto;color:#475569}
.agenthint{font-size:11.5px;color:#b45309;background:#fffbeb;border:1px solid #fde68a;
  border-radius:7px;padding:5px 9px;margin-bottom:8px}
.jumpbtn{position:absolute;right:14px;bottom:118px;z-index:6;border:1px solid #c7d2fe;background:#fff;
  color:#185fa5;border-radius:20px;padding:4px 12px;font-size:12px;cursor:pointer;
  box-shadow:0 3px 10px rgba(16,24,40,.14)}
.jumpbtn:hover{background:#185fa5;color:#fff}
"""


CHAT_JS = r"""
var CHAT_API = '/api/ai/chat';
var STREAM_API = '/api/ai/stream';
var es = null;               // 当前 SSE 连接
var curJob = null;           // 当前对话 job id
var elThink = null, elThinkBody = null, elBub = null, elBar = null, elSteps = null;
var curReasoning = '', curContent = '', curSteps = [];
var AGENT_MODE = false;
var timerId = null, tStart = 0, thinkTouched = false;

function agentUrl(){ return AGENT_MODE ? '/api/ai/agent' : '/api/ai/chat'; }
function setAgent(v){
  AGENT_MODE = !!v;
  try{ localStorage.setItem('aiAgentMode', AGENT_MODE?'1':'0'); }catch(e){}
  var h=document.getElementById('agenthint');
  if(h) h.style.display = AGENT_MODE ? '' : 'none';
  var t=document.getElementById('modetag');
  if(t) t.textContent = AGENT_MODE ? '（能查数据 / 抓数 / 写并跑脚本）' : '（基于当前区间数据问答）';
}
function toggleAgent(cb){ setAgent(cb.checked); }

/* ---------------- 粘底滚动：用户往上翻时**不许**被拽回去 ----------------
   做法：给可滚动容器绑定「是否贴底」标记；新内容到达时，
   只有它本来就在底部（或显式 force）才自动滚到底。 */
function bindStick(el, onchange){
  if(!el || el.__bound) return el;
  el.__bound = true; el.__stick = true;
  el.addEventListener('scroll', function(){
    el.__stick = (el.scrollHeight - el.scrollTop - el.clientHeight) <= 28;
    if(onchange) onchange(el.__stick);
  });
  return el;
}
function stickScroll(el, force){
  if(!el) return;
  if(force || el.__stick !== false) el.scrollTop = el.scrollHeight;
}
function chatLog(){ return document.getElementById('chatlog'); }
function toggleJump(){
  var b = document.getElementById('jumpBtn'); if(!b) return;
  var log = chatLog();
  var off = log && log.__stick === false;
  b.style.display = off ? 'block' : 'none';
}
function jumpBottom(){
  var log = chatLog(); if(!log) return;
  log.scrollTop = log.scrollHeight; log.__stick = true; toggleJump();
}

function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function mdInline(s){
  s = esc(s);
  s = s.replace(/`([^`]+)`/g,'<code>$1</code>');
  s = s.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');
  return s;
}
function mdBlock(txt){
  var lines = (txt||'').split(/\r?\n/); var out=[]; var list=null;
  function closeList(){ if(list){ out.push('</ul>'); list=null; } }
  for(var i=0;i<lines.length;i++){
    var ln = lines[i];
    var h = ln.match(/^\s*#{1,6}\s+(.*)$/);
    if(h){ closeList(); out.push('<h4>'+mdInline(h[1])+'</h4>'); continue; }
    var li = ln.match(/^\s*(?:[-*]|\d+[.)])\s+(.*)$/);
    if(li){ if(!list){ out.push('<ul>'); list=true; } out.push('<li>'+mdInline(li[1])+'</li>'); continue; }
    if(!ln.trim()){ closeList(); continue; }
    closeList(); out.push('<div>'+mdInline(ln)+'</div>');
  }
  closeList();
  return out.join('');
}
function chatKey(){ return 'aiChat|'+CHAT_START+'|'+CHAT_END+'|'+CHAT_KEYS; }
function jobKey(){ return 'aiChatJob|'+CHAT_START+'|'+CHAT_END+'|'+CHAT_KEYS; }
function loadChat(){ try{ return JSON.parse(localStorage.getItem(chatKey())||'[]'); }catch(e){ return []; } }
function saveChat(a){ try{ localStorage.setItem(chatKey(), JSON.stringify(a.slice(-40))); }catch(e){} }
function saveJob(o){ try{ if(o) localStorage.setItem(jobKey(), JSON.stringify(o)); else localStorage.removeItem(jobKey()); }catch(e){} }
function loadJob(){ try{ return JSON.parse(localStorage.getItem(jobKey())||'null'); }catch(e){ return null; } }

function stepHTML(s){
  if(s.t==='plan') return '<div class="step plan"><span class="ic">💭</span><span>'+esc(s.text)+'</span></div>';
  if(s.t==='tool') return '<div class="step tool"><span class="ic">🛠</span><span>调用 <span class="nm">'
      + esc(s.name)+'</span> <span class="ar">'+esc(s.args||'')+'</span></span></div>';
  if(s.t==='obs') return '<div class="step obs '+(s.ok?'':'err')+'"><span class="ic">'+(s.ok?'📥':'⚠️')+'</span>'
      + '<span>'+esc(s.name)+' 返回'+(s.ms!=null?('（'+s.ms+'ms）'):'')
      + '<details><summary>查看结果（'+(s.summary||'').length+' 字）</summary><pre>'+esc(s.summary)+'</pre></details></span></div>';
  return '';
}
function msgHTML(m){
  var cls = m.role==='user' ? 'user' : 'ai';
  var h = '<div class="msg '+cls+'"><div class="who">'+(m.role==='user'?'我':'AI')+'</div>';
  if(m.role!=='user' && m.steps && m.steps.length){
    h += '<details class="steps-wrap" style="margin:0 0 6px"><summary style="cursor:pointer;font-size:11.5px;color:#64748b">'
       + '🛠 执行过程（'+m.steps.length+' 步）</summary><div class="steps" style="margin-top:5px">'
       + m.steps.map(stepHTML).join('') + '</div></details>';
  }
  if(m.role!=='user' && m.reasoning){
    h += '<details class="think"><summary>🧠 思考过程 <span class="tst">'+m.reasoning.length+' 字</span></summary>'
       + '<div class="tbody">'+esc(m.reasoning)+'</div></details>';
  }
  h += '<div class="bub">'+(m.role==='user'? esc(m.content) : mdBlock(m.content))+'</div></div>';
  return h;
}
function renderChat(force){
  var log=document.getElementById('chatlog'); var arr=loadChat();
  var wasStick = (log.__stick !== false), keepTop = log.scrollTop;
  elThink=elThinkBody=elBub=null; elSteps=null;
  bindStick(log, toggleJump);
  if(!arr.length){
    log.innerHTML='<div class="chatempty">就当前区间向 AI 追问，例如：<br>'
      + '· 定向人群（DMP 人群包）到底要不要做？先怎么做？<br>'
      + '· 哪些地域该屏蔽、哪些该加价？<br>'
      + '· 这周只改三件事，先改哪三件？</div>';
    log.__stick = true; toggleJump(); return;
  }
  log.innerHTML = arr.map(msgHTML).join('');
  var goBottom = (force === undefined) ? wasStick : !!force;
  if(goBottom) log.scrollTop = log.scrollHeight; else log.scrollTop = keepTop;
  log.__stick = goBottom;
  toggleJump();
}
function pushMsg(role, content, reasoning, steps){
  var a=loadChat(); a.push({role:role,content:content,reasoning:reasoning||'',steps:steps||[]}); saveChat(a);
}

function setBusy(b){
  var btn=document.getElementById('chatSend');
  if(btn){ btn.disabled=b; btn.textContent=b?'生成中…':'发送'; }
  if(!b && elBar){ elBar.remove(); elBar=null; if(timerId){clearInterval(timerId);timerId=null;} }
}
function setStatus(t){ var el=document.getElementById('chatst'); if(el) el.textContent=t; }

function startThinkingBar(){
  var log=document.getElementById('chatlog');
  if(elBar) elBar.remove();
  elBar=document.createElement('div');
  elBar.className='thinkingbar';
  elBar.innerHTML='<span class="spin"></span><span id="tbTxt">AI 正在思考…</span><span class="el" id="tbEl">0s</span>';
  log.appendChild(elBar);
  tStart=Date.now();
  if(timerId) clearInterval(timerId);
  timerId=setInterval(function(){
    var e=document.getElementById('tbEl'); if(e) e.textContent=Math.round((Date.now()-tStart)/1000)+'s';
  },500);
  bindStick(log, toggleJump); stickScroll(log, true);
}
function setBarText(t){ var el=document.getElementById('tbTxt'); if(el) el.textContent=t; }

function beginStreamMsg(){
  var log=document.getElementById('chatlog');
  var wrap=document.createElement('div'); wrap.className='msg ai';
  wrap.innerHTML='<div class="who">AI</div>'
    + '<div class="steps"></div>'
    + '<details class="think" open><summary>🧠 思考过程 <span class="tst"></span></summary>'
    + '<div class="tbody"></div></details>'
    + '<div class="bub streaming"></div>';
  log.appendChild(wrap);
  elSteps=wrap.querySelector('.steps');
  elThink=wrap.querySelector('.think');
  elThinkBody=wrap.querySelector('.think .tbody');
  elBub=wrap.querySelector('.bub');
  elThink.querySelector('summary').addEventListener('click',function(){ thinkTouched=true; });
  bindStick(elThinkBody);
  curReasoning=''; curContent=''; curSteps=[];
  bindStick(log, toggleJump); stickScroll(log, true);
}
function addStep(s){
  curSteps.push(s);
  if(!elSteps) return;
  var d=document.createElement('div');
  d.innerHTML=stepHTML(s);
  elSteps.appendChild(d.firstChild);
  stickScroll(chatLog());
}
function paintChunk(){
  if(elThinkBody){
    elThinkBody.textContent = curReasoning;
    var c=elThink.querySelector('.tst'); if(c) c.textContent = curReasoning.length+' 字';
    stickScroll(elThinkBody);          // 思考框也只在你自己没往上翻时才跟到底
  }
  if(elBub) elBub.innerHTML = mdBlock(curContent);
  stickScroll(chatLog());               // ← 关键：不再无条件拽到底部
}
/* 分片很密（一次生成常有几百片），按帧合并重绘，滑动更跟手 */
var _paintPending = false;
function paintSoon(){
  if(_paintPending) return;
  _paintPending = true;
  (window.requestAnimationFrame || function(f){ setTimeout(f, 16); })(function(){
    _paintPending = false; paintChunk();
  });
}

/* 挂载事件流：断开不影响服务端生成；重挂从 0 回放，重建完整内容。 */
function attach(jobId){
  if(es){ es.close(); es=null; }
  curJob = jobId;
  es = new EventSource(STREAM_API + '?job=' + encodeURIComponent(jobId) + '&after=0');
  es.addEventListener('phase', function(ev){
    var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){}
    setBarText(d.t||'处理中…');
  });
  es.addEventListener('reset', function(){ curReasoning=''; curContent=''; paintChunk(); });
  es.addEventListener('plan', function(ev){
    var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){}
    var p={}; try{ p=JSON.parse(d.t||'{}'); }catch(e){}
    addStep({t:'plan', text: p.thought || d.t || ''});
  });
  es.addEventListener('tool', function(ev){
    var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){}
    var p={}; try{ p=JSON.parse(d.t||'{}'); }catch(e){}
    var args=''; try{ args=JSON.stringify(p.args||{}); }catch(e){}
    addStep({t:'tool', name:p.name||'', args:args});
  });
  es.addEventListener('obs', function(ev){
    var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){}
    var p={}; try{ p=JSON.parse(d.t||'{}'); }catch(e){}
    addStep({t:'obs', name:p.name||'', ok:p.ok!==false, ms:p.ms, summary:p.summary||''});
  });
  es.addEventListener('reasoning', function(ev){
    var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){}
    curReasoning += (d.t||''); paintSoon();
  });
  es.addEventListener('content', function(ev){
    var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){}
    curContent += (d.t||'');
    if(curContent && elThink && elThink.open && !thinkTouched) elThink.open=false;   // 出正文后自动收起思考
    paintSoon();
  });
  es.addEventListener('error', function(ev){
    var d = {}; try{ d=JSON.parse(ev.data||'{}'); }catch(e){}
    curContent += '\n\n[生成失败] ' + (d.t || '未知错误');
    paintChunk(); closeStream(true);
  });
  es.addEventListener('done', function(){ closeStream(false); });
  es.addEventListener('state', function(ev){
    var d={}; try{ d=JSON.parse(ev.data||'{}'); }catch(e){}
    if(d.status && d.status!=='running') closeStream(false);
  });
  es.onerror = function(){
    // 探测任务是否还在（服务重启/过期会消失）；短暂网络抖动由 EventSource 自动重连
    fetch('/api/ai/jobs').then(function(r){return r.json();}).then(function(j){
      var found=(j.jobs||[]).some(function(x){return x.id===jobId;});
      if(!found && es){ curContent += '\n\n[连接中断] 任务已结束或服务重启，请重新提问。'; paintChunk(); closeStream(true); }
    }).catch(function(){});
  };
}
function closeStream(isErr){
  if(es){ es.close(); es=null; }
  var txt = curContent || (isErr ? '(生成失败)' : '(无内容返回)');
  var a=loadChat();
  if(a.length && a[a.length-1].role==='assistant' && !a[a.length-1].content){
    a[a.length-1].content = txt; a[a.length-1].reasoning = curReasoning;
    a[a.length-1].steps = curSteps; saveChat(a);
  } else {
    pushMsg('assistant', txt, curReasoning, curSteps);
  }
  curJob=null; saveJob(null); setBusy(false); setStatus('Ctrl/⌘+Enter 发送'); renderChat();
}

function askAi(q){
  if(curJob) return;                        // 有正在跑的生成，等它完（或点「停止」）
  q = (q!==undefined && q!==null) ? q : document.getElementById('chatq').value.trim();
  if(!q) return;
  var hist = loadChat().slice(-8).map(function(m){ return {role:m.role, content:m.content}; });
  pushMsg('user', q);
  document.getElementById('chatq').value='';
  if(elBar) elBar.remove();
  setBusy(true); setStatus(AGENT_MODE ? 'Agent 执行中，可离开页面' : '生成中，可离开页面');
  startThinkingBar();
  beginStreamMsg();
  var body = AGENT_MODE
    ? JSON.stringify({start:CHAT_START,end:CHAT_END,accounts:CHAT_KEYS,task:q})
    : JSON.stringify({start:CHAT_START,end:CHAT_END,accounts:CHAT_KEYS,question:q,history:hist});
  fetch(agentUrl(),{method:'POST',headers:{'Content-Type':'application/json'},body:body})
   .then(function(r){ return r.json(); })
   .then(function(j){
     if(!j.ok){ setBarText('失败：'+(j.error||'未知')); elBub.innerHTML='<b>请求失败：</b>'+esc(j.error||'未知'); elBub.classList.remove('streaming'); setBusy(false); return; }
     saveJob({jobId:j.jobId});
     attach(j.jobId);
   })
   .catch(function(e){ setBarText('请求失败'); elBub.textContent='请求失败：'+e; elBub.classList.remove('streaming'); setBusy(false); });
}
function stopChat(){ if(es){ es.close(); es=null; } curJob=null; saveJob(null); setBusy(false); }
function clearChat(){ if(curJob) return; try{ localStorage.removeItem(chatKey()); }catch(e){} renderChat(); }
function chatKeydown(e){ if(e.key==='Enter' && (e.ctrlKey||e.metaKey)){ e.preventDefault(); askAi(); } }

/* ---------------- 报告生成过程中的思考流 ---------------- */
var rptJob=null, rptEs=null, rptBody=null, rptTxt=null, rptEl=null, rptTimer=null, rptStart=0;
function rptPanel(){
  var box=document.getElementById('rptprog');
  box.style.display='';
  box.innerHTML='<div class="hd"><span class="spin"></span><span id="rptHdTxt">正在生成 AI 报告…</span>'
    + '<span class="el" id="rptEl">0s</span></div>'
    + '<div class="pg" id="rptPg">准备中…</div>'
    + '<div class="tbody" id="rptBody"></div>';
  rptBody=document.getElementById('rptBody'); rptTxt=document.getElementById('rptPg'); rptEl=document.getElementById('rptEl');
  bindStick(rptBody);
  rptStart=Date.now();
  if(rptTimer) clearInterval(rptTimer);
  rptTimer=setInterval(function(){ if(rptEl) rptEl.textContent=Math.round((Date.now()-rptStart)/1000)+'s'; },1000);
}
function rptAttach(jobId){
  rptJob=jobId; try{ localStorage.setItem('aiReportJob', JSON.stringify({jobId:jobId})); }catch(e){}
  rptPanel();
  if(rptEs) rptEs.close();
  rptEs=new EventSource(STREAM_API+'?job='+encodeURIComponent(jobId)+'&after=0');
  rptEs.addEventListener('phase', function(ev){ var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){} if(rptTxt) rptTxt.textContent=d.t||''; });
  rptEs.addEventListener('reset', function(){ if(rptBody) rptBody.textContent=''; });
  rptEs.addEventListener('reasoning', function(ev){ var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){}
    if(rptBody){ rptBody.textContent += (d.t||''); stickScroll(rptBody); } });
  rptEs.addEventListener('done', function(){ rptDone(false); });
  rptEs.addEventListener('error', function(ev){ var d={}; try{d=JSON.parse(ev.data||'{}');}catch(e){}
    if(rptTxt) rptTxt.textContent='生成失败：'+(d.t||'未知'); rptDone(true); });
  rptEs.onerror=function(){ fetch('/api/ai/jobs').then(function(r){return r.json();}).then(function(j){
    var f=(j.jobs||[]).some(function(x){return x.id===jobId;}); if(!f) rptDone(true); }).catch(function(){}); };
}
function rptDone(isErr){
  if(rptEs){ rptEs.close(); rptEs=null; }
  if(rptTimer){ clearInterval(rptTimer); rptTimer=null; }
  try{ localStorage.removeItem('aiReportJob'); }catch(e){}
  if(!isErr){
    var box=document.getElementById('rptprog');
    if(box) box.innerHTML='<div class="hd">✅ 生成完成，正在刷新报告…</div>';
    setTimeout(function(){ location.reload(); }, 700);
  }
}
function genAiTop(btn, start, end, keys){
  if(btn.disabled) return;
  btn.disabled=true; var old=btn.textContent; btn.textContent='生成中…';
  var qs='start='+encodeURIComponent(start)+'&end='+encodeURIComponent(end)+'&accounts='+encodeURIComponent(keys);
  rptPanel();
  fetch('/api/ai/run?'+qs,{method:'POST'}).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){ btn.disabled=false; btn.textContent=old; rptDone(true); alert('生成失败：'+(j.error||'未知')); return; }
    if(j.jobId){ rptAttach(j.jobId); }
    else { setTimeout(function(){ location.reload(); }, 1000); }
  }).catch(function(e){ btn.disabled=false; btn.textContent=old; rptDone(true); alert('请求失败：'+e); });
}

function statTick(){
  fetch('/api/ai/status').then(function(r){return r.json();}).then(function(s){
    var el=document.getElementById('aistat'); if(!el) return;
    el.textContent = s.running ? '正在生成报告…' : (s.error? ('上次失败：'+s.error) : '就绪');
  }).catch(function(){});
}
function bootstrap(){
  // 0) 恢复「自主执行」开关
  try{ AGENT_MODE = localStorage.getItem('aiAgentMode')==='1'; }catch(e){}
  setAgent(AGENT_MODE);
  var cb=document.getElementById('agentSw'); if(cb) cb.checked = AGENT_MODE;
  // 1) 回到页面：若上次有未看完的追问，重新挂载回放（服务端 job 还在）
  var j=loadJob();
  if(j && j.jobId){ setStatus('恢复上次的生成…'); setBusy(true); startThinkingBar(); beginStreamMsg(); attach(j.jobId); }
  else { renderChat(); }
  // 2) 报告生成中的思考流（本页发起，或别的标签页发起）
  try{ var rj=JSON.parse(localStorage.getItem('aiReportJob')||'null');
       if(rj && rj.jobId){ rptAttach(rj.jobId); } }catch(e){}
  statTick(); setInterval(statTick, 8000);
}
document.addEventListener('DOMContentLoaded', bootstrap);
"""

_QUICK = ["定向人群（DMP 人群包）要不要做？先怎么做？",
          "哪些地域该屏蔽、哪些该加价？",
          "这周只改三件事，先改哪三件？",
          "拉新和收割的计划结构平衡吗？",
          "哪些词该否定、哪些该提价？",
          "看看 10 月这几天缺哪些数据，缺的直接抓一下",
          "把近 20 天的日花费和 ROI 导出成 CSV（自己写脚本跑）",
          "对比两个账号近 7 天的地域投产差异，写个小脚本算并给出结论"]


def render(ai, d, ui: str = "", title: str = "AI 运营分析台") -> str:
    """渲染独立 AI 分析台（报告 + 对话框）。"""
    meta = d.get("meta") or {}
    rng = meta.get("range") or {}
    start = rng.get("start") or (meta.get("windows") or [{}])[-1].get("start") or ""
    end = rng.get("end") or (meta.get("windows") or [{}])[-1].get("end") or ""
    keys = ",".join(a.get("key", "") for a in (meta.get("accounts") or []))
    labels = " / ".join(str(a.get("label") or a.get("key")) for a in (meta.get("accounts") or []))

    report_html = report2._ai_block(ai, d, link=False)
    if not ai:
        report_html = (
            '<div class="aicard empty"><div class="aihd"><span class="aibadge off">AI</span> 暂无本区间解读'
            '</div><div class="aidisc">点下面「生成本区间解读」，AI 会套用《京东快车底层逻辑》方法论，'
            '贴着本地已抓取的数据出一份详细报告（含诊断、逐计划判断、词级建议、'
            '以及定向人群/地域等**还没用起来的功能**建议）。生成过程中可实时看到它的思考过程，'
            '中途离开页面也不会中断。</div>'
            '<div class="aidt" style="margin-top:10px"><button class="aibig" style="max-width:320px" '
            f'onclick="genAiTop(this,\'{_AI(start)}\',\'{_AI(end)}\',\'{_AI(keys)}\')">生成本区间解读</button></div></div>')

    chips = "".join(f'<button type="button" onclick="askAi(this.textContent)">{_AI(q)}</button>'
                    for q in _QUICK)
    js = (f"var CHAT_START={json.dumps(start)};var CHAT_END={json.dumps(end)};"
          f"var CHAT_KEYS={json.dumps(keys)};")

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_AI(title)}</title><style>{AI_PAGE_CSS}</style></head><body>
<header class="aitop"><div class="wrap">
  <h1>AI 运营分析台</h1>
  <div class="metaline">区间 {_AI(start)} ~ {_AI(end)}　｜　账号 {_AI(labels)}
    　｜　生成 {_AI(str((ai or {}).get("generatedAt") or "—"))}</div>
  <div class="navlinks">
    <a href="/">← 控制台</a>
    <a href="/report?start={_AI(start)}&end={_AI(end)}&accounts={_AI(keys)}">聚合报表</a>
    <a href="/api/ai/status" target="_blank">AI 状态 JSON</a>
    <span style="color:#93c5fd;font-size:12.5px;margin-left:auto">生成状态：<b id="aistat">…</b></span>
  </div>
</div></header>
<div class="wrap">
  {ui}
  <div class="aigrid">
    <div>
      <div class="rptprog" id="rptprog" style="display:none"></div>
      {report_html}
    </div>
    <aside class="chatcard">
      <div class="chathd"><span class="aibadge">AI</span><h3>就这份数据追问</h3>
        <span class="st" id="chatst">Ctrl/⌘+Enter 发送</span></div>
      <div class="modesw" style="padding:9px 14px 0">
        <label><input type="checkbox" id="agentSw" onchange="toggleAgent(this)"> 自主执行（Agent）</label>
        <span class="tag" id="modetag">（基于当前区间数据问答）</span>
      </div>
      <div class="agenthint" id="agenthint" style="display:none;margin:8px 14px 0">
        Agent 会自己查数据覆盖、按需<b>触发抓取</b>、取指标，必要时<b>写并运行新脚本</b>，
        也能直接打本机内部接口；每一步的执行过程与思考都实时可见，离开页面也不中断。
      </div>
      <div class="chatlog" id="chatlog"></div>
      <div class="chips">{chips}</div>
      <button id="jumpBtn" class="jumpbtn" style="display:none" type="button" onclick="jumpBottom()">↓ 回到底部</button>
      <div class="chatbox">
        <textarea id="chatq" placeholder="例如：定向人群要不要开？开的话怎么设溢价？（Ctrl/⌘+Enter 发送）"
          onkeydown="chatKeydown(event)"></textarea>
        <div class="chatrow">
          <span class="hintline">生成在服务端跑，离开页面不中断；回来会自动续看。</span>
          <button class="btn sec mini" type="button" onclick="stopChat()">断开查看</button>
          <button class="btn sec mini" type="button" onclick="clearChat()">清空</button>
          <button class="btn mini" type="button" id="chatSend" onclick="askAi()">发送</button>
        </div>
      </div>
    </aside>
  </div>
  <div class="foot" style="color:#94a3b8;font-size:11.5px;margin-top:14px">
    对话与「思考过程」在本机浏览器保存；生成任务在服务端执行，页面离开/刷新不中断。
    AI 生成内容仅供参考，执行前请人工复核。
  </div>
</div>
<script>{js}</script>
<script>{CHAT_JS}</script>
</body></html>"""
