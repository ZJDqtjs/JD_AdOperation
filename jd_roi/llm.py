# -*- coding: utf-8 -*-
"""最小 OpenAI 兼容 LLM 客户端（独立于规则层，可单独复用）。

配置优先级：
  1) 环境变量：LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_MAX_TOKENS
  2) WorkBuddy 的 models.json（默认 ~/.workbuddy/models.json，可用 LLM_MODELS_JSON 覆盖）
  3) 抛出 LlmConfigError

设计要点：
  - 只依赖 httpx，不引入 openai/anthropic SDK。
  - **默认走 SSE 流式**：慢模型（实测 ~14 token/s）用非流式极易触发读超时；
    流式每个分片都会重置读超时，长文生成不再被掐断。
  - 对超时/5xx 做有限重试；JSON 解析交给 extract_json()，容忍代码围栏与前后杂讯。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx

# 触发项目 .env 加载（否则单独 import llm 时读不到 LLM_* 配置）
try:  # noqa: SIM105
    from . import settings  # noqa: F401
except Exception:  # noqa: BLE001
    pass


class LlmConfigError(RuntimeError):
    pass


class LlmCallError(RuntimeError):
    pass


def _models_json_path() -> Path:
    env = os.getenv("LLM_MODELS_JSON")
    if env:
        return Path(env)
    return Path.home() / ".workbuddy" / "models.json"


def resolve_config() -> dict:
    """返回 {base_url, api_key, model, source}。"""
    base = os.getenv("LLM_BASE_URL", "").strip()
    key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    if base and key:
        return {"base_url": base.rstrip("/"), "api_key": key,
                "model": model or "default", "source": "env"}

    p = _models_json_path()
    if p.exists():
        try:
            arr = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(arr, dict):
                arr = [arr]
            for item in arr:
                url = (item.get("url") or "").strip()
                ak = (item.get("apiKey") or "").strip()
                if url and ak:
                    return {"base_url": url.rstrip("/"), "api_key": ak,
                            "model": model or item.get("id") or item.get("name") or "default",
                            "source": f"models.json:{p}"}
        except Exception as exc:  # noqa: BLE001
            raise LlmConfigError(f"读取 {p} 失败：{exc}") from exc

    raise LlmConfigError(
        "未找到可用的 LLM 配置。请设置环境变量 LLM_BASE_URL / LLM_API_KEY，"
        f"或确认 {p} 存在且含 url/apiKey。")


def _timeout(read: float) -> httpx.Timeout:
    # read 给足（分片间隔上限）；connect/write 用常规值
    return httpx.Timeout(connect=30.0, read=read, write=60.0, pool=30.0)


def _iter_sse(url: str, headers: dict, payload: dict, read_timeout: float):
    """逐分片 yield `(kind, text)`；kind ∈ {"reasoning","content"}。

    推理型模型（如本项目用的 qwen 系）会在 delta 里同时给
    `reasoning_content`（思考过程）与 `content`（正式回答）——
    把两者都透出来，前端就能边等边看「思考过程」，而不是干等。
    """
    got = False
    with httpx.Client(timeout=_timeout(read_timeout)) as client:
        with client.stream("POST", url, json=payload, headers=headers) as r:
            if r.status_code >= 400:
                body = b"".join(r.iter_bytes()).decode("utf-8", "ignore")[:300]
                raise LlmCallError(f"HTTP {r.status_code}: {body}")
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                ch = (obj.get("choices") or [{}])[0]
                delta = ch.get("delta") or {}
                rz = delta.get("reasoning_content") or delta.get("reasoning")
                if rz:
                    got = True
                    yield ("reasoning", rz)
                piece = delta.get("content")
                if piece:
                    got = True
                    yield ("content", piece)
    if not got:
        raise LlmCallError("流式返回为空（可能该端点不支持 stream）")


def chat_stream_events(messages: list[dict], *, model: str | None = None,
                       temperature: float = 0.4, max_tokens: int | None = None,
                       timeout: float = 180.0):
    """逐分片 yield `(kind, text)`（思考过程 + 正式回答）。不做整轮重试。

    给网页对话框/报告生成做「思考过程流式展示」用。
    """
    cfg = resolve_config()
    url = cfg["base_url"] + "/chat/completions"
    mt = max_tokens or int(os.getenv("LLM_MAX_TOKENS", "6000"))
    payload = {
        "model": model or cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": mt,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}",
               "Content-Type": "application/json"}
    yield from _iter_sse(url, headers, payload, timeout)


def _chat_stream(url: str, headers: dict, payload: dict, read_timeout: float,
                 on_event=None) -> str:
    """聚合流式内容；给了 on_event 就顺带把每个分片 (kind, text) 回调出去。"""
    content: list[str] = []
    for kind, text in _iter_sse(url, headers, payload, read_timeout):
        if kind == "content":
            content.append(text)
        if on_event is not None:
            on_event(kind, text)
    text = "".join(content)
    if not text.strip():
        raise LlmCallError("流式返回为空（可能该端点不支持 stream）")
    return text


def chat_stream_iter(messages: list[dict], **kw):
    """兼容旧接口：逐分片 yield **正式回答**文本（丢掉思考过程）。"""
    for kind, text in chat_stream_events(messages, **kw):
        if kind == "content":
            yield text


def _chat_once(url: str, headers: dict, payload: dict, read_timeout: float) -> str:
    with httpx.Client(timeout=_timeout(read_timeout)) as client:
        r = client.post(url, json=payload, headers=headers)
    if r.status_code >= 500:
        raise LlmCallError(f"HTTP {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise LlmCallError(f"响应无 choices：{json.dumps(data, ensure_ascii=False)[:200]}")
    return (choices[0].get("message") or {}).get("content") or ""


def chat(messages: list[dict], *, model: str | None = None,
         temperature: float = 0.2, max_tokens: int | None = None,
         timeout: float = 180.0, retries: int = 2, stream: bool = True,
         on_event=None) -> str:
    """调用 chat/completions，返回助手文本。

    stream=True（默认）走 SSE；timeout 是**分片间隔**上限（非总时长），
    慢模型长文不会因总耗时超限被掐断。

    on_event(kind, text)：给了就把每个分片回调出去（kind ∈ reasoning/content），
    并在**每次尝试开始前**回调一次 `("reset", "")` —— 调用方收到 reset 就清空已累积内容，
    这样重试不会产生重复文本（网页端可据此重放思考过程）。
    """
    cfg = resolve_config()
    url = cfg["base_url"] + "/chat/completions"
    mt = max_tokens or int(os.getenv("LLM_MAX_TOKENS", "6000"))
    payload = {
        "model": model or cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": mt,
        "stream": stream,
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}",
               "Content-Type": "application/json"}
    last = None
    for attempt in range(max(1, retries + 1)):
        try:
            if stream:
                if on_event is not None and attempt > 0:
                    # 只在**重试**时发 reset：调用方据此清空已累积内容，避免重复；
                    # 首次尝试前不发，否则会把调用方之前累积的思考/正文误清掉。
                    on_event("reset", "")
                return _chat_stream(url, headers, payload, timeout, on_event=on_event)
            return _chat_once(url, headers, payload, timeout)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
    raise LlmCallError(f"调用失败（已重试 {retries} 次）：{type(last).__name__}: {last}")


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _balance(s: str) -> str:
    """补全被截断的 JSON：闭合未结束的字符串与括号。"""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                stack.append("}")
            elif ch == "[":
                stack.append("]")
            elif ch in "}]":
                if stack:
                    stack.pop()
    if in_str:
        s += '"'
    return s + "".join(reversed(stack))


def _repair(s: str) -> dict | None:
    """尽力从残缺/轻微畸形的 JSON 里抢救出一个对象（从尾部 '}' 逐步回退）。"""
    i = s.find("{")
    if i < 0:
        return None
    s = s[i:]
    tries = 0
    for pos in range(len(s) - 1, -1, -1):
        if s[pos] != "}":
            continue
        tries += 1
        if tries > 200:
            break
        cand = s[:pos + 1]
        for text in (cand, _balance(cand)):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    return obj
            except Exception:  # noqa: BLE001
                continue
    return None


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON 对象（容忍代码围栏 / 前后杂讯 / 轻微畸形 / 截断）。"""
    s = (text or "").strip()
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    for text in (s, None):
        if text is None:
            break
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except Exception:  # noqa: BLE001
            pass
    fixed = _repair(s)
    if fixed is not None:
        return fixed
    raise LlmCallError(f"输出中找不到可用的 JSON；原文前 300 字：{text[:300] if text else s[:300]}")


def chat_json(messages: list[dict], *, parse_retries: int = 2, **kw) -> tuple[dict, str]:
    """调用并解析 JSON；解析失败会**重新调用**再试（模型偶尔吐坏 JSON）。

    返回 (parsed, raw_text)。
    """
    last_exc: Exception | None = None
    for _ in range(max(1, parse_retries + 1)):
        raw = chat(messages, **kw)
        try:
            return extract_json(raw), raw
        except LlmCallError as exc:  # noqa: PERF203
            last_exc = exc
            continue
    raise LlmCallError(f"JSON 解析连续失败：{last_exc}")
