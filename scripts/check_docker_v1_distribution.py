#!/usr/bin/env python3
"""完整多阶段 Docker 镜像的 /v1 分发证据检查。

默认执行:
  1. docker build -f deploy/runner.Dockerfile
  2. 记录 image id
  3. 在容器内确认 dist 与 FastAPI /v1、/v1/mcp、hashed asset 返回 200

用法:
  python scripts/check_docker_v1_distribution.py
  python scripts/check_docker_v1_distribution.py --skip-build --image data2agent:m6
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAG = "data2agent:m6"
DOCKERFILE = "deploy/runner.Dockerfile"

_INNER_CHECK = r"""
import os, re, tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from data2agent.console.app import create_app, resolve_vue_dist

dist = resolve_vue_dist()
assert dist is not None, "resolve_vue_dist returned None"
assert (dist / "index.html").is_file(), f"missing {dist}/index.html"
html = (dist / "index.html").read_text(encoding="utf-8")
refs = re.findall(r'(?:src|href)="(/v1/[^"]+)"', html)
assert refs, "index.html has no /v1/ asset refs"
os.environ["D2A_VUE_DIST"] = str(dist)
landing = Path(tempfile.mkdtemp()) / "landing.sqlite"
landing.touch()
app = create_app(str(landing), "templates")
client = TestClient(app)
for path in ("/v1/", "/v1/mcp", *refs[:3]):
    r = client.get(path)
    print(f"OK {path} -> {r.status_code} ({len(r.content)} bytes)")
    assert r.status_code == 200, (path, r.status_code)
print("DOCKER_FULL_BUILD_V1_OK")
print(f"DIST={dist}")
"""


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def capture(cmd: list[str]) -> str:
    print("+", " ".join(cmd), flush=True)
    return subprocess.check_output(cmd, text=True).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_TAG, help="镜像标签")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="跳过 build,直接检查已有 --image",
    )
    parser.add_argument(
        "--dockerfile",
        default=DOCKERFILE,
        help="Dockerfile 路径(相对仓库根)",
    )
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=None,
        help="将证据 JSON 写入该文件(默认打印到 stdout)",
    )
    args = parser.parse_args(argv)

    if not args.skip_build:
        run([
            "docker", "build",
            "-f", str(ROOT / args.dockerfile),
            "-t", args.image,
            str(ROOT),
        ])

    image_id = capture([
        "docker", "image", "inspect", args.image, "--format", "{{.Id}}",
    ])
    created = capture([
        "docker", "image", "inspect", args.image, "--format", "{{.Created}}",
    ])
    try:
        commit = capture(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    except subprocess.CalledProcessError:
        commit = "unknown"

    print("--- evidence ---", flush=True)
    print(f"commit={commit}", flush=True)
    print(f"image={args.image}", flush=True)
    print(f"image_id={image_id}", flush=True)
    print(f"created={created}", flush=True)
    print(f"dockerfile={args.dockerfile}", flush=True)

    cmd = [
        "docker", "run", "--rm",
        "-e", "D2A_VUE_DIST=/app/console-ui/dist",
        args.image,
        "python", "-c", _INNER_CHECK,
    ]
    print("+ docker run … python -c <inner /v1 check>", flush=True)
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print("FAIL: container /v1 check", file=sys.stderr)
        return result.returncode or 1

    evidence = {
        "commit": commit,
        "image": args.image,
        "image_id": image_id,
        "created": created,
        "dockerfile": args.dockerfile,
        "result": "DOCKER_FULL_BUILD_V1_OK",
    }
    text = json.dumps(evidence, indent=2, ensure_ascii=False)
    print("--- evidence.json ---", flush=True)
    print(text, flush=True)
    if args.evidence_out is not None:
        args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.evidence_out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
