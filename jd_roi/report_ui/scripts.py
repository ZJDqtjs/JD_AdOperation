# -*- coding: utf-8 -*-
"""分析报表客户端交互脚本（页签路由 / 表格排序筛选 / CSV 导出 / ECharts 渲染 / AI 轮询）。"""
from __future__ import annotations

REPORT_JS = """window.__CH=window.__CH||{};
function _resizeAllCharts(){
  Object.keys(window.__CH||{}).forEach(function(k){
    try{
      var ch=window.__CH[k];
      if(ch && ch.getDom && ch.getDom().offsetWidth>0) ch.resize();
    }catch(e){}
  });
}
function showTab(id, el, skipHash){
  var sec=document.getElementById(id);
  if(!sec) return;
  document.querySelectorAll('section.page').forEach(function(s){s.classList.remove('active');});
  sec.classList.add('active');
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active');});
  if(!el) el=document.querySelector('.tab[data-tab="'+id+'"]');
  if(el) el.classList.add('active');
  if(!skipHash){
    try{ history.replaceState(null,'','#'+id); }catch(e){}
  }
  setTimeout(_resizeAllCharts, 20);
  if(window.scrollY>160) window.scrollTo({top:0,behavior:'smooth'});
}
function _restoreTabFromHash(){
  var h=(location.hash||'').replace(/^#/,'');
  if(!h) return;
  var sec=document.getElementById(h);
  if(sec && sec.classList.contains('page')){
    showTab(h, document.querySelector('.tab[data-tab="'+h+'"]'), true);
  }
}
function planView(btn, v){
  /* 计划明细：区间详细 / 三期趋势。两张表都已在页面里，只切显示，不重新请求。 */
  var card=btn.closest('.card');
  card.querySelectorAll('.vswitch button').forEach(function(b){
    b.classList.toggle('on', b.dataset.v===v);
  });
  card.querySelectorAll('[data-pview]').forEach(function(w){
    w.style.display=(w.dataset.pview===v)?'':'none';
  });
  cardFilter(card);
}
function sortTable(th){
  var tb=th.closest('table');
  var idx=Array.prototype.indexOf.call(th.parentNode.children,th);
  var asc=!(th.dataset.asc==='1'); th.dataset.asc=asc?'1':'0';
  Array.prototype.forEach.call(th.parentNode.children,function(h){
    if(h!==th){h.dataset.asc='';var m=h.querySelector('.sortmark');if(m)m.remove();}
  });
  var rows=Array.prototype.slice.call(tb.tBodies[0].rows);
  var num=th.dataset.num==='1';
  rows.sort(function(a,b){
    var ca=a.cells[idx], cb=b.cells[idx];
    var x=ca?(ca.dataset.v!==undefined?ca.dataset.v:ca.innerText):'';
    var y=cb?(cb.dataset.v!==undefined?cb.dataset.v:cb.innerText):'';
    if(num){
      var fx=parseFloat(String(x).replace(/[¥,%+,]/g,''));
      var fy=parseFloat(String(y).replace(/[¥,%+,]/g,''));
      if(isNaN(fx))fx=-1e18; if(isNaN(fy))fy=-1e18;
      return asc?fx-fy:fy-fx;
    }
    return asc?String(x).localeCompare(String(y),'zh'):String(y).localeCompare(String(x),'zh');
  });
  rows.forEach(function(r){tb.tBodies[0].appendChild(r);});
  var mk=th.querySelector('.sortmark')||th.appendChild(document.createElement('span'));
  mk.className='sortmark'; mk.textContent=asc?' ▲':' ▼';
}
function initSort(){
  document.querySelectorAll('table thead th').forEach(function(th){
    if(th.classList.contains('noSort') || th.dataset.sortBound) return;
    th.dataset.sortBound='1';
    th.addEventListener('click',function(){sortTable(th);});
  });
}
/* 关键词匹配：「；」= 任一命中（或），「：」= 同时命中（且），全角半角都认。 */
function matchQuery(text, q){
  if(!q) return true;
  var t=(text||'').toLowerCase();
  var groups=q.split(/[：:]/);
  for(var g=0; g<groups.length; g++){
    var part=groups[g].trim();
    if(!part) continue;
    var words=part.split(/[；;]/).map(function(x){return x.trim().toLowerCase();})
                  .filter(function(x){return x;});
    if(!words.length) continue;
    var hit=false;
    for(var w=0; w<words.length; w++){ if(t.indexOf(words[w])>=0){hit=true;break;} }
    if(!hit) return false;
  }
  return true;
}
function _fmtMoney(v){
  return Math.round(v).toLocaleString('zh-CN');
}
function cardFilter(card){
  var tier=card.dataset.tier||'all', acct=card.dataset.acct||'all';
  var q=(card.dataset.q||'').trim().toLowerCase();
  var rows=card.querySelectorAll('table tbody tr'), shown=0, total=0;
  var sumCost=0, sumAmt=0, hasSum=false;
  for(var i=0;i<rows.length;i++){
    var tr=rows[i], ok=true;
    if(tier!=='all' && (tr.dataset.tier||'')!==tier) ok=false;
    if(ok && acct!=='all' && (tr.dataset.acct||'').split(',').indexOf(acct)<0) ok=false;
    if(ok && q && !matchQuery(tr.textContent, q)) ok=false;
    tr.style.display=ok?'':'none';
    var pv=tr.closest('[data-pview]');
    if(!pv || pv.style.display!=='none'){
      total++;
      if(ok){
        shown++;
        if(tr.dataset.sumCost!==undefined){
          hasSum=true;
          sumCost+=parseFloat(tr.dataset.sumCost)||0;
          sumAmt+=parseFloat(tr.dataset.sumAmt)||0;
        }
      }
    }
  }
  var active=(q||tier!=='all'||acct!=='all');
  var lab=card.querySelector('.srchcount');
  if(lab) lab.textContent=active?('显示 '+shown+' / '+total):'';
  var rst=card.querySelector('.btn-reset-filter');
  if(rst) rst.style.display=active?'':'none';
  var fsum=card.querySelector('.filter-sum');
  if(fsum){
    if(active && hasSum){
      var roi=sumCost>0?(sumAmt/sumCost).toFixed(2):'—';
      fsum.innerHTML='<span>当前筛选 <b>'+shown+'</b> 条</span>'
        +'<span>花费合计 <b>¥'+_fmtMoney(sumCost)+'</b></span>'
        +'<span>成交合计 <b>¥'+_fmtMoney(sumAmt)+'</b></span>'
        +'<span>合并 ROI <b>'+roi+'</b></span>';
      fsum.classList.add('show');
    }else{
      fsum.classList.remove('show');
    }
  }
}
function applyTier(btn){
  var card=btn.closest('.card');
  card.querySelectorAll('.chip[data-f]').forEach(function(c){c.classList.remove('on');});
  btn.classList.add('on');
  card.dataset.tier=btn.dataset.f||'all';
  cardFilter(card);
}
function applyAcct(btn){
  var card=btn.closest('.card');
  card.querySelectorAll('.chip[data-a]').forEach(function(c){c.classList.remove('on');});
  btn.classList.add('on');
  card.dataset.acct=btn.dataset.a||'all';
  cardFilter(card);
}
function applySearch(input){
  var card=input.closest('.card');
  card.dataset.q=input.value||'';
  cardFilter(card);
}
function resetCardFilter(btn){
  var card=btn.closest('.card');
  card.dataset.tier='all'; card.dataset.acct='all'; card.dataset.q='';
  card.querySelectorAll('.chip[data-f]').forEach(function(c){c.classList.toggle('on', c.dataset.f==='all');});
  card.querySelectorAll('.chip[data-a]').forEach(function(c){c.classList.toggle('on', c.dataset.a==='all');});
  var inp=card.querySelector('input.srch');
  if(inp) inp.value='';
  cardFilter(card);
}
/* 导出当前卡片内可见表格行为 UTF-8 BOM CSV（Excel 直接打开无乱码） */
function exportTableCsv(btn, filename){
  var card=btn.closest('.card');
  var tables=card.querySelectorAll('table');
  var tb=null;
  for(var i=0;i<tables.length;i++){
    var pv=tables[i].closest('[data-pview]');
    if(!pv || pv.style.display!=='none'){ tb=tables[i]; break; }
  }
  if(!tb) return;
  function esc(s){
    s=String(s||'').replace(/\\s+/g,' ').trim();
    if(/[",\\n]/.test(s)) return '"'+s.replace(/"/g,'""')+'"';
    return s;
  }
  var lines=[];
  if(tb.tHead && tb.tHead.rows.length){
    var hdr=Array.prototype.map.call(tb.tHead.rows[0].cells,function(c){
      return esc(c.innerText.replace(/[▲▼]/g,''));
    });
    lines.push(hdr.join(','));
  }
  Array.prototype.forEach.call(tb.tBodies[0].rows,function(r){
    if(r.style.display==='none') return;
    var vals=Array.prototype.map.call(r.cells,function(c){return esc(c.innerText);});
    lines.push(vals.join(','));
  });
  var blob=new Blob(['\\uFEFF'+lines.join('\\r\\n')],{type:'text/csv;charset=utf-8;'});
  var a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=(filename||'export')+'.csv';
  document.body.appendChild(a); a.click();
  setTimeout(function(){URL.revokeObjectURL(a.href); a.remove();}, 500);
}
/* ---- SKU 图（ROI vs 保本线 / 广告渗透率）的卡片内筛选 ---- */
function _skLatest(win){
  var ks=(window.__SKUCHART||{}).wkeys||[], i, v;
  for(i=ks.length-1;i>=0;i--){v=(win||{})[ks[i]]; if(v&&v.cost>0) return v;}
  for(i=ks.length-1;i>=0;i--){v=(win||{})[ks[i]]; if(v) return v;}
  return null;
}
function _skCalc(sk, ak){
  var all=sk.a||{};
  var use=(ak==='all')?Object.keys(all):(all[ak]?[ak]:[]);
  var cost=0, amt=0, sz=0;
  use.forEach(function(k){
    var v=_skLatest(all[k]); if(!v) return;
    cost+=v.cost||0; amt+=v.amt||0; sz+=v.szAmt||0;
  });
  var share=(sz>0)?Math.round(amt/sz*1000)/10:null;
  if(share!==null && share>110) share=null;
  return {cost:cost, roi:cost?Math.round(amt/cost*100)/100:0, share:share};
}
function skuChartRender(card){
  if(!card.dataset.skc){
    var bar=card.querySelector('.toolbar[data-skc]');
    if(bar) card.dataset.skc=bar.dataset.skc;
  }
  var cid=card.dataset.skc, el=document.getElementById(cid);
  var ch=window.__CH&&window.__CH[cid], p=window.__SKUCHART;
  if(!el||!ch||!p) return;
  var ak=card.dataset.ak||'all', q=(card.dataset.q||'').trim();
  var n=parseInt(card.dataset.n||'15',10);
  var rows=[];
  (p.rows||[]).forEach(function(sk){
    if(q && !matchQuery(sk.name+' '+sk.id, q)) return;
    var m=_skCalc(sk, ak);
    if(cid==='chSkuRoi'){ if(!(m.cost>0)) return; rows.push({n:sk.name, v:m.roi, be:sk.be, c:m.cost}); }
    else { if(m.share===null) return; rows.push({n:sk.name, v:m.share, be:sk.be, c:m.cost}); }
  });
  var total=rows.length;
  rows.sort(function(a,b){return b.c-a.c;});
  if(n>0) rows=rows.slice(0,n);
  rows.reverse();
  var names=rows.map(function(r){return (r.n||'').slice(0,16);});
  var opt;
  if(cid==='chSkuRoi'){
    opt={tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},
      legend:{data:['实际ROI','保本ROI'],top:0},
      grid:{left:130,right:50,top:40,bottom:30},
      xAxis:{type:'value',name:'ROI'},
      yAxis:{type:'category',data:names,axisLabel:{fontSize:11}},
      series:[
        {name:'实际ROI',type:'bar',barMaxWidth:16,
         label:{show:true,position:'right',fontSize:11},
         data:rows.map(function(r){
           return {value:Math.round(r.v*100)/100,
                   itemStyle:{color:(r.be>0&&r.v>=r.be)?'#5db872':'#c64545'}};})},
        {name:'保本ROI',type:'bar',barMaxWidth:16,
         itemStyle:{color:'#d9d2c7'},label:{show:true,position:'right',fontSize:11},
         data:rows.map(function(r){return Math.round(r.be*100)/100;})}]};
  }else{
    opt={tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},
      grid:{left:130,right:60,top:24,bottom:30},
      xAxis:{type:'value',name:'广告成交/店铺成交 %',max:100},
      yAxis:{type:'category',data:names,axisLabel:{fontSize:11}},
      series:[{type:'bar',barMaxWidth:16,
        itemStyle:{color:'#5db8a6',borderRadius:[0,4,4,0]},
        label:{show:true,position:'right',fontSize:11,formatter:'{c}%'},
        data:rows.map(function(r){return Math.round(r.v*10)/10;}),
        markLine:{silent:true,symbol:'none',lineStyle:{color:'#c64545',type:'dashed'},
                  data:[{xAxis:100,label:{formatter:'广告=自然'}}]}}]};
  }
  el.style.height=Math.max(430, rows.length*26+80)+'px';
  opt.textStyle={fontFamily:'-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",Roboto,sans-serif',color:'#3d3d3a'};
  ch.setOption(opt,true);
  ch.resize();
  var lab=card.querySelector('.srchcount');
  if(lab) lab.textContent='显示 '+rows.length+' / '+total+' 个';
}
function skuChartAcct(btn){
  var card=btn.closest('.card');
  card.querySelectorAll('.chip[data-ak]').forEach(function(c){c.classList.remove('on');});
  btn.classList.add('on');
  card.dataset.ak=btn.dataset.ak||'all';
  skuChartRender(card);
}
function skuChartN(sel){
  var card=sel.closest('.card');
  card.dataset.n=sel.value||'15';
  skuChartRender(card);
}
function skuChartSearch(input){
  var card=input.closest('.card');
  card.dataset.q=input.value||'';
  skuChartRender(card);
}
function skuChartInit(){
  if(!window.__SKUCHART) return;
  document.querySelectorAll('.toolbar[data-skc]').forEach(function(bar){
    var card=bar.closest('.card');
    if(!card) return;
    card.dataset.skc=bar.dataset.skc;
    card.dataset.ak='all';
    card.dataset.n='15';
    skuChartRender(card);
  });
}
function applyWordType(btn){
  var val=btn.dataset.w; var card=btn.closest('.card');
  card.querySelectorAll('.toolbar .chip').forEach(function(c){c.classList.remove('on');});
  btn.classList.add('on');
  card.querySelectorAll('[data-wtype]').forEach(function(b){
    b.style.display=(val==='all'||b.dataset.wtype===val)?'':'none';
  });
}
function toggleWords(btn){
  var d=btn.nextElementSibling;
  if(!d) return;
  d.style.display=(d.style.display==='none'||!d.style.display)?'block':'none';
  btn.textContent=d.style.display==='block'?'收起':'展开';
}
function indShow(key, el){
  var root=el.closest('.indwrap')||document;
  root.querySelectorAll('.indacct').forEach(function(s){s.classList.remove('active');});
  var target=root.querySelector('.indacct[data-ind="'+key+'"]');
  if(target) target.classList.add('active');
  root.querySelectorAll('.indtab').forEach(function(t){t.classList.remove('active');});
  el.classList.add('active');
  try{
    var u=new URL(location.href); u.searchParams.set('ind_account',key);
    history.replaceState(null,'',u.toString());
  }catch(e){}
  if(target) target.querySelectorAll('.chart').forEach(function(d){
    var ch=window.__CH&&window.__CH[d.id]; if(ch){try{ch.resize();}catch(e){}}
  });
}
function indInit(){
  var root=document.querySelector('#p-ind .indwrap'); if(!root) return;
  var want=new URLSearchParams(location.search).get('ind_account');
  var tabs=root.querySelectorAll('.indtab'), tab=null;
  tabs.forEach(function(t){if(t.dataset.ind===want) tab=t;});
  if(!tab) tab=tabs[0];
  if(tab) indShow(tab.dataset.ind,tab);
}
function _initShortcuts(){
  document.addEventListener('keydown',function(e){
    var a=document.activeElement;
    var inInput=a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
    if(e.key==='Escape' && inInput && a.classList.contains('srch')){
      a.value=''; applySearch(a); a.blur(); return;
    }
    if(inInput || e.ctrlKey || e.metaKey || e.altKey) return;
    if(e.key==='/'){
      var act=document.querySelector('section.page.active input.srch');
      if(act){ e.preventDefault(); act.focus(); act.select(); }
      return;
    }
    if(/^[1-9]$/.test(e.key)){
      var tabs=document.querySelectorAll('.tabs .tab');
      var idx=parseInt(e.key,10)-1;
      if(tabs[idx]){ showTab(tabs[idx].dataset.tab, tabs[idx]); }
    }
  });
}
function _initBackToTop(){
  var btn=document.getElementById('bttBtn');
  if(!btn) return;
  window.addEventListener('scroll',function(){
    btn.classList.toggle('show', window.scrollY>420);
  },{passive:true});
  btn.addEventListener('click',function(){
    window.scrollTo({top:0,behavior:'smooth'});
  });
}
function initCharts(){
  window.__CH=window.__CH||{};
  initSort();
  _restoreTabFromHash();
  _initShortcuts();
  _initBackToTop();
  if(typeof echarts==='undefined'){
    document.querySelectorAll('.chart').forEach(function(d){
      d.innerHTML='<div style="padding:20px;color:var(--muted)">ECharts 未内联</div>';});
    return;
  }
  (window.__CHARTS||[]).forEach(function(c){
    var el=document.getElementById(c.id); if(!el) return;
    var opt=c.option||{};
    opt.textStyle={fontFamily:'-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",Roboto,sans-serif',color:'#3d3d3a'};
    var ch=echarts.init(el); ch.setOption(opt); window.__CH[c.id]=ch;
  });
  if(typeof skuChartInit==='function') skuChartInit();
  if(typeof indInit==='function') indInit();
  window.addEventListener('resize', _resizeAllCharts);
  window.addEventListener('hashchange', _restoreTabFromHash);
}
window.addEventListener('DOMContentLoaded', initCharts);
"""

AI_JS = """
function genAi(btn, start, end, keys){
  if(btn.disabled) return;
  btn.disabled=true; var old=btn.textContent; btn.textContent='生成中…';
  var qs='start='+encodeURIComponent(start)+'&end='+encodeURIComponent(end)+'&accounts='+encodeURIComponent(keys);
  fetch('/api/ai/run?'+qs,{method:'POST'}).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){ btn.disabled=false; btn.textContent=old; alert('生成失败：'+(j.error||'未知')); return; }
    var t=setInterval(function(){
      fetch('/api/ai/status').then(function(r){return r.json();}).then(function(s){
        if(!s.running){ clearInterval(t); location.reload(); return; }
        var jb=s.job||{};
        btn.textContent='思考中… '+(jb.reasoningChars||0)+' 字 / 成文 '+(jb.contentChars||0)+' 字';
      }).catch(function(){ clearInterval(t); btn.disabled=false; btn.textContent=old; });
    }, 4000);
  }).catch(function(e){ btn.disabled=false; btn.textContent=old; alert('请求失败：'+e); });
}
"""

IND_JS = """
(function(){
  var TIMER=null, TRIES=0, MAX=60, SAW=false;
  function typing(){
    var a=document.activeElement;
    return !!(a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }
  function onIndTab(){
    var p=document.getElementById('p-ind');
    return !!(p && p.classList.contains('active'));
  }
  function stop(){ if(TIMER){ clearInterval(TIMER); TIMER=null; } }
  function tick(){
    fetch('/api/industry/status').then(function(r){ return r.json(); }).then(function(j){
      TRIES++;
      if(j && j.running){ SAW=true; }
      else{
        stop();
        if(SAW && window.__IND_PENDING && onIndTab() && !typing()) location.reload();
        return;
      }
      if(TRIES>=MAX) stop();
    }).catch(function(){ if(++TRIES>=8) stop(); });
  }
  function start(){
    if(!window.__IND_PENDING || TIMER) return;
    TIMER=setInterval(tick, 8000); tick();
  }
  var prev=window.showTab;
  if(typeof prev==='function'){
    window.showTab=function(id, el){
      prev.apply(this, arguments);
      if(id==='p-ind') start();
    };
  }
  if(onIndTab()) start();
})();
"""
