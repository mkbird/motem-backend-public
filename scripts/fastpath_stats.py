"""Fast Path 流量观测 — 读 logs/game_creation.jsonl 打印聚合统计。

无外部依赖，纯 stdlib。staging 上线后定期跑这个看：
- 命中率（fast 占比）：决定 Router 阈值要不要调
- fallback 原因分布：no_archetype_match 多 → archetype 覆盖不够；
  low_confidence 多 → Router 准度不够；compile_error 多 → Compiler bug
- archetype 命中分布：哪个 archetype 用得多/少，指导 Phase 2 优先级
- p50 / p95 延迟：是否真的比 slow path 快、超 SLO 没

用法：
    python scripts/fastpath_stats.py                # 默认 logs/game_creation.jsonl
    python scripts/fastpath_stats.py path/to.jsonl  # 自定义路径
    python scripts/fastpath_stats.py --since 1d     # 过滤近 1 天
    python scripts/fastpath_stats.py --since 6h
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


def _parse_since(s: str) -> float | None:
    """'1d' / '6h' / '30m' → 起始 unix ts。None 表示不过滤。"""
    if not s:
        return None
    m = re.match(r"^(\d+)\s*([dhm])$", s.strip())
    if not m:
        raise SystemExit(f"bad --since value: {s!r} (expect like 1d/6h/30m)")
    n, unit = int(m.group(1)), m.group(2)
    delta = n * {"m": 60, "h": 3600, "d": 86400}[unit]
    return time.time() - delta


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(round(q * (len(s) - 1)))
    return s[idx]


def main() -> int:
    p = argparse.ArgumentParser(description="Fast Path stats from JSONL trace.")
    p.add_argument("path", nargs="?",
                   default=os.environ.get("ROBOT_FASTPATH_TRACE_PATH", "logs/game_creation.jsonl"))
    p.add_argument("--since", default="", help="time window (e.g. '1d', '6h', '30m')")
    args = p.parse_args()

    path = Path(args.path)
    if not path.is_file():
        print(f"no trace file at: {path}", file=sys.stderr)
        return 1

    since_ts = _parse_since(args.since)

    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since_ts is not None and ev.get("_ts", 0) < since_ts:
            continue
        events.append(ev)

    if not events:
        print("no events in window")
        return 0

    # ─── 流量分布 ────────────────────────────────────────────────────────────
    total = len(events)
    by_route = Counter(e.get("route_decision", "?") for e in events)
    fast_n = by_route.get("fast", 0)
    slow_n = by_route.get("slow", 0)
    fast_pct = 100.0 * fast_n / total if total else 0.0

    print("=" * 62)
    print(f"Fast Path stats — {total} events" + (f" (since {args.since})" if since_ts else ""))
    print("=" * 62)
    print(f"\nROUTE DISTRIBUTION:")
    print(f"  fast: {fast_n:5d} ({fast_pct:5.1f}%)")
    print(f"  slow: {slow_n:5d} ({100*slow_n/total:5.1f}%)")

    # ─── Fast hits ───────────────────────────────────────────────────────────
    fast_events = [e for e in events if e.get("route_decision") == "fast"]
    if fast_events:
        archetype_hits = Counter(e.get("archetype_id", "?") for e in fast_events)
        print(f"\nARCHETYPE HIT DISTRIBUTION:")
        for arch, n in archetype_hits.most_common():
            print(f"  {arch}: {n} ({100*n/len(fast_events):.1f}%)")

        # 延迟
        wall_ms = [e.get("wall_clock_ms", 0) for e in fast_events]
        print(f"\nFAST PATH LATENCY (ms):")
        print(f"  p50:  {_quantile(wall_ms, 0.5):.0f}")
        print(f"  p95:  {_quantile(wall_ms, 0.95):.0f}")
        print(f"  mean: {sum(wall_ms)/len(wall_ms):.0f}")
        print(f"  max:  {max(wall_ms)}")

        # Token / cost
        avg_router_in = sum(e.get("router_tokens_in", 0) for e in fast_events) / len(fast_events)
        avg_router_out = sum(e.get("router_tokens_out", 0) for e in fast_events) / len(fast_events)
        avg_cust_in = sum(e.get("customizer_tokens_in", 0) for e in fast_events) / len(fast_events)
        avg_cust_out = sum(e.get("customizer_tokens_out", 0) for e in fast_events) / len(fast_events)
        avg_cache_read = sum(e.get("customizer_cache_read_input_tokens", 0) for e in fast_events) / len(fast_events)
        print(f"\nFAST PATH TOKENS (avg/hit):")
        print(f"  router:     in {avg_router_in:6.0f}  out {avg_router_out:6.0f}")
        print(f"  customizer: in {avg_cust_in:6.0f}  out {avg_cust_out:6.0f}")
        print(f"  cache_read (customizer): {avg_cache_read:6.0f}  ({100*avg_cache_read/max(avg_cust_in,1):.1f}% of in)")

        # Confidence 分布
        confs = [e.get("confidence", 0) for e in fast_events]
        if confs:
            print(f"\nFAST PATH CONFIDENCE:")
            print(f"  p50:  {_quantile(confs, 0.5):.3f}")
            print(f"  p05:  {_quantile(confs, 0.05):.3f}")
            print(f"  mean: {sum(confs)/len(confs):.3f}")
            print(f"  min:  {min(confs):.3f}")

    # ─── Slow path / fallback ────────────────────────────────────────────────
    slow_events = [e for e in events if e.get("route_decision") == "slow"]
    if slow_events:
        reasons = Counter(e.get("fallback_reason", "?") for e in slow_events)
        print(f"\nFALLBACK REASON DISTRIBUTION:")
        for r, n in reasons.most_common():
            print(f"  {r}: {n} ({100*n/len(slow_events):.1f}%)")

        # 低置信但路由有 archetype 的样本 → 候选阈值调整建议
        low_conf = [e for e in slow_events if e.get("archetype_id") and e.get("confidence", 0) > 0]
        if low_conf:
            print(f"\nLOW-CONFIDENCE NEAR-MISS (archetype matched but below threshold):")
            print(f"  {len(low_conf)} events ({100*len(low_conf)/len(slow_events):.1f}% of fallbacks)")
            confs = [e["confidence"] for e in low_conf]
            print(f"  confidence p50={_quantile(confs, 0.5):.2f}  p95={_quantile(confs, 0.95):.2f}")
            print(f"  → 阈值从 0.7 调到 {_quantile(confs, 0.5):.2f} 可能把这些都召回")

    # ─── 最终 outcome ────────────────────────────────────────────────────────
    outcomes = Counter(e.get("final_outcome", "?") for e in events)
    print(f"\nFINAL OUTCOMES:")
    for o, n in outcomes.most_common():
        print(f"  {o}: {n} ({100*n/total:.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
