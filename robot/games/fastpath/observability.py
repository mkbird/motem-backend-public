"""Fast Path 观测埋点 — JSONL trace writer。

Phase 1 用最朴素的 append-only JSONL，**不上 ClickHouse / Grafana**——
先把字段结构定下来，让 Phase 1 上 staging 时能立刻看到真实路由分布。

事件字段对齐 docs/game-fastpath-implementation-plan.md 的"观测面板"小节。
所有字段都可空：未触发的阶段（如 fast path 没走 customizer）字段留 None。

环境变量：
- ROBOT_FASTPATH_TRACE_PATH — JSONL 文件路径，默认 `logs/game_creation.jsonl`
- ROBOT_FASTPATH_TRACE_DISABLE — 设为 "1" 关闭埋点（测试用）
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TraceWriter:
    """Append-only JSONL writer，线程安全。"""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            path = Path(
                os.environ.get(
                    "ROBOT_FASTPATH_TRACE_PATH",
                    "logs/game_creation.jsonl",
                )
            )
        self.path = path
        self._lock = threading.Lock()
        self._disabled = os.environ.get("ROBOT_FASTPATH_TRACE_DISABLE", "").strip() in {"1", "true", "yes"}

    def write(self, event: dict[str, Any]) -> None:
        if self._disabled:
            return
        event = {"_ts": time.time(), **event}
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as e:
            # 埋点失败不能阻塞创建链路
            logger.warning("[Fastpath/Trace] write failed: %s", e)


# 全局单例，import 即用
_default_writer: TraceWriter | None = None


def get_trace_writer() -> TraceWriter:
    global _default_writer
    if _default_writer is None:
        _default_writer = TraceWriter()
    return _default_writer


# ─── 事件构造便利 helpers ────────────────────────────────────────────────────

def emit_fast_path_hit(
    *,
    request_id: str,
    user_id: str | None,
    archetype_id: str,
    confidence: float,
    router_tokens_in: int = 0,
    router_tokens_out: int = 0,
    router_cost_usd: float = 0.0,
    customizer_tokens_in: int = 0,
    customizer_tokens_out: int = 0,
    customizer_cost_usd: float = 0.0,
    customizer_cache_read_input_tokens: int = 0,
    compiler_duration_ms: int = 0,
    wall_clock_ms: int = 0,
) -> None:
    get_trace_writer().write({
        "event": "fast_path_hit",
        "request_id": request_id,
        "user_id": user_id,
        "route_decision": "fast",
        "archetype_id": archetype_id,
        "confidence": confidence,
        "fallback_reason": None,
        "router_tokens_in": router_tokens_in,
        "router_tokens_out": router_tokens_out,
        "router_cost_usd": router_cost_usd,
        "customizer_tokens_in": customizer_tokens_in,
        "customizer_tokens_out": customizer_tokens_out,
        "customizer_cost_usd": customizer_cost_usd,
        "customizer_cache_read_input_tokens": customizer_cache_read_input_tokens,
        "compiler_duration_ms": compiler_duration_ms,
        "wall_clock_ms": wall_clock_ms,
        "final_outcome": "game_ready",
    })


def emit_slow_path_fallback(
    *,
    request_id: str,
    user_id: str | None,
    archetype_id: str | None,
    confidence: float | None,
    fallback_reason: str,
    router_tokens_in: int = 0,
    router_tokens_out: int = 0,
    router_cost_usd: float = 0.0,
    wall_clock_ms: int = 0,
    final_outcome: str = "game_ready",
) -> None:
    get_trace_writer().write({
        "event": "slow_path_fallback",
        "request_id": request_id,
        "user_id": user_id,
        "route_decision": "slow",
        "archetype_id": archetype_id,
        "confidence": confidence,
        "fallback_reason": fallback_reason,
        "router_tokens_in": router_tokens_in,
        "router_tokens_out": router_tokens_out,
        "router_cost_usd": router_cost_usd,
        "wall_clock_ms": wall_clock_ms,
        "final_outcome": final_outcome,
    })
