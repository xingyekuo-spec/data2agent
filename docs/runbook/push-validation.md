# 推送链路部署与验收

生产中间机只允许跨机推送,不允许 `sink: local`。对端有两条协议,**不得靠改 URL 混用**:

| 路径 | `sink.type` | 实现 | 对端 | 信封 |
| --- | --- | --- | --- | --- |
| A · 本仓平台 | `http` | `HttpPushSink` | data2agent 平台 `/ingest/*` | 表级 ingest v3 |
| B · AI Hub | `ai_hub` | `AiHubObjectPushSink` | AI Hub `/platform-api/v1/ingest/push/*` | 对象级 PUSH_AGENT v1 |

路径 A 是现行工厂试点与便携包验收主路径(下文 §1–§4)。
路径 B 是 C1-B 适配器:代码与 mock 契约已落地, **不得当作生产启用**;跨仓联调与按来源打开 AI Hub 推送属 C1-C。

## 1. 路径 A:对接 data2agent 平台

适用链路:

```text
ERP → 中间服务器 → data2agent 数据平台
```

### 1.1 部署前确认

| # | 确认项 |
| --- | --- |
| 1 | 数据平台已拿到拟安装的 `d2a-portable-platform-<版本>.zip` |
| 2 | 中间服务器已有可用的 `d2a-portable-middle-<版本>.zip`（可与平台应用版本不同） |
| 3 | 平台 `supported_ingest_protocol_versions` 覆盖中间机发送协议（见 Release 兼容性说明；通常 v2 中间机可对接新平台） |
| 4 | 已确定数据源标识（建议 `<厂区>_<系统>`，如 `kunshan_e10`;多中间机时必须全局唯一） |
| 5 | 中间服务器已安装 ODBC Driver 18 for SQL Server |
| 6 | 中间服务器可以访问 ERP SQL Server |
| 7 | 中间服务器可以访问数据平台 ingest 端口,默认 `8850` |
| 8 | ERP 已创建只读账号 |
| 9 | （可选）已有待抽取业务表的业务侧清单；**最终表名以现场元数据扫描为准**，不依赖安装包内嵌候选表 |

### 1.2 部署

#### 数据平台

1. 解压 `d2a-portable-platform-<版本>.zip`。
2. 双击 `data2agent.exe`。
3. 浏览器打开 `/setup`。
4. 填写 ingest Token（**管理员 Token**,迁移期/逃生用）、管理 Token、MCP Token。
5. 保存配置。
6. 确认托盘「运行状态」正常。
7. 打开控制台「数据源管理 → 添加数据源」:填写源标识（如 `kunshan_e10`）登记，
   **平台签发该源专属推送 Token（明文仅此一次）**,复制生成的中间机配置片段。

> **签发制说明(2026-08 起)**:数据源须在平台登记后才可推送；ingest 按
> 「source + 专属 Token」校验,停用/重置在数据源管理页操作。兼容规则:
> 空登记簿 + 无管理员 Token 为开发引导期(开放);管理员 Token 可推任何源
> (迁移期),现场全部切换为专属 Token 后可从 secrets.env 移除以严格化。

#### 中间服务器

1. 安装 ODBC Driver 18 for SQL Server。
2. 解压 `d2a-portable-middle-<版本>.zip`。
3. 双击 `data2agent.exe`。
4. 浏览器打开 `/config`，将平台签发的配置片段(source 标识 / 平台 URL /
   专属 Token)与 ERP 连接填入，保存。`sink.type` 必须为 `http`。
5. 「测试数据库连接」通过后，打开 `/metadata` 刷新扫描,选表加入计划。
6. 在 `/tables` 确认模式 / 业务键 / 水位,校验并保存抽取计划。
7. 重启抽取进程。
8. 确认托盘「运行状态」正常。

### 1.3 管理界面验收

| # | 位置 | 检查 | 期望 |
| --- | --- | --- | --- |
| 1 | 中间服务器 `:8851` | `/config` 连接测试 | `connected` / `connected_limited`；无密码明文 |
| 2 | 中间服务器 `:8851` | `/metadata` | 可扫描；失败不影响已有 connector |
| 3 | 中间服务器 `:8851` | `/tables` | 可校验、差异确认、原子保存；空计划时有引导 |
| 4 | 中间服务器 `:8851` | `/status` | 连接 / 配置 / 运行分层；有水位或全量快照记录 |
| 5 | 中间服务器 `:8851` | 日志页 | connector 无持续 ERROR；协议版本不一致须明确失败 |
| 6 | 数据平台 `:8849` | 仪表盘 | 出现 `raw_*`；全量表删除行不在 raw 残留 |
| 7 | 数据平台 `:8849` | 仪表盘 | 出现 published / 对象层数据 |
| 8 | 两台机器 | 托盘「运行状态」 | 后台进程正常 |

### 1.4 二次同步验收

1. 等待下一轮同步,或在中间服务器管理界面触发一次同步。
2. 打开数据平台管理界面。
3. 确认 `raw_*` 行数没有因重复推送异常膨胀。
4. 确认对象层仍可正常浏览。
5. 对 `full_refresh` 表：在 ERP 删除一行后再次同步,确认平台 raw 中该行消失。

## 2. 路径 B:对接 AI Hub(C1-B,非生产)

适用链路:

```text
ERP → 中间服务器 → AI Hub DATA_INGEST (PUSH_AGENT)
```

当前范围是适配器与 mock 契约,不是现场开通清单。

| # | 确认项 | 说明 |
| --- | --- | --- |
| 1 | `sink.type: ai_hub` | 必须用 `AiHubObjectPushSink`;禁止把路径 A 的 `http` URL 改成 AI Hub |
| 2 | 对象信封 | 每张表登记 `object_type`、`payload_contract_version`、`payload_schema_fingerprint`(64 hex)、`payload_columns`;可选 `delete_flag_column` |
| 3 | 认证 | OIDC client credentials(`oidc_token_url` / `oidc_client_id` / `oidc_client_secret_env`),不是 ingest Token |
| 4 | AI Hub 推送开关 | `DATA_INGEST_PUSH_ENABLED` 默认关闭;本仓 C1-B **不得**要求打开 |
| 5 | 写入门 | AI Hub 变更日志 purpose 唯一约束未切 contract 前,Push 写入 API 仍关闭 |
| 6 | 验证方式 | `tests/contract/test_ai_hub_object_push_sink.py` 走进程内 mock,不连真实 AI Hub / Authentik / MSSQL |
| 7 | 管理界面 | 中间机 `middle_admin` 生产就绪度按「非 http 即失败」;`ai_hub` 在 `deployment_mode=production` 时 connector 同样拒绝(C1-C 前) |
| 8 | 对账 | 禁止 `reconcile_at` / 手工对账;没有远端对账协议,也不得把 ERP 对账写进中间机 state_db |

跨仓联调、按来源打开 Push、以及把 `ai_hub` 纳入中间机管理界面就绪度,一律放到 C1-C,不在本 runbook 勾验收。

配置示例见 [source-dev.md](source-dev.md) §6.1 与仓库根 `connect.example.yaml`。
