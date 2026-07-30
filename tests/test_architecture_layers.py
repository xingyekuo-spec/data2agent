"""架构分层契约:middle / platform / shared / protocol 依赖方向必须保持。

实现见 scripts/check_architecture_layers.py;此处固化为测试,防止重构后
两端重新互相渗透(例如中间端再 import 平台端模块)。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_architecture_layers import check_layers  # noqa: E402


@pytest.mark.contract
def test_architecture_layer_dependencies():
    violations = check_layers()
    assert not violations, "架构分层违规:\n" + "\n".join(violations)
