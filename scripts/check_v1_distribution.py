#!/usr/bin/env python3
"""检查 Vue /v1 分发就绪:dist 存在性、index 资源引用、FastAPI 挂载行为。"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_dist_files(dist: Path) -> list[str]:
    errors: list[str] = []
    index = dist / "index.html"
    if not index.is_file():
        return [f"missing {index}"]
    html = index.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/v1/[^"]+)"', html)
    if not refs:
        errors.append("index.html 未引用 /v1/ 资源")
    for ref in refs:
        rel = ref.removeprefix("/v1/")
        path = dist / rel
        if not path.is_file():
            errors.append(f"missing asset {path} (from {ref})")
    if "mockServiceWorker" in html or "msw" in html.lower():
        errors.append("dist 疑似包含 Mock/MSW")
    return errors


def check_fastapi_mount(dist: Path | None) -> list[str]:
    errors: list[str] = []
    try:
        from fastapi.testclient import TestClient
        from data2agent.console.app import create_app
    except Exception as e:  # pragma: no cover
        return [f"无法导入 create_app: {e}"]

    with tempfile.TemporaryDirectory() as tmp:
        landing = Path(tmp) / "landing.sqlite"
        landing.touch()
        if dist is not None:
            import os
            os.environ["D2A_VUE_DIST"] = str(dist)
        else:
            import os
            os.environ.pop("D2A_VUE_DIST", None)
        app = create_app(str(landing), str(ROOT / "templates"))
        client = TestClient(app)
        if dist is not None:
            for path in ("/v1/", "/v1/mcp"):
                r = client.get(path)
                if r.status_code != 200:
                    errors.append(f"{path} expected 200, got {r.status_code}")
            root = client.get("/", follow_redirects=False)
            if root.status_code != 302 or root.headers.get("location") != "/v1/":
                errors.append("/ should redirect to /v1/")
            v0 = client.get("/v0", follow_redirects=False)
            if v0.status_code != 302 or v0.headers.get("location") != "/v1/":
                errors.append("/v0 should redirect to /v1/")
        else:
            r = client.get("/v1/")
            if r.status_code != 503:
                errors.append(f"missing dist: /v1/ expected 503, got {r.status_code}")
            if "未安装" not in r.text and "未构建" not in r.text:
                errors.append("missing dist page should diagnose 未安装/未构建")
            v0 = client.get("/v0", follow_redirects=False)
            if v0.status_code != 302 or v0.headers.get("location") != "/v1/":
                errors.append("/v0 should redirect to /v1/ when /v1 missing")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=ROOT / "console-ui" / "dist",
        help="Vue dist 目录(默认 console-ui/dist)",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="允许 dist 缺失并验证 503 诊断路径",
    )
    args = parser.parse_args(argv)
    errors: list[str] = []
    if args.dist.is_dir() and (args.dist / "index.html").is_file():
        errors.extend(check_dist_files(args.dist))
        errors.extend(check_fastapi_mount(args.dist))
    elif args.allow_missing:
        errors.extend(check_fastapi_mount(None))
    else:
        errors.append(f"dist 不存在: {args.dist}(可先 npm run build,或传 --allow-missing)")
    if errors:
        print("FAIL: /v1 distribution checks")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK: /v1 distribution checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
