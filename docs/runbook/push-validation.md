# 推送链路部署与验收

生产中间机只允许跨机推送,不允许 `sink: local`。推送出口有三条,**不得靠改 URL 混用**:

| 路径 | `sink.type` | 实现 | 对端 | 信封 |
| --- | --- | --- | --- | --- |
| A · 通用 Webhook | `webhook` | `WebhookSink` | 任意平台的一个 HTTPS 端点 | CloudEvents,[d2a-push v1](webhook-push.md) |
| B · ingest v3 | `http` | `HttpPushSink` | 实现 ingest v3 的平台 `/ingest/*` | 表级 ingest v3 + E6b 对账 |
| C · AI Hub | `ai_hub` | `AiHubObjectPushSink` | AI Hub `/platform-api/v1/ingest/push/*` | 对象级 PUSH_AGENT v1 |

路径 A 是接入新平台的默认选择;路径 B 用于需要远端对账的强一致场景。
路径 C 是 **C1-B(本仓已落地)**:适配器、加密 spool 与 mock 契约可用,
**不得当作生产启用**;跨仓联调与按来源打开 AI Hub 推送属 **C1-C**。

## 1. 路径 A / B:对接数据平台

适用链路:

```text
ERP → 中间服务器 → 接收方数据平台(webhook 端点或 ingest v3)
```

### 1.1 部署前确认

| # | 确认项 |
| --- | --- |
| 1 | 中间服务器已有可用的 `d2a-portable-middle-<版本>.zip` |
| 2 | 已与接收方确定推送路径:webhook(端点 URL + Token / HMAC 密钥)或 ingest v3(根地址 + Token,且其 `supported_ingest_protocol_versions` 覆盖中间机发送协议) |
| 3 | 已确定数据源标识(建议 `<厂区>_<系统>`,如 `kunshan_e10`;多中间机时必须全局唯一) |
| 4 | 中间服务器已安装 ODBC Driver 18 for SQL Server |
| 5 | 中间服务器可以访问 ERP SQL Server |
| 6 | 中间服务器可以出站访问接收端 HTTPS 地址 |
| 7 | ERP 已创建只读账号 |
| 8 | webhook 路径:需要删除语义的表已约定用 `full_refresh`(webhook 无远端对账) |
| 9 | (可选)已有待抽取业务表的业务侧清单;**最终表名以现场元数据扫描为准** |

### 1.2 部署

1. 安装 ODBC Driver 18 for SQL Server。
2. 解压 `d2a-portable-middle-<版本>.zip`,双击 `data2agent.exe`。
3. 浏览器打开 `/config`,填入接收端地址 / Token 与 ERP 连接,保存。
   webhook 路径按 [webhook-push.md §2](webhook-push.md#2-中间机配置) 改写 `sink`。
4. 「测试平台连通」:webhook 发送 ping 事件,ingest v3 读取 `/ingest/health` 并比对协议。
5. 「测试数据库连接」通过后,打开 `/metadata` 刷新扫描,选表加入计划。
6. 在 `/tables` 确认模式 / 业务键 / 水位,校验并保存抽取计划。
7. 确认托盘「运行状态」正常。

### 1.3 管理界面验收

| # | 位置 | 检查 | 期望 |
| --- | --- | --- | --- |
| 1 | `/config` | 连接测试 | 数据库 `connected` / `connected_limited`;平台连通 `ok` 且 `compatible`;无密码明文 |
| 2 | `/metadata` | 刷新扫描 | 可扫描;失败不影响已有 connector |
| 3 | `/tables` | 保存计划 | 可校验、差异确认、原子保存;空计划时有引导 |
| 4 | `/status` | 分层状态 | 连接 / 配置 / 运行分层;有水位或全量快照记录 |
| 5 | `/push-logs` | 推送记录 | 每张表 begin → write → complete 均为 ok;失败带错误分类与是否可重试 |
| 6 | 日志页 | connector | 无持续 ERROR;协议 / 鉴权不一致须明确失败 |
| 7 | 接收端 | 数据到达 | 各表行数与中间机运行记录一致 |

### 1.4 二次同步验收

1. 等待下一轮同步,或在中间服务器管理界面触发一次同步。
2. 确认接收端行数没有因重复推送异常膨胀(webhook 按事件 id 去重;ingest v3 按 batch / generation 幂等)。
3. 对 `full_refresh` 表:在 ERP 删除一行后再次同步,确认接收端该行消失。
4. ingest v3 路径另需验证对账:在 ERP 删除一行增量表数据,执行对账后接收端该行被软删。

## 2. 路径 C:对接 AI Hub(C1-B 已落地,非生产)

适用链路:

```text
ERP → 中间服务器 → AI Hub DATA_INGEST (PUSH_AGENT)
```

本仓范围是适配器、加密 spool 与 mock 契约验收,不是现场开通清单。

| # | 确认项 | 说明 |
| --- | --- | --- |
| 1 | `sink.type: ai_hub` | 必须用 `AiHubObjectPushSink`;禁止把 `http` / `webhook` URL 改成 AI Hub |
| 2 | 对象信封 | 每张表登记 `object_type`、`payload_contract_version`、`payload_schema_fingerprint`(64 hex)、`payload_columns`;可选 `delete_flag_column` |
| 3 | 认证 | OIDC client credentials(`oidc_token_url` / `oidc_client_id` / `oidc_client_secret_env`),不是 ingest Token |
| 4 | 加密 spool | 必须 `spool.policy=encrypted_temp_volume` + `directory` + `encrypted_at_rest: true`;禁止 `strict_stream` / `temporary_file`。目录按来源摘要隔离,批次文件名为可移植摘要,读写删除做根目录 containment |
| 5 | AI Hub 推送开关 | `DATA_INGEST_PUSH_ENABLED` 默认关闭;本仓 C1-B **不得**要求打开 |
| 6 | 写入门 | AI Hub 变更日志 purpose 唯一约束未切 contract 前,Push 写入 API 仍关闭 |
| 7 | 验证方式 | `tests/contract/test_ai_hub_object_push_sink.py` 走进程内 mock,不连真实 AI Hub / Authentik / MSSQL;覆盖 pending 排空、变更 chunk 续发、崩溃去重与 Windows 可移植写入 |
| 8 | 管理界面 | 中间机 `middle_admin` 生产就绪度只接受 `http` / `webhook`;`ai_hub` 在 `deployment_mode=production` 时 connector 同样拒绝(C1-C 前) |
| 9 | 对账 | 禁止 `reconcile_at` / 手工对账;没有远端对账协议,也不得把 ERP 对账写进中间机 state_db |

跨仓联调、按来源打开 Push、以及把 `ai_hub` 纳入中间机管理界面就绪度,一律放到 C1-C,不在本 runbook 勾验收。

配置示例见 [source-dev.md](source-dev.md) §6.1 与仓库根 `connect.example.yaml`。
