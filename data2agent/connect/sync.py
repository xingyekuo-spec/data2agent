"""同步报告结构与全量入口;增量编排见 increment.py。"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..metamodel.schema import TemplatePack
from .adapters.base import SourceAdapter
from .landing import LandingStore


def whitelist_from_pack(pack: TemplatePack, source: str) -> set[str]:
    """白名单由模板 binding 自动推导 —— 元模型作为唯一事实来源。"""
    return {
        t
        for o in pack.objects
        for b in o.bindings
        if b.source == source and b.enabled
        for t in b.source_tables
    }


@dataclass
class TableReport:
    table: str
    rows: int
    batches: int
    batch_id: str
    strategy: str = "full_refresh"
    high_water: str | None = None


@dataclass
class SyncReport:
    source: str
    run_id: int
    tables: list[TableReport] = field(default_factory=list)
    paused: bool = False        # 错峰窗口越界,批次边界优雅暂停

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)


def full_sync(adapter: SourceAdapter, landing: LandingStore, source: str) -> SyncReport:
    """全量同步 = 所有表按 full_refresh 策略的增量编排(不建立水位)。"""
    from .increment import incremental_sync

    return incremental_sync(adapter, landing, source, watermarks={})


def migrate_config_to_tables(
    config_path: str,
    pack: "TemplatePack | None" = None,
) -> tuple[str, dict[str, list[str]]]:
    """迁移旧配置到显式 tables。返回 (备份路径, {源名: [表名...]})。"""
    import shutil
    from datetime import datetime
    from pathlib import Path
    import yaml

    from ..metamodel.loader import load_pack as _load_pack
    from .increment import watermarks_from_pack

    path = Path(config_path)
    raw_text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}

    # 检查是否需要迁移
    sources = data.get("sources") or {}
    needs_migration = False
    for sdata in sources.values():
        if "whitelist_from_bindings" in sdata or "extra_whitelist" in sdata:
            needs_migration = True
            break
        if "tables" not in sdata:
            needs_migration = True
            break

    if not needs_migration:
        raise RuntimeError("配置已包含 tables,无需迁移。")

    # 备份
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_path = path.with_suffix(path.suffix + f".bak-{ts}")
    shutil.copy2(path, bak_path)

    # 加载模板
    if pack is None:
        tmpl = data.get("templates", "templates")
        pack = _load_pack(tmpl)

    result: dict[str, list[str]] = {}

    for src_name, sdata in sources.items():
        # Skip sources that already have tables and no old fields
        has_old = "whitelist_from_bindings" in sdata or "extra_whitelist" in sdata
        if "tables" in sdata and not has_old:
            result[src_name] = sorted(sdata["tables"].keys())
            continue

        wfb = sdata.pop("whitelist_from_bindings", True)
        extra = set(sdata.pop("extra_whitelist", []))

        tables_set: set[str] = set()
        if wfb:
            tables_set |= {
                t for o in pack.objects
                for b in o.bindings
                if b.source == src_name and b.enabled
                for t in b.tables
            }
        tables_set |= extra

        wm_map = watermarks_from_pack(pack, src_name)

        new_tables: dict[str, dict[str, str]] = {}
        for tbl in sorted(tables_set):
            wm = wm_map.get(tbl)
            if wm:
                new_tables[tbl] = {"mode": "incremental", "watermark": wm}
            else:
                new_tables[tbl] = {"mode": "full_refresh"}

        sdata["tables"] = new_tables
        result[src_name] = sorted(tables_set)

    # 写入新配置
    path.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # 校验新配置
    from .config import load_config
    try:
        load_config(path)
    except Exception as e:
        shutil.copy2(bak_path, path)
        raise RuntimeError(f"迁移后配置校验失败,已恢复备份: {e}") from e

    return str(bak_path), result
