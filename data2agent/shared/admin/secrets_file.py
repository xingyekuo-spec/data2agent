"""Local secrets.env helpers (KEY=VALUE). Loaded into os.environ for the process.

Used so the browser admin UI can set DSN/token without PowerShell scripts.
File mode is user-restrictive when the OS allows.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load_secrets(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if len(val) >= 2 and val[0] == val[-1] == '"':
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                val = val[1:-1]
        elif len(val) >= 2 and val[0] == val[-1] == "'":
            val = val[1:-1]
        else:
            # 兼容旧版 save_secrets 的反斜杠/换行转义。
            val = val.replace("\\n", "\n").replace("\\\\", "\\")
        out[key] = val
    return out


def apply_secrets_to_environ(path: Path) -> dict[str, str]:
    data = load_secrets(path)
    for k, v in data.items():
        os.environ[k] = v
    return data


def restore_environ(prior: dict[str, str | None]) -> None:
    """Restore keys captured before apply_secrets_to_environ (tests / temporary loads)."""
    for key, old in prior.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def load_home_secrets_if_present(home: str | Path | None = None) -> Path | None:
    """Load D2A_HOME/config/secrets.env into process env (browser first-setup).

    Does not create the file. Returns the path if loaded, else None.
    Existing env vars are overwritten so the file is the factory source of truth
    when present (Machine env still works when the file is absent).
    """
    from .home_layout import HomeLayout

    layout = HomeLayout.from_path(home)
    path = layout.secrets_env
    if not path.is_file():
        return None
    apply_secrets_to_environ(path)
    return path


def save_secrets(path: Path, updates: dict[str, str], merge: bool = True) -> None:
    """Write secrets.env. Empty string values delete the key when merging."""
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_secrets(path) if merge and path.is_file() else {}
    for k, v in updates.items():
        if v is None or v == "":
            current.pop(k, None)
        else:
            current[k] = v
    lines = ["# data2agent secrets — do not commit; written by admin UI", ""]
    for k in sorted(current):
        lines.append(f"{k}={json.dumps(current[k], ensure_ascii=False)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if sys.platform == "win32":
        username = os.environ.get("USERNAME", "").strip()
        if username:
            try:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r",
                     f"{username}:(F)", "/grant:r", "SYSTEM:(F)"],
                    capture_output=True, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                pass
