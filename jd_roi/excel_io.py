# -*- coding: utf-8 -*-
"""Excel 读写：保留原有公式，仅填数。

注意：openpyxl 以 data_only=False 加载时会保留公式字符串；
若被 Excel 打开过且未重算，公式的缓存值会丢失，但公式本身不受影响。
"""
from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

from . import config


def load(path: Path | None = None):
    return openpyxl.load_workbook(path or config.EXCEL_PATH, data_only=False)


def save(wb, path: Path | None = None) -> Path:
    p = path or config.EXCEL_PATH
    wb.save(p)
    return p


def sheet(wb, title: str) -> Worksheet:
    if title not in wb.sheetnames:
        raise KeyError(f"未找到 sheet: {title}，现有: {wb.sheetnames}")
    return wb[title]


def set_values(ws: Worksheet, values: dict[str, object]) -> None:
    """按 {'B2': 123, ...} 批量写值。"""
    for coord, val in values.items():
        ws[coord] = val


def find_row(ws: Worksheet, col: int, text: str, start: int = 1) -> int | None:
    """在指定列查找包含 text 的行号。"""
    for r in range(start, ws.max_row + 1):
        v = ws.cell(row=r, column=col).value
        if v is not None and text in str(v):
            return r
    return None
