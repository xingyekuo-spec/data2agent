"""抽取表计划读写与校验(M5):独立于通用 config 白名单。"""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import ValidationError

from ...shared.config import SourceConfig, TableExtractConfig, config_revision, load_config
from ..extract.metadata import MetadataDiscoveryUnsupported, MetadataError, build_discoverer
from ...shared.admin.suggestions import field_error


_STATUS_SUGGESTIONS: dict[str, str] = {
    "key_missing": "在「抽取表」页为该表填写 key_columns（业务键或主键）后重新校验",
    "watermark_missing": "为 incremental 表指定水位列（通常为更新时间类字段）后重新校验",
    "watermark_invalid": "更换合适的水位列，或在元数据页确认字段类型后重试",
    "permission_denied": "为只读账号授予该表/元数据权限后重新扫描并校验",
    "connection_failed": "先在「配置」页测试数据库连接，修复连通性问题后再校验",
    "table_missing": "确认表名/schema，或先在「元数据」页刷新扫描后再选表",
    "key_not_unique": "更换唯一键组合，或先清洗源表重复/空值后再作为抽取键",
    "fingerprint_mismatch": "重新打开元数据扫描确认结构，再写回抽取计划",
    "metadata_stale": "重新打开元数据扫描确认结构，再写回抽取计划",
}


def _mark(
    entry: dict[str, Any],
    status: str,
    detail: str,
    suggestion: str | None = None,
) -> dict[str, Any]:
    entry["status"] = status
    entry["detail"] = detail
    entry["suggestion"] = suggestion or _STATUS_SUGGESTIONS.get(
        status, "修正配置后重新校验并保存")
    return entry


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def table_spec_to_dict(spec: TableExtractConfig) -> dict[str, Any]:
    return {
        "mode": spec.mode,
        "schema": spec.schema,
        "key_columns": list(spec.key_columns) if spec.key_columns else None,
        "watermark": spec.watermark,
        "start_date": spec.start_date,
        "schema_fingerprint": spec.schema_fingerprint,
        "validated_at": spec.validated_at,
    }


def parse_tables_payload(raw: dict[str, Any] | None) -> dict[str, TableExtractConfig]:
    """将请求体 tables 解析为模型;非法项抛 ValidationError。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("tables 必须是对象")
    out: dict[str, TableExtractConfig] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("表名不能为空")
        if not isinstance(spec, dict):
            raise ValueError(f"{name}: 配置必须是对象")
        out[name] = TableExtractConfig.model_validate(spec)
    # 大小写冲突
    seen: dict[str, str] = {}
    for name in out:
        key = name.casefold()
        if key in seen:
            raise ValueError(f"表名大小写冲突: {seen[key]} / {name}")
        seen[key] = name
    return out


def validate_table_plan(
    scfg: SourceConfig,
    tables: dict[str, TableExtractConfig],
    *,
    live: bool = True,
) -> list[dict[str, Any]]:
    """逐表返回校验结果。live=True 时尝试元数据发现与键/水位检查。"""
    results: list[dict[str, Any]] = []
    discoverer = None
    discoverer_error: str | None = None
    if live:
        try:
            discoverer = build_discoverer(scfg)
        except MetadataDiscoveryUnsupported:
            discoverer = None
            discoverer_error = "metadata_discovery_unsupported"
        except MetadataError as e:
            discoverer = None
            discoverer_error = e.code
        except Exception:
            discoverer = None
            discoverer_error = "connection_failed"

    default_schema = "main" if scfg.adapter == "sqlite_readonly" else "dbo"
    for name, spec in sorted(tables.items()):
        schema = spec.schema or default_schema
        entry: dict[str, Any] = {
            "table": name,
            "schema": schema,
            "mode": spec.mode,
            "status": "ready",
            "detail": None,
            "suggestion": None,
        }
        if spec.mode == "incremental":
            if not spec.key_columns:
                results.append(_mark(
                    entry, "key_missing", "incremental 必须配置 key_columns"))
                continue
            if not spec.watermark:
                results.append(_mark(
                    entry, "watermark_missing", "incremental 必须配置 watermark"))
                continue

        if discoverer is None:
            if not live:
                results.append(entry)
                continue
            if discoverer_error == "permission_denied":
                results.append(_mark(entry, "permission_denied", "无元数据访问权限"))
            else:
                code = discoverer_error or "connection_failed"
                results.append(_mark(
                    entry, "connection_failed",
                    f"无法现场校验元数据({code})"))
            continue

        try:
            detail = discoverer.get_table(schema, name)
        except MetadataError as e:
            if e.code in ("table_not_found", "not_found"):
                status = "table_missing"
            elif e.code == "permission_denied":
                status = "permission_denied"
            else:
                status = "table_missing"
            results.append(_mark(
                entry, status, str(e)[:300], getattr(e, "suggestion", None)))
            continue
        except Exception as e:
            results.append(_mark(entry, "table_missing", str(e)[:300]))
            continue

        cols = {c.name for c in detail.columns}
        if spec.key_columns:
            missing = [c for c in spec.key_columns if c not in cols]
            if missing:
                results.append(_mark(
                    entry, "key_missing",
                    f"键列不存在: {', '.join(missing)}"))
                continue
            try:
                check = discoverer.check_key(
                    schema, name, list(spec.key_columns), timeout_seconds=15)
                if not check.ok:
                    code = "key_not_unique"
                    if check.code == "key_missing":
                        code = "key_missing"
                    results.append(_mark(
                        entry, code, check.detail or check.code))
                    continue
            except Exception as e:
                results.append(_mark(entry, "key_not_unique", str(e)[:300]))
                continue

        if spec.watermark:
            if spec.watermark not in cols:
                results.append(_mark(
                    entry, "watermark_invalid",
                    f"水位列不存在: {spec.watermark}"))
                continue
            try:
                wm = discoverer.check_watermark(schema, name, spec.watermark)
                if not wm.ok:
                    results.append(_mark(
                        entry, "watermark_invalid", wm.detail or wm.code))
                    continue
            except Exception as e:
                results.append(_mark(entry, "watermark_invalid", str(e)[:300]))
                continue

        if spec.schema_fingerprint and detail.schema_fingerprint:
            if spec.schema_fingerprint != detail.schema_fingerprint:
                results.append(_mark(
                    entry, "metadata_stale",
                    "结构指纹与最近扫描不一致,请重新确认"))
                continue

        entry["status"] = "ready"
        results.append(entry)


    if discoverer is not None and hasattr(discoverer, "close"):
        try:
            discoverer.close()
        except Exception:
            pass
    return results


def replace_source_tables(
    path: Path,
    source: str,
    tables: dict[str, TableExtractConfig],
    *,
    validate: Callable[[Path], Any] | None = load_config,
    stamp_validated_at: bool = False,
) -> tuple[bool, list[dict[str, str]], str]:
    """原子替换 sources.<source>.tables;返回 (ok, errors, new_revision)。

    stamp_validated_at=True 仅在现场校验全部 ready 后由 PUT 传入,避免未验证计划伪装已验证。
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = data.get("sources")
    if not isinstance(sources, dict) or source not in sources:
        return False, [field_error(
            "source",
            f"配置中没有源 '{source}'",
            "在「配置」页确认 sources 名称，或先完成首次配置",
        )], config_revision(path)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = path.with_suffix(path.suffix + f".bak-{timestamp}")
    shutil.copy2(path, backup_path)

    now = _utc_now() if stamp_validated_at else None
    merged = copy.deepcopy(data)
    merged["sources"][source]["tables"] = {}
    for name, spec in tables.items():
        row = {
            "mode": spec.mode,
            "schema": spec.schema,
            "key_columns": list(spec.key_columns) if spec.key_columns else None,
            "watermark": spec.watermark,
            "start_date": spec.start_date,
            "schema_fingerprint": spec.schema_fingerprint,
            "validated_at": now if stamp_validated_at else spec.validated_at,
        }
        merged["sources"][source]["tables"][name] = {
            k: v for k, v in row.items() if v is not None
        }

    yaml_text = yaml.dump(merged, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        suffix=".yaml", dir=str(path.parent), prefix=".tmp-")
    tmp_path = Path(tmp_path_str)
    try:
        os.write(tmp_fd, yaml_text.encode("utf-8"))
        os.close(tmp_fd)
        if validate is not None:
            validate(tmp_path)
        os.replace(tmp_path_str, str(path))
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        try:
            os.close(tmp_fd)
        except Exception:
            pass
        return False, [field_error(
            "",
            str(e),
            "根据报错修正 connect.yaml / 抽取表计划后重试保存",
        )], config_revision(path)

    return True, [], config_revision(path)


def plan_diff(
    before: dict[str, TableExtractConfig],
    after: dict[str, TableExtractConfig],
) -> dict[str, list[str]]:
    b, a = set(before), set(after)
    added = sorted(a - b)
    removed = sorted(b - a)
    changed = sorted(
        name for name in (a & b)
        if table_spec_to_dict(before[name]) != table_spec_to_dict(after[name])
    )
    return {"added": added, "removed": removed, "changed": changed}
