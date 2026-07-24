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

from ..connect.config import SourceConfig, TableExtractConfig, config_revision, load_config
from ..connect.metadata import MetadataDiscoveryUnsupported, MetadataError, build_discoverer


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def table_spec_to_dict(spec: TableExtractConfig) -> dict[str, Any]:
    return {
        "mode": spec.mode,
        "schema": spec.schema,
        "key_columns": list(spec.key_columns) if spec.key_columns else None,
        "watermark": spec.watermark,
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
        }
        # 结构层已由 TableExtractConfig 保证;此处补业务语义码
        if spec.mode == "incremental":
            if not spec.key_columns:
                entry["status"] = "key_missing"
                entry["detail"] = "incremental 必须配置 key_columns"
                results.append(entry)
                continue
            if not spec.watermark:
                entry["status"] = "watermark_missing"
                entry["detail"] = "incremental 必须配置 watermark"
                results.append(entry)
                continue

        if discoverer is None:
            if not live:
                # 仅结构校验:不冒充现场已验证
                results.append(entry)
                continue
            if discoverer_error == "permission_denied":
                entry["status"] = "permission_denied"
                entry["detail"] = "无元数据访问权限"
            else:
                code = discoverer_error or "connection_failed"
                entry["status"] = "connection_failed"
                entry["detail"] = f"无法现场校验元数据({code})"
            results.append(entry)
            continue

        try:
            detail = discoverer.get_table(schema, name)
        except MetadataError as e:
            if e.code in ("table_not_found", "not_found"):
                entry["status"] = "table_missing"
            elif e.code == "permission_denied":
                entry["status"] = "permission_denied"
            else:
                entry["status"] = "table_missing"
            entry["detail"] = str(e)[:300]
            results.append(entry)
            continue
        except Exception as e:
            entry["status"] = "table_missing"
            entry["detail"] = str(e)[:300]
            results.append(entry)
            continue

        cols = {c.name for c in detail.columns}
        if spec.key_columns:
            missing = [c for c in spec.key_columns if c not in cols]
            if missing:
                entry["status"] = "key_missing"
                entry["detail"] = f"键列不存在: {', '.join(missing)}"
                results.append(entry)
                continue
            try:
                check = discoverer.check_key(
                    schema, name, list(spec.key_columns), timeout_seconds=15)
                if not check.ok:
                    code = "key_not_unique"
                    if check.code == "key_missing":
                        code = "key_missing"
                    entry["status"] = code
                    entry["detail"] = check.detail or check.code
                    results.append(entry)
                    continue
            except Exception as e:
                entry["status"] = "key_not_unique"
                entry["detail"] = str(e)[:300]
                results.append(entry)
                continue

        if spec.watermark:
            if spec.watermark not in cols:
                entry["status"] = "watermark_invalid"
                entry["detail"] = f"水位列不存在: {spec.watermark}"
                results.append(entry)
                continue
            try:
                wm = discoverer.check_watermark(schema, name, spec.watermark)
                if not wm.ok:
                    entry["status"] = "watermark_invalid"
                    entry["detail"] = wm.detail or wm.code
                    results.append(entry)
                    continue
            except Exception as e:
                entry["status"] = "watermark_invalid"
                entry["detail"] = str(e)[:300]
                results.append(entry)
                continue

        if spec.schema_fingerprint and detail.schema_fingerprint:
            if spec.schema_fingerprint != detail.schema_fingerprint:
                entry["status"] = "metadata_stale"
                entry["detail"] = "结构指纹与最近扫描不一致,请重新确认"
                results.append(entry)
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
        return False, [{"field": "source", "message": f"配置中没有源 '{source}'"}], config_revision(path)

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
        return False, [{"field": "", "message": str(e)}], config_revision(path)

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
