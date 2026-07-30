"""场景预计算器注册表。

materializer 在 raw→对象 mapping 前运行。它只能读取已同步的 raw 表并写入
内部结果表；对象仍由 YAML binding、候选表和 dataset publish 统一治理。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..store.landing import LandingStore
from ..metamodel.schema import SourceBinding

from .dead_stock import materialize_dead_stock_item
from .dead_stock_attribution import materialize_dead_stock_attribution
from .dead_stock_attribution_m3 import materialize_dead_stock_attribution_m3
from .dead_stock_attribution_m3b import materialize_dead_stock_attribution_m3b

Materializer = Callable[[LandingStore, str], int]

_REGISTRY: dict[str, Materializer] = {
    "dead_stock_item_v1": materialize_dead_stock_item,
    "dead_stock_attribution_v1": materialize_dead_stock_attribution,
    "dead_stock_attribution_v2": materialize_dead_stock_attribution_m3,
    "dead_stock_attribution_v3": materialize_dead_stock_attribution_m3b,
}

_ORDER = (
    "dead_stock_item_v1",
    "dead_stock_attribution_v1",
    "dead_stock_attribution_v2",
    "dead_stock_attribution_v3",
)


def materialize_bindings(
    store: LandingStore, source: str, bindings: Iterable[SourceBinding],
) -> dict[str, int]:
    """运行本次 dataset build 所需的预计算器，每个名称最多一次。"""
    result: dict[str, int] = {}
    requested = {b.materializer for b in bindings if b.materializer}
    unknown = requested - set(_REGISTRY)
    if unknown:
        raise ValueError(f"未知场景 materializer: {sorted(unknown)}")
    for name in [*filter(requested.__contains__, _ORDER), *sorted(requested - set(_ORDER))]:
        materializer = _REGISTRY.get(name)
        assert materializer is not None
        result[name] = materializer(store, source)
    return result
