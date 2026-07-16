# 管理 Web 界面实现计划

> **给代理执行者：** 必需技能：优先用 superpowers:subagent-driven-development，或用 superpowers:executing-plans，按本计划逐 Task 实现。步骤用 checkbox（`- [ ]`）跟踪。

**目标：** 按 `docs/superpowers/specs/2026-07-16-admin-web-ui-design.md`，交付中间机（`:8851`）与平台机（扩展 console `:8849`）各自独立的 Jinja2+HTMX 管理界面：白名单 YAML 编辑（写文件 + 提示重启）、状态/日志/调试；不做热加载；浏览器不暴露凭据真实值。

**架构：** 共享 `admin_templates/` + 内嵌 HTMX。新增包 `data2agent.middle_admin`（FastAPI）。扩展 `data2agent.console`：增加 `/api/config|services|logs|debug/*`，`/` 用 Jinja；`ui.py` 保留在 `/v0`。写配置时只合并白名单字段，经 `load_config` 校验，备份 `.yaml.bak`。Token：CLI `--token` 优先于环境变量（`D2A_MIDDLE_ADMIN_TOKEN` / `D2A_CONSOLE_TOKEN`）；无 Token 且绑定非本机环回时打印警告。

**技术栈：** Python 3.14 / FastAPI / uvicorn / Jinja2 / HTMX（vendored）/ pytest + TestClient / PyYAML。

**设计文档：** `docs/superpowers/specs/2026-07-16-admin-web-ui-design.md`

---

## 文件清单

| 路径 | 职责 |
| --- | --- |
| `pyproject.toml` | `console` 增加 `jinja2`；新增 `middle_admin` extra；`package-data` 打包模板/静态资源 |
| `data2agent/admin_templates/layout.html` | 共享页框（导航 + 内容区） |
| `data2agent/admin_templates/form_macros.html` | 表单字段宏 |
| `data2agent/admin_templates/static/htmx.min.js` | 内嵌 HTMX（两应用挂载此目录或各拷一份） |
| `data2agent/admin_common/config_edit.py` | 白名单合并、备份、校验（共享） |
| `data2agent/admin_common/logs.py` | 读文件尾 N 行 + 可选级别关键词过滤 |
| `data2agent/admin_common/auth_token.py` | 从 CLI/环境解析 Token；绑定告警 |
| `data2agent/middle_admin/__init__.py` | 包标记 |
| `data2agent/middle_admin/__main__.py` | CLI 入口 |
| `data2agent/middle_admin/app.py` | FastAPI 路由 |
| `data2agent/middle_admin/status.py` | 由 YAML + 时钟推算下次同步/窗口内外；读 sqlite |
| `data2agent/middle_admin/templates/*.html` | status / config / logs 页面 |
| `data2agent/console/app.py` | 增加 config/services/logs/debug API；`/` → Jinja；`/v0` → UI_HTML |
| `data2agent/console/__main__.py` | 增加 `--log-dir` |
| `data2agent/console/templates/*.html` | dashboard / config / logs / debug |
| `tests/test_admin_common.py` | 白名单合并 + 备份 |
| `tests/test_middle_admin.py` | 中间机 API |
| `tests/test_console.py` | 扩展新 API + `/v0` 仍可用 |
| `deploy/setup-middle.ps1` / `setup-platform.ps1` | 管理端口/Token/日志提示 |
| `docs/runbook/install-middle.md` / `install-platform.md` | 安装 extras + 管理界面验收 |
| `.github/workflows/release.yml` | 离线包打入 `jinja2`/`markupsafe` |

---

### Task 1：Extras + package-data + 共享辅助模块

**文件：**
- 修改：`pyproject.toml`
- 新建：`data2agent/admin_common/__init__.py`
- 新建：`data2agent/admin_common/config_edit.py`
- 新建：`data2agent/admin_common/logs.py`
- 新建：`data2agent/admin_common/auth_token.py`
- 新建：`tests/test_admin_common.py`

- [ ] **步骤 1：更新 `pyproject.toml`**

```toml
# in [project.optional-dependencies]
console = ["fastapi>=0.110", "uvicorn>=0.29", "jinja2>=3.0"]
middle_admin = ["fastapi>=0.110", "uvicorn>=0.29", "jinja2>=3.0"]

# add:
[tool.setuptools.package-data]
data2agent = [
  "admin_templates/*.html",
  "admin_templates/static/*.js",
  "middle_admin/templates/*.html",
  "middle_admin/static/*.js",
  "console/templates/*.html",
  "console/static/*.js",
]
```

- [ ] **步骤 2：先写会失败的白名单合并测试**

```python
# tests/test_admin_common.py
from pathlib import Path
import yaml
from data2agent.admin_common.config_edit import (
    MIDDLE_EDITABLE,
    merge_whitelist_and_save,
)

def test_merge_whitelist_preserves_secrets_and_backs_up(tmp_path):
    p = tmp_path / "connect.yaml"
    p.write_text(
        "templates: t\nlanding: L\nsources:\n  digiwin_e10:\n"
        "    adapter: mssql_readonly\n    dsn_env: D2A_E10_DSN\n"
        "    sync_every: 30m\n    sink: {type: http, url: http://a:8850, token_env: D2A_INGEST_TOKEN}\n",
        encoding="utf-8",
    )
    ok, errors = merge_whitelist_and_save(
        p, MIDDLE_EDITABLE,
        {"sources": {"digiwin_e10": {"sync_every": "15m", "dsn_env": "HACKED"}}},
        validate=None,  # 本单元跳过 load_config；后续 Task 接真实校验
    )
    assert ok and not errors
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["sources"]["digiwin_e10"]["sync_every"] == "15m"
    assert data["sources"]["digiwin_e10"]["dsn_env"] == "D2A_E10_DSN"
    assert list(tmp_path.glob("connect.yaml.bak*")) or (tmp_path / "connect.yaml.bak").exists()
```

- [ ] **步骤 3：跑测试 — 预期失败（模块尚不存在）**

```bash
pytest tests/test_admin_common.py::test_merge_whitelist_preserves_secrets_and_backs_up -v
```

预期：`ImportError` 或收集失败。

- [ ] **步骤 4：实现辅助模块**

```python
# data2agent/admin_common/config_edit.py
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable
import shutil
from datetime import datetime
import yaml

MIDDLE_EDITABLE = {
    "templates", "landing",
    "sources.*.windows", "sources.*.rate.batch_size", "sources.*.rate.rows_per_second",
    "sources.*.lookback", "sources.*.sync_every", "sources.*.extra_whitelist",
    "sources.*.sink.url",
}
PLATFORM_EDITABLE = {"templates", "landing"}

def _set_path(root: dict, dotted: str, value: Any) -> None:
    # 仅处理白名单路径；忽略对 dsn_env 等的写入企图
    ...

def merge_whitelist_and_save(
    path: Path,
    editable: set[str],
    patch: dict,
    validate: Callable[[Path], None] | None,
) -> tuple[bool, list[dict]]:
    """只合并可编辑字段；备份；可选 validate(path)。
    校验失败则从 bak 恢复，返回 (False, errors)。
    """
    ...
```

```python
# data2agent/admin_common/logs.py
from pathlib import Path

def tail_lines(path: Path, lines: int = 200, level: str | None = None) -> tuple[bool, str]:
    """返回 (ok, text)。缺失/不可读时 ok=False 并带说明。lines 上限 1000。"""
    ...
```

```python
# data2agent/admin_common/auth_token.py
import os

def resolve_token(cli_token: str | None, env_name: str) -> str | None:
    """CLI 非空优先；否则 os.environ.get(env_name)；空则 None。"""
    if cli_token:
        return cli_token
    v = os.environ.get(env_name, "").strip()
    return v or None
```

- [ ] **步骤 5：再跑测试 — 预期通过**

```bash
pytest tests/test_admin_common.py -q
```

- [ ] **步骤 6：提交**

```bash
git add pyproject.toml data2agent/admin_common tests/test_admin_common.py
git commit -m "feat(admin): shared config edit helpers and middle_admin/console extras"
```

---

### Task 2：内嵌 HTMX + 共享布局骨架

**文件：**
- 新建：`data2agent/admin_templates/layout.html`
- 新建：`data2agent/admin_templates/form_macros.html`
- 新建：`data2agent/admin_templates/static/htmx.min.js`（从官方发布下载 HTMX 2.x min；运行时禁止 CDN）

- [ ] **步骤 1：把 HTMX 下载进包内**

```bash
# 在仓库根目录；固定版本（如 2.0.4）
curl -fsSL -o data2agent/admin_templates/static/htmx.min.js \
  https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
test -s data2agent/admin_templates/static/htmx.min.js
```

- [ ] **步骤 2：写最小 layout**

`layout.html` 必须包含：
- `{% block title %}` / `{% block nav %}` / `{% block content %}`
- `<script src="{{ static_url }}/htmx.min.js"></script>`
- Token 引导脚本：若已有 `sessionStorage.d2a_token`，则 `document.body.addEventListener('htmx:configRequest', (e) => { e.detail.headers['Authorization'] = 'Bearer ' + sessionStorage.d2a_token })`
- 当 `needs_token` 为 true 时显示简单登录面板（表单写入 `sessionStorage` 后刷新）

- [ ] **步骤 3：提交**

```bash
git add data2agent/admin_templates
git commit -m "feat(admin): shared Jinja layout and vendored HTMX"
```

---

### Task 3：中间机管理 — config + status API

**文件：**
- 新建：`data2agent/middle_admin/__init__.py`
- 新建：`data2agent/middle_admin/__main__.py`
- 新建：`data2agent/middle_admin/app.py`
- 新建：`data2agent/middle_admin/status.py`
- 新建：`tests/test_middle_admin.py`

- [ ] **步骤 1：先写会失败的 API 测试**

```python
# tests/test_middle_admin.py
from pathlib import Path
import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from data2agent.middle_admin.app import create_app

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture()
def middle_env(tmp_path):
    landing = tmp_path / "middle.sqlite"
    # 用 LandingStore 建最小落地库
    from data2agent.connect.landing import LandingStore
    LandingStore(landing)
    cfg = tmp_path / "connect.yaml"
    cfg.write_text(
        f"templates: {ROOT / 'templates'}\nlanding: {landing}\n"
        "sources:\n  digiwin_e10:\n    adapter: sqlite_readonly\n"
        f"    path: {tmp_path / 'missing.sqlite'}\n"
        "    sync_every: 30m\n"
        "    sink: {type: http, url: http://127.0.0.1:8850, token_env: D2A_INGEST_TOKEN}\n",
        encoding="utf-8",
    )
    # 若 load_config 检查 path，建空 sqlite 文件
    (tmp_path / "missing.sqlite").write_bytes(b"")
    # 实现前建议改成与 test_console 类似的真实小 sqlite + 合法配置
    app = create_app(config_path=cfg, token="secret", log_path=tmp_path / "c.log")
    return TestClient(app), cfg

def test_config_get_requires_token(middle_env):
    client, _ = middle_env
    assert client.get("/api/config").status_code == 401
    r = client.get("/api/config", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    body = r.json()
    assert "sync_every" in str(body)

def test_config_post_whitelist_and_validate(middle_env):
    client, cfg = middle_env
    h = {"Authorization": "Bearer secret"}
    r = client.post("/api/config", headers=h, json={
        "sources": {"digiwin_e10": {"sync_every": "15m", "dsn_env": "NOPE"}}
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    text = cfg.read_text(encoding="utf-8")
    assert "15m" in text and "NOPE" not in text
```

fixture 请参照 `tests/test_console.py` 的 `env`，保证 `load_config` 能通过（sqlite 源库 + templates 路径）。实现前按此调整步骤 1 的 fixture。

- [ ] **步骤 2：跑测试 — 预期失败**

```bash
pytest tests/test_middle_admin.py -v
```

- [ ] **步骤 3：实现 `status.py` + `app.py` + `__main__.py`**

`status.py`：
- 每个源：用 `data2agent.connect.config.in_window(now, scfg.windows)`
- 下次同步：窗外 → 下一窗口起点（解析 `HH:MM-HH:MM`）；窗内 → `last_run_at + sync_every`，从未跑过则为 `now`（读落地库 `d2a_sync_state` / `d2a_sync_run`）
- API 响应注明：`"schedule_source": "derived_from_yaml"`（不是存活的 APScheduler）

`app.py` 的 `create_app(config_path, token, log_path)`：
- Auth Depends 对齐 console
- `GET /api/config` → YAML 子集 + `dsn_env` / `token_env` 对应环境变量是否已设置（`os.environ.get(name) is not None`）
- `POST /api/config` → `merge_whitelist_and_save`，`validate=lambda p: load_config(p)`
- `POST /api/config/validate` → 只校验不写
- `GET /api/status` → status 辅助函数
- Mount `admin_templates/static` 的 StaticFiles
- HTML 路由可先返回 200 占位文本，到 Task 5 再换 Jinja（或先挂最小模板）

`__main__.py`：
```python
ap.add_argument("--config", required=True)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=8851)
ap.add_argument("--token", default=None)  # resolve_token(..., "D2A_MIDDLE_ADMIN_TOKEN")
ap.add_argument("--log-path", default=r"C:\d2a\data\logs\d2a-connector.log")
# if not token and host not in (127.0.0.1, ::1, localhost): print warning
```

- [ ] **步骤 4：测试通过**

```bash
pytest tests/test_middle_admin.py tests/test_admin_common.py -q
```

- [ ] **步骤 5：提交**

```bash
git add data2agent/middle_admin tests/test_middle_admin.py
git commit -m "feat(middle_admin): config and status JSON APIs"
```

---

### Task 4：中间机管理 — 连接测试、日志、触发同步

**文件：**
- 修改：`data2agent/middle_admin/app.py`
- 修改：`tests/test_middle_admin.py`

- [ ] **步骤 1：补充测试**

```python
def test_logs_missing_file(middle_env, tmp_path):
    client, _ = middle_env
    r = client.get("/api/logs?lines=50", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200
    assert r.json()["ok"] is False  # 日志文件不存在

def test_trigger_sync_warns_or_runs(middle_env):
    client, _ = middle_env
    r = client.post("/api/actions/trigger", headers={"Authorization": "Bearer secret"},
                    json={"action": "sync"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("action") == "sync"
    assert "overlap_warning" in body  # 设计：无法探测 NSSM 时始终警告
```

- [ ] **步骤 2：实现**

- `GET /api/logs`：`tail_lines(log_path, lines, level)`；上限 1000
- `POST /api/test-connection`：按已加载配置构建 adapter（与 connect 相同），对白名单做探测或 adapter ping；超时 10s（`concurrent.futures` 等）；**永不返回 DSN 字符串**；sqlite_readonly 则打开 path
- `POST /api/actions/trigger` 仅接受 `action=="sync"`：进程内调用 `run_sync_cycle(cfg, source)`；v1 始终设 `overlap_warning=True`（或后续再做锁文件探测）；`reconcile` 返回 400

- [ ] **步骤 3：pytest 通过并提交**

```bash
pytest tests/test_middle_admin.py -q
git add data2agent/middle_admin tests/test_middle_admin.py
git commit -m "feat(middle_admin): logs, ERP connection test, sync trigger"
```

---

### Task 5：中间机管理 — Jinja 页面

**文件：**
- 新建：`data2agent/middle_admin/templates/layout.html`（经多路径 FileSystemLoader 继承 `admin_templates/layout.html`）
- 新建：`data2agent/middle_admin/templates/status.html`
- 新建：`data2agent/middle_admin/templates/config.html`
- 新建：`data2agent/middle_admin/templates/logs.html`
- 修改：`data2agent/middle_admin/app.py`（TemplateResponse）
- 修改：`tests/test_middle_admin.py`（页面 GET 冒烟 200）

- [ ] **步骤 1：配置双搜索路径的 Jinja2Templates**

```python
from fastapi.templating import Jinja2Templates
from importlib import resources
# searchpath = [middle_admin/templates, admin_templates 所在目录]
```

- [ ] **步骤 2：页面**

- `/` → 重定向 `/status`
- `/status` → HTMX 每 5s 请求 `GET /api/status` 填入区块；Trigger 按钮旁展示 `overlap_warning` 说明
- `/config` → 白名单字段表单；HTMX POST `/api/config`；成功后显示重启横幅：`nssm restart d2a-connector` / 服务管理器
- `/logs` → HTMX 加载 `/api/logs?lines=200`

- [ ] **步骤 3：冒烟测试**

```python
def test_html_pages(middle_env):
    client, _ = middle_env
    h = {"Authorization": "Bearer secret"}
    for path in ("/status", "/config", "/logs"):
        r = client.get(path, headers=h)
        assert r.status_code == 200
        assert b"htmx" in r.content.lower() or b"HX-" in r.content or "nav" in r.text.lower()
```

- [ ] **步骤 4：提交**

```bash
git commit -am "feat(middle_admin): Jinja status/config/logs pages"
```

---

### Task 6：平台 console — config / services / logs / debug API

**文件：**
- 修改：`data2agent/console/app.py`
- 修改：`data2agent/console/__main__.py`（`--log-dir`；文档默认 `C:\d2a\data\logs`；测试可用 `landing` 同级 `logs`）
- 修改：`tests/test_console.py`

- [ ] **步骤 1：新接口的失败测试**

```python
def test_console_config_whitelist(env):
    landing, cfg_file = env
    from data2agent.connect.config import load_config
    cfg = load_config(cfg_file)
    client = TestClient(create_app(cfg.landing, cfg.templates, cfg, token="t",
                                   config_path=cfg_file, log_dir=Path(".")))
    h = {"Authorization": "Bearer t"}
    r = client.get("/api/config", headers=h)
    assert r.status_code == 200
    r2 = client.post("/api/config", headers=h, json={"landing": str(landing.db_path), "templates": str(ROOT / "templates")})
    assert r2.json()["ok"] is True

def test_v0_still_embedded(env):
    landing, _ = env
    client = TestClient(create_app(landing.db_path, ROOT / "templates"))
    assert client.get("/v0").status_code == 200
    assert "运维控制台" in client.get("/v0").text
```

扩展签名：`create_app(..., config_path: Path | None = None, log_dir: Path | None = None)`。

- [ ] **步骤 2：实现 API**

- `GET/POST /api/config`：用 `PLATFORM_EDITABLE`；需要 `config_path`
- `GET /api/services`：HTTP 探测 `http://127.0.0.1:8850/ingest/health` 与 `:8848`（mcp 有健康接口则用，否则 TCP）；apply = 检查 `log_dir / d2a-apply.log` 的 mtime 是否在 N 分钟内，或进程名含 `data2agent.connect`（尽力而为；在 JSON 里写 `method`）
- `GET /api/logs?service=ingest|apply|mcp|console`
- `GET /api/debug/raw-table?table=&offset=&limit=` — 仅 `raw_%` 表；只读 SQL
- `POST /api/debug/mcp-call` — 白名单仅 `query_objects`、`query_metrics`；优先进程内调 mcp 辅助函数，否则带 `D2A_MCP_TOKEN` HTTP 调本机 mcp

- [ ] **步骤 3：无真实 ERP 时隐藏误导动作**

仪表盘 JSON/HTML：若源 adapter 为 mssql 且 `dsn_env` 未设置或名为 `D2A_E10_DSN_PLACEHOLDER`，则 `actions_sync_reconcile=false`，隐藏 sync/reconcile（apply/retry 保留）。

- [ ] **步骤 4：pytest + 提交**

```bash
pytest tests/test_console.py tests/test_middle_admin.py -q
git commit -am "feat(console): admin config/services/logs/debug APIs; keep /v0"
```

---

### Task 7：平台 console — Jinja 页面替换 `/`

**文件：**
- 新建：`data2agent/console/templates/{layout,dashboard,config,logs,debug}.html`
- 修改：`data2agent/console/app.py` — `/` → dashboard TemplateResponse；增加 `/config` `/logs` `/debug`
- 修改：`tests/test_console.py` — `GET /` 返回新壳（overview 可由 HTMX 拉）；`GET /v0` 不变

- [ ] **步骤 1：实现模板**（复用宏；dashboard 用 HTMX 调已有 `/api/overview` 等）

- [ ] **步骤 2：冒烟 + 全量相关套件**

```bash
pytest tests/test_console.py tests/test_middle_admin.py tests/test_admin_common.py -q
```

- [ ] **步骤 3：提交**

```bash
git commit -am "feat(console): Jinja admin pages for dashboard/config/logs/debug"
```

---

### Task 8：部署脚本、runbook、离线 wheel

**文件：**
- 修改：`deploy/setup-middle.ps1`
- 修改：`deploy/setup-platform.ps1`
- 修改：`docs/runbook/install-middle.md`
- 修改：`docs/runbook/install-platform.md`
- 修改：`docs/runbook/windows-deploy.md`（简要入口）
- 修改：`.github/workflows/release.yml`

- [ ] **步骤 1：中间机安装**

- pip：`.[connect,middle_admin]`
- 环境变量：`D2A_MIDDLE_ADMIN_TOKEN`（机器级）
- NSSM AppParameters 示例（Token 从进程环境读，**不要**在参数里写 `%VAR%`）：

```
-m data2agent.middle_admin --config C:\d2a\config\connect.yaml --host 0.0.0.0 --port 8851 --log-path C:\d2a\data\logs\d2a-connector.log
```

文档写明：服务进程继承机器级环境变量中的 Token。

- [ ] **步骤 2：平台机**

- 说明 console 地址：`http://<ip>:8849`（Token 登录后）
- `d2a-console` NSSM 可选加 `--log-dir C:\d2a\data\logs`

- [ ] **步骤 3：release.yml**

在 connect/platform 的 `pip download` 列表中加入 `jinja2`、`markupsafe`（平台经 console 会带上；中间机 connect 包要装 `middle_admin` 时必须打进）。

优先把中间机离线包依赖对齐为 `.[connect,middle_admin]` 等价 wheel 集合。

- [ ] **步骤 4：提交**

```bash
git commit -am "docs(deploy): middle_admin service and jinja2 offline wheels"
```

---

### Task 9：手工冒烟清单（无代码）

- [ ] **步骤 1：开发机**

```bash
pip install -e ".[dev,connect,middle_admin,console,ingest,mcp]"
# 准备合法 connect.yaml（showroom 或现场拷贝）
python -m data2agent.middle_admin --config <cfg> --port 8851 --token dev
# 浏览器: http://127.0.0.1:8851 — 登录、改 sync_every、看到重启横幅
python -m data2agent.console --config <platform-or-connect.yaml> --port 8849 --token dev
# 浏览器: http://127.0.0.1:8849/ 与 /v0
pytest tests -q
```

- [ ] **步骤 2：仅在工厂打包时再 bump 版本并打 tag**（如 `0.1.7`）— 冒烟通过后单独做发布提交。

---

## 设计覆盖对照

| 设计要求 | Task |
| --- | --- |
| 中间机管理 :8851 独立 | 3–5 |
| 平台扩展 console | 6–7 |
| 写 YAML + 提示重启，无热加载 | 1, 3, 5, 6 |
| 凭据白名单 / 环境变量已设置标志 | 1, 3, 6 |
| 中间机推送模式无对账 | 4 |
| 状态由 YAML+时钟推算 | 3 |
| 日志 `--log-path` / `--log-dir` | 3, 4, 6, 8 |
| Token sessionStorage + Bearer | 2, 5 |
| MCP 调试白名单 | 6 |
| 服务健康（apply 非 HTTP） | 6 |
| `ui.py` 在 `/v0` | 6–7 |
| Jinja2 extra + package-data | 1 |
| 部署与离线包 jinja2 | 8 |
| 中间机可编辑 `templates`/`landing` | 1, 3 |
| 触发同步重叠警告 | 4–5 |

---

## 本计划不做（范围外）

- 热加载 / SIGHUP
- 一键 NSSM 重启
- Vue `console-ui/`
- 经 YAML 编辑 `apply --every`
- 平台 UI 编辑 `reconcile_at`
