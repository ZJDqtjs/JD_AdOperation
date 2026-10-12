# -*- coding: utf-8 -*-
"""独立 AI 分析台页面：左侧=每日详细 AI 报告，右侧=可追问的对话框。

与 /report 里的 AI 卡片互补：
  - /report 的卡片是「报告的一部分」，只读；
  - 本页把同一份 AI 解读**单独成页**，并配一个对话框，让运营能就具体问题追问
    （如「定向人群到底要不要开」「哪个地域该屏蔽」「这周该先改什么」）。

页面自包含（内联 CSS/JS），报告区直接复用 report2._ai_block 的渲染，
保证两处口径一致。对话框走后端 /api/ai/chat 的流式接口，
上下文（本地已抓取数据摘要 + 方法论）由后端准备，模型只做解释。
"""
from __future__ import annotations

import json

from . import report2

_AI = report2._e


AI_PAGE_CSS = report2.CSS + report2.UI_CSS + report2.AI_CSS + """
.aitop{background:var(--soft);border-bottom:1px solid var(--hair);color:var(--ink);padding:32px 0 20px;margin-bottom:18px}
.aitop .wrap{padding-bottom:0}
.aitop h1{margin:0 0 8px;font-size:30px;font-weight:400;font-family:var(--serif);letter-spacing:-.5px;color:var(--ink)}
.aitop .metaline{color:var(--muted);font-size:13px}
.aitop .navlinks{margin-top:12px;display:flex;gap:14px;flex-wrap:wrap;align-items:center}
.aitop .navlinks a{color:var(--primary);font-size:13px;text-decoration:none;border-bottom:1px dashed var(--hair)}
.aitop .navlinks a:hover{color:var(--primary-a)}
.aigrid{display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:16px;align-items:start}
@media(max-width:1180px){.aigrid{grid-template-columns:1fr}}
.chatcard{position:sticky;top:14px;background:var(--canvas);border:1px solid var(--hair);border-radius:12px;
  display:flex;flex-direction:column;max-height:calc(100vh - 30px)}
.chathd{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--hair)}
.chathd h3{margin:0;font-size:16px;font-weight:400;font-family:var(--serif);color:var(--ink)}
.chathd .st{font-size:11.5px;color:var(--muted2);margin-left:auto}
.chatlog{flex:1;overflow:auto;padding:12px 14px;min-height:220px}
.msg{margin:0 0 12px;display:flex;flex-direction:column}
.msg .who{font-size:11px;color:var(--muted2);margin-bottom:3px}
.msg .bub{border-radius:10px;padding:9px 12px;font-size:13px;line-height:1.7;white-space:pre-wrap;
  word-break:break-word;border:1px solid var(--hair);background:var(--soft);color:var(--body)}
.msg.user{align-items:flex-end}
.msg.user .bub{background:var(--bluebg);border-color:var(--hair);color:var(--primary-a)}
.msg.ai .bub{background:var(--canvas)}
.msg .bub h4{margin:8px 0 4px;font-size:13.5px;color:var(--ink);font-weight:500}
.msg .bub ul{margin:4px 0;padding-left:18px}
.msg .bub code{background:var(--soft);padding:0 5px;border-radius:4px;font-size:12px;font-family:var(--mono)}
.chatbox{border-top:1px solid var(--hair);padding:10px 12px}
.chatbox textarea{width:100%;min-height:62px;max-height:160px;resize:vertical;border:1px solid var(--hair);
  border-radius:8px;padding:9px 11px;font-size:13px;font-family:inherit;line-height:1.6;
  background:var(--canvas);color:var(--ink)}
.chatbox textarea:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(204,120,92,.15)}
.chatrow{display:flex;align-items:center;gap:8px;margin-top:8px}
.chatrow .hintline{font-size:11.5px;color:var(--muted2);margin-right:auto}
.chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 10px}
.chips button{border:1px solid var(--hair);background:var(--canvas);color:var(--primary);border-radius:9999px;
  padding:3px 11px;font-size:12px;cursor:pointer}
.chips button:hover{background:var(--primary);color:var(--on-primary)}
.aibig{width:100%;border:none;background:var(--primary);color:var(--on-primary);border-radius:8px;padding:11px;
  font-weight:500;font-size:14px;cursor:pointer}
.aibig:disabled{opacity:.55;cursor:default}
.aibig.sec{background:var(--canvas);color:var(--primary);border:1px solid var(--hair)}
.chatempty{color:var(--muted2);font-size:12.8px;line-height:1.9}
"""


# 轻量行内 markdown：加粗/行内代码（够对话框用，避免引入外部库）
CHAT_JS = r"""
var CHAT_API = '/api/ai/chat';
var chatBusy = false;
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
function chatKey(){
  return 'aiChat|'+CHAT_START+'|'+CHAT_END+'|'+CHAT_KEYS;
}
function loadChat(){
  try{ return JSON.parse(localStorage.getItem(chatKey())||'[]'); }catch(e){ return []; }
}
function saveChat(arr){ try{ localStorage.setItem(chatKey(), JSON.stringify(arr.slice(-40))); }catch(e){} }
function renderChat(){
  var log=document.getElementById('chatlog'); var arr=loadChat();
  if(!arr.length){
    log.innerHTML='<div class="chatempty">就当前区间向 AI 追问，例如：<br>'
      + '· 定向人群（DMP 人群包）到底要不要做？先怎么做？<br>'
      + '· 哪些地域该屏蔽、哪些该加价？<br>'
      + '· 这周只改三件事，先改哪三件？</div>';
    return;
  }
  var h='';
  for(var i=0;i<arr.length;i++){
    var m=arr[i];
    var cls = m.role==='user'?'user':'ai';
    var who = m.role==='user'?'我':'AI';
    h += '<div class="msg '+cls+'"><div class="who">'+who+'</div><div class="bub">'
       + (m.role==='user'? esc(m.content) : mdBlock(m.content)) + '</div></div>';
  }
  log.innerHTML=h; log.scrollTop=log.scrollHeight;
}
function pushMsg(role, content){ var a=loadChat(); a.push({role:role,content:content}); saveChat(a); }
function setChatBusy(b){
  chatBusy=b;
  document.getElementById('chatSend').disabled=b;
  document.getElementById('chatSend').textContent=b?'生成中…':'发送';
}
function askAi(q){
  if(chatBusy) return;
  q = (q!==undefined && q!==null) ? q : document.getElementById('chatq').value.trim();
  if(!q) return;
  var hist = loadChat().slice(-8);
  pushMsg('user', q); document.getElementById('chatq').value=''; renderChat();
  setChatBusy(true);
  var aiIdx = loadChat().length; pushMsg('assistant',''); renderChat();
  var body = JSON.stringify({start:CHAT_START,end:CHAT_END,accounts:CHAT_KEYS,question:q,history:hist});
  fetch(CHAT_API,{method:'POST',headers:{'Content-Type':'application/json'},body:body})
   .then(function(resp){
     if(!resp.ok || !resp.body){ throw new Error('HTTP '+resp.status); }
     var reader=resp.body.getReader(); var dec=new TextDecoder(); var acc='';
     function pump(){
       return reader.read().then(function(r){
         if(r.done){ finish(); return; }
         acc += dec.decode(r.value,{stream:true});
         var a=loadChat(); a[a.length-1]={role:'assistant',content:acc}; saveChat(a); renderChat();
         return pump();
       });
     }
     function finish(){ setChatBusy(false); renderChat();
       if(!acc.trim()){ var a=loadChat(); a[a.length-1]={role:'assistant',content:'（无内容返回，请重试）'}; saveChat(a); renderChat(); } }
     return pump();
   })
   .catch(function(e){
     var a=loadChat(); a[a.length-1]={role:'assistant',content:'请求失败：'+e+'　（AI 服务可能未配置或超时）'};
     saveChat(a); renderChat(); setChatBusy(false);
   });
}
function clearChat(){ if(chatBusy) return; saveChat([]); renderChat(); }
function chatKeydown(e){ if(e.key==='Enter' && (e.ctrlKey||e.metaKey)){ e.preventDefault(); askAi(); } }
function genAiTop(btn, start, end, keys){
  if(btn.disabled) return;
  btn.disabled=true; var old=btn.textContent; btn.textContent='生成中…（约 2~5 分钟）';
  var qs='start='+encodeURIComponent(start)+'&end='+encodeURIComponent(end)+'&accounts='+encodeURIComponent(keys);
  fetch('/api/ai/run?'+qs,{method:'POST'}).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){ btn.disabled=false; btn.textContent=old; alert('生成失败：'+(j.error||'未知')); return; }
    var t=setInterval(function(){
      fetch('/api/ai/status').then(function(r){return r.json();}).then(function(s){
        if(!s.running){ clearInterval(t); location.reload(); }
      }).catch(function(){ clearInterval(t); btn.disabled=false; btn.textContent=old; });
    }, 5000);
  }).catch(function(e){ btn.disabled=false; btn.textContent=old; alert('请求失败：'+e); });
}
function statTick(){
  fetch('/api/ai/status').then(function(r){return r.json();}).then(function(s){
    var el=document.getElementById('aistat'); if(!el) return;
    el.textContent = s.running ? ('正在生成… '+(s.range?('（'+s.range[0]+' ~ '+s.range[1]+'）'):'')) : (s.error? ('上次失败：'+s.error) : '就绪');
  }).catch(function(){});
}
document.addEventListener('DOMContentLoaded', function(){ renderChat(); statTick(); setInterval(statTick, 8000); });
"""

_QUICK = ["定向人群（DMP 人群包）要不要做？先怎么做？",
          "哪些地域该屏蔽、哪些该加价？",
          "这周只改三件事，先改哪三件？",
          "拉新和收割的计划结构平衡吗？",
          "哪些词该否定、哪些该提价？",
          "曝光/点击率/转化率，当前最该补哪块？"]


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
            '</div><div class="aidisc">点右上「生成本区间解读」，AI 会套用《京东快车底层逻辑》方法论，'
            '贴着本地已抓取的数据出一份详细报告（含诊断、逐计划判断、词级建议、'
            '以及定向人群/地域等**还没用起来的功能**建议）。</div>'
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
    <span style="color:var(--muted);font-size:12.5px;margin-left:auto">生成状态：<b id="aistat">…</b></span>
  </div>
</div></header>
<div class="wrap">
  {ui}
  <div class="aigrid">
    <div>{report_html}</div>
    <aside class="chatcard">
      <div class="chathd"><span class="aibadge">AI</span><h3>就这份数据追问</h3>
        <span class="st" id="chatst">Ctrl/⌘+Enter 发送</span></div>
      <div class="chatlog" id="chatlog"></div>
      <div class="chips">{chips}</div>
      <div class="chatbox">
        <textarea id="chatq" placeholder="例如：定向人群要不要开？开的话怎么设溢价？（Ctrl/⌘+Enter 发送）"
          onkeydown="chatKeydown(event)"></textarea>
        <div class="chatrow">
          <span class="hintline">上下文＝本地已抓取数据摘要 + 方法论；AI 不会编造摘要外的数字。</span>
          <button class="btn sec mini" type="button" onclick="clearChat()">清空</button>
          <button class="btn mini" type="button" id="chatSend" onclick="askAi()">发送</button>
        </div>
      </div>
    </aside>
  </div>
  <div class="foot" style="color:var(--on-dark-soft);font-size:11.5px;margin-top:14px">
    对话仅在本机浏览器保存（localStorage）；AI 生成内容仅供参考，执行前请人工复核。
  </div>
</div>
<script>{js}</script>
<script>{CHAT_JS}</script>
</body></html>"""
