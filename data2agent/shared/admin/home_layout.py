"""Install-home layout under D2A_HOME (default C:\\d2a)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_home() -> Path:
    return Path(os.environ.get("D2A_HOME", r"C:\d2a"))


@dataclass(frozen=True)
class HomeLayout:
    root: Path

    @classmethod
    def from_path(cls, root: str | Path | None = None) -> HomeLayout:
        return cls(Path(root) if root else default_home())

    @property
    def app(self) -> Path:
        return self.root / "app"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def connect_yaml(self) -> Path:
        return self.config_dir / "connect.yaml"

    @property
    def platform_yaml(self) -> Path:
        return self.config_dir / "platform.yaml"

    @property
    def secrets_env(self) -> Path:
        return self.config_dir / "secrets.env"

    def ensure_dirs(self) -> None:
        for p in (self.config_dir, self.data_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)


def resolve_templates(home: HomeLayout) -> Path:
    app_t = home.app / "templates"
    if app_t.is_dir():
        return app_t
    # editable / source tree: repo templates/
    pkg_templates = Path(__file__).resolve().parents[2] / "templates"
    if pkg_templates.is_dir():
        return pkg_templates
    return app_t
