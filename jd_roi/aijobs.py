# -*- coding: utf-8 -*-
"""AI 生成任务的「可续传」事件流。

为什么需要
----------
AI 一次生成要几分钟（模型实测 ~14 token/s）。如果直接把结果流式写到浏览器连接上，
用户一旦**切走页面 / 刷新 / 断网**，连接被取消，生成也跟着被取消 —— 回来还得重跑。
所以改成三段式：

  1. **后台线程产事件**：生成跑在服务端线程里，把 (思考过程 / 正式回答) 分片
     以事件形式追加进内存 job；
  2. **浏览器随时挂载**：SSE 端点只负责「读 job、下发事件」，与生成解耦；
     断开不影响生成；
  3. **回到页面可续传**：带 `after=<已收到的序号>` 重新挂载，先补发历史事件再续读；
     SSE 的 `id:` 让浏览器用 `Last-Event-ID` 自动续传（重连不丢内容）。

事件 kind：
  - `reset`      一次尝试开始（模型重试时会发）→ 前端清空已累积内容
  - `reasoning`  思考过程分片
  - `content`    正式回答分片
  - `phase`      服务端阶段的文字提示（如「正在读取本地数据…」）
  - `error`      出错
  - `done`       结束（正常或出错后也会补一个，便于前端收尾）
"""
from __future__ import annotations

import threading
import time
import uuid

MAX_JOBS = 24          # 最多保留多少个任务
TTL_SEC = 2 * 3600.0   # 超过这个时间（且已结束）的任务被回收


class Job:
    def __init__(self, kind: str, meta: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                      # chat | report
        self.meta = dict(meta or {})
        self.status = "running"               # running | done | error
        self.error: str | None = None
        self.result = None                    # 生成类任务的最终结构化结果
        self.content = ""                     # 正式回答累积
        self.reasoning = ""                   # 思考过程累积
        self._events: list[tuple[int, str, str]] = []
        self._seq = 0
        self.createdAt = time.time()
        self.updatedAt = self.createdAt
        self._cond = threading.Condition()

    # ---------------- 写入（生产者） ----------------
    def emit(self, kind: str, text: str = "") -> int:
        text = text or ""
        with self._cond:
            self._seq += 1
            seq = self._seq
            if kind == "reset":
                self.reasoning = ""
                self.content = ""
            elif kind == "reasoning":
                self.reasoning += text
            elif kind == "content":
                self.content += text
            self._events.append((seq, kind, text))
            self.updatedAt = time.time()
            self._cond.notify_all()
            return seq

    def finish(self, *, error: str | None = None, result=None) -> None:
        with self._cond:
            if error:
                self.status = "error"
                self.error = error
                self._seq += 1
                self._events.append((self._seq, "error", error))
            else:
                self.status = "done"
            self.result = result
            self._seq += 1
            self._events.append((self._seq, "done", ""))
            self.updatedAt = time.time()
            self._cond.notify_all()

    # ---------------- 读取（消费者） ----------------
    def since(self, after: int) -> list[tuple[int, str, str]]:
        with self._cond:
            return [e for e in self._events if e[0] > after]

    def wait(self, after: int, timeout: float = 15.0) -> list[tuple[int, str, str]]:
        """等到有新事件或超时；返回 after 之后的新事件（可能为空＝该发心跳了）。"""
        with self._cond:
            pending = [e for e in self._events if e[0] > after]
            if pending or self.status != "running":
                return pending
            self._cond.wait(timeout)
            return [e for e in self._events if e[0] > after]

    def to_dict(self) -> dict:
        with self._cond:
            return {"id": self.id, "kind": self.kind, "status": self.status,
                    "error": self.error, "seq": self._seq,
                    "meta": self.meta, "hasResult": self.result is not None,
                    "createdAt": self.createdAt, "updatedAt": self.updatedAt,
                    "reasoningChars": len(self.reasoning),
                    "contentChars": len(self.content)}


class Manager:
    """进程内的任务表（线程安全）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    def new(self, kind: str, meta: dict | None = None) -> Job:
        job = Job(kind, meta)
        with self._lock:
            self._prune_locked()
            self._jobs[job.id] = job
        return job

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def latest_running(self, kind: str | None = None) -> Job | None:
        with self._lock:
            run = [j for j in self._jobs.values()
                   if j.status == "running" and (kind is None or j.kind == kind)]
        run.sort(key=lambda j: j.createdAt, reverse=True)
        return run[0] if run else None

    def snapshot(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.createdAt, reverse=True)
        return [j.to_dict() for j in jobs]

    def _prune_locked(self) -> None:
        now = time.time()
        # 先按 TTL 清已结束的
        for jid, j in list(self._jobs.items()):
            if j.status != "running" and now - j.updatedAt > TTL_SEC:
                self._jobs.pop(jid, None)
        # 再按数量上限清最旧的（不动运行中的）
        if len(self._jobs) > MAX_JOBS:
            order = sorted(self._jobs.values(), key=lambda j: j.createdAt)
            for j in order:
                if len(self._jobs) <= MAX_JOBS:
                    break
                if j.status != "running":
                    self._jobs.pop(j.id, None)


JOBS = Manager()
