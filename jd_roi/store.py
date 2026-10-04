# -*- coding: utf-8 -*-
"""抓取结果的本地缓存：以 JSON 落地，便于复跑与分步处理。

支持多账号：account=None/主账号 -> data/；其他账号 -> data/<account>/。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config


def _path(name: str, account: str | None = None) -> Path:
    d = config.account_dir(account)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def save(name: str, payload: Any, account: str | None = None) -> Path:
    p = _path(name, account)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load(name: str, account: str | None = None) -> Any:
    p = _path(name, account)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def exists(name: str, account: str | None = None) -> bool:
    return _path(name, account).exists()
