#!/usr/bin/env python3
"""Prepare a data2agent release.

This script updates the project version in pyproject.toml and the console UI
package files, verifies version consistency, and can optionally create the
release commit/tag and push them.  GitHub Actions still builds the portable
packages and creates the GitHub Release after the tag is pushed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)?$")


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, check=check, text=True)


def _capture(cmd: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(
        cmd, cwd=ROOT, check=check, text=True, capture_output=True)
    return proc.stdout.strip()


def _normalize_version(raw: str) -> tuple[str, str]:
    version = raw.strip().removeprefix("v")
    if not VERSION_RE.fullmatch(version):
        raise SystemExit(
            f"版本号格式不正确: {raw!r}; 请使用 0.5.3 或 v0.5.3")
    return version, f"v{version}"


def _replace_pyproject_version(version: str) -> bool:
    path = ROOT / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    new = re.sub(
        r'(?m)^version = "[^"]+"$',
        f'version = "{version}"',
        text,
        count=1,
    )
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def _update_json_version(path: Path, version: str) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") == version:
        return False
    data["version"] = version
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def _update_package_lock(version: str) -> bool:
    path = ROOT / "console-ui" / "package-lock.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    if data.get("version") != version:
        data["version"] = version
        changed = True
    root_pkg = data.get("packages", {}).get("")
    if isinstance(root_pkg, dict) and root_pkg.get("version") != version:
        root_pkg["version"] = version
        changed = True
    if changed:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return changed


def update_versions(version: str) -> list[str]:
    changed: list[str] = []
    if _replace_pyproject_version(version):
        changed.append("pyproject.toml")
    if _update_json_version(ROOT / "console-ui" / "package.json", version):
        changed.append("console-ui/package.json")
    if _update_package_lock(version):
        changed.append("console-ui/package-lock.json")
    return changed


def _tag_exists(tag: str) -> bool:
    return bool(_capture(["git", "tag", "-l", tag], check=True))


def _ensure_no_tag(tag: str) -> None:
    if _tag_exists(tag):
        raise SystemExit(f"tag 已存在: {tag}")


def _has_changes() -> bool:
    return bool(_capture(["git", "status", "--short"], check=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="目标版本号,例如 0.5.3 或 v0.5.3")
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="跳过版本测试;仍会运行版本一致性校验")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="创建 release commit")
    parser.add_argument(
        "--tag",
        action="store_true",
        help="创建 git tag vX.Y.Z")
    parser.add_argument(
        "--push",
        action="store_true",
        help="推送当前分支和 tag;会触发 GitHub release workflow")
    parser.add_argument(
        "--remote",
        default="origin",
        help="推送远端名(默认 origin)")
    parser.add_argument(
        "--branch",
        default=None,
        help="推送分支名;默认当前分支")
    args = parser.parse_args(argv)

    version, tag = _normalize_version(args.version)
    if args.tag or args.push:
        _ensure_no_tag(tag)

    changed = update_versions(version)
    if changed:
        print("已更新版本文件:")
        for path in changed:
            print(f"  - {path}")
    else:
        print("版本文件已是目标版本,无需修改。")

    _run([sys.executable, "scripts/check_release_version.py", "--tag", tag])
    if not args.no_tests:
        _run([sys.executable, "-m", "pytest", "tests/test_version*.py"])

    if args.commit:
        _run([
            "git", "add",
            "pyproject.toml",
            "console-ui/package.json",
            "console-ui/package-lock.json",
        ])
        _run(["git", "commit", "-m", f"Release {tag}"])
    elif changed:
        print("未传 --commit;请检查后手动提交或重新运行并加 --commit。")

    if args.tag:
        if _has_changes():
            raise SystemExit("工作区仍有未提交改动;请先提交后再创建 tag")
        _run(["git", "tag", tag])

    if args.push:
        if not args.tag:
            raise SystemExit("--push 需要同时传 --tag")
        branch = args.branch or _capture(["git", "branch", "--show-current"])
        if not branch:
            raise SystemExit("无法确定当前分支;请用 --branch 指定")
        _run(["git", "push", args.remote, branch])
        _run(["git", "push", args.remote, tag])

    print(f"发布准备完成: {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
