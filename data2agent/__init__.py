"""data2agent — Data to Agent, for factories.

工厂内网只读抽取中间机:ERP 连接器 + 水位增量 / 对账 + 推送到任意数据平台(webhook / ingest v3 / AI Hub)。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _version_from_metadata() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            return str(data["project"]["version"])
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
            pass
    try:
        return version("data2agent")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _version_from_metadata()
