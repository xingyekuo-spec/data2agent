"""架构分层检查:强制 middle / platform / shared / protocol 的依赖方向。

分层规则(见 docs/design/00-overview.md):

- ``data2agent.middle`` 不得依赖 ``data2agent.platform``;
- ``data2agent.platform`` 不得依赖 ``data2agent.middle``;
- ``data2agent.protocol`` 与 ``data2agent.shared`` 不得依赖任何端目录
  (``data2agent.middle`` / ``data2agent.platform``),它们是两端共享的底座;
- 允许的方向:middle → shared → protocol,platform → shared → protocol。

用法:python scripts/check_architecture_layers.py(无输出退出码 0 表示通过)。
CI 由 tests/test_architecture_layers.py 调用同一实现。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "data2agent"

MIDDLE = "data2agent.middle"
PLATFORM = "data2agent.platform"
SHARED = "data2agent.shared"
PROTOCOL = "data2agent.protocol"

# (源顶层目录, 禁止依赖的目标前缀)
FORBIDDEN: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("middle", (PLATFORM,)),
    ("platform", (MIDDLE,)),
    ("shared", (MIDDLE, PLATFORM)),
    ("protocol", (MIDDLE, PLATFORM, SHARED)),
)


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


def _imported_modules(tree: ast.AST, current: str) -> set[str]:
    """收集文件中出现的 data2agent 内部模块(相对 import 解析为绝对)。"""
    found: set[str] = set()
    pkg_parts = current.split(".")[:-1]  # 当前模块所在包
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            found.add(module)
            # from X import Y:Y 可能是子模块
            for alias in node.names:
                found.add(f"{module}.{alias.name}")
    return found


def check_layers(root: Path = ROOT) -> list[str]:
    violations: list[str] = []
    for side, forbidden in FORBIDDEN:
        side_dir = PKG / side
        if not side_dir.is_dir():
            continue
        for path in sorted(side_dir.rglob("*.py")):
            current = _module_name(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for imported in _imported_modules(tree, current):
                for target in forbidden:
                    if imported == target or imported.startswith(target + "."):
                        violations.append(
                            f"{path.relative_to(root)}: "
                            f"data2agent.{side} 不得依赖 {target}"
                            f"(发现 {imported})"
                        )
    return violations


def main() -> int:
    violations = check_layers()
    if violations:
        print("架构分层违规:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print("架构分层检查通过:middle / platform / shared / protocol 依赖方向正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
