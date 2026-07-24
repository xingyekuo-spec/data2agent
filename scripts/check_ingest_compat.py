#!/usr/bin/env python3
"""ingest 协议兼容性门禁与 Release 说明生成。

区分:
- application_version:平台/中间机可独立升级
- send_ingest_protocol_version:中间机实际发送的协议(INGEST_PROTOCOL_VERSION)
- supported_ingest_protocol_versions:平台接受的协议列表

破坏性变更声明(可审计、随提交进仓):
  deploy/ingest_protocol_compat.json
  - field_baseline_send_protocols:现场仍可能在跑的中间机发送协议基线
  - unsupported: { "<version>": { "reason": "...", "since_release": "vX.Y.Z" } }
    仅当平台 SUPPORTED 不再包含某基线协议时填写;CI/tag Release 均读此文件。

用法:
  python scripts/check_ingest_compat.py
  python scripts/check_ingest_compat.py --emit-release-notes --release-version v0.5.1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPAT_PATH = ROOT / "deploy" / "ingest_protocol_compat.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data2agent.ingest.protocol import (  # noqa: E402
    INGEST_PROTOCOL_VERSION,
    SUPPORTED_INGEST_PROTOCOL_VERSIONS,
)


def load_compat_manifest(path: Path = COMPAT_PATH) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"缺少协议兼容声明: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 须为 JSON object")
    return data


def field_baseline(manifest: dict) -> list[str]:
    raw = manifest.get("field_baseline_send_protocols")
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) and x for x in raw):
        raise ValueError("field_baseline_send_protocols 须为非空字符串列表")
    return list(raw)


def unsupported_map(manifest: dict) -> dict[str, dict]:
    raw = manifest.get("unsupported")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("unsupported 须为 object")
    out: dict[str, dict] = {}
    for ver, meta in raw.items():
        if not isinstance(ver, str) or not ver:
            raise ValueError("unsupported 的键须为非空协议号字符串")
        if not isinstance(meta, dict):
            raise ValueError(f"unsupported[{ver!r}] 须为 object")
        out[ver] = meta
    return out


def dropped_undeclared(manifest: dict | None = None) -> list[str]:
    """基线中已不在 SUPPORTED、且未在 manifest.unsupported 声明的协议。"""
    man = manifest if manifest is not None else load_compat_manifest()
    supported = set(SUPPORTED_INGEST_PROTOCOL_VERSIONS)
    declared = set(unsupported_map(man))
    return [v for v in field_baseline(man) if v not in supported and v not in declared]


def check_constants(manifest: dict | None = None) -> list[str]:
    errors: list[str] = []
    try:
        man = manifest if manifest is not None else load_compat_manifest()
        baseline = field_baseline(man)
        unsupported = unsupported_map(man)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return [str(e)]

    if not SUPPORTED_INGEST_PROTOCOL_VERSIONS:
        errors.append("SUPPORTED_INGEST_PROTOCOL_VERSIONS 不能为空")
    if INGEST_PROTOCOL_VERSION not in SUPPORTED_INGEST_PROTOCOL_VERSIONS:
        errors.append(
            f"INGEST_PROTOCOL_VERSION={INGEST_PROTOCOL_VERSION!r} "
            "必须落在 SUPPORTED_INGEST_PROTOCOL_VERSIONS 内"
        )

    supported = set(SUPPORTED_INGEST_PROTOCOL_VERSIONS)
    for ver, meta in unsupported.items():
        reason = meta.get("reason")
        since = meta.get("since_release")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"unsupported[{ver!r}].reason 须为非空字符串")
        if not isinstance(since, str) or not since.strip():
            errors.append(f"unsupported[{ver!r}].since_release 须为非空字符串")
        if ver in supported:
            errors.append(
                f"unsupported 声明了 {ver!r},但该协议仍在 "
                "SUPPORTED_INGEST_PROTOCOL_VERSIONS 中"
            )
        if ver not in baseline:
            errors.append(
                f"unsupported 声明了 {ver!r},但 field_baseline_send_protocols "
                "未包含该协议(请先纳入基线或删除声明)"
            )

    missing = [v for v in baseline if v not in supported and v not in unsupported]
    if missing:
        errors.append(
            "平台不再支持现场基线协议 "
            f"{missing},须在 deploy/ingest_protocol_compat.json 的 "
            "unsupported 中写明 reason 与 since_release"
        )
    return errors


def render_release_notes(
    *,
    release_version: str | None = None,
    manifest: dict | None = None,
) -> str:
    man = manifest if manifest is not None else load_compat_manifest()
    unsupported = unsupported_map(man)
    supported = ", ".join(f"v{v}" for v in SUPPORTED_INGEST_PROTOCOL_VERSIONS)
    active = INGEST_PROTOCOL_VERSION
    ver = release_version or os.environ.get("RELEASE_VERSION", "").strip() or "本版本"
    lines = [
        f"## ingest 协议兼容性（{ver}）",
        "",
        f"- 平台当前协议（active / 新中间机发送）: **v{active}**",
        f"- 平台兼容中间机发送协议: **{supported}**",
        "",
    ]
    # 相对本版仍「故意不支持」的基线协议 → 破坏性提示
    breaking = [
        v for v in field_baseline(man)
        if v in unsupported and v not in SUPPORTED_INGEST_PROTOCOL_VERSIONS
    ]
    if breaking:
        details = []
        for v in breaking:
            meta = unsupported[v]
            details.append(
                f"v{v}（自 {meta.get('since_release', '?')}: {meta.get('reason', '').strip()}）"
            )
        lines.extend(
            [
                "> ⚠️ **破坏性发布**:本版本不再接受以下中间机发送协议:",
                "> - " + "\n> - ".join(details),
                "> **对应既有中间机必须升级**到发送受支持协议的包后再同步。",
                "",
            ]
        )
    else:
        # 若基线中仍有受支持的旧发送协议,强调可只升平台
        legacy_ok = [v for v in field_baseline(man) if v in SUPPORTED_INGEST_PROTOCOL_VERSIONS]
        if legacy_ok:
            legacy = ", ".join(f"v{v}" for v in legacy_ok)
            lines.extend(
                [
                    f"使用 **{legacy}** 的既有中间机**无需升级**即可向本版数据平台推送。",
                    "数据平台可单独升级;仅当本说明出现破坏性协议变更时才需升级中间机。",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "请按平台 supported 列表选择匹配的中间机包。",
                    "",
                ]
            )
    lines.append("详见 `docs/runbook/portable.md`。")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--emit-release-notes",
        action="store_true",
        help="向 stdout 打印 Release 兼容性段落",
    )
    parser.add_argument(
        "--release-version",
        default="",
        help="写入说明中的版本标签(默认读 RELEASE_VERSION 环境变量)",
    )
    parser.add_argument(
        "--write",
        type=Path,
        help="把 Release 兼容性段落写入该文件",
    )
    args = parser.parse_args()

    errors = check_constants()
    if errors:
        for e in errors:
            print(f"ingest compat check failed: {e}", file=sys.stderr)
        return 1

    notes = render_release_notes(release_version=args.release_version or None)
    if args.write:
        args.write.write_text(notes, encoding="utf-8")
        print(f"wrote {args.write}")
    if args.emit_release_notes:
        sys.stdout.write(notes)
    elif not args.write:
        print(
            "ingest compat check: OK "
            f"(send/active={INGEST_PROTOCOL_VERSION}, "
            f"supported={list(SUPPORTED_INGEST_PROTOCOL_VERSIONS)}, "
            f"manifest={COMPAT_PATH.relative_to(ROOT)})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
