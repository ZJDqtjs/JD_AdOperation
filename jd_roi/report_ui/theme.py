# -*- coding: utf-8 -*-
"""全站统一设计系统（Claude 风：奶油画布 × 珊瑚 × 深墨）与公共顶栏/资源加载。"""
from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "echarts.min.js"

# 品牌符号：放射状尖刺（Anthropic 风格），用于导航与页头的品牌标记
SPIKE_SVG = (
    '<svg class="spike" viewBox="0 0 24 24" aria-hidden="true">'
    '<g stroke="currentColor" stroke-width="2.5" stroke-linecap="round" fill="none">'
    '<path d="M12 2.6v18.8M2.6 12h18.8M5.4 5.4l13.2 13.2M18.6 5.4L5.4 18.6"/>'
    '</g></svg>'
)

_NAV_ITEMS = [
    ("/", "控制台"),
    ("分析报表", [("/report", "区间报表"), ("/vs", "史上最强vs现代最强")]),
    ("/ai", "AI 分析台"),
    ("广告情况", [
        ("/paycomm", "CPS花费"),
        ("/paycomm/spend", "京准通花费"),
        ("/cashflow", "广告费流水"),
        ("/profit", "月度利润"),
        ("/maobao", "毛保诊断"),
    ]),
    ("/price", "SKU前台价"),
    ("/pop", "pop店订单"),
    ("/activity", "团长活动"),
]


def navtop(active: str = "", right: str = "") -> str:
    """全站统一顶栏：品牌符号 + 主导航 + 右侧插槽。"""
    parts = []
    for key, label in _NAV_ITEMS:
        if isinstance(label, list):
            on = " on" if any(h == active for h, _ in label) else ""
            items = "".join(
                f'<a class="navdrop-a{" on" if h == active else ""}" href="{h}">{t}</a>'
                for h, t in label)
            parts.append(
                f'<div class="navgroup"><span class="nava navgroup-btn{on}" tabindex="0" '
                f'role="button" aria-haspopup="true">{key}'
                f'<svg class="navcaret" viewBox="0 0 12 12" aria-hidden="true">'
                f'<path d="M2.5 4.5 6 8l3.5-3.5" fill="none" stroke="currentColor" '
                f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></span>'
                f'<div class="navdrop" role="menu">{items}</div></div>')
        else:
            parts.append(f'<a class="nava{" on" if key == active else ""}" href="{key}">{label}</a>')
    links = "".join(parts)
    return (
        f'<nav class="navtop"><div class="wrap">'
        f'<a class="brand" href="/">{SPIKE_SVG}'
        f'<span>京东广告运营<small>AD ROI CONSOLE</small></span></a>'
        f'<div class="navlinks">{links}</div>'
        f'<div class="navspacer"></div>{right}'
        f'</div></nav>'
    )


_ECHARTS_CACHE: dict = {"src": None}


def _echarts_src() -> str:
    if _ECHARTS_CACHE["src"] is None:
        src = ""
        if ASSETS.exists():
            ec = ASSETS.read_text(encoding="utf-8").replace("</script", "<\\/script")
            src = "<script>" + ec + "</script>"
        _ECHARTS_CACHE["src"] = src
    return _ECHARTS_CACHE["src"]


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
/* 导航二级菜单（分析报表 / 广告情况） */
.navgroup{position:relative;display:flex;align-items:center}
.navgroup-btn{display:inline-flex;align-items:center;gap:4px;cursor:default}
.navcaret{width:11px;height:11px;flex:none;transition:transform .18s}
.navgroup:hover .navcaret,.navgroup:focus-within .navcaret{transform:rotate(180deg)}
.navdrop{position:absolute;top:100%;left:0;min-width:176px;padding:6px;display:none;z-index:80;
  background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-md);
  box-shadow:var(--shadow-lift)}
.navgroup:hover .navdrop,.navgroup:focus-within .navdrop{display:block}
.navdrop-a{display:block;padding:8px 12px;border-radius:var(--r-sm);color:var(--muted);
  font-size:13.5px;font-weight:500;text-decoration:none;white-space:nowrap;
  transition:background .15s,color .15s}
.navdrop-a:hover{background:var(--soft);color:var(--ink)}
.navdrop-a.on{background:var(--surface-card);color:var(--ink);font-weight:600}
.navspacer{margin-left:auto}
.navpill{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:9999px;
  background:var(--surface-card);color:var(--strong);font-size:12px;font-weight:600}
.navpill i{width:7px;height:7px;border-radius:50%;background:var(--green);display:block}

/* ---------------------------------------------------------------- 页头 */
header.top{background:var(--soft);border-bottom:1px solid var(--hair);color:var(--ink);
  padding:26px 0 0;margin-bottom:0}
header.top .wrap{padding-bottom:16px}
h1{margin:0 0 10px;font-size:38px;font-weight:400;font-family:var(--serif);letter-spacing:-1px;
  line-height:1.12;color:var(--ink);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1 .spike{width:26px;height:26px;flex:none;color:var(--primary)}
.metaline{color:var(--muted);font-size:13px;line-height:1.7;display:flex;flex-wrap:wrap;gap:6px 14px;
  align-items:center}
.metaline .dot{width:3px;height:3px;border-radius:50%;background:var(--muted2);display:inline-block}
.eyebrow{font-size:11.5px;font-weight:600;letter-spacing:1.6px;text-transform:uppercase;color:var(--primary);
  margin-bottom:8px;display:flex;align-items:center;gap:8px}
.eyebrow .spike{width:13px;height:13px;vertical-align:0}

/* 页头（紧凑、编辑感） */
.pagehead{padding:22px 0 0}
.pagehead h1{font-size:34px}
.pagehead .actions{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:14px}

/* ---------------------------------------------------------------- 粘性页签栏 */
.tabs-sticky{position:sticky;top:62px;z-index:55;background:rgba(245,240,232,.92);
  -webkit-backdrop-filter:saturate(160%) blur(12px);backdrop-filter:saturate(160%) blur(12px);
  border-bottom:1px solid var(--hair);margin-bottom:22px}
.tabs-sticky .wrap{padding-top:8px;padding-bottom:8px;display:flex;align-items:center;gap:10px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;align-items:center;flex:1;min-width:0}
.tabs-kbd{font-size:11px;color:var(--muted2);white-space:nowrap;flex:none}
.tabs-kbd kbd{display:inline-block;padding:1px 5px;border:1px solid var(--hair);border-radius:4px;
  background:var(--canvas);font-family:var(--mono);font-size:10.5px;color:var(--muted)}
@media(max-width:1100px){.tabs-kbd{display:none}}
.tab{display:inline-flex;align-items:center;gap:6px;padding:7px 13px;border-radius:var(--r-sm);
  background:transparent;color:var(--muted);cursor:pointer;font-weight:500;font-size:13px;
  border:1px solid transparent;user-select:none;transition:background .15s,color .15s,border-color .15s;
  white-space:nowrap}
.tab:hover{background:var(--cream);color:var(--ink)}
.tab.active{background:var(--canvas);color:var(--ink);font-weight:600;
  border-color:var(--hair);box-shadow:0 1px 2px rgba(20,20,19,.05)}
.tab-cnt{display:inline-block;padding:0 6px;border-radius:9999px;font-size:11px;font-weight:600;
  font-variant-numeric:tabular-nums;background:var(--surface-card);color:var(--muted);line-height:1.55}
.tab.active .tab-cnt{background:var(--bluebg);color:var(--primary-a)}
section.page{display:none}
section.page.active{display:block;animation:fadeup .24s ease}
@keyframes fadeup{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}

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
.caliber-note{margin:8px 0 14px;border:1px solid var(--hair);border-radius:var(--r-md);
  background:var(--soft);padding:8px 14px}
.caliber-note>summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--muted);
  list-style:none;display:flex;align-items:center;gap:6px;user-select:none}
.caliber-note>summary::-webkit-details-marker{display:none}
.caliber-note>summary:before{content:"▸";font-size:10.5px;color:var(--primary);transition:transform .16s}
.caliber-note[open]>summary:before{transform:rotate(90deg)}
.caliber-note[open]>summary{color:var(--ink);margin-bottom:8px}
.caliber-note .caliber-body{font-size:12.3px;color:var(--body);line-height:1.75;
  border-top:1px dashed var(--hair);padding-top:8px}
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
.sortmark{font-size:10px;color:var(--primary);margin-left:2px}
td.l,th.l{text-align:left}
td.mono,th.mono{font-variant-numeric:tabular-nums;font-family:var(--mono)}
tbody tr:hover{background:var(--soft)}
.tblwrap{overflow:auto;max-height:720px;border:1px solid var(--hair);border-radius:var(--r-md);background:var(--canvas)}
.tblwrap.short{max-height:460px}
/* 宽表首两列固定（# + 商品/计划），横向滚动不丢失主标识 */
.tbl-sticky th:nth-child(1),.tbl-sticky td:nth-child(1){position:sticky;left:0;z-index:3;
  background:var(--canvas);width:42px;min-width:42px}
.tbl-sticky th:nth-child(2),.tbl-sticky td:nth-child(2){position:sticky;left:42px;z-index:3;
  background:var(--canvas);box-shadow:4px 0 8px -4px rgba(20,20,19,.09)}
.tbl-sticky thead th:nth-child(1),.tbl-sticky thead th:nth-child(2){z-index:5;background:var(--soft)}
.tbl-sticky tbody tr:hover td:nth-child(1),.tbl-sticky tbody tr:hover td:nth-child(2){background:var(--soft)}
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
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.toolbar-sep{width:1px;height:20px;background:var(--hair);display:inline-block;margin:0 2px}
.filter-sum{display:none;align-items:center;gap:14px;flex-wrap:wrap;margin:0 0 12px;padding:8px 14px;
  border-radius:var(--r-sm);background:var(--surface-card);border:1px solid var(--hair);
  font-size:12.5px;color:var(--strong);font-variant-numeric:tabular-nums}
.filter-sum.show{display:flex}
.filter-sum b{color:var(--ink);font-family:var(--mono)}
.chip{padding:6px 14px;border-radius:9999px;border:1px solid var(--hair);background:var(--canvas);cursor:pointer;
  font-size:12.5px;font-weight:500;color:var(--muted);transition:all .15s;line-height:1.4;user-select:none}
.chip:hover{border-color:var(--primary);color:var(--ink)}
.chip.on{background:var(--surface-card);color:var(--ink);border-color:var(--cream);font-weight:600}
/* 卡片标题右侧的视图切换（如计划明细：区间详细 / 三期趋势） */
.vswitch{margin-left:auto;display:inline-flex;border:1px solid var(--hair);border-radius:9999px;
  overflow:hidden;background:var(--canvas)}
.vswitch button{border:0;background:transparent;color:var(--muted);font-size:12.5px;font-weight:500;
  padding:5px 14px;cursor:pointer;transition:all .15s;line-height:1.4;font-family:var(--sans)}
.vswitch button:hover{color:var(--ink)}
.vswitch button.on{background:var(--surface-card);color:var(--ink);font-weight:600}
input,select,textarea{font-family:inherit}
input[type=date],input[type=text],select{background:var(--canvas);border:1px solid var(--hair);
  border-radius:var(--r-sm);padding:7px 10px;font-size:13px;color:var(--ink)}
input[type=date]:focus,input[type=text]:focus,select:focus,textarea:focus{outline:none;
  border-color:var(--primary);box-shadow:0 0 0 3px rgba(204,120,92,.15)}
.srch{flex:1 1 210px;min-width:190px}
.srchcount{color:var(--muted2);font-size:12px;white-space:nowrap}
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
.gapmark{color:var(--red);font-weight:700;margin-left:2px;cursor:help}
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

/* 返回顶部 */
.btt{position:fixed;right:24px;bottom:24px;z-index:60;width:40px;height:40px;border-radius:9999px;
  border:1px solid var(--hair);background:var(--canvas);color:var(--ink);cursor:pointer;
  box-shadow:var(--shadow-soft);display:none;align-items:center;justify-content:center;
  font-size:16px;transition:all .15s}
.btt.show{display:inline-flex}
.btt:hover{background:var(--primary);border-color:var(--primary);color:var(--on-primary)}

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

UI_CSS = """
/* 区间选择器：奶油卡片，分段预设 + 日期输入 */
.rangebar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px;
  background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-md);padding:11px 14px}
.rangebar label{font-size:12.5px;color:var(--muted);display:inline-flex;align-items:center;gap:6px;font-weight:500}
.rangebar input[type=date]{background:var(--canvas);border:1px solid var(--hair);border-radius:var(--r-sm);
  padding:6px 9px;font-size:13px;color:var(--ink);font-variant-numeric:tabular-nums}
.rangebar input[type=date]:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgba(204,120,92,.15)}
.rangebar input[type=checkbox]{accent-color:var(--primary)}
.rangebar .sep{width:1px;height:22px;background:var(--hair);display:inline-block}
.rangebar .pre{padding:5px 12px;border-radius:9999px;border:1px solid var(--hair);
  background:var(--canvas);color:var(--muted);cursor:pointer;font-size:12px;font-weight:500;
  transition:all .15s;line-height:1.4;font-family:inherit}
.rangebar .pre:hover{background:var(--cream);color:var(--ink);border-color:var(--cream)}
.rangebar .pres{display:inline-flex;gap:4px;flex-wrap:wrap}
.rangebar .accts{display:inline-flex;gap:12px;flex-wrap:wrap;align-items:center}
.rangebar .accts label{color:var(--body)}
.rangebar .mini{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.rangebar button[type=submit]{background:var(--primary);color:var(--on-primary);border:1px solid var(--primary);
  border-radius:var(--r-sm);padding:8px 16px;font-weight:500;cursor:pointer;font-size:13px;font-family:inherit;
  transition:background .15s,border-color .15s}
.rangebar button[type=submit]:hover{background:var(--primary-a);border-color:var(--primary-a)}
.rangebar a{color:var(--primary);font-size:12.5px;text-decoration:none;margin-left:4px;font-weight:500;
  padding:4px 8px;border-radius:var(--r-sm);transition:background .15s}
.rangebar a:hover{background:var(--soft);text-decoration:none}
@media(max-width:760px){.rangebar{gap:8px}.rangebar .sep{display:none}}
/* 快车 SOP 体检 */
.sopgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px}
.sopcard{border:1px solid var(--hair);border-radius:var(--r-md);background:var(--canvas);padding:14px 16px}
.sophd{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:14px;color:var(--ink);
  padding-bottom:9px;border-bottom:1px solid var(--hair);margin-bottom:10px}
.sophd .mini{margin-left:auto}
.sopv{display:inline-block;border-radius:9999px;padding:2px 9px;font-size:11.5px;font-weight:600;line-height:1.6}
.sopv.ok{background:var(--greenbg);color:var(--greenink)}
.sopv.warn{background:var(--amberbg);color:var(--amberink)}
.sopv.bad{background:var(--redbg);color:var(--redink)}
.sopv.info{background:var(--surface-card);color:var(--muted)}
.sopmetrics{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--muted);margin-bottom:9px}
.sopmetrics b{color:var(--ink);font-variant-numeric:tabular-nums}
.sopchecks{display:grid;gap:6px;margin-top:8px}
.sopitem{border-top:1px dashed var(--hair);padding-top:6px;font-size:12.5px;line-height:1.65;color:var(--body)}
.sopitem:first-child{border-top:0;padding-top:0}
.sopitem>b{display:inline-block;min-width:56px;font-size:11.5px}
.sopitem>b.ok{color:var(--greenink)} .sopitem>b.warn{color:var(--amberink)}
.sopitem>b.bad{color:var(--redink)} .sopitem>b.info{color:var(--muted)}
.sopitem .sopk{font-weight:600;color:var(--strong);margin-left:4px}
.sopitem .sopact{color:var(--muted);margin-top:1px}
/* 行业大盘：按账号切换 */
.indtabs{display:flex;gap:6px;flex-wrap:wrap;margin-top:4px}
.indtab{border:1px solid var(--hair);background:var(--canvas);color:var(--muted);
  border-radius:999px;padding:6px 13px;font-size:12.5px;font-weight:600;cursor:pointer;
  font-family:inherit;line-height:1.4;display:inline-flex;align-items:center;gap:6px}
.indtab:hover{border-color:var(--primary);color:var(--primary)}
.indtab.active{background:var(--primary);border-color:var(--primary);color:var(--on-primary)}
.indtab .indmeta{font-size:10.5px;font-weight:400;color:var(--muted2)}
.indtab.active .indmeta{color:rgba(255,255,255,.78)}
.indacct{display:none}
.indacct.active{display:block}
@media(max-width:760px){.indtab{font-size:12px;padding:5px 10px}}
"""

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
