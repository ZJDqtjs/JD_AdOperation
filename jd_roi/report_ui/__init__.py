# -*- coding: utf-8 -*-
"""分析报表前端 UI 公共模块（设计系统 / 导航 / 交互脚本）。

被 report2.py、vs.py、ai_page.py 与 webapp.py 复用，保证全站视觉语言、
顶栏导航、表格排序/筛选/导出与图表主题完全一致。
"""
from .scripts import AI_JS, IND_JS, REPORT_JS
from .theme import (AI_CSS, CSS, SPIKE_SVG, UI_CSS, _NAV_ITEMS,
                    _echarts_src, navtop)

__all__ = [
    "AI_CSS",
    "AI_JS",
    "CSS",
    "IND_JS",
    "REPORT_JS",
    "SPIKE_SVG",
    "UI_CSS",
    "_NAV_ITEMS",
    "_echarts_src",
    "navtop",
]
