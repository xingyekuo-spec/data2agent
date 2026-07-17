# 05 · 运维控制台与管理界面

> 状态:平台管理界面已实现(2026-07-16)· 实现:`data2agent/console/`(+ 中间机 `middle_admin/`)  
> 入口:现场推荐 `--home`(浏览器首次配置);展厅 / 开发可用 `--config` / `--landing`

## 1. 定位

两类界面共存,场景不同:

| | 管理界面(当前现场主路径) | 运维仪表盘 v0 / Vue v1(远期) |
| --- | --- | --- |
| 实现 | 平台 `console` Jinja+HTMX(`:8849`);中间 `middle_admin`(`:8851`) | v0:`console/ui.py` 内嵌单页(`/v0`);v1:规划中的 `console-ui/`(Vue 3) |
| 场景 | 部署初调、改 YAML 白名单参数、看日志、服务状态 | 日常监控、隔离区复核、运维动作 |
| 用户 | 部署人员 / 实施 | 工厂 IT |
| 配置 | 写 YAML + 提示重启;凭据只显示已设置/未设置 | 动作复用 connect 引擎(窗口/白名单原样生效) |

现场部署形态见 [runbook/portable](../runbook/portable.md);链路验收见 [push-validation](../runbook/push-validation.md)。

设计详设(历史):[admin-web-ui-design](../superpowers/specs/2026-07-16-admin-web-ui-design.md)。

## 2. 架构(平台 `console`)

- 后端:FastAPI + uvicorn(`console` 依赖组),JSON API(`/api/*`)同时服务 Jinja 管理页与远期 Vue;
- `/` → Jinja 管理界面(dashboard / config / logs / debug);
- `/v0` → 保留的内嵌运维单页(`ui.py`),本机 pip / 展厅简版可用;
- Vue `console-ui/` 仍按原规划远期推进,上线后拟挂 `/v1`;不与当前 Jinja 管理界面冲突。

**中间机**独立进程 `python -m data2agent.middle_admin`(不依赖平台可达性):status / config / logs。

## 3. 运维 API 与动作(v0 协议,仍可用)

| 视图(GET /api/*) | 内容 |
| --- | --- |
| overview | 每源水位状态、对象层(行数 / 物化时间 / 未处理隔离)、只读/完整模式 |
| runs | d2a_sync_run 最近运行(状态徽章:ok / paused / failed) |
| quarantine | 未处理隔离明细(业务键 / 原因 / 时间) |
| audit | 发往源库的每条 SQL(审计承诺的可视化) |

| 动作(POST /api/actions/*) | 语义 |
| --- | --- |
| sync | 立即同步一轮(= 调度器的 run_sync_cycle) |
| reconcile(deep 可选) | L1 对账 / 深度全段修复(**推送拆机模式下中间侧禁用**,E6b 未实现) |
| apply | 重新映射全部对象 |
| retry | 单对象修复后重试(成功自动取代旧隔离记录) |

**动作不开旁路**:全部复用 connect 引擎,错峰窗口、白名单、只读适配器约束
原样生效(窗口外发起同步会被拒绝并说明)。

## 4. 安全

- Bearer Token:`--token` 优先,否则环境变量 / `config/secrets.env`
  (`D2A_CONSOLE_TOKEN` / `D2A_MIDDLE_ADMIN_TOKEN`);
- 无 Token 且绑定非本机环回时启动打印警告;生产内网建议启用 Token;
- 管理界面默认 `--host 127.0.0.1`;便携包 / 内网运维按需改为内网 IP;
- 展厅 compose 暴露 `:8849`(可不启用认证);生产部署必须配 Token 且仅内网可达。

## 5. 运行方式

```bash
# 现场推荐:家目录布局(无 yaml 时浏览器 /config 首次配置)
python -m data2agent.console --home C:\d2a --host 127.0.0.1 --port 8849
python -m data2agent.middle_admin --home C:\d2a --host 127.0.0.1 --port 8851

# 开发 / 展厅
python -m data2agent.console --config connect.example.yaml   # 或 platform.yaml
python -m data2agent.console --landing landing/factory.sqlite  # 只读模式
docker compose up --build                                    # 展厅:http://localhost:8849

# 便携包:双击 data2agent.exe(见 portable.md)
```
