"""README 首屏 SVG 生成器:跑真实演示链,把建议卡渲染成终端风格 SVG。

用法:python deploy/render_hero_svg.py [--db landing/factory.sqlite]
                                       [--out docs/assets/review-card.svg]
产物是可再生的"截图":seed 数据或演示逻辑变更后重跑本脚本即可刷新,
不依赖任何截图工具;GitHub README 原生渲染 SVG。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data2agent.mcp_server.core import QueryService  # noqa: E402
from data2agent.showroom.review_demo import build_review, render_card  # noqa: E402

# GitHub 深色系配色
BG, FRAME, TITLE = "#0d1117", "#30363d", "#8b949e"
TEXT, DIM, GREEN, AMBER, CYAN = "#c9d1d9", "#8b949e", "#3fb950", "#d29922", "#58a6ff"
FONT = ("SF Mono, Menlo, Monaco, 'Cascadia Mono', 'Sarasa Mono SC', "
        "'Noto Sans Mono CJK SC', monospace")
FONT_SIZE, LINE_H, PAD_X, PAD_Y = 13.5, 22, 18, 14
MAX_COLS = 100  # 显示列上限(CJK 记 2 列),超出按标点折行


def _width(s: str) -> int:
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


def _wrap(line: str) -> list[str]:
    if _width(line) <= MAX_COLS:
        return [line]
    out, rest, indent = [], line, "│      "
    while _width(rest) > MAX_COLS:
        cut = None
        acc = 0
        for i, c in enumerate(rest):
            acc += 2 if ord(c) > 0x2E7F else 1
            if acc > MAX_COLS:
                break
            if c in ";,、;":
                cut = i + 1
        cut = cut or i
        out.append(rest[:cut])
        rest = indent + rest[cut:]
    out.append(rest)
    return out


def _color(line: str) -> tuple[str, str]:
    """(颜色, 字重)。规则按内容判定,与 render_card 的行结构对应。"""
    s = line.strip()
    if s.startswith("$"):
        return GREEN, "normal"
    if s.startswith(("┌", "└")):
        return DIM, "normal"
    if "结论:" in s:
        return AMBER, "bold"
    if s.lstrip("│ ").startswith("↳"):
        return CYAN, "normal"
    if s.lstrip("│ ").startswith("-") or "口径警示" in s:
        return DIM if "口径警示" in s else AMBER, "normal"
    if "治理:" in s:
        return DIM, "normal"
    return TEXT, "normal"


def build_svg(card_text: str, command: str) -> str:
    # 逐逻辑行定色,折行的续行继承原行样式(如结论的第二行保持琥珀加粗)
    lines: list[tuple[str, str, str]] = [(f"$ {command}", *_color("$")), ("", TEXT, "normal")]
    for raw in card_text.splitlines():
        color, weight = _color(raw)
        lines.extend((part, color, weight) for part in _wrap(raw))

    width = 1000
    height = PAD_Y * 2 + 40 + LINE_H * len(lines)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        'aria-label="data2agent 接单评审建议卡演示">',
        f'<rect width="{width}" height="{height}" rx="10" fill="{BG}" '
        f'stroke="{FRAME}"/>',
        # 窗口标题栏
        f'<circle cx="24" cy="22" r="6" fill="#ff5f57"/>'
        f'<circle cx="44" cy="22" r="6" fill="#febc2e"/>'
        f'<circle cx="64" cy="22" r="6" fill="#28c840"/>',
        f'<text x="{width / 2}" y="27" text-anchor="middle" fill="{TITLE}" '
        f'font-family="{FONT}" font-size="12">data2agent · 接单评审演示链'
        '(数字可溯源)</text>',
        f'<line x1="0" y1="40" x2="{width}" y2="40" stroke="{FRAME}"/>',
    ]
    y = 40 + PAD_Y + FONT_SIZE
    for line, color, weight in lines:
        if line:
            parts.append(
                f'<text x="{PAD_X}" y="{y:.0f}" xml:space="preserve" '
                f'font-family="{FONT}" font-size="{FONT_SIZE}" '
                f'fill="{color}" font-weight="{weight}">{escape(line)}</text>')
        y += LINE_H
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 README 首屏建议卡 SVG")
    ap.add_argument("--db", default="landing/factory.sqlite")
    ap.add_argument("--templates", default="templates")
    ap.add_argument("--out", default="docs/assets/review-card.svg")
    args = ap.parse_args()

    svc = QueryService(args.db, args.templates)
    card = build_review(svc, "C002", "矶钓竿", 2000, 28.0)
    text = render_card(card, "C002 · 矶钓竿 · 2000 支 · 目标价 28 USD")
    svg = build_svg(text, "python -m data2agent.showroom.review_demo")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
    print(f"已生成 {out}({len(svg)} 字节,{len(text.splitlines())} 行卡片内容)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
