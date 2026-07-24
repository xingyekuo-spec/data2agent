#!/usr/bin/env python3
"""ingest 协议兼容性门禁与 Release 说明生成。

区分:
- application_version:平台/中间机可独立升级
- ingest_protocol_version(s):跨机推送契约;平台仍声明支持则旧中间机无需升级

用法:
  python scripts/check_ingest_compat.py
  python scripts/check_ingest_compat.py --emit-release-notes
  D2A_BREAKING_INGEST_PROTOCOL=1 python scripts/check_ingest_compat.py \\
      --emit-release-notes   # 允许从 supported 移除 BASELINE 中的协议
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data2agent.ingest.protocol import (  # noqa: E402
    BASELINE_INGEST_PROTOCOL_VERSIONS,
    INGEST_PROTOCOL_VERSION,
    SUPPORTED_INGEST_PROTOCOL_VERSIONS,
)


def dropped_baseline_protocols() -> list[str]:
    supported = set(SUPPORTED_INGEST_PROTOCOL_VERSIONS)
    return [v for v in BASELINE_INGEST_PROTOCOL_VERSIONS if v not in supported]


def breaking_declared() -> bool:
    raw = os.environ.get("D2A_BREAKING_INGEST_PROTOCOL", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def check_constants() -> list[str]:
    errors: list[str] = []
    if not SUPPORTED_INGEST_PROTOCOL_VERSIONS:
        errors.append("SUPPORTED_INGEST_PROTOCOL_VERSIONS 不能为空")
    if INGEST_PROTOCOL_VERSION not in SUPPORTED_INGEST_PROTOCOL_VERSIONS:
        errors.append(
            f"INGEST_PROTOCOL_VERSION={INGEST_PROTOCOL_VERSION!r} "
            "必须落在 SUPPORTED_INGEST_PROTOCOL_VERSIONS 内"
        )
    dropped = dropped_baseline_protocols()
    if dropped and not breaking_declared():
        errors.append(
            "平台不再支持既有中间机协议 "
            f"{dropped},须设置 D2A_BREAKING_INGEST_PROTOCOL=1 "
            "并在 Release 中提示中间机必须升级"
        )
    return errors


def render_release_notes(*, release_version: str | None = None) -> str:
    supported = ", ".join(f"v{v}" for v in SUPPORTED_INGEST_PROTOCOL_VERSIONS)
    active = INGEST_PROTOCOL_VERSION
    dropped = dropped_baseline_protocols()
    ver = release_version or os.environ.get("RELEASE_VERSION", "").strip() or "本版本"
    lines = [
        f"## ingest 协议兼容性（{ver}）",
        "",
        f"- 平台当前协议（active）: **v{active}**",
        f"- 平台兼容中间机协议: **{supported}**",
        "",
    ]
    if dropped:
        lines.extend(
            [
                "> ⚠️ **破坏性发布**:本版本不再接受中间机协议 "
                + ", ".join(f"v{v}" for v in dropped)
                + "。",
                "> **既有中间机必须升级**到支持新协议的包后再同步。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"使用 **v{active}** 的既有中间机**无需升级**即可向本版数据平台推送。",
                "数据平台可单独升级;仅当本说明出现破坏性协议变更时才需升级中间机。",
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
    if args.emit_release_notes or args.write:
        if args.emit_release_notes:
            sys.stdout.write(notes)
    else:
        print(
            "ingest compat check: OK "
            f"(active={INGEST_PROTOCOL_VERSION}, "
            f"supported={list(SUPPORTED_INGEST_PROTOCOL_VERSIONS)})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
