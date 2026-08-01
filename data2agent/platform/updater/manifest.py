"""更新清单(latest.json)获取、校验与版本比较。

清单由 release 流程生成并随 GitHub Release 附件发布,字段:

```json
{
  "version": "v0.6.0",
  "package": "d2a-portable-platform-v0.6.0.zip",
  "url": "https://github.com/<org>/<repo>/releases/download/v0.6.0/d2a-portable-platform-v0.6.0.zip",
  "sha256": "...",
  "supported_ingest_protocol_versions": ["2", "3"],
  "notes": "可选,发布说明摘要"
}
```
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = "data2agent-updater"


class UpdateError(RuntimeError):
    """更新流程中的可预期失败(信息面向现场操作员,用中文)。"""


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    package: str
    url: str
    sha256: str
    supported_ingest_protocol_versions: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UpdateManifest:
        if not isinstance(data, dict):
            raise UpdateError("更新清单格式错误:不是 JSON 对象")
        missing = [k for k in ("version", "package", "url", "sha256")
                   if not (isinstance(data.get(k), str) and data[k].strip())]
        if missing:
            raise UpdateError(f"更新清单缺少字段:{', '.join(missing)}")
        sha = data["sha256"].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise UpdateError("更新清单 sha256 格式错误")
        supported = data.get("supported_ingest_protocol_versions") or []
        if not isinstance(supported, list) or not all(isinstance(v, str) for v in supported):
            raise UpdateError("更新清单 supported_ingest_protocol_versions 格式错误")
        notes = data.get("notes")
        return cls(
            version=data["version"].strip(),
            package=data["package"].strip(),
            url=data["url"].strip(),
            sha256=sha,
            supported_ingest_protocol_versions=tuple(v.strip() for v in supported),
            notes=notes.strip() if isinstance(notes, str) else "",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "package": self.package,
            "url": self.url,
            "sha256": self.sha256,
            "supported_ingest_protocol_versions": list(self.supported_ingest_protocol_versions),
            "notes": self.notes,
        }


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.6.0' / '0.6.0-beta' → (0, 6, 0);无法解析返回 ()。"""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", text or "")
    if not m:
        return ()
    return tuple(int(part) for part in m.group(1).split("."))


def is_newer_version(latest: str, current: str) -> bool:
    """latest 是否比 current 新;任一无法解析时,只要不同即视为可更新。"""
    new, old = parse_version(latest), parse_version(current)
    if not new or not old:
        return latest.strip() != current.strip()
    width = max(len(new), len(old))
    new += (0,) * (width - len(new))
    old += (0,) * (width - len(old))
    return new > old


def http_get(url: str, *, token: str | None = None, timeout: float = 15.0):
    """GET(可选 Bearer token;file:// 跳过鉴权头),返回 response 对象。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if token and not url.startswith("file:"):
        request.add_header("Authorization", f"Bearer {token}")
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except UpdateError:
        raise
    except Exception as exc:  # URLError / OSError / ValueError
        raise UpdateError(f"下载失败:{exc}") from exc


def fetch_manifest(url: str, *, token: str | None = None,
                   timeout: float = 15.0) -> UpdateManifest:
    """从更新源拉取 latest.json 并校验。"""
    with http_get(url, token=token, timeout=timeout) as resp:
        try:
            payload = resp.read()
        except Exception as exc:
            raise UpdateError(f"读取更新清单失败:{exc}") from exc
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError(f"更新清单不是合法 JSON:{exc}") from exc
    return UpdateManifest.from_dict(data)
