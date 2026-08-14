"""场景预计算器注册表。

materializer 在 raw→对象 mapping 前运行。它只能读取已同步的 raw 表并写入
内部结果表；对象仍由 YAML binding、候选表和 dataset publish 统一治理。

每个 materializer 以 MaterializerSpec 声明输入/输出表:apply 在构建前据此
做输入就绪预检——输入表未同步到时**跳过**相关对象(暂时状态,下轮补齐),
而不是让整个数据集构建失败并卡死 generation 屏障。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..store.landing import LandingStore
from ..metamodel.schema import SourceBinding

from .dead_stock import materialize_dead_stock_item
from .dead_stock_attribution import materialize_dead_stock_attribution
from .dead_stock_attribution_m3 import materialize_dead_stock_attribution_m3
from .dead_stock_attribution_m3b import materialize_dead_stock_attribution_m3b

Materializer = Callable[[LandingStore, str], int]


@dataclass(frozen=True)
class MaterializerSpec:
    """预计算器声明。requires/produces 必须与实现读取/写入的表严格一致。"""

    fn: Materializer
    requires: frozenset[str]  # 逻辑输入表(raw 表或上游产出的 D2A_* 表)
    produces: frozenset[str]  # 产出的 D2A_* 逻辑表


_REGISTRY: dict[str, MaterializerSpec] = {
    "dead_stock_item_v1": MaterializerSpec(
        materialize_dead_stock_item,
        requires=frozenset({"ITEM_WAREHOUSE"}),
        produces=frozenset({"D2A_DEAD_STOCK_ITEM"}),
    ),
    "dead_stock_attribution_v1": MaterializerSpec(
        materialize_dead_stock_attribution,
        requires=frozenset({"D2A_DEAD_STOCK_ITEM"}),
        produces=frozenset({
            "D2A_PURCHASE_OVERBUY_EVIDENCE",
            "D2A_PRODUCTION_LOSS_EVIDENCE",
            "D2A_DEAD_STOCK_ATTRIBUTION",
        }),
    ),
    "dead_stock_attribution_v2": MaterializerSpec(
        materialize_dead_stock_attribution_m3,
        requires=frozenset({"D2A_DEAD_STOCK_ITEM"}),
        produces=frozenset({
            "D2A_MATERIAL_ORDER_EVIDENCE",
            "D2A_ECN_CHANGE_EVIDENCE",
        }),
    ),
    "dead_stock_attribution_v3": MaterializerSpec(
        materialize_dead_stock_attribution_m3b,
        requires=frozenset({"D2A_DEAD_STOCK_ITEM"}),
        produces=frozenset({
            "D2A_SPECIAL_CONDITION_EVIDENCE",
            "D2A_DUPLICATE_MATERIAL_CANDIDATE",
            "D2A_MATERIAL_BOM_USAGE",
            "D2A_MATERIAL_SUBSTITUTE_CANDIDATE",
        }),
    ),
}

# 依赖序:上游产物必须先于下游评估/执行。
_ORDER = (
    "dead_stock_item_v1",
    "dead_stock_attribution_v1",
    "dead_stock_attribution_v2",
    "dead_stock_attribution_v3",
)


def materializer_specs() -> dict[str, MaterializerSpec]:
    """按依赖序返回全部已注册预计算器声明。"""
    return {
        name: _REGISTRY[name]
        for name in [*_ORDER, *sorted(set(_REGISTRY) - set(_ORDER))]
    }


def materialize_bindings(
    store: LandingStore, source: str, bindings: Iterable[SourceBinding],
) -> dict[str, int]:
    """运行本次 dataset build 所需的预计算器，每个名称最多一次。"""
    result: dict[str, int] = {}
    requested = {b.materializer for b in bindings if b.materializer}
    unknown = requested - set(_REGISTRY)
    if unknown:
        raise ValueError(f"未知场景 materializer: {sorted(unknown)}")
    for name, spec in materializer_specs().items():
        if name in requested:
            result[name] = spec.fn(store, source)
    return result
