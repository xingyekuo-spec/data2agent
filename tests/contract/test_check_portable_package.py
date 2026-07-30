"""便携包检查脚本的本地可跑冒烟(无需真实 Windows zip)。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.check_portable_package import main


ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_check_portable_middle_ok(tmp_path: Path, monkeypatch):
    portable = tmp_path / "portable"
    expected = tmp_path / "templates"
    _write(expected / "metrics" / "dead_stock.yaml")
    _write(expected / "objects" / "dead_stock_item.yaml")
    _write(expected / "objects" / "dead_stock_attribution.yaml")
    shutil.copytree(expected, portable / "app" / "templates")
    _write(portable / "BUILD-INFO.json", json.dumps({
        "application_version": "0.5.0-test",
        "release_version": "0.5.0-test",
        "role": "middle",
        "send_ingest_protocol_version": "2",
        "commit": "test",
    }))

    pkg = portable / "runtime" / "Lib" / "site-packages" / "data2agent" / "middle" / "admin"
    src = ROOT / "data2agent" / "middle" / "admin" / "templates"
    shutil.copytree(src, pkg / "templates")
    _write(pkg / "__init__.py", "")

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_portable_package.py",
            "--portable", str(portable),
            "--role", "middle",
            "--expected-templates", str(expected),
        ],
    )
    assert main() == 0


def test_check_portable_middle_rejects_missing_metadata(tmp_path: Path, monkeypatch):
    portable = tmp_path / "portable"
    expected = tmp_path / "templates"
    _write(expected / "metrics" / "dead_stock.yaml")
    _write(expected / "objects" / "dead_stock_item.yaml")
    _write(expected / "objects" / "dead_stock_attribution.yaml")
    shutil.copytree(expected, portable / "app" / "templates")
    _write(portable / "BUILD-INFO.json", json.dumps({
        "application_version": "0.5.0-test",
        "release_version": "0.5.0-test",
        "role": "middle",
        "send_ingest_protocol_version": "2",
        "commit": "test",
    }))
    pkg = portable / "runtime" / "Lib" / "site-packages" / "data2agent" / "middle" / "admin"
    src = ROOT / "data2agent" / "middle" / "admin" / "templates"
    shutil.copytree(src, pkg / "templates")
    (pkg / "templates" / "metadata.html").unlink()
    _write(pkg / "__init__.py", "")

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_portable_package.py",
            "--portable", str(portable),
            "--role", "middle",
            "--expected-templates", str(expected),
        ],
    )
    with pytest.raises(SystemExit, match="metadata.html"):
        main()


def test_check_portable_rejects_erp_configs_anywhere(tmp_path: Path, monkeypatch):
    """旧 ERP 清单即使在 app/ 而非 middle_admin 下也应失败。"""
    portable = tmp_path / "portable"
    expected = tmp_path / "templates"
    _write(expected / "metrics" / "dead_stock.yaml")
    _write(expected / "objects" / "dead_stock_item.yaml")
    _write(expected / "objects" / "dead_stock_attribution.yaml")
    shutil.copytree(expected, portable / "app" / "templates")
    _write(portable / "BUILD-INFO.json", json.dumps({
        "application_version": "0.5.0-test",
        "release_version": "0.5.0-test",
        "role": "middle",
        "send_ingest_protocol_version": "2",
        "commit": "test",
    }))
    pkg = portable / "runtime" / "Lib" / "site-packages" / "data2agent" / "middle" / "admin"
    src = ROOT / "data2agent" / "middle" / "admin" / "templates"
    shutil.copytree(src, pkg / "templates")
    _write(pkg / "__init__.py", "")
    _write(portable / "app" / "erp-configs" / "e10.yaml", "tables: [CUSTOMER]\n")

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_portable_package.py",
            "--portable", str(portable),
            "--role", "middle",
            "--expected-templates", str(expected),
        ],
    )
    with pytest.raises(SystemExit, match="erp-configs"):
        main()


def test_check_portable_rejects_showroom_anywhere(tmp_path: Path, monkeypatch):
    """产品展厅路径不得出现在便携包任意位置。"""
    portable = tmp_path / "portable"
    expected = tmp_path / "templates"
    _write(expected / "metrics" / "dead_stock.yaml")
    _write(expected / "objects" / "dead_stock_item.yaml")
    _write(expected / "objects" / "dead_stock_attribution.yaml")
    shutil.copytree(expected, portable / "app" / "templates")
    _write(portable / "BUILD-INFO.json", json.dumps({
        "application_version": "0.5.0-test",
        "release_version": "0.5.0-test",
        "role": "middle",
        "send_ingest_protocol_version": "2",
        "commit": "test",
    }))
    pkg = portable / "runtime" / "Lib" / "site-packages" / "data2agent" / "middle" / "admin"
    src = ROOT / "data2agent" / "middle" / "admin" / "templates"
    shutil.copytree(src, pkg / "templates")
    _write(pkg / "__init__.py", "")
    _write(
        portable / "runtime" / "Lib" / "site-packages" / "data2agent" / "showroom" / "seed.py",
        "print('demo')\n",
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_portable_package.py",
            "--portable", str(portable),
            "--role", "middle",
            "--expected-templates", str(expected),
        ],
    )
    with pytest.raises(SystemExit, match="showroom"):
        main()


def test_check_portable_middle_rejects_supported_list(tmp_path: Path, monkeypatch):
    portable = tmp_path / "portable"
    expected = tmp_path / "templates"
    _write(expected / "metrics" / "dead_stock.yaml")
    _write(expected / "objects" / "dead_stock_item.yaml")
    _write(expected / "objects" / "dead_stock_attribution.yaml")
    shutil.copytree(expected, portable / "app" / "templates")
    _write(
        portable / "BUILD-INFO.json",
        json.dumps(
            {
                "application_version": "0.5.0-test",
                "release_version": "0.5.0-test",
                "role": "middle",
                "send_ingest_protocol_version": "2",
                "supported_ingest_protocol_versions": ["2", "3"],
                "commit": "test",
            }
        ),
    )
    pkg = portable / "runtime" / "Lib" / "site-packages" / "data2agent" / "middle" / "admin"
    src = ROOT / "data2agent" / "middle" / "admin" / "templates"
    shutil.copytree(src, pkg / "templates")
    _write(pkg / "__init__.py", "")
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_portable_package.py",
            "--portable", str(portable),
            "--role", "middle",
            "--expected-templates", str(expected),
        ],
    )
    with pytest.raises(SystemExit, match="must not declare supported"):
        main()


def test_check_portable_platform_requires_legacy_health_protocol(tmp_path: Path, monkeypatch):
    portable = tmp_path / "portable"
    expected = tmp_path / "templates"
    _write(expected / "metrics" / "dead_stock.yaml")
    _write(expected / "objects" / "dead_stock_item.yaml")
    _write(expected / "objects" / "dead_stock_attribution.yaml")
    shutil.copytree(expected, portable / "app" / "templates")
    _write(portable / "app" / "console-ui" / "dist" / "index.html", "<html></html>")
    from data2agent.platform.updater.apply_script import UPDATE_BAT
    _write(portable / "升级.bat", UPDATE_BAT)
    _write(
        portable / "runtime" / "Lib" / "site-packages" / "data2agent" / "platform" / "console" / "app.py",
        'app.mount("/assets", StaticFiles(directory=assets_dir), name="vue-assets")\n'
        "def legacy_v1_index():\n    pass\n",
    )
    _write(
        portable / "BUILD-INFO.json",
        json.dumps(
            {
                "application_version": "0.5.0-test",
                "release_version": "0.5.0-test",
                "role": "platform",
                "active_ingest_protocol_version": "3",
                "legacy_health_ingest_protocol_version": "2",
                "supported_ingest_protocol_versions": ["2", "3"],
                "commit": "test",
            }
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_portable_package.py",
            "--portable", str(portable),
            "--role", "platform",
            "--expected-templates", str(expected),
        ],
    )
    assert main() == 0
