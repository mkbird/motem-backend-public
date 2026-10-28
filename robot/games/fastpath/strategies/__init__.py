"""Archetype strategies registry.

v2 切换策略（P3 起逐个 archetype 替换）：
- slicer：v2 已经替换 v1，v1 文件保留作 reference 不再注册
- catch / dodger / whack：仍是 v1，待 P4+ 替换

老 zip 不受影响（meta.json 里写死 archetype_id，但 zip 完全 self-contained，
不依赖 STRATEGIES 注册）。router 看到的只有当前注册的 archetype，新生成游戏
自动走 v2。
"""

from robot.games.fastpath.strategies.base import ArchetypeStrategy
from robot.games.fastpath.strategies.catch_v1 import CatchV1Strategy  # 保留 import 作 reference
from robot.games.fastpath.strategies.catch_v2 import CatchV2Strategy
from robot.games.fastpath.strategies.dodger_v1 import DodgerV1Strategy  # 保留 import 作 reference
from robot.games.fastpath.strategies.dodger_v2 import DodgerV2Strategy
from robot.games.fastpath.strategies.slicer_v1 import SlicerV1Strategy  # 保留 import 作 reference
from robot.games.fastpath.strategies.slicer_v2 import SlicerV2Strategy
from robot.games.fastpath.strategies.whack_v1 import WhackV1Strategy  # 保留 import 作 reference
from robot.games.fastpath.strategies.whack_v2 import WhackV2Strategy


STRATEGIES: dict[str, type[ArchetypeStrategy]] = {
    CatchV2Strategy.archetype_id: CatchV2Strategy,    # v2 替换 v1
    DodgerV2Strategy.archetype_id: DodgerV2Strategy,  # v2 替换 v1
    WhackV2Strategy.archetype_id: WhackV2Strategy,    # v2 替换 v1
    SlicerV2Strategy.archetype_id: SlicerV2Strategy,  # v2 替换 v1（P3.1）
}


__all__ = [
    "ArchetypeStrategy",
    "CatchV1Strategy",
    "CatchV2Strategy",
    "DodgerV1Strategy",
    "DodgerV2Strategy",
    "WhackV1Strategy",
    "WhackV2Strategy",
    "SlicerV1Strategy",
    "SlicerV2Strategy",
    "STRATEGIES",
]
