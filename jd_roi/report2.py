# -*- coding: utf-8 -*-
"""新版可视化报告：跨账号 · 按 SKU 交叉 · 调整操作复盘。

自包含 HTML（内联 ECharts），6 个页签：
  总结 / 商品ROI / 调整复盘 / 计划明细 / 搜索词诊断 / 行动清单
"""
from __future__ import annotations

import html as _html
import json
import sys
from pathlib import Path

from . import config

ASSETS = Path(__file__).resolve().parent / "assets" / "echarts.min.js"

# 品牌符号：放射状尖刺（Anthropic 风格），用于导航与页头的品牌标记
SPIKE_SVG = (
    '<svg class="spike" viewBox="0 0 24 24" aria-hidden="true">'
    '<g stroke="currentColor" stroke-width="2.5" stroke-linecap="round" fill="none">'
    '<path d="M12 2.6v18.8M2.6 12h18.8M5.4 5.4l13.2 13.2M18.6 5.4L5.4 18.6"/>'
    '</g></svg>'
)

_NAV_ITEMS = [("/", "控制台"), ("/report", "分析报表"), ("/ai", "AI 分析台"),
              ("/paycomm", "CPS花费"), ("/paycomm/spend", "京准通花费"), ("/login", "登录")]


def navtop(active: str = "", right: str = "") -> str:
    """全站统一顶栏：品牌符号 + 主导航 + 右侧插槽。"""
    links = "".join(
        f'<a class="nava{" on" if href == active else ""}" href="{href}">{label}</a>'
        for href, label in _NAV_ITEMS)
    return (
        f'<nav class="navtop"><div class="wrap">'
        f'<a class="brand" href="/">{SPIKE_SVG}'
        f'<span>京东广告运营<small>AD ROI CONSOLE</small></span></a>'
        f'<div class="navlinks">{links}</div>'
        f'<div class="navspacer"></div>{right}'
        f'</div></nav>'
    )


CSS = """/* ============================================================================
   设计系统 · Claude 风（奶油画布 × 珊瑚 × 深墨）
   参考：Anthropic Claude 官网视觉语言 —— 暖色奶油底、衬线标题、珊瑚主色、
   深色产品面板。三态表面交替：cream → cream-card → dark-panel。
   ============================================================================ */
:root{
  --canvas:#faf9f5; --soft:#f5f0e8; --surface-card:#efe9de; --cream:#e8e0d2;
  --ink:#141413; --body:#3d3d3a; --strong:#252523; --muted:#6c6a64; --muted2:#8e8b82;
  --hair:#e6dfd8; --hair2:#ebe6df;
  --primary:#cc785c; --primary-a:#a9583e; --primary-dim:#e6dfd8; --on-primary:#ffffff;
  --dark:#181715; --dark-elev:#252320; --dark-soft:#1f1e1b; --on-dark:#faf9f5; --on-dark-soft:#a09d96;
  --green:#5db872; --greenbg:#e8f2ea; --greenink:#3e7d55;
  --amber:#d4a017; --amberbg:#f6efda; --amberink:#8a6a1e;
  --red:#c64545; --redbg:#f6e6e3; --redink:#a5382f;
  --teal:#5db8a6; --tealbg:#e4f1ee; --tealink:#3d7d72;
  /* 兼容旧别名 */
  --bg:var(--canvas); --card:#faf9f5; --sub:var(--muted); --line:var(--hair);
  --blue:var(--primary); --bluebg:#f4e9e3; --purple:var(--teal);
  --r-sm:8px; --r-md:12px; --r-lg:16px;
  --serif:"Palatino Linotype","Iowan Old Style",Palatino,"Book Antiqua",Georgia,
    "Songti SC","Noto Serif SC","Source Han Serif SC","SimSun",serif;
  --sans:"Inter",-apple-system,BlinkMacSystemFont,"PingFang SC","HarmonyOS Sans SC",
    "Microsoft YaHei","Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --shadow-soft:0 1px 2px rgba(20,20,19,.04), 0 10px 28px rgba(20,20,19,.05);
  --shadow-lift:0 2px 6px rgba(20,20,19,.05), 0 16px 40px rgba(20,20,19,.09);
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--canvas);color:var(--body);font-family:var(--sans);
  font-size:14px;line-height:1.65;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wrap{max-width:1480px;margin:0 auto;padding:0 24px 72px}
::selection{background:rgba(204,120,92,.22)}
/* 品牌尖刺符号：默认小尺寸，按场景微调（不给 SVG 设尺寸会撑满容器） */
.spike{width:16px;height:16px;flex:none;display:inline-block;vertical-align:-2px;color:var(--primary)}

/* ---------------------------------------------------------------- 顶部导航 */
.navtop{position:sticky;top:0;z-index:70;background:rgba(250,249,245,.88);
  -webkit-backdrop-filter:saturate(170%) blur(14px);backdrop-filter:saturate(170%) blur(14px);
  border-bottom:1px solid var(--hair)}
.navtop .wrap{display:flex;align-items:center;gap:14px;height:62px;padding-top:0;padding-bottom:0}
.brand{display:inline-flex;align-items:center;gap:9px;text-decoration:none;color:var(--ink);
  font-weight:600;font-size:15px;letter-spacing:-.2px;white-space:nowrap}
.brand .spike{width:18px;height:18px;color:var(--ink);vertical-align:0}
.brand small{display:block;font-size:10.5px;font-weight:500;color:var(--muted2);letter-spacing:1.4px;
  text-transform:uppercase;line-height:1.1}
.navlinks{display:flex;align-items:center;gap:2px;margin-left:10px;flex-wrap:wrap}
.nava{padding:7px 13px;border-radius:var(--r-sm);color:var(--muted);font-size:13.5px;font-weight:500;
  text-decoration:none;transition:background .15s,color .15s;white-space:nowrap}
.nava:hover{background:var(--soft);color:var(--ink)}
.nava.on{background:var(--surface-card);color:var(--ink);font-weight:600}
.navspacer{margin-left:auto}
.navpill{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:9999px;
  background:var(--surface-card);color:var(--strong);font-size:12px;font-weight:600}
.navpill i{width:7px;height:7px;border-radius:50%;background:var(--green);display:block}

/* ---------------------------------------------------------------- 页头 */
header.top{background:var(--soft);border-bottom:1px solid var(--hair);color:var(--ink);
  padding:30px 0 0;margin-bottom:26px}
header.top .wrap{padding-bottom:0}
h1{margin:0 0 10px;font-size:38px;font-weight:400;font-family:var(--serif);letter-spacing:-1px;
  line-height:1.12;color:var(--ink);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1 .spike{width:26px;height:26px;flex:none;color:var(--primary)}
.metaline{color:var(--muted);font-size:13px;line-height:1.7;display:flex;flex-wrap:wrap;gap:6px 14px;
  align-items:center}
.metaline .dot{width:3px;height:3px;border-radius:50%;background:var(--muted2);display:inline-block}
.eyebrow{font-size:11.5px;font-weight:600;letter-spacing:1.6px;text-transform:uppercase;color:var(--primary);
  margin-bottom:8px;display:flex;align-items:center;gap:8px}
.eyebrow .spike{width:13px;height:13px;vertical-align:0}

/* 页头（新版：紧凑、编辑感） */
.pagehead{padding:26px 0 0}
.pagehead h1{font-size:34px}
.pagehead .actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:14px}

/* ---------------------------------------------------------------- 页签 */
.tabs{display:flex;gap:4px;margin-top:22px;flex-wrap:wrap;padding-bottom:0}
.tab{padding:9px 16px;border-radius:var(--r-sm);background:transparent;color:var(--muted);
  cursor:pointer;font-weight:500;font-size:13.5px;border:1px solid transparent;user-select:none;
  transition:background .15s,color .15s}
.tab:hover{background:var(--cream);color:var(--ink)}
.tab.active{background:var(--canvas);color:var(--ink);font-weight:600;
  border-color:var(--hair);box-shadow:0 1px 0 rgba(20,20,19,.03)}
section.page{display:none}
section.page.active{display:block;animation:fadeup .28s ease}
@keyframes fadeup{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* ---------------------------------------------------------------- 卡片 */
.card{background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-lg);
  padding:24px 26px;margin-bottom:18px}
.card.tight{padding:18px 20px}
.card.dark{background:var(--dark);border-color:var(--dark);color:var(--on-dark)}
.card.dark h2,.card.dark h3{color:var(--on-dark)}
.card.dark .hint{color:var(--on-dark-soft)}
.card h2{margin:0 0 6px;font-size:22px;font-weight:400;font-family:var(--serif);letter-spacing:-.4px;
  color:var(--ink);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.card h2 .tagline{font-size:12px;font-weight:500;color:var(--muted);font-family:var(--sans);letter-spacing:0;
  background:var(--soft);border:1px solid var(--hair);padding:2px 9px;border-radius:9999px}
.card h3{margin:20px 0 8px;font-size:16px;font-weight:600;color:var(--strong)}
.hint{color:var(--muted);font-size:12.5px;margin:2px 0 14px;line-height:1.7}
.grid{display:grid;gap:14px}
.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.g3{grid-template-columns:repeat(3,minmax(0,1fr))}
.g4{grid-template-columns:repeat(4,minmax(0,1fr))}
@media(max-width:1180px){.g4{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:1100px){.g3{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:760px){.g2,.g3,.g4{grid-template-columns:1fr}}

/* ---------------------------------------------------------------- KPI */
.kpi{background:var(--surface-card);border:1px solid var(--hair);border-radius:var(--r-md);
  padding:18px 20px;display:flex;flex-direction:column;gap:6px;min-height:112px;
  transition:box-shadow .18s,border-color .18s,transform .18s}
.kpi .lbl{font-size:11.5px;font-weight:600;color:var(--muted);letter-spacing:.4px;line-height:1.5}
.kpi .val{font-size:27px;font-weight:400;font-family:var(--serif);color:var(--ink);
  font-variant-numeric:tabular-nums;letter-spacing:-.7px;line-height:1.2}
.kpi .sub{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;line-height:1.55}
.kpi.hero{background:var(--canvas);border-color:var(--hair)}
.kpi.click{cursor:pointer}
.kpi.click:hover{border-color:var(--primary);box-shadow:var(--shadow-soft);transform:translateY(-1px)}
.up{color:var(--greenink)} .down{color:var(--redink)} .flat{color:var(--muted)}
.delta{display:inline-block;font-weight:600;font-variant-numeric:tabular-nums}
.delta.up{color:var(--greenink)} .delta.down{color:var(--redink)}

/* ---------------------------------------------------------------- 徽标 */
.badge{display:inline-block;padding:3px 11px;border-radius:9999px;font-size:12px;font-weight:600;
  white-space:nowrap;line-height:1.5}
.b-grow,.b-ok{background:var(--greenbg);color:var(--greenink)}
.b-opt,.b-warn{background:var(--amberbg);color:var(--amberink)}
.b-stop,.b-bad{background:var(--redbg);color:var(--redink)}
.b-idle{background:var(--surface-card);color:var(--muted)}
.b-info{background:var(--bluebg);color:var(--primary-a)}
.b-vio{background:var(--tealbg);color:var(--tealink)}

/* ---------------------------------------------------------------- 表格 */
table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px}
th,td{padding:9px 11px;border-bottom:1px solid var(--hair);text-align:right;vertical-align:middle}
th{background:var(--soft);font-weight:600;color:var(--strong);position:sticky;top:0;z-index:2;
  cursor:pointer;white-space:nowrap;font-size:12px;letter-spacing:.2px}
th.noSort{cursor:default}
th:hover{background:var(--cream)}
.sortmark{font-size:10px;color:var(--muted2);margin-left:2px}
td.l,th.l{text-align:left}
td.mono,th.mono{font-variant-numeric:tabular-nums;font-family:var(--mono)}
tbody tr:hover{background:var(--soft)}
.tblwrap{overflow:auto;max-height:720px;border:1px solid var(--hair);border-radius:var(--r-md);background:var(--canvas)}
.tblwrap.short{max-height:460px}
.bars{display:flex;align-items:flex-end;gap:2px;height:26px}
.bars i{width:5px;border-radius:1.5px;background:var(--muted2);display:block}
.bars i.hi{background:var(--green)} .bars i.lo{background:var(--red)}

/* ---------------------------------------------------------------- 商品单元 */
.prod{display:flex;align-items:center;gap:10px;min-width:250px;text-align:left}
.prod img{width:38px;height:38px;border-radius:var(--r-sm);object-fit:cover;background:var(--soft);flex:none;
  border:1px solid var(--hair)}
.prod .nm{font-weight:600;font-size:12.5px;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.prod .sid{font-size:11px;color:var(--muted);font-family:var(--mono)}

/* ---------------------------------------------------------------- 图表 */
.chart{width:100%;height:330px}
.chart.tall{height:430px}

/* ---------------------------------------------------------------- 控件 */
.btn{display:inline-flex;align-items:center;gap:7px;background:var(--primary);color:var(--on-primary);
  border:1px solid var(--primary);border-radius:var(--r-sm);padding:9px 17px;font-weight:500;cursor:pointer;
  font-size:13px;text-decoration:none;line-height:1.2;transition:background .15s,border-color .15s,box-shadow .15s}
.btn:hover{background:var(--primary-a);border-color:var(--primary-a)}
.btn.sec{background:var(--canvas);color:var(--ink);border-color:var(--hair)}
.btn.sec:hover{background:var(--soft);border-color:var(--cream)}
.btn.ghost{background:transparent;color:var(--muted);border-color:transparent}
.btn.ghost:hover{background:var(--soft);color:var(--ink)}
.btn.dark{background:var(--dark);border-color:var(--dark);color:var(--on-dark)}
.btn.dark:hover{background:var(--dark-elev);border-color:var(--dark-elev)}
.btn.danger{background:var(--red);border-color:var(--red);color:#fff}
.btn.danger:hover{background:var(--redink);border-color:var(--redink)}
.btn.mini{padding:5px 11px;font-size:12px;border-radius:7px}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.chip{padding:6px 14px;border-radius:9999px;border:1px solid var(--hair);background:var(--canvas);cursor:pointer;
  font-size:12.5px;font-weight:500;color:var(--muted);transition:all .15s;line-height:1.4}
.chip:hover{border-color:var(--primary);color:var(--ink)}
.chip.on{background:var(--surface-card);color:var(--ink);border-color:var(--cream);font-weight:600}
input,select,textarea{font-family:inherit}
input[type=date],input[type=text],select{background:var(--canvas);border:1px solid var(--hair);
  border-radius:var(--r-sm);padding:7px 10px;font-size:13px;color:var(--ink)}
input[type=date]:focus,select:focus,textarea:focus{outline:none;border-color:var(--primary);
  box-shadow:0 0 0 3px rgba(204,120,92,.15)}
input[type=checkbox]{accent-color:var(--primary)}

/* ---------------------------------------------------------------- 提示块 */
.note{background:var(--soft);border-left:3px solid var(--hair);padding:12px 16px;border-radius:0 var(--r-sm) var(--r-sm) 0;
  font-size:12.5px;color:var(--body);margin:12px 0;line-height:1.75}
.warnbox{background:var(--amberbg);border-left:3px solid var(--amber);padding:12px 16px;
  border-radius:0 var(--r-sm) var(--r-sm) 0;font-size:12.5px;color:var(--amberink);margin:12px 0;line-height:1.75}
.dangerbox{background:var(--redbg);border-left:3px solid var(--red);padding:12px 16px;
  border-radius:0 var(--r-sm) var(--r-sm) 0;font-size:12.5px;color:var(--redink);margin:12px 0;line-height:1.75}
.covbox{background:var(--amberbg);border-left:3px solid var(--amber);padding:12px 16px;
  border-radius:0 var(--r-sm) var(--r-sm) 0;font-size:12.5px;color:var(--amberink);margin-bottom:16px;line-height:1.75}
ul.sum{margin:8px 0 0;padding-left:20px}
ul.sum li{margin:7px 0;line-height:1.7}
.opDetail{font-size:11.5px;color:var(--muted);max-width:560px;word-break:break-word}

/* ---------------------------------------------------------------- 时间线 */
.evt{border-left:2px solid var(--hair);padding:9px 0 9px 16px;position:relative;margin-left:6px}
.evt:before{content:"";position:absolute;left:-6px;top:15px;width:9px;height:9px;border-radius:50%;
  background:var(--muted2);border:2px solid var(--canvas)}
.evt.ok{border-left-color:var(--green)} .evt.ok:before{background:var(--green)}
.evt.bad{border-left-color:var(--red)} .evt.bad:before{background:var(--red)}
.evt.warn{border-left-color:var(--amber)} .evt.warn:before{background:var(--amber)}
.evt .h{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.evt .t{font-family:var(--mono);font-size:12px;color:var(--muted);flex:none}
.evt .a{font-weight:600;color:var(--ink)}

/* ---------------------------------------------------------------- 杂项 */
.mini{font-size:12px;color:var(--muted)}
.qtysub{display:block;font-size:11px;color:var(--muted2);font-weight:400;line-height:1.2}
.pill{display:inline-block;background:var(--soft);border:1px solid var(--hair);border-radius:9999px;
  padding:1px 9px;font-size:11.5px;color:var(--muted);margin-right:4px}
code{background:var(--soft);border:1px solid var(--hair);padding:1px 6px;border-radius:5px;
  font-size:12px;font-family:var(--mono);color:var(--strong)}
a{color:var(--primary)}
a.lk{color:var(--primary);text-decoration:none;font-weight:500}
a.lk:hover{text-decoration:underline}

/* ---------------------------------------------------------------- 深色面板（代码/日志） */
.panel-dark{background:var(--dark);border:1px solid var(--dark-elev);border-radius:var(--r-md);
  padding:14px 16px;color:var(--on-dark);font-family:var(--mono);font-size:12px;line-height:1.75;
  white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto}

/* ---------------------------------------------------------------- 页脚 */
.foot{background:var(--dark);color:var(--on-dark-soft);font-size:12px;text-align:center;margin-top:30px;
  line-height:1.95;border-radius:var(--r-lg);padding:32px 28px}
.foot b{color:var(--on-dark)}
.foot code{background:var(--dark-elev);border-color:var(--dark-elev);color:var(--on-dark)}
"""

JS = """window.__CH=window.__CH||{};
function showTab(id, el){
  document.querySelectorAll('section.page').forEach(function(s){s.classList.remove('active');});
  document.getElementById(id).classList.add('active');
  document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active');});
  el.classList.add('active');
  Object.keys(window.__CH||{}).forEach(function(k){try{window.__CH[k].resize();}catch(e){}});
  window.scrollTo({top:0,behavior:'smooth'});
}
function sortTable(th){
  var tb=th.closest('table');
  var idx=Array.prototype.indexOf.call(th.parentNode.children,th);
  var asc=!(th.dataset.asc==='1'); th.dataset.asc=asc?'1':'0';
  // 清掉同表其它表头的排序标记，只保留当前列
  Array.prototype.forEach.call(th.parentNode.children,function(h){
    if(h!==th){h.dataset.asc='';var m=h.querySelector('.sortmark');if(m)m.remove();}
  });
  var rows=Array.prototype.slice.call(tb.tBodies[0].rows);
  var num=th.dataset.num==='1';
  rows.sort(function(a,b){
    var ca=a.cells[idx], cb=b.cells[idx];
    var x=ca?(ca.dataset.v!==undefined?ca.dataset.v:ca.innerText):'';
    var y=cb?(cb.dataset.v!==undefined?cb.dataset.v:cb.innerText):'';
    if(num){var fx=parseFloat(x), fy=parseFloat(y); if(isNaN(fx))fx=-1e18; if(isNaN(fy))fy=-1e18; return asc?fx-fy:fy-fx;}
    return asc?String(x).localeCompare(String(y),'zh'):String(y).localeCompare(String(x),'zh');
  });
  rows.forEach(function(r){tb.tBodies[0].appendChild(r);});
  var mk=th.querySelector('.sortmark')||th.appendChild(document.createElement('span'));
  mk.className='sortmark'; mk.textContent=asc?' ▲':' ▼';
}
function initSort(){
  document.querySelectorAll('table thead th').forEach(function(th){
    if(th.classList.contains('noSort'))return;
    th.addEventListener('click',function(){sortTable(th);});
  });
}
function applyTier(btn){
  var val=btn.dataset.f; var card=btn.closest('.card');
  card.querySelectorAll('.chip').forEach(function(c){c.classList.remove('on');});
  btn.classList.add('on');
  card.querySelectorAll('table tbody tr').forEach(function(tr){
    var t=tr.dataset.tier||'';
    tr.style.display=(val==='all'||t===val)?'':'none';
  });
}
function applyAcct(btn){
  var val=btn.dataset.a; var card=btn.closest('.card');
  card.querySelectorAll('.chip').forEach(function(c){c.classList.remove('on');});
  btn.classList.add('on');
  card.querySelectorAll('table tbody tr').forEach(function(tr){
    var t=(tr.dataset.acct||'').split(',');
    tr.style.display=(val==='all'||t.indexOf(val)>=0)?'':'none';
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
function initCharts(){
  window.__CH=window.__CH||{};
  initSort();
  if(typeof echarts==='undefined'){
    document.querySelectorAll('.chart').forEach(function(d){
      d.innerHTML='<div style="padding:20px;color:var(--muted)">ECharts 未内联</div>';});
    return;
  }
  (window.__CHARTS||[]).forEach(function(c){
    var el=document.getElementById(c.id); if(!el) return;
    var opt=c.option||{}; opt.textStyle={fontFamily:'-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",Roboto,sans-serif',color:'#3d3d3a'};
    var ch=echarts.init(el); ch.setOption(opt); window.__CH[c.id]=ch;
  });
  window.addEventListener('resize',function(){
    Object.keys(window.__CH).forEach(function(k){try{window.__CH[k].resize();}catch(e){}});});
}
window.addEventListener('DOMContentLoaded', initCharts);
"""


# ---------------------------------------------------------------- 基础工具
def _e(s) -> str:
    # 接口返回里含 &amp; / &ldquo; 等实体，先解码再转义，避免页面出现 &amp;amp;
    return _html.escape(_html.unescape(str("" if s is None else s)), quote=True)


def _n(v, d=0.0) -> float:
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def _money(v, dec=0) -> str:
    return f"{_n(v):,.{dec}f}"


def _num(v, dec=2) -> str:
    return f"{_n(v):,.{dec}f}"


def _pct(v, dec=1) -> str:
    return "—" if v is None else f"{_n(v):.{dec}f}%"


def _delta(cur, pre, better_high=True, dec=1) -> tuple:
    cur, pre = _n(cur), _n(pre)
    if abs(pre) < 1e-9:
        return ("", "flat")
    d = (cur - pre) / abs(pre) * 100
    good = d > 0 if better_high else d < 0
    if abs(d) < 0.05:
        return (f"{d:+.{dec}f}%", "flat")
    return (f"{d:+.{dec}f}%", "up" if good else "down")


def _badge(tier) -> str:
    m = {"放大": "b-grow", "优化": "b-opt", "止损": "b-stop", "严重亏损": "b-bad",
         "未投放": "b-idle", "正确": "b-ok", "有问题": "b-bad", "有效但不彻底": "b-warn",
         "存疑": "b-warn", "效果不明显": "b-info", "已止损": "b-ok", "观察中": "b-info",
         "待观察": "b-idle", "无法评估": "b-idle", "无基线": "b-idle"}
    return f'<span class="badge {m.get(tier, "b-idle")}">{_e(tier)}</span>'


def _roi_cls(roi, be) -> str:
    roi, be = _n(roi), _n(be, 99)
    if roi <= 0:
        return "flat"
    if roi >= 4:
        return "up"
    if roi >= be:
        return ""
    return "down"


def _latest(s) -> dict:
    w2, w1 = s["wins"]["w2"], s["wins"]["w1"]
    return w2 if _n(w2["cost"]) > 0 else w1


def _latest_key(s) -> str:
    return "w2" if _n(s["wins"]["w2"]["cost"]) > 0 else "w1"


def _spark(vals, be) -> str:
    mx = max([_n(v) for v in vals] + [1e-9])
    out = []
    for v in vals:
        h = max(int(_n(v) / mx * 24), 2)
        cls = "hi" if _n(v) >= 4 else ("lo" if _n(v) < _n(be, 99) else "")
        out.append(f'<i class="{cls}" style="height:{h}px" title="ROI {_n(v):.2f}"></i>')
    return '<div class="bars">' + "".join(out) + "</div>"


def _sku_advice(s) -> str:
    be = _n(s.get("breakeven"), 4.00)
    cur = _latest(s)
    roi, cost = _n(cur["roi"]), _n(cur["cost"])
    tier = s["verdict"]["tier"]
    if tier == "放大":
        return f"ROI {roi:.2f} 明显高于保本 {be:.2f}：日预算 +20~30%，核心词出价 +10~20%，当作增量池"
    if tier == "优化":
        return f"ROI {roi:.2f} 刚过保本 {be:.2f}：先抠掉 0 单词与低效词，再谈加预算"
    if tier == "止损":
        return f"ROI {roi:.2f} < 保本 {be:.2f}：广告在亏（花 ¥{_money(cost)}），但自然成交兜住整体，降预算+否定废词，别直接关"
    if tier == "严重亏损":
        return f"ROI {roi:.2f} < 保本 {be:.2f} 且扣掉广告费为负：优先降预算 50% 或暂停，预算让给高 ROI 商品"
    return "本周无广告投放"


def _plan_advice(p) -> str:
    be = _n(p.get("breakeven"), 4.00)
    w0, w1, w2 = p["wins"]["w0"], p["wins"]["w1"], p["wins"]["w2"]
    cur = w2 if _n(w2["cost"]) > 0 else w1
    roi, cost = _n(cur["roi"]), _n(cur["cost"])
    t = p["tier"]
    if t == "放大":
        return f"ROI {roi:.2f} ≥ 4：加预算 20~30%，可复制到同款 SKU"
    if t == "优化":
        return f"ROI {roi:.2f} 在保本 {be:.2f} 之上：抠废词、压 CPC，目标提到 {max(roi + 0.4, be + 0.3):.1f}"
    if t == "止损":
        d = f"ROI {roi:.2f} < 保本 {be:.2f}，花费 ¥{_money(cost)}：先降到保本以上再加量"
        if _n(w1["cost"]) > 0 and _n(w0["cost"]) > 0:
            d += "；花费 " + _money(w0["cost"]) + " → " + _money(w1["cost"]) + " → " + _money(w2["cost"])
        return d
    return "本周无花费"

# ---------------------------------------------------------------- 图表
_PALETTE = ["#cc785c", "#5db8a6", "#e8a55a", "#a9583e", "#5db872", "#8e8b82", "#c64545", "#181715"]


def _win_labels(d):
    return [w["label"].replace(" ", "\n") for w in d["meta"]["windows"]]


def build_charts(d):
    comb = d["advice"]["combined"]
    wkeys = [w["key"] for w in d["meta"]["windows"]]
    cats = _win_labels(d)
    charts = []

    # 1) 合计：花费/成交 + ROI
    charts.append({"id": "chTrend", "option": {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["花费", "成交", "ROI"], "top": 0},
        "grid": {"left": 70, "right": 60, "top": 44, "bottom": 52},
        "xAxis": {"type": "category", "data": cats, "axisLabel": {"lineHeight": 15}},
        "yAxis": [
            {"type": "value", "name": "金额(元)"},
            {"type": "value", "name": "ROI", "splitLine": {"show": False}}],
        "series": [
            {"name": "花费", "type": "bar", "data": [round(_n(comb[k]["cost"]), 0) for k in wkeys],
             "itemStyle": {"color": "#e8a55a", "borderRadius": [4, 4, 0, 0]}, "barMaxWidth": 46,
             "label": {"show": True, "position": "top", "fontSize": 11}},
            {"name": "成交", "type": "bar", "data": [round(_n(comb[k]["amt"]), 0) for k in wkeys],
             "itemStyle": {"color": "#cc785c", "borderRadius": [4, 4, 0, 0]}, "barMaxWidth": 46,
             "label": {"show": True, "position": "top", "fontSize": 11}},
            {"name": "ROI", "type": "line", "yAxisIndex": 1, "smooth": True, "symbolSize": 10,
             "data": [round(_n(comb[k]["roi"]), 2) for k in wkeys],
             "itemStyle": {"color": "#5db872"}, "lineStyle": {"width": 3},
             "label": {"show": True, "fontSize": 12, "fontWeight": "bold"}}]}})

    # 2) 分账号 花费 & ROI
    accts = d["accounts"]
    series = []
    for i, a in enumerate(accts):
        series.append({"name": a["label"] + " 花费", "type": "bar", "barMaxWidth": 34,
                       "itemStyle": {"color": _PALETTE[i * 2 % len(_PALETTE)], "borderRadius": [4, 4, 0, 0]},
                       "data": [round(_n(a["totals"][k]["cost"]), 0) for k in wkeys]})
    for i, a in enumerate(accts):
        series.append({"name": a["label"] + " ROI", "type": "line", "yAxisIndex": 1, "smooth": True,
                       "symbolSize": 9, "itemStyle": {"color": _PALETTE[(i * 2 + 1) % len(_PALETTE)]},
                       "lineStyle": {"width": 3, "type": "dashed"},
                       "data": [round(_n(a["totals"][k]["roi"]), 2) for k in wkeys]})
    charts.append({"id": "chAcct", "option": {
        "tooltip": {"trigger": "axis"},
        "legend": {"top": 0, "type": "scroll"},
        "grid": {"left": 70, "right": 60, "top": 52, "bottom": 52},
        "xAxis": {"type": "category", "data": cats, "axisLabel": {"lineHeight": 15}},
        "yAxis": [{"type": "value", "name": "花费(元)"},
                  {"type": "value", "name": "ROI", "splitLine": {"show": False}}],
        "series": series}})

    # 3) SKU：实际 ROI vs 保本 ROI（花费前 15）
    skus = [s for s in d["skus"] if _n(_latest(s)["cost"]) > 0][:15]
    names = [(s["name"] or s["skuId"])[:16] for s in skus][::-1]
    roi = [round(_n(_latest(s)["roi"]), 2) for s in skus][::-1]
    be = [round(_n(s["breakeven"]), 2) for s in skus][::-1]
    colors = ["#5db872" if _n(_latest(s)["roi"]) >= _n(s["breakeven"]) else "#c64545" for s in skus][::-1]
    charts.append({"id": "chSkuRoi", "option": {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["实际ROI", "保本ROI"], "top": 0},
        "grid": {"left": 130, "right": 50, "top": 40, "bottom": 30},
        "xAxis": {"type": "value", "name": "ROI"},
        "yAxis": {"type": "category", "data": names, "axisLabel": {"fontSize": 11}},
        "series": [
            {"name": "实际ROI", "type": "bar", "data": [{"value": v, "itemStyle": {"color": c}}
                                                        for v, c in zip(roi, colors)],
             "barMaxWidth": 16, "label": {"show": True, "position": "right", "fontSize": 11}},
            {"name": "保本ROI", "type": "bar", "data": be, "barMaxWidth": 16,
             "itemStyle": {"color": "#d9d2c7"},
             "label": {"show": True, "position": "right", "fontSize": 11}}]}})

    # 4) 广告渗透率
    pen = [s for s in d["skus"] if s["wins"]["w2"].get("adShare") or s["wins"]["w1"].get("adShare")][:15]
    pnames = [(s["name"] or s["skuId"])[:16] for s in pen][::-1]
    pval = [round(_n(_latest(s).get("adShare")), 1) for s in pen][::-1]
    charts.append({"id": "chShare", "option": {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "grid": {"left": 130, "right": 60, "top": 24, "bottom": 30},
        "xAxis": {"type": "value", "name": "广告成交/店铺成交 %", "max": 100},
        "yAxis": {"type": "category", "data": pnames, "axisLabel": {"fontSize": 11}},
        "series": [{"type": "bar", "data": pval, "barMaxWidth": 16,
                    "itemStyle": {"color": "#5db8a6", "borderRadius": [0, 4, 4, 0]},
                    "label": {"show": True, "position": "right", "fontSize": 11, "formatter": "{c}%"},
                    "markLine": {"silent": True, "symbol": "none",
                                 "lineStyle": {"color": "#c64545", "type": "dashed"},
                                 "data": [{"xAxis": 100, "label": {"formatter": "广告=自然"}}]}}]}})

    # 5) 人工调整前后 ROI
    _seen_plan = set()
    _adj_all = [a for a in d["adjustments"]
                if a.get("before") and a.get("after") and _n(a["before"]["cost"]) > 1]
    adj = []
    for a in sorted(_adj_all, key=lambda x: x["time"], reverse=True):
        key = (a["account"], a["plan"])
        if key in _seen_plan:
            continue
        _seen_plan.add(key)
        adj.append(a)
    adj = sorted(adj, key=lambda x: -(x["after"]["roi"] - x["before"]["roi"]))[:18]
    if adj:
        an = [(a["plan"] or "")[:18] for a in adj][::-1]
        b = [round(_n(a["before"]["roi"]), 2) for a in adj][::-1]
        aft = [round(_n(a["after"]["roi"]), 2) for a in adj][::-1]
        charts.append({"id": "chAdj", "option": {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "legend": {"data": ["调整前7天ROI", "调整后7天ROI"], "top": 0},
            "grid": {"left": 140, "right": 50, "top": 40, "bottom": 30},
            "xAxis": {"type": "value", "name": "ROI"},
            "yAxis": {"type": "category", "data": an, "axisLabel": {"fontSize": 11}},
            "series": [
                {"name": "调整前7天ROI", "type": "bar", "data": b, "barMaxWidth": 13,
                 "itemStyle": {"color": "#b9b1a4"}, "label": {"show": True, "position": "right", "fontSize": 10}},
                {"name": "调整后7天ROI", "type": "bar", "data": aft, "barMaxWidth": 13,
                 "itemStyle": {"color": "#cc785c"}, "label": {"show": True, "position": "right", "fontSize": 10}}]}})

    # 6) 生意大盘：商智访客 / 广告点击 / 订单
    sv, sc, so = [], [], []
    for k in wkeys:
        tot_v = 0
        for a in accts:
            f = a["totals"][k].get("flow") or {}
            tot_v += _n(f.get("visitors"))
        sv.append(int(tot_v))
        sc.append(int(sum(_n(a["totals"][k]["clk"]) for a in accts)))
        so.append(int(sum(_n(a["totals"][k]["ord"]) for a in accts)))
    charts.append({"id": "chFlow", "option": {
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["店铺访客", "广告点击", "广告订单"], "top": 0},
        "grid": {"left": 70, "right": 30, "top": 44, "bottom": 52},
        "xAxis": {"type": "category", "data": cats, "axisLabel": {"lineHeight": 15}},
        "yAxis": {"type": "value", "name": "数量"},
        "series": [
            {"name": "店铺访客", "type": "bar", "data": sv, "barMaxWidth": 44,
             "itemStyle": {"color": "#5db8a6", "borderRadius": [4, 4, 0, 0]}},
            {"name": "广告点击", "type": "bar", "data": sc, "barMaxWidth": 44,
             "itemStyle": {"color": "#e8a55a", "borderRadius": [4, 4, 0, 0]}},
            {"name": "广告订单", "type": "bar", "data": so, "barMaxWidth": 44,
             "itemStyle": {"color": "#5db872", "borderRadius": [4, 4, 0, 0]}}]}})
    return charts


def _chart_html(cid, tall=False):
    return f'<div class="chart{" tall" if tall else ""}" id="{cid}"></div>'


def _kpi(label, main, sub="", cls="") -> str:
    return (f'<div class="kpi"><div class="lbl">{label}</div>'
            f'<div class="val {cls}">{main}</div><div class="sub">{sub}</div></div>')


# ---------------------------------------------------------------- 页面：总览
def _seq(vals, fmt=lambda v: f"{_n(v):,.0f}") -> str:
    return " → ".join(fmt(v) for v in vals)


def page_overview(d) -> str:
    comb = d["advice"]["combined"]
    wkeys = [w["key"] for w in d["meta"]["windows"]]
    cur = wkeys[-1] if _n(comb[wkeys[-1]]["cost"]) > 0 else wkeys[-2]
    pre = wkeys[0]
    c = comb[cur]
    adv = d["advice"]
    manual = [o for o in d["ops"] if o["manual"]]
    right = [a for a in d["adjustments"] if a["verdict"] == "正确"]
    wrong = [a for a in d["adjustments"] if a["verdict"] == "有问题"]

    dc, dccls = _delta(c["cost"], comb[pre]["cost"], better_high=False)
    da, dacls = _delta(c["amt"], comb[pre]["amt"])
    dr, drcls = _delta(c["roi"], comb[pre]["roi"])

    kpis = "".join([
        _kpi(_acct_scope(d) + "广告花费（所选区间 " +
             f'{_e((d["meta"].get("range") or {}).get("start",""))[5:]}~{_e((d["meta"].get("range") or {}).get("end",""))[5:]}）',
             "¥" + _money(c["cost"]),
             f'<span class="{dccls}">{dc}</span> vs 前一期 ¥{_money(comb[pre]["cost"])}'),
        _kpi("广告成交额", "¥" + _money(c["amt"]),
             f'<span class="{dacls}">{da}</span> vs 前一期 ¥{_money(comb[pre]["amt"])}'),
        _kpi("整体投产比 ROI", f'{_n(c["roi"]):.2f}',
             f'<span class="{drcls}">{dr}</span> ｜ {_be_label(d)} {_be_all(d):.2f}',
             drcls),
        _kpi("店铺总成交（商智）", "¥" + _money(c.get("szAmt")),
             (f'广告贡献 {_pct(c.get("adShare"))} ｜ 自然成交 ¥{_money(max(_n(c.get("szAmt")) - _n(c["amt"]), 0))}'
              if _n(c.get("szAmt")) else '<span class="down">商智数据缺失</span>')),
        _kpi("广告订单 / CPA", f'{int(_n(c["ord"])):,} 单',
             f'单均获客成本 ¥{_n(c["cpa"]):.2f} ｜ 点击 {int(_n(c["clk"])):,} ｜ CPC ¥{_n(c["cpc"]):.2f}'),
        _kpi("人工调整 / 系统自动改预算",
             f'{len(manual)} / {len([o for o in d["ops"] if o["kind"] == "autoBudget"])} 条',
             f'可对比前后效果 {len(right) + len(wrong) + len([o for o in d["adjustments"] if o["verdict"] in ("效果不明显", "存疑", "已止损")])} 条：'
             f'正确 {len(right)} ｜ 有问题 {len(wrong)}'),
    ])

    acct_rows = []
    for a in d["accounts"]:
        t = a["totals"]
        cells = f'<td class="l"><b>{_e(a["label"])}</b><div class="mini">ID {_e(a["accountId"])}</div></td>'
        for k in wkeys:
            m = t[k]
            cls = _roi_cls(m["roi"], _be_all(d))
            cells += (f'<td class="mono">{_money(m["cost"])}</td>'
                      f'<td class="mono">{_money(m["amt"])}</td>'
                      f'<td class="mono {cls}"><b>{_n(m["roi"]):.2f}</b></td>'
                      f'<td class="mono">{_pct(m.get("adShare"))}</td>')
        acct_rows.append("<tr>" + cells + "</tr>")

    ths = '<th class="l noSort">账号</th>'
    for w in d["meta"]["windows"]:
        ths += f'<th class="mono noSort" colspan="4">{_e(w["label"])}</th>'

    sz_notes = []
    for a in d["accounts"]:
        f = a["totals"][wkeys[-1]].get("flow") or a["totals"][wkeys[-2]].get("flow") or {}
        if f:
            sz_notes.append(f'{_e(a["label"])} 店铺访客 {int(_n(f.get("visitors"))):,}、'
                            f'浏览量 {int(_n(f.get("views"))):,}、转化率 {_n(f.get("cvr")):.2f}%')

    return f"""
<div class="card">
  <h2>核心结论 <span class="tagline">{_e(_acct_scope(d))}合并口径</span></h2>
  <ul class="sum">{''.join(f"<li>{_e(s)}</li>" for s in adv["summary"])}</ul>
</div>

<div class="grid g3">{kpis}</div>

<div class="card" style="margin-top:16px">
  <h2>三期趋势 <span class="tagline">前两期 / 前一期 / 所选区间</span></h2>
  <p class="hint">金额单位：元。ROI 走右轴。{"；".join(_e(x) for x in sz_notes)}</p>
  {_chart_html("chTrend")}
</div>

<div class="card">
  <h2>分账号表现</h2>
  <p class="hint">主账号以「全站智能推广」为主、账号B以「智能化/关键词」为主，两者打法不同，需分开看。</p>
  {_chart_html("chAcct")}
  <div class="tblwrap short" style="margin-top:14px">
    <table><thead><tr>{ths}</tr></thead><tbody>{''.join(acct_rows)}</tbody></table>
  </div>
</div>

<div class="card">
  <h2>流量与转化大盘</h2>
  <p class="hint">店铺访客来自商智；广告点击/订单来自京准通。若访客远大于广告点击，说明自然流量占比高。</p>
  {_chart_html("chFlow")}
</div>

<div class="card">
  <h2>口径与成本假设 <span class="tagline">重要：这些是推算值</span></h2>
  <div class="note">
    <b>数据口径</b>：广告=京准通概览（点击15天/成交订单）；智能投放=智能投放报表；商智=各店铺成交口径。
    搜索词报表按「花费」降序取前 2000 行，覆盖约 99.9% 花费。
  </div>
  {_source_note(d)}
  {_cost_block(d)}
  <div class="note">
    <b>毛利率敏感性</b>：保本 ROI = 1 ÷ 毛利率。毛利率 20% → 保本 5.00；25% → 4.00；30% → 3.33；35% → 2.86。
    未接入真实成本的 SKU（下表标 <b>*</b>）仍按全店 {_n(d["meta"]["breakevenFlat"]):.2f} 判定，
    这部分标「放大」的商品里会有一部分其实只是打平。
  </div>
  <p class="hint">生成时间 {_e(d["meta"]["generatedAt"])} ｜ 页签顺序即为建议的阅读顺序</p>
</div>
"""


# ---------------------------------------------------------------- 页面：商品 ROI 交叉表
def _be_all(d) -> float:
    """整店一条线：有真实成本时是「按本期成交额加权」的线，否则退回全店 Excel 线。"""
    m = d.get("meta") or {}
    return _n(m.get("breakevenBlend")) or _n(m.get("breakevenFlat"), 4.00)


def _be_label(d) -> str:
    m = d.get("meta") or {}
    return "加权保本线" if _n(m.get("breakevenBlend")) else "全店保本线"


def _cost_tip(s) -> str:
    """保本线旁边那个悬停：这条线是怎么来的，一眼能看到成本明细。"""
    cp = s.get("costParams") or {}
    if s.get("costSource") != "erp":
        return (f"未接入该 SKU 真实成本：保本线按全店 Excel 口径 {_n(s.get('breakeven')):.2f}；"
                f"毛利/件 = 前台件单价 × 全店毛利率")
    tip = (f"每件（ERP 真实口径）：到手结算价 ¥{_n(cp.get('supply'))}"
           f" ＝ 前台件单价 ¥{_n(cp.get('aov'))} × 到手率 {_n(cp.get('settleRate')) * 100:.0f}%"
           f"（扣点与京东差价已扣）"
           f"；− 货款 ¥{_n(cp.get('productCost'))} − 运费 ¥{_n(cp.get('shipping'))}"
           f" − 包材 ¥{_n(cp.get('package'))} − 人工 ¥{_n(cp.get('labor'))}"
           f" = 贡献 ¥{_n(cp.get('grossPerOrder'))}"
           f" → 占前台成交额 {_n(cp.get('margin')) * 100:.1f}% ⇒ 保本 {_n(s.get('breakeven')):.2f}"
           f"、放量 {_n(s.get('growLine')):.2f}"
           f"；占到手结算价 {_n(cp.get('marginSettle')) * 100:.1f}%（ERP 毛利率口径）"
           f" ⇒ 到手结算口径保本 {_n(cp.get('breakevenSettle')):.2f}"
           f"。判定用前者：京准通报的 ROI 分子是<b>前台成交额</b>（实测广告客单价≈商智客单价）")
    if _n(cp.get("unitFactor"), 1.0) != 1.0:
        tip += f"；单位换算 ×{_n(cp.get('unitFactor'))}（{cp.get('unitFactorSource')}）"
    if cp.get("unitWarn"):
        tip += f"；⚠ {cp.get('unitWarn')}"
    if cp.get("erpName"):
        tip += f"；ERP 品名 {cp.get('erpName')}"
    return tip


def _supply_cell(s) -> str:
    cp = s.get("costParams") or {}
    if s.get("costSource") != "erp":
        return '<td class="mono" data-v="0">—</td>'
    rate = _n(cp.get("settleRate")) * 100
    return (f'<td class="mono" data-v="{_n(cp.get("supply"))}" title="{_e(_cost_tip(s))}">'
            f'{_money(cp.get("supply"), 1)}'
            f'<span class="mini" style="color:var(--muted)"> {rate:.0f}%</span></td>')


def _roi_tip(s, roi, be) -> str:
    """ROI 悬停：平台报的是前台成交额，顺手折算成「每花 1 元真正回到我们账户多少钱」。"""
    cp = s.get("costParams") or {}
    rate = _n(cp.get("settleRate"))
    if s.get("costSource") != "erp" or rate <= 0:
        return "广告 ROI = 前台成交额 ÷ 花费（平台口径）"
    return (f"前台口径：ROI {roi:.2f}（分子是消费者实付）vs 保本 {be:.2f} ⇒ 判定用这一对\n"
            f"到手结算口径：{roi:.2f} × 到手率 {rate * 100:.0f}% = {roi * rate:.2f}，"
            f"即每花 1 元广告，实际到手结算 {roi * rate:.2f} 元；"
            f"该口径保本 {cp.get('breakevenSettle')}（两对不能混用）")


def _natural_qty(m) -> float:
    """自然成交件数 = 商智成交件数 − 广告订单数（近似：广告一单一弊，仅用于量级参考）。"""
    q = _n(m.get("szQty"))
    if q <= 0:
        return 0.0
    return max(q - _n(m.get("ord")), 0.0)


def _sku_row(i, s, labels) -> str:
    cur = _latest(s)
    wk = _latest_key(s)
    roi, be = _n(cur["roi"]), _n(s["breakeven"])
    gap = roi - be
    accts = ",".join(s.get("adAccounts") or [])
    pills = "".join(f'<span class="pill">{_e(labels.get(x, x))}</span>' for x in (s.get("adAccounts") or []))
    img = (f'<img src="{_e(s["img"])}" loading="lazy" alt="" onerror="this.style.visibility=\'hidden\'">'
           if s.get("img") else '<img alt="" style="visibility:hidden">')
    shops = "／".join(f'{_e(k)} ¥{_money(v)}' for k, v in (s.get("shops") or {}).items())
    spark = _spark([s["wins"][k]["roi"] for k in ("w0", "w1", "w2")], be)
    tier = s["verdict"]["tier"]
    return (
        f'<tr data-tier="{_e(tier)}" data-acct="{_e(accts)}">'
        f'<td class="mono">{i}</td>'
        f'<td class="l"><div class="prod">{img}<div><div class="nm" title="{_e(s["name"])}｜{_e(shops)}">'
        f'{_e(s["name"])}</div><div class="sid">{_e(s["skuId"])}</div></div></div></td>'
        f'<td class="l">{pills or "—"}</td>'
        f'<td class="mono" data-v="{_n(cur["cost"])}">{_money(cur["cost"])}</td>'
        f'<td class="mono" data-v="{_n(cur["amt"])}">{_money(cur["amt"])}</td>'
        f'<td class="mono {_roi_cls(roi, be)}" data-v="{roi}" title="{_e(_roi_tip(s, roi, be))}">{roi:.2f}</td>'
        f'<td class="mono" data-v="{be}" title="{_e(_cost_tip(s))}">'
        f'{be:.2f}{"*" if s.get("costSource") != "erp" else ""}</td>'
        f'<td class="mono" data-v="{_n(cur["ord"])}">{int(_n(cur["ord"]))}</td>'
        f'<td class="mono" data-v="{_n(cur["cpa"])}">{_num(cur["cpa"], 2)}</td>'
        f'<td class="mono" data-v="{_n(s.get("unitPrice"))}" '
        f'title="前台件单价＝商智成交额÷件数（消费者实付；扣点与京东差价已从里面扣掉才到我们手里）">'
        f'{_money(s.get("unitPrice"), 1)}</td>'
        f'{_supply_cell(s)}'
        f'<td class="mono" data-v="{_n(s["grossPerOrder"])}" title="{_e(_cost_tip(s))}">{_num(s["grossPerOrder"], 1)}</td>'
        f'<td class="mono" data-v="{_n(cur.get("adShare"))}">{_pct(cur.get("adShare"))}</td>'
        f'<td class="mono" data-v="{_n(cur.get("szAmt"))}">{_money(cur.get("szAmt"))}'
        f'<span class="qtysub"> {_num(_n(cur.get("szQty")), 0)}件</span></td>'
        f'<td class="mono" data-v="{_n(cur.get("naturalAmt"))}" '
        f'title="自然成交＝商智总成交 − 广告成交（金额口径）；件数为商智件数 − 广告订单数（近似）">'
        f'{_money(cur.get("naturalAmt"))}'
        f'<span class="qtysub"> {_num(_natural_qty(cur), 0)}件</span></td>'
        f'<td class="mono {"up" if _n(s["est"][wk]["netProfit"]) > 0 else "down"}" '
        f'data-v="{_n(s["est"][wk]["netProfit"])}">{_money(s["est"][wk]["netProfit"])}</td>'
        f'<td data-v="{roi}">{spark}</td>'
        f'<td class="l" title="{_e(_sku_advice(s))}">{_badge(tier)}</td>'
        f'</tr>')


def page_sku(d) -> str:
    labels = {a["key"]: a["label"] for a in d["accounts"]}
    skus = [s for s in d["skus"] if _n(s["wins"]["w0"]["cost"]) + _n(s["wins"]["w1"]["cost"]) + _n(s["wins"]["w2"]["cost"]) > 0]
    rows = [_sku_row(i + 1, s, labels) for i, s in enumerate(skus)]
    tot_cost = sum(_n(_latest(s)["cost"]) for s in skus)
    tot_amt = sum(_n(_latest(s)["amt"]) for s in skus)
    tot_sz = sum(_n(_latest(s).get("szAmt")) for s in skus)
    tot_net = sum(_n(s["est"][_latest_key(s)]["netProfit"]) for s in skus)
    tier_count = {}
    for s in skus:
        tier_count[s["verdict"]["tier"]] = tier_count.get(s["verdict"]["tier"], 0) + 1
    chips = "".join(
        f'<div class="chip{" on" if k == "all" else ""}" data-f="{k}" onclick="applyTier(this)">{v}</div>'
        for k, v in [("all", f"全部 {len(skus)}")] +
        [(t, f"{t} {tier_count.get(t, 0)}") for t in ["放大", "优化", "止损", "严重亏损"] if tier_count.get(t)])
    achips = "".join(
        f'<div class="chip{" on" if k == "all" else ""}" data-a="{k}" onclick="applyAcct(this)">{v}</div>'
        for k, v in [("all", "全部账号")] + [(a["key"], a["label"]) for a in d["accounts"]])
    return f"""
<div class="card">
  <h2>商品 ROI 交叉表 <span class="tagline">按 SKU 合并两个账号的广告，商智成交分店展示</span></h2>
  <p class="hint">
    共 {len(skus)} 个有广告投放的 SKU，其中 {(d["meta"].get("costCoverage") or {}).get("skuErp", 0)} 个已接入
    ERP 真实成本（覆盖本期花费 {((d["meta"].get("costCoverage") or {}).get("costShare") or 0):.0f}%）。
    近周花费合计 <b>¥{_money(tot_cost)}</b>、广告成交 <b>¥{_money(tot_amt)}</b>
    （ROI {_n(tot_amt) / max(tot_cost, 1):.2f}），这些商品商智总成交 <b>¥{_money(tot_sz)}</b>，
    按成本假设估算净利 <b>¥{_money(tot_net)}</b>。
    「ROI」= 京准通平台报的广告 ROI（广告成交额 ÷ 花费，分子是消费者实付）；「保本ROI」= 该 SKU
    自己的保本线，算法是（到手结算价 − 货款 − 运费 − 包材 − 人工）÷ <b>前台件单价</b>，
    ROI 高于它才值得继续投（两者同一前台口径，可直接比大小）。标 <b>*</b> 表示 ERP 无此 SKU，按全店口径线判。
    「商智总成交」下方为该商品<b>全店全渠道成交件数</b>，「自然成交」下方为
    <b>自然成交件数</b>（商智件数 − 广告订单数，近似）。
    「毛利/件」= 每卖一件真正赚到的钱（未扣广告费），算法是
    到手结算价 − 货款 − 运费 − 包材 − 人工，其中到手结算价 ≈ 前台件单价 × 到手率（7~9 成）。
  </p>
  <div class="toolbar" id="skuChips">{chips}<span style="width:14px"></span>{achips}
    <span class="mini" style="margin-left:8px">点表头排序 · 悬停商品名看分店成交 · 悬停 ROI / 到手结算价看「前台口径 vs 到手口径」换算</span>
  </div>
  <div class="tblwrap">
    <table id="skuTable"><thead><tr>
      <th class="noSort">#</th>
      <th class="l noSort">商品</th>
      <th class="l">投放账号</th>
      <th data-num="1">近周花费</th><th data-num="1">成交额</th>
      <th data-num="1" title="京准通平台报的广告ROI＝广告成交额÷花费">ROI</th>
      <th data-num="1" title="该 SKU 自己的保本 ROI 线，ROI 高于它才值得继续投">保本ROI</th>
      <th data-num="1" title="广告带来的成交订单数（京准通，点击后15天归因）">促成订单</th>
      <th data-num="1">CPA</th>
      <th data-num="1" title="商智成交额 ÷ 商智成交件数，消费者实付">前台件单价</th>
      <th data-num="1">到手结算价(到手率)</th>
      <th data-num="1" title="每卖一件真正赚到的钱（未扣广告费）">毛利/件</th>
      <th data-num="1">广告占比</th>
      <th data-num="1" title="商智全店全渠道成交额，下方为成交件数">商智总成交</th>
      <th data-num="1" title="商智总成交 − 广告成交；下方为自然成交件数（近似）">自然成交</th>
      <th data-num="1">预估净利</th>
      <th data-num="1">近3周ROI</th><th class="l">档位</th>
    </tr></thead><tbody>{''.join(rows)}</tbody></table>
  </div>
</div>

<div class="card">
  <h2>ROI vs 保本线 <span class="tagline">花费前 15 的 SKU</span></h2>
  <p class="hint">绿色=实际 ROI 达到该商品自己的保本线；红色=低于保本线，广告纯亏。</p>
  {_chart_html("chSkuRoi", tall=True)}
</div>

<div class="card">
  <h2>广告渗透率 <span class="tagline">广告成交 ÷ 该商品店铺总成交</span></h2>
  <p class="hint">越低说明越靠自然流量；接近 100% 说明这个商品的成交基本靠广告买来，一旦停投会掉量。</p>
  {_chart_html("chShare", tall=True)}
</div>
"""


# ---------------------------------------------------------------- 页面：调整复盘
def _adj_block(a) -> str:
    cls = {"正确": "ok", "已止损": "ok", "有问题": "bad", "有效但不彻底": "warn", "存疑": "warn"}.get(a["verdict"], "")
    b, af = a.get("before"), a.get("after")
    perf = ""
    if b and af:
        dcost, _c1 = _delta(af["dailyCost"], b["dailyCost"], better_high=False)
        droi = af["roi"] - b["roi"]
        perf = (f'<div class="mini">调整前7天：日均花费 ¥{_num(b["dailyCost"], 1)} · ROI {b["roi"]:.2f} · 日单 {_num(b["dailyOrd"], 1)}'
                f' ｜ 调整后7天：日均花费 ¥{_num(af["dailyCost"], 1)}'
                f'<span class="{_c1}">({dcost})</span> · ROI <b>{af["roi"]:.2f}</b>'
                f'<span class="{"up" if droi >= 0 else "down"}">({droi:+.2f})</span></div>')
    return (f'<div class="evt {cls}"><div class="h"><span class="t">{_e(a["time"])}</span>'
            f'<span class="a">{_e(a["plan"])}</span>{_badge(a["verdict"])}'
            f'<span class="mini">{_e(a["accountLabel"])} · {_e(a["content"])} · {_e(a["operator"])}</span></div>'
            f'<div class="opDetail">{_e(a["detail"])}</div>{perf}'
            f'<div class="mini" style="color:var(--primary)">{_e(a["note"])}</div></div>')


def page_adj(d) -> str:
    adjs = d["adjustments"]
    manual = [o for o in d["ops"] if o["manual"]]
    auto = [o for o in d["ops"] if o["kind"] == "autoBudget"]
    right = [a for a in adjs if a["verdict"] == "正确"]
    wrong = [a for a in adjs if a["verdict"] == "有问题"]
    mid = [a for a in adjs if a["verdict"] in ("有效但不彻底", "存疑", "效果不明显")]
    by_acct = {}
    for a in adjs:
        by_acct.setdefault(a["account"], []).append(a)

    _order = {"有问题": 0, "正确": 1, "有效但不彻底": 2, "存疑": 3, "效果不明显": 4, "已止损": 5,
              "待观察": 6, "观察中": 7, "无基线": 8, "无花费": 9, "无法评估": 10}
    blocks = ""
    for k, lst in by_acct.items():
        lst = sorted(lst, key=lambda a: (_order.get(a["verdict"], 99), a["time"]))
        blocks += f'<h3>{_e(lst[0]["accountLabel"])}（{len(lst)} 条调整记录）</h3>'
        blocks += "".join(_adj_block(a) for a in lst)

    # 系统自动改预算 Top
    auto_by_plan = {}
    for o in auto:
        e = auto_by_plan.setdefault(o.get("target") or "未知计划", {"n": 0, "last": "", "acct": o["accountLabel"], "detail": ""})
        e["n"] += 1
        if o["time"] > e["last"]:
            e["last"] = o["time"]
            e["detail"] = o["detail"]
    auto_rows = sorted(auto_by_plan.items(), key=lambda x: -x[1]["n"])[:15]
    auto_html = "".join(
        f'<tr><td class="l">{_e(k)}</td><td class="l">{_e(v["acct"])}</td>'
        f'<td class="mono" data-v="{v["n"]}">{v["n"]}</td>'
        f'<td class="mono">{_e(v["last"])}</td>'
        f'<td class="l opDetail">{_e(v["detail"])}</td></tr>' for k, v in auto_rows)

    return f"""
<div class="card">
  <h2>调整操作复盘 <span class="tagline">来自京准通「操作日志」，用日粒度数据对比前后 7 天</span></h2>
  <p class="hint">
    数据窗口内共 <b>{len(d["ops"])}</b> 条操作日志：人工操作 <b>{len(manual)}</b> 条、
    系统「自动提升预算」<b>{len(auto)}</b> 条。进入复盘时间线 <b>{len(adjs)}</b> 条，
    其中<b>能真正对比调整前后 7 天效果</b>的 {len(right) + len(wrong) + len(mid) + len([a for a in adjs if a["verdict"] == "已止损"])} 条 →
    <span class="up">正确 {len(right)}</span>、<span class="down">有问题 {len(wrong)}</span>、
    效果不明显/存疑 {len(mid)}、止损 {len([a for a in adjs if a["verdict"] == "已止损"])}；
    其余因新建计划 / 已删除 / 无花费，没有基线可比（报告里保留原始操作，供核对）。
  </p>
  <div class="warnbox">
    <b>最重要的发现</b>：账号B 的「自动提升预算」一直在自动加预算
    （示例：某计划预算被反复上调 300→450→675），
    人工几乎没在管预算上限。系统加预算是「因为花得出去」，不等于「因为赚得回来」——
    必须用 ROI 反过来约束它，否则预算会被低效流量吃掉。
  </div>
  {_chart_html("chAdj", tall=True)}
</div>

<div class="card">
  <h2>人工调整时间线 <span class="tagline">按账号分组，绿=正确 红=有问题 黄=存疑</span></h2>
  <p class="hint">
    「调整前7天 / 调整后7天」为该计划在操作当日的日粒度前后对比；新计划或无数据的调整标注为无法评估。
  </p>
  {blocks or '<p class="hint">窗口内没有可评估的人工调整。</p>'}
</div>

<div class="card">
  <h2>系统自动改预算 Top <span class="tagline">这些不是人做的，但直接影响花费</span></h2>
  <p class="hint">同一计划被系统反复加预算的次数。「自动提升预算」通常由「稳赚计划 / 智能出价」触发，建议改为固定预算或关闭自动提预算。</p>
  <div class="tblwrap short">
    <table><thead><tr><th class="l noSort">计划</th><th class="l noSort">账号</th>
      <th data-num="1">自动加预算次数</th><th class="mono noSort">最近一次</th><th class="l noSort">最近内容</th></tr></thead>
      <tbody>{auto_html}</tbody></table>
  </div>
</div>
"""


# ---------------------------------------------------------------- 页面：计划明细
def page_plan(d) -> str:
    plans = [p for p in d["plans"] if _n(p["wins"]["w0"]["cost"]) + _n(p["wins"]["w1"]["cost"]) + _n(p["wins"]["w2"]["cost"]) > 0]
    plans.sort(key=lambda p: -(_n(p["wins"]["w1"]["cost"]) + _n(p["wins"]["w2"]["cost"])))
    tier_count = {}
    for p in plans:
        tier_count[p["tier"]] = tier_count.get(p["tier"], 0) + 1
    rows = []
    for i, p in enumerate(plans):
        cells = (f'<td class="mono">{i + 1}</td>'
                 f'<td class="l"><b>{_e(p["name"])}</b><div class="sid">{_e(p["campaignId"])}</div></td>'
                 f'<td class="l">{_e(p["accountLabel"])}</td>'
                 f'<td class="l">{_e(p["typeLabel"])}</td>')
        for k in ("w0", "w1", "w2"):
            m = p["wins"][k]
            cells += (f'<td class="mono" data-v="{_n(m["cost"])}">{_money(m["cost"])}</td>'
                      f'<td class="mono {_roi_cls(m["roi"], p["breakeven"])}" data-v="{_n(m["roi"])}">{_n(m["roi"]):.2f}</td>')
        droi = _n(p["wins"]["w2"]["roi"]) - _n(p["wins"]["w0"]["roi"])
        cells += (f'<td class="mono {"up" if droi >= 0 else "down"}" data-v="{droi}">{droi:+.2f}</td>'
                  f'<td data-v="{_n(p["wins"]["w2"]["roi"])}">{_spark([p["wins"][k]["roi"] for k in ("w0", "w1", "w2")], p["breakeven"])}</td>'
                  f'<td class="l" title="{_e(_plan_advice(p))}">{_badge(p["tier"])}</td></tr>')
        rows.append(f'<tr data-acct="{_e(p["account"])}" data-tier="{_e(p["tier"])}">' + cells)

    achips = "".join(f'<div class="chip{" on" if k == "all" else ""}" data-a="{k}" onclick="applyAcct(this)">{v}</div>'
                     for k, v in [("all", f"全部 {len(plans)}")] + [(a["key"], a["label"]) for a in d["accounts"]])
    return f"""
<div class="card">
  <h2>计划明细 <span class="tagline">概览口径，按账号区分</span></h2>
  <p class="hint">
    「概览」里的计划包含智能化 / 关键词 / 全站智能推广三种类型，口径与智能投放报表略有差异（订单口径）。
    放大 {tier_count.get("放大", 0)} 个 · 优化 {tier_count.get("优化", 0)} 个 ·
    止损 {tier_count.get("止损", 0)} 个 · 未投放 {tier_count.get("未投放", 0)} 个。
  </p>
  <div class="toolbar" id="planChips">{achips}<span class="mini" style="margin-left:8px">悬停档位看具体建议</span></div>
  <div class="tblwrap">
    <table><thead><tr><th class="noSort">#</th><th class="l noSort">计划</th><th class="l noSort">账号</th>
      <th class="l noSort">类型</th>
      <th class="mono noSort">w0花费</th><th class="mono noSort">w0ROI</th>
      <th class="mono noSort">w1花费</th><th class="mono noSort">w1ROI</th>
      <th class="mono noSort">w2花费</th><th class="mono noSort">w2ROI</th>
      <th data-num="1">ROI变化</th><th data-num="1">3周ROI</th><th class="l noSort">档位</th>
    </tr></thead><tbody>{''.join(rows)}</tbody></table>
  </div>
</div>
"""


# ---------------------------------------------------------------- 页面：搜索词诊断
def _word_pills(items, kind) -> str:
    if not items:
        return '<span class="mini">无</span>'
    out = []
    for w in items[:40]:
        cls = "b-grow" if kind == "good" else ("b-stop" if kind == "waste" else "b-opt")
        out.append(f'<span class="badge {cls}" style="margin:2px 4px 2px 0;font-weight:600" '
                   f'title="花费 ¥{_money(w["cost"])} · 成交 ¥{_money(w["amt"])} · {w["ord"]}单 · ROI {_n(w["roi"]):.2f}">'
                   f'{_e(w["word"])} <span style="opacity:.75">¥{_money(w["cost"])}/{_n(w["roi"]):.1f}</span></span>')
    return "".join(out)


WLABEL = {"kw": "关键词推广", "smart": "智能化推广", "other": "其他推广"}


def _wtype_of(name, t):
    """把搜索词分组按推广类型归类：关键词推广 / 智能化推广 / 其他。

    智能化(61)与全站智能推广(153)的词由系统自动调控、人工改不了，必须与关键词计划分开看。
    搜索词接口只回计划名（没有 campaignType），所以先拿计划名回查 d["plans"]，
    查不到再按计划名里的「智能 / 关键词 / 快车」兜底判断。
    """
    if t == 2:
        return "kw"
    if t in (61, 153):
        return "smart"
    nm = name or ""
    if "智能" in nm:
        return "smart"
    if "关键词" in nm or "快车" in nm:
        return "kw"
    return "other"


def page_word(d) -> str:
    # 搜索词接口只有计划名、没有 campaignType，用 d["plans"] 的 (账号, 计划名) 回查推广类型
    tmap = {}
    for p in (d.get("plans") or []):
        nm = (p.get("name") or "").strip()
        if nm:
            tmap[(p.get("account"), nm)] = p.get("type")

    def _picked(v):
        """该计划要看的词：优先搜索词；快车关键词计划没有搜索词明细，改用关键词。"""
        if v.get("sw"):
            return v["sw"], "搜索词"
        return (v.get("kw") or []), "关键词"

    out = []
    for a in d["accounts"]:
        k = a["key"]
        wins = d["words"].get(k) or {}
        wk = "w2" if wins.get("w2") else ("w1" if wins.get("w1") else "w0")
        per = wins.get(wk) or {}
        agg = {"good": [], "waste": [], "low": []}
        blocks = []
        counts = {"kw": 0, "smart": 0, "other": 0}
        order = sorted(per.items(), key=lambda x: -sum(w["cost"] for w in _picked(x[1])[0]))[:14]
        for cid, v in order:
            ws, src = _picked(v)
            if not ws:
                continue
            wt = _wtype_of(cid, tmap.get((k, (cid or "").strip())))
            counts[wt] += 1
            g = [w for w in ws if w["kind"] == "good"][:12]
            wa = [w for w in ws if w["kind"] == "waste"][:12]
            lo = [w for w in ws if w["kind"] == "low"][:8]
            agg["good"] += [w for w in ws if w["kind"] == "good"]
            agg["waste"] += [w for w in ws if w["kind"] == "waste"]
            agg["low"] += [w for w in ws if w["kind"] == "low"]
            cost = sum(w["cost"] for w in ws)
            roi = sum(w["amt"] for w in ws) / cost if cost else 0
            scope = "搜索词下" if src == "搜索词" else "关键词下"
            blocks.append(f"""
<div data-wtype="{wt}" style="border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px">
  <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;flex-wrap:wrap">
    <b>{_e(cid)}</b> <span class="pill">{WLABEL[wt]} · {src}</span>
    <span class="mini">{scope}：花费 ¥{_money(cost)} · ROI {roi:.2f} · 高效 {len(g)} / 废词 {len(wa)} / 低效 {len(lo)}</span>
  </div>
  <div style="margin-top:8px"><span class="mini">✅ 高效（提价/加词）</span><br>{_word_pills(g, "good")}</div>
  <div style="margin-top:8px"><span class="mini">🚫 0 单废词（直接否定）</span><br>{_word_pills(wa, "waste")}</div>
  <div style="margin-top:8px"><span class="mini">⚠️ 低效（降价或精准否定）</span><br>{_word_pills(lo, "low")}</div>
</div>""")
        wa_cost = sum(w["cost"] for w in agg["waste"])
        wa_top = sorted(agg["waste"], key=lambda x: -x["cost"])[:8]
        chips = "".join(
            f'<div class="chip{" on" if ck == "all" else ""}" data-w="{ck}" onclick="applyWordType(this)">{_e(cv)}</div>'
            for ck, cv in [("all", f"全部 {sum(counts.values())}"),
                           ("kw", f"关键词 {counts['kw']}"),
                           ("smart", f"智能化 {counts['smart']}"),
                           ("other", f"其他 {counts['other']}")])
        out.append(f"""
<div class="card">
  <h2>{_e(a["label"])} · 搜索词诊断 <span class="tagline">{_e(wk)} 窗口，按花费排序前 14 个计划</span></h2>
  <p class="hint">
    高效 {len(agg["good"])} 个 · 0单废词 {len(agg["waste"])} 个（浪费 ¥{_money(wa_cost)}）· 低效 {len(agg["low"])} 个。
    判定标准：高效=订单≥2 且 ROI&gt;4.00；废词=花费≥3 且 0 单；低效=花费≥10 且 ROI&lt;4.00。
    关键词推广计划看「关键词」，智能化 / 全站智能推广计划看「搜索词」。
  </p>
  <div class="toolbar" style="margin:2px 0 6px">{chips}</div>
  <p class="mini" style="margin:0 0 10px">智能化 / 全站智能推广计划的词由系统自动调控，<b>不能手工增删</b>；要调词请切到「关键词」。</p>
  <div class="dangerbox"><b>最该否定的词</b>：{'、'.join(f'{_e(w["word"])}(¥{_money(w["cost"])})' for w in wa_top) or '无'}</div>
  {''.join(blocks) or '<p class="hint">该账号本窗口没有搜索词 / 关键词数据。</p>'}
</div>""")
    return "".join(out)


# ---------------------------------------------------------------- 页面：行动清单
def page_action(d) -> str:
    adv = d["advice"]
    right = [a for a in d["adjustments"] if a["verdict"] == "正确"]
    wrong = [a for a in d["adjustments"] if a["verdict"] == "有问题"]
    stop = adv["stop"]
    grow = adv["grow"]
    up = adv["upside"]

    stop_rows = "".join(
        f'<tr><td class="l"><b>{_e(s["name"])}</b></td><td class="mono" data-v="{_n(s["cost"])}">¥{_money(s["cost"])}</td>'
        f'<td class="mono down" data-v="{_n(s["roi"])}">{_n(s["roi"]):.2f}</td>'
        f'<td class="mono" data-v="{_n(s["breakeven"])}">{_n(s["breakeven"]):.2f}'
        f'{"*" if s.get("costSource") != "erp" else ""}</td>'
        f'<td class="mono" data-v="{_n(s["netProfit"])}">¥{_money(s["netProfit"])}</td>'
        f'<td class="l">{_e(_sku_advice(next(x for x in d["skus"] if x["skuId"] == s["skuId"])))}</td></tr>'
        for s in stop)
    grow_rows = "".join(
        f'<tr><td class="l"><b>{_e(s["name"])}</b></td><td class="mono" data-v="{_n(s["cost"])}">¥{_money(s["cost"])}</td>'
        f'<td class="mono up" data-v="{_n(s["roi"])}">{_n(s["roi"]):.2f}</td>'
        f'<td class="mono" data-v="{_n(s["breakeven"])}">{_n(s["breakeven"]):.2f}'
        f'{"*" if s.get("costSource") != "erp" else ""}</td>'
        f'<td class="mono" data-v="{_n(s.get("growLine"))}">{_n(s.get("growLine")):.2f}</td>'
        f'<td class="mono" data-v="{_n(s.get("grossPerOrder"))}">{_num(s.get("grossPerOrder"), 1)}</td>'
        f'<td class="mono" data-v="{_n(s["cost"]) * 0.3}">¥{_money(_n(s["cost"]) * 0.3)}</td>'
        f'<td class="l">{_e(_sku_advice(next(x for x in d["skus"] if x["skuId"] == s["skuId"])))}</td></tr>'
        for s in grow[:15])
    wrong_html = "".join(
        f'<div class="evt bad"><div class="h"><span class="t">{_e(a["time"])}</span><span class="a">{_e(a["plan"])}</span>'
        f'<span class="mini">{_e(a["accountLabel"])} · {_e(a["content"])}</span></div>'
        f'<div class="mini">{_e(a["note"])}</div></div>' for a in wrong)
    right_html = "".join(
        f'<div class="evt ok"><div class="h"><span class="t">{_e(a["time"])}</span><span class="a">{_e(a["plan"])}</span>'
        f'<span class="mini">{_e(a["accountLabel"])} · {_e(a["content"])}</span></div>'
        f'<div class="mini">{_e(a["note"])}</div></div>' for a in right)

    return f"""
<div class="card">
  <h2>第一步 · 止血 <span class="tagline">所选区间 ROI 低于「各自保本线」的 SKU（标 * = 按全店线判）</span></h2>
  <p class="hint">
    合计花费 <b>¥{_money(up["stopCost"])}</b>，占所选区间总花费
    {_pct(_n(up["stopCost"]) / max(_n(up["lastCost"]), 1) * 100)}。逐个处理，不要一刀切关闭。
  </p>
  <div class="tblwrap short"><table><thead><tr>
    <th class="l noSort">商品</th><th data-num="1">近周花费</th><th data-num="1">ROI</th>
    <th data-num="1">保本ROI</th><th data-num="1">预估净利</th><th class="l noSort">动作</th>
  </tr></thead><tbody>{stop_rows or '<tr><td colspan="6" class="l">没有低于保本线的商品 👍</td></tr>'}</tbody></table></div>
</div>

<div class="card">
  <h2>第二步 · 加投 <span class="tagline">ROI 已到「各自放量线」（保本线 ×{_n(d['meta'].get('growFactor'), 1.25):.2f}）的 SKU，才是增量池</span></h2>
  <p class="hint">按 30% 加预算试探，加完盯 3 天 ROI 是否守住。保本线只是不亏钱，放量线以上加投才划算。</p>
  <div class="tblwrap short"><table><thead><tr>
    <th class="l noSort">商品</th><th data-num="1">近周花费</th><th data-num="1">ROI</th>
    <th data-num="1">保本ROI</th><th data-num="1">放量线</th><th data-num="1">毛利/件</th>
    <th data-num="1">建议加投</th><th class="l noSort">动作</th>
  </tr></thead><tbody>{grow_rows or '<tr><td colspan="8" class="l">暂无明显高效商品</td></tr>'}</tbody></table></div>
</div>

<div class="card">
  <h2>第三步 · 量化收益 <span class="tagline">把低效花费挪到高效 SKU</span></h2>
  <div class="grid g4">
    {_kpi("可腾挪花费", "¥" + _money(up["stopCost"]), "来自低于保本线的 SKU")}
    {_kpi("目标 ROI", f'{_n(up["targetRoi"]):.2f}', "高效 SKU 的加权 ROI")}
    {_kpi("预计多产出", "¥" + _money(up["estGain"]), "同样花费下的增量成交")}
    {_kpi("整体 ROI", f'{_n(adv["combined"]["w2"]["roi"]):.2f} → {_n(up["roiAfter"]):.2f}', "调整后估算")}
  </div>
</div>

<div class="card">
  <h2>第四步 · 调整纠错 <span class="tagline">哪些调整做错了</span></h2>
  <p class="hint">这些调整执行后 7 天，计划 ROI 明显下滑。建议回滚到调整前的出价/投产比目标。</p>
  {wrong_html or '<p class="hint">窗口内没有判定为「有问题」的人工调整。</p>'}
  <h3>做对了的（保持）</h3>
  {right_html or '<p class="hint">—</p>'}
</div>

<div class="card">
  <h2>第五步 · 盯住系统自动加预算</h2>
  <div class="warnbox">
    账号B 的「自动提升预算」在窗口内触发了几十次，把日预算从 200/300 一路抬到 450/675。
    这类加预算由系统按「花得出去」触发，<b>不代表赚得回来</b>。
    建议：对 ROI 已低于保本的计划，<b>关闭自动提升预算 / 稳赚计划</b>，或改成固定日预算。
    操作路径：京准通 → 推广计划 → 计划 → 编辑 → 预算设置。
  </div>
</div>
"""


UI_CSS = """
/* 区间选择器：奶油卡片，分段预设 + 日期输入 */
.rangebar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:16px;
  background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-md);padding:12px 14px}
.rangebar label{font-size:12.5px;color:var(--muted);display:inline-flex;align-items:center;gap:6px;font-weight:500}
.rangebar input[type=date]{background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-sm);
  padding:7px 10px;font-size:13px;color:var(--ink);font-variant-numeric:tabular-nums}
.rangebar input[type=date]:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(204,120,92,.15)}
.rangebar input[type=checkbox]{accent-color:var(--primary)}
.rangebar .sep{width:1px;height:22px;background:var(--hair);display:inline-block}
.rangebar .pre{padding:6px 13px;border-radius:9999px;border:1px solid var(--hair);
  background:var(--canvas);color:var(--muted);cursor:pointer;font-size:12px;font-weight:500;
  transition:all .15s;line-height:1.4;font-family:inherit}
.rangebar .pre:hover{background:var(--cream);color:var(--ink);border-color:var(--cream)}
.rangebar .pres{display:inline-flex;gap:4px;flex-wrap:wrap}
.rangebar .accts{display:inline-flex;gap:12px;flex-wrap:wrap;align-items:center}
.rangebar .accts label{color:var(--body)}
.rangebar .mini{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.rangebar button[type=submit]{background:var(--primary);color:var(--on-primary);border:1px solid var(--primary);
  border-radius:var(--r-sm);padding:9px 18px;font-weight:500;cursor:pointer;font-size:13px;font-family:inherit;
  transition:background .15s,border-color .15s}
.rangebar button[type=submit]:hover{background:var(--primary-a);border-color:var(--primary-a)}
.rangebar a{color:var(--primary);font-size:12.5px;text-decoration:none;margin-left:6px;font-weight:500}
.rangebar a:hover{text-decoration:underline}
@media(max-width:760px){.rangebar{gap:9px}.rangebar .sep{display:none}}
"""


def _acct_scope(d) -> str:
    labels = [a["label"] for a in d.get("accounts", [])]
    if len(labels) == 2:
        return "两账号"
    if len(labels) == 1:
        return labels[0]
    return "、".join(labels)


def _source_note(d) -> str:
    """当某些窗口的账号总额退化为智能投放口径时提醒一下。"""
    bad = []
    for a in d.get("accounts", []):
        srcs = {a["totals"][k].get("source") for k in a["totals"] if a["totals"][k].get("source")}
        if srcs and srcs != {"概览"}:
            bad.append(f'{a["label"]}：{"/".join(sorted(srcs))}')
    if not bad:
        return ""
    return ('<div class="warnbox"><b>口径降级</b>：' + "；".join(_e(x) for x in bad) +
            '。缺「概览」数据时会退化为智能投放口径（不含全站智能推广），账号总额会偏小。</div>')


def _cost_block(d) -> str:
    """成本来源面板：每个 SKU 的保本线到底是谁算的、覆盖了多少钱、哪些数还不可信。"""
    m = d.get("meta") or {}
    cov = m.get("costCoverage") or {}
    src = m.get("costSource") or {}
    flat, blend = _n(m.get("breakevenFlat"), 4.00), _n(m.get("breakevenBlend"))
    kind = src.get("kind") or "全店 Excel 口径"
    head = (f'<b>成本来源</b>：{_e(kind)}'
            + (f'（{_e(src.get("detail") or "")}）' if src.get("detail") else '')
            + (f'｜ERP 更新日 {_e(m.get("costUpdatedAt") or "")}' if m.get("costUpdatedAt") else '')
            + '</>')
    if not cov.get("skuErp"):
        cm = m.get("costModel") or {}
        return ('<div class="warnbox">' + head +
                f'还没接入每个 SKU 的真实供货价，全店只用 Excel「计算公式」里那<b>一套</b>假设 —— '
                f'客单价 ¥{_n(cm.get("refPrice"))}、产品成本 ¥{_n(cm.get("product"))}、'
                f'包装 ¥{_n(cm.get("package"))}、运费 ¥{_n(cm.get("shipping"))}，'
                f'得出<b>保本 ROI = {flat:.2f}</b>，所有商品共用这一条线。<br>'
                '生鲜各商品毛利率差好几倍，共用一条线会把「打平」误判成「放大」：'
                '<b>接入各 SKU 真实成本后结论会变</b>（配 JD_COSTS_URL / JD_COSTS_TOKEN 即可自动接入）。</div>')
    parts = [head,
             f'按<b>每个 SKU 自己的保本线</b>判定：本期 {cov.get("skuErp")}/{cov.get("skuTotal")} 个在投 SKU '
             f'命中 ERP 成本，覆盖花费 ¥{_money(cov.get("costErp"))}／¥{_money(cov.get("costTotal"))}'
             f'（<b>{_n(cov.get("costShare")):.0f}%</b>）；'
             f'其余 {cov.get("skuStore")} 个仍按全店 {flat:.2f} 线并标 <b>*</b>。']
    if _n(cov.get("settleRate")) > 0:
        settle_line = ('')
        if blend:
            settle_line = (f'换成到手结算价口径，整店保本线是 <b>{_n(cov.get("breakevenSettle")):.2f}</b>'
                           f'（与前台口径 {blend:.2f} 等价，判定一律用前台口径）。')
        parts.append(
            f'<b>口径提醒：前台件单价 ≠ 我们到手的钱</b>。本期命中 SKU 的前台成交 ¥{_money(cov.get("gmvErp"))}'
            f' → 实际到手结算价 ¥{_money(cov.get("settleAmt"))}（<b>到手率 {_n(cov.get("settleRate")) * 100:.0f}%</b>，'
            f'差额是京东扣点与入仓差价，入仓品常见只有 7 成上下）。'
            f'报表里的毛利率是<b>占前台成交额</b>的比率（所以比 ERP 那个「占到手结算价」的低一截），'
            f'只有这个口径才和京准通报的 ROI 同底 —— 实测广告客单价 ≈ 商智客单价，平台报的就是前台成交额。'
            + settle_line)
    if blend:
        parts.append(f'整店参考线：全店口径 {flat:.2f} → <b>按真实成本加权 {blend:.2f}</b>'
                     f'（加权毛利率 {(1 / blend * 100):.1f}%），放量线 = 保本线 × {_n(m.get("growFactor"), 1.25):.2f}。')
    neg = cov.get("negativeMargin") or []
    if neg:
        parts.append(f'<b class="down">到手结算价低于单件成本（卖一件亏一件，投广告只会放大亏损）</b>：' +
                     "；".join(f'{_e(x.get("name") or x.get("skuId"))}（贡献 ¥{_num(x.get("grossPerOrder"), 2)}/件，'
                               f'本期花费 ¥{_money(x.get("cost"))}）' for x in neg[:6]) + '。')
    warn = cov.get("unitWarn") or []
    if warn:
        parts.append('<b>成本口径待确认</b>（这些 SKU 的 ERP 数字与商智对不上，按 1:1 计算，'
                     '结论请抽查后再落地）：' +
                     "；".join(f'{_e(x.get("name") or x.get("skuId"))} — { _e(x.get("warn"))}' for x in warn[:6]) + '。')
    miss = cov.get("missing") or []
    if miss:
        parts.append(f'<b>未接入真实成本</b>（¥{_money(cov.get("missingCost"))} 花费，按全店线判）：' +
                     "、".join(f'{_e((x.get("name") or "")[:14])} ¥{_money(x.get("cost"))}' for x in miss[:8]) +
                     ('…' if len(miss) > 8 else '') + '。')
    return '<div class="warnbox">' + '<br>'.join(parts) + '</div>'


def _coverage_notice(d) -> str:
    cov = (d.get("meta") or {}).get("coverage") or {}
    bad = []
    for a in d.get("accounts", []):
        per = cov.get(a["key"]) or {}
        wk = per.get("w2") or {}
        if wk and not wk.get("complete"):
            bad.append(f'{a["label"]} 所选区间仅 {wk.get("got")}/{wk.get("want")} 天有数据')
    if not bad:
        return ""
    return ('<div class="covbox"><b>数据不完整</b>：' + "；".join(_e(x) for x in bad) +
            '。未覆盖的日期按 0 计入，可能低估区间表现 —— 可在控制台补抓这些日期。</div>')


# ---------------------------------------------------------------- 组装
TABS = [("p-ov", "① 总结"), ("p-sku", "② 商品ROI"), ("p-adj", "③ 调整复盘"),
        ("p-plan", "④ 计划明细"), ("p-word", "⑤ 搜索词诊断"), ("p-act", "⑥ 行动清单")]


_ECHARTS_CACHE: dict = {"src": None}


def _echarts_src() -> str:
    if _ECHARTS_CACHE["src"] is None:
        src = ""
        if ASSETS.exists():
            ec = ASSETS.read_text(encoding="utf-8").replace("</script", "<\\/script")
            src = "<script>" + ec + "</script>"
        _ECHARTS_CACHE["src"] = src
    return _ECHARTS_CACHE["src"]


AI_CSS = """
/* AI 解读块：奶油卡片 + 珊瑚徽标；深色仅用于「方法论/校验」细节 */
.aicard{border:1px solid var(--hair);border-radius:var(--r-lg);padding:20px 24px;margin:0 0 18px;
  background:var(--surface-card);position:relative;overflow:hidden}
.aicard:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--primary)}
.aicard.empty{background:var(--soft);border-style:dashed}
.aicard.empty:before{background:var(--muted2)}
.aihd{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:16px;font-weight:400;
  font-family:var(--serif);color:var(--ink);letter-spacing:-.2px}
.aibadge{display:inline-block;background:var(--primary);color:var(--on-primary);border-radius:6px;
  padding:2px 9px;font-size:11.5px;font-weight:700;font-family:var(--sans);letter-spacing:.6px}
.aibadge.off{background:var(--muted2)}
.aimeta{font-size:12px;color:var(--muted);font-weight:400;font-family:var(--sans);line-height:1.6}
.aiov{margin:12px 0 14px;font-size:13.5px;line-height:1.8;color:var(--body)}
.aihead{font-size:19px;font-weight:400;font-family:var(--serif);color:var(--ink);margin:10px 0 4px;
  line-height:1.5;letter-spacing:-.3px}
.aigrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.aigrid{grid-template-columns:1fr}}
.aicol{background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-md);padding:14px 16px}
.aicol h4{margin:0 0 8px;font-size:13px;color:var(--strong);font-weight:600}
.aicol ul{margin:0;padding-left:18px}
.aicol li{font-size:12.8px;margin:4px 0;line-height:1.65}
.aipri{display:inline-block;border-radius:5px;padding:0 7px;font-size:11.5px;font-weight:700;margin-right:6px}
.aipri.l1{background:var(--redbg);color:var(--redink)}
.aipri.l2{background:var(--amberbg);color:var(--amberink)}
.aipri.l3{background:var(--greenbg);color:var(--greenink)}
.aibtn{margin-left:auto;border:1px solid var(--primary);background:var(--canvas);color:var(--primary);
  border-radius:var(--r-sm);padding:6px 13px;font-size:12.5px;cursor:pointer;font-weight:500;
  font-family:inherit;transition:all .15s;white-space:nowrap}
.aibtn:disabled{opacity:.55;cursor:default}
.aibtn:hover:not(:disabled){background:var(--primary);color:var(--on-primary)}
.aidisc{margin-top:12px;font-size:11.5px;color:var(--muted2);line-height:1.75}
.aidt{margin-top:12px;border-top:1px solid var(--hair);padding-top:10px}
.aidt>summary{cursor:pointer;font-size:13px;font-weight:600;color:var(--primary);list-style:none;
  display:flex;align-items:center;gap:7px;user-select:none}
.aidt>summary::-webkit-details-marker{display:none}
.aidt>summary:before{content:"▸";font-size:11px;transition:transform .18s}
.aidt[open]>summary:before{transform:rotate(90deg)}
.aip{margin:10px 0;padding:12px 14px;background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-md)}
.aip .t{font-size:13px;font-weight:600;color:var(--ink)}
.aip .b{font-size:12.6px;color:var(--body);margin-top:5px;line-height:1.7}
.aik{display:inline-block;border-radius:4px;padding:0 6px;margin:1px 3px 1px 0;font-size:11.5px;
  font-family:var(--mono)}
.aik.neg{background:var(--redbg);color:var(--redink)}
.aik.boost{background:var(--greenbg);color:var(--greenink)}
.aivd{color:var(--primary);font-weight:600}
.aiacts{margin:8px 0 0;padding-left:18px}
.aiacts li{font-size:12.8px;margin:4px 0;line-height:1.7}
"""


def _ai_block(ai, d, link: bool = True, default_open: bool = False) -> str:
    # /report 页里 AI 只是"摘要"（默认折叠，别把数据表挤下去）；
    # /ai 页里 AI 报告是主体，默认展开。
    opn = " open" if default_open else ""
    meta = d.get("meta") or {}
    rng = meta.get("range") or {}
    start = rng.get("start") or ""
    end = rng.get("end") or ""
    keys = ",".join(a.get("key", "") for a in (meta.get("accounts") or []))
    if not ai:
        return f"""<div class="aicard empty">
  <div class="aihd"><span class="aibadge off">AI</span> AI 运营解读
    <span class="aimeta">当前区间（{_e(start)} ~ {_e(end)}）暂无 AI 解读</span>
    <button class="aibtn" onclick="genAi(this, '{_e(start)}', '{_e(end)}', '{_e(keys)}')">生成本区间解读</button>
  </div>
  <div class="aidisc">每晚自动任务会**贴着本地已抓取的数据**生成 AI 解读（套用《京东快车底层逻辑》方法论）；
也可点按钮即时生成（约 1~2 分钟）。</div>
</div>"""

    a = ai.get("advice") or {}
    v = ai.get("validation") or {}
    n_pj = v.get("n_plan_judgements", v.get("n_plan_actions", 0))
    n_warn = len(v.get("warnings") or []) + len(v.get("issues") or [])
    ok = "通过" if v.get("ok") else "有问题"
    conf = str(a.get("confidence") or "")
    head = (f'<div class="aihd"><span class="aibadge">AI</span> AI 运营解读'
            f'<span class="aimeta">模型 {_e(str(ai.get("model")))} ｜ 生成 {_e(str(ai.get("generatedAt")))} ｜ '
            f'方法论 {_e(str(ai.get("methodology_source")))} ｜ 校验 {ok}'
            f'（{n_pj} 条计划判断/{n_warn} 告警'
            + (f'/置信 {_e(conf)}' if conf else '') + '）</span>'
            f'<button class="aibtn" onclick="genAi(this, \'{_e(start)}\', \'{_e(end)}\', \'{_e(keys)}\')">重新生成</button></div>')

    hd = ""
    if a.get("headline"):
        hd = f'<div class="aihead">{_e(str(a.get("headline")))}</div>'
    h = a.get("health") or {}
    hl = ""
    if h:
        hl = (f'<div class="aiov"><b>健康度：{_e(str(h.get("verdict") or "—"))}'
              f'（{_e(str(h.get("score") or "—"))}/100）</b> —— {_e(str(h.get("reason") or ""))}</div>')

    diags = "".join(
        f'<div class="aip"><div class="t">{_e(str(x.get("aspect") or ""))}</div>'
        f'<div class="b"><b>现象：</b>{_e(str(x.get("finding") or ""))}</div>'
        + (f'<div class="b"><b>方法论：</b>{_e(str(x.get("method") or ""))}</div>' if x.get("method") else "")
        + (f'<div class="b"><b>影响：</b>{_e(str(x.get("impact") or ""))}</div>' if x.get("impact") else "")
        + '</div>'
        for x in (a.get("diagnosis") or []))
    diag = (f'<details class="aidt"{opn}><summary>诊断（套方法论，{len(a.get("diagnosis") or [])} 条）</summary>'
            f'{diags}</details>' if diags else "")

    reads = "".join(f'<li><b>{_e(str(x.get("account")))}</b>：{_e(str(x.get("read")))}</li>'
                    for x in (a.get("account_reads") or []))
    reads_block = (f'<div class="aicol" style="margin-top:12px"><h4>账号特征</h4><ul>{reads}</ul></div>'
                   if reads else "")

    # 未用起来 / 待优化的投放功能（定向人群、地域、时间折扣、测图测词…）
    tpri = {"高": "l1", "中": "l2", "低": "l3"}
    tas = []
    for t in (a.get("targeting_advice") or []):
        pr = tpri.get(str(t.get("priority") or ""), "l2")
        st = str(t.get("status") or "")
        tas.append(
            f'<div class="aip"><div class="t"><span class="aipri {pr}">{_e(str(t.get("priority") or "中"))}</span>'
            f'{_e(str(t.get("feature") or ""))}'
            + (f' <span class="aimeta">· {_e(st)}</span>' if st else "") + '</div>'
            f'<div class="b"><b>证据：</b>{_e(str(t.get("evidence") or ""))}</div>'
            + (f'<div class="b"><b>方法论：</b>{_e(str(t.get("method") or ""))}</div>' if t.get("method") else "")
            + (f'<div class="b"><b>建议：</b>{_e(str(t.get("suggest") or ""))}</div>' if t.get("suggest") else "")
            + '</div>')
    tgt_block = ""
    if tas:
        tgt_block = (f'<details class="aidt"{opn}><summary>未用起来 / 待优化的投放功能'
                     f'（{len(tas)} 条 · 定向人群/地域/时段等）</summary>{"".join(tas)}</details>')

    # 分时（时段）策略：数据只覆盖「最近一天」，标题里标出日期，避免被当成区间结论
    dps = []
    for x in (a.get("dayparting_advice") or []):
        pr = tpri.get(str(x.get("priority") or ""), "l2")
        scope = str(x.get("scope") or "")
        tgt = str(x.get("target") or x.get("campaign") or "")
        dhead = (f'<span class="aipri {pr}">{_e(str(x.get("priority") or "中"))}</span>'
                 + (_e(tgt) if tgt else "账号级")
                 + (f' <span class="aimeta">· {_e(scope)}</span>' if scope else "")
                 + (f' <span class="aimeta">· {_e(str(x.get("hours") or ""))}</span>' if x.get("hours") else "")
                 + (f' <span class="aivd">{_e(str(x.get("direction") or ""))}</span>' if x.get("direction") else ""))
        dps.append(f'<div class="aip"><div class="t">{dhead}</div>'
                   + (f'<div class="b"><b>证据：</b>{_e(str(x.get("evidence") or ""))}</div>' if x.get("evidence") else "")
                   + (f'<div class="b"><b>方法论：</b>{_e(str(x.get("method") or ""))}</div>' if x.get("method") else "")
                   + (f'<div class="b"><b>做法：</b>{_e(str(x.get("suggest") or ""))}</div>' if x.get("suggest") else "")
                   + '</div>')
    dp_block = ""
    if dps:
        hdays: list = []
        for v in (d.get("hourly") or {}).values():
            for dd in (v.get("days") or []):
                if dd not in hdays:
                    hdays.append(dd)
        dp_block = (f'<details class="aidt"{opn}><summary>分时（时段）策略建议（{len(dps)} 条'
                    + (f' · 数据日期 {"、".join(hdays)}' if hdays else " · 无分时数据") + '）</summary>'
                    + "".join(dps) + '</details>')

    ps = []
    for p in (a.get("plan_judgements") or []):
        acts = "".join(f'<li>{_e(str(t))}</li>' for t in (p.get("actions") or []))
        ps.append(f'<div class="aip"><div class="t">{_e(str(p.get("name"))) }'
                  f'（{_e(str(p.get("campaignId")))}） · {_e(str(p.get("stage") or ""))} → '
                  f'<span class="aivd">{_e(str(p.get("verdict") or ""))}</span></div>'
                  f'<div class="b"><b>依据：</b>{_e(str(p.get("why") or ""))}</div>'
                  + (f'<ul class="aiacts">{acts}</ul>' if acts else "")
                  + '</div>')
    dt = (f'<details class="aidt"><summary>逐计划判断（{len(ps)} 条）</summary>{"".join(ps)}</details>'
          if ps else "")

    kws = []
    for k in (a.get("keyword_plan") or []):
        neg = "".join(f'<span class="aik neg" title="{_e(str(w.get("why") or ""))}">{_e(str(w.get("word") or w))}</span>'
                      for w in (k.get("negate") or []))
        boo = "".join(f'<span class="aik boost" title="{_e(str(w.get("why") or ""))}">{_e(str(w.get("word") or w))}</span>'
                      for w in (k.get("boost") or []))
        bits = []
        if neg:
            bits.append(f'<div class="b">否定：{neg}</div>')
        if boo:
            bits.append(f'<div class="b">提价：{boo}</div>')
        if bits:
            kws.append(f'<div class="aip"><div class="t">{_e(str(k.get("campaign") or ""))}</div>'
                       + "".join(bits) + '</div>')
    kw_block = (f'<details class="aidt"><summary>词级建议（候选，需人工确认 · {len(kws)} 个计划）</summary>'
                f'{"".join(kws)}</details>' if kws else "")

    ra = a.get("reallocation") or {}
    ra_block = ""
    if ra.get("logic"):
        ra_block = ('<div class="aicol" style="margin-top:12px"><h4>预算再分配</h4><ul>'
                    f'<li>从：{_e("、".join(str(x) for x in (ra.get("from") or [])))}</li>'
                    f'<li>到：{_e("、".join(str(x) for x in (ra.get("to") or [])))}</li>'
                    f'<li>{_e(str(ra.get("logic")))}</li></ul></div>')
    seq_items = "".join(
        f'<li><b>{_e(str(s.get("step", i + 1)))}</b>. {_e(str(s.get("do") or ""))}'
        f' —— <span class="aimeta">{_e(str(s.get("because") or ""))}</span></li>'
        for i, s in enumerate(a.get("sequence") or []))
    seq_block = (f'<div class="aicol" style="margin-top:12px"><h4>执行顺序</h4><ul>{seq_items}</ul></div>'
                 if seq_items else "")
    watch = "".join(f'<li>{_e(str(x))}</li>' for x in (a.get("watchlist") or []))
    watch_block = (f'<div class="aicol" style="margin-top:12px"><h4>观察清单</h4><ul>{watch}</ul></div>'
                   if watch else "")
    risks = "".join(f'<li>{_e(str(r))}</li>' for r in (a.get("risks") or []))
    risk = f'<div class="aicol" style="margin-top:12px"><h4>风险提示</h4><ul>{risks}</ul></div>' if risks else ""

    warns = (v.get("issues") or []) + (v.get("warnings") or [])
    warn = ""
    if warns:
        warn = ('<div class="aicol" style="margin-top:12px;border-color:var(--amber);background:var(--amberbg)">'
                '<h4>校验告警（需人工核对）</h4><ul>'
                + "".join(f"<li>{_e(str(w))}</li>" for w in warns) + "</ul></div>")

    # 账号特征 / 预算再分配 / 执行顺序 / 风险 / 观察清单 —— 收进一个折叠块，避免卡片过高
    _mid_inner = f'{reads_block}<div>{ra_block}{seq_block}{risk}{watch_block}{warn}</div>'
    _mid_has = bool(reads_block or ra_block or seq_block or risk or watch_block or warn)
    mid_block = (
        f'<details class="aidt"{opn}><summary>账号特征 · 预算再分配 · 执行顺序 · 风险提示'
        f'（{len(a.get("account_reads") or [])} 账号 / {len(a.get("sequence") or [])} 步）</summary>'
        f'{_mid_inner}</details>' if _mid_has else "")

    return (f'<div class="aicard">{head}{hd}{hl}{diag}{mid_block}{dp_block}{tgt_block}{dt}{kw_block}'
            f'<div class="aidisc">AI 套用《京东快车底层逻辑》方法论生成，<b>仅供参考</b>；'
            f'算数与口径由规则层代码保证，执行前请人工复核。'
            + (f'　<a href="/ai?start={_e(start)}&end={_e(end)}&accounts={_e(keys)}">'
               f'打开 AI 分析台（可追问）→</a>' if link else '') + '</div></div>')


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
        // 生成中：显示「思考过程」已产出多少字，用户不空等（生成在服务端跑，离开页面也不中断）
        var jb=s.job||{};
        btn.textContent='思考中… '+(jb.reasoningChars||0)+' 字 / 成文 '+(jb.contentChars||0)+' 字';
      }).catch(function(){ clearInterval(t); btn.disabled=false; btn.textContent=old; });
    }, 4000);
  }).catch(function(e){ btn.disabled=false; btn.textContent=old; alert('请求失败：'+e); });
}
"""


def render(d, ui: str = "", title: str = "京东广告运营分析 · 跨账号 × 按商品 × 调整复盘",
           ai=None) -> str:
    charts = build_charts(d)
    chart_json = json.dumps(charts, ensure_ascii=False).replace("</", "<\\/")
    echarts_src = _echarts_src()
    tabs = "".join(
        f'<div class="tab{" active" if i == 0 else ""}" onclick="showTab(\'{cid}\',this)">{_e(label)}</div>'
        for i, (cid, label) in enumerate(TABS))
    ai_html = _ai_block(ai, d)
    pages = [
        ("p-ov", ai_html + page_overview(d)),
        ("p-sku", page_sku(d)),
        ("p-adj", page_adj(d)),
        ("p-plan", page_plan(d)),
        ("p-word", page_word(d)),
        ("p-act", page_action(d)),
    ]
    body = "".join(
        f'<section class="page{" active" if i == 0 else ""}" id="{cid}">{html}</section>'
        for i, (cid, html) in enumerate(pages))
    meta = d["meta"]
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
<style>{CSS}{UI_CSS}{AI_CSS}</style>{echarts_src}</head>
<body>
{navtop("/report")}
<header class="top"><div class="wrap pagehead">
  <div class="eyebrow">{SPIKE_SVG}跨账号 · 按商品 · 调整复盘</div>
  <h1>{_e(title)}</h1>
  <div class="metaline">
    <span>账号：{' / '.join(_e(a["label"]) for a in meta["accounts"])}</span><span class="dot"></span>
    <span>区间：{_e((meta.get("range") or {}).get("start") or meta["windows"][2]["start"])}
      ~ {_e((meta.get("range") or {}).get("end") or meta["windows"][2]["end"])}</span><span class="dot"></span>
    <span>对比基准：{_e(meta["windows"][1]["label"])}</span><span class="dot"></span>
    <span>生成 {_e(meta["generatedAt"])}</span>
  </div>
  {ui}
  <div class="tabs">{tabs}</div>
</div></header>
<div class="wrap">{_coverage_notice(d)}{body}
  <div class="foot">
    数据来源：京准通（概览 / 智能投放 / 快车关键词 / 操作日志）+ 商智（商品明细 / 流量概况）。<br>
    「保本ROI」「毛利/件」「预估净利」：已接入的 SKU 用 ERP 每件<b>到手结算价</b>与货款/运费/包材/人工逐件算，
    分母用商智<b>前台件单价</b>（消费者实付，与京准通 ROI 同口径）；
    未接入（标 <b>*</b>）仍按全店 Excel 成本结构推算 —— 均为投放决策参考，不是财务对账数据。<br>
    本页为按天入库后的<b>区间聚合</b>结果，区间由上方选择器决定。
  </div>
</div>
<script>{JS}</script>
<script>{AI_JS}</script>
<script>window.__CHARTS={chart_json};</script>
</body></html>"""


def main() -> int:
    src = config.BASE_DIR / "data" / "analysis2.json"
    if not src.exists():
        print("[ERR] 未找到 data/analysis2.json，请先运行 python -m jd_roi.analyze2")
        return 1
    d = json.loads(src.read_text(encoding="utf-8"))
    html = render(d)
    out = config.BASE_DIR / "ad_roi_report_v2.html"
    out.write_text(html, encoding="utf-8")
    print(f"[OK] 报告已生成 -> {out}  ({len(html) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
