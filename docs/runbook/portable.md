# 便携包部署

## 1. Release 附件

| 附件 | 部署位置 |
| --- | --- |
| `d2a-portable-platform-<版本>.zip` | 数据平台 |
| `d2a-portable-middle-<版本>.zip` | 中间服务器 |

### 升级策略（现场）

典型现场：**数据平台定期升级，中间机很少动。**

| 概念 | 含义 |
| --- | --- |
| `application_version` | 应用包版本（平台/中间机可各自独立升级） |
| `send_ingest_protocol_version` | **中间机包**实际发送的协议（写入中间机 `BUILD-INFO.json`） |
| `supported_ingest_protocol_versions` | **平台**接受的协议列表（健康接口 + 平台 `BUILD-INFO.json`） |

平台 `/ingest/health` 示例:

```json
{
  "ok": true,
  "active_ingest_protocol_version": "2",
  "supported_ingest_protocol_versions": ["2"],
  "ingest_protocol_version": "2"
}
```

中间机只要自己发送的协议落在平台 `supported_ingest_protocol_versions` 中即可推送；
否则 fail-fast，不会产生半份数据。旧中间机若只认 `ingest_protocol_version` 精确相等，
在平台仍以 v2 为 active 时同样可用。

包内 `BUILD-INFO.json`（按角色不同字段）:

```json
// 平台
{
  "application_version": "0.5.1",
  "release_version": "v0.5.1",
  "role": "platform",
  "active_ingest_protocol_version": "2",
  "supported_ingest_protocol_versions": ["2"],
  "commit": "..."
}

// 中间机（只声明自己发送的协议，不声明平台支持列表）
{
  "application_version": "0.5.1",
  "release_version": "v0.5.1",
  "role": "middle",
  "send_ingest_protocol_version": "2",
  "commit": "..."
}
```

因此：

- **平台仍声明支持 v2**：只升级数据平台即可，既有 v2 中间机**无需升级**。
- **破坏性协议变更**（平台不再接受某基线发送协议）：在提交内更新
  `deploy/ingest_protocol_compat.json` 的 `unsupported`（含 reason / since_release），
  CI 与 tag Release 正文会据此提示「中间机必须升级」。
- 两个 zip **不必**来自同一次 Release；以各自 `BUILD-INFO.json` 与平台 health 为准。
- 每个正式 tag 仍会打出两个便携包，便于按需取用。

以 Release 正文中的「ingest 协议兼容性」段落与 `deploy/ingest_protocol_compat.json` 为准。

Windows 端到端打包验收：构建完成后由 `deploy/build_portable.ps1` 调用
`scripts/check_portable_package.py`（扫描**整个**便携包根，禁止 `erp-configs` /
旧 profile 等路径）。打 `v*` tag 的 Release workflow 会先跑
`tests/integration/mssql` compose、ingest 兼容门禁与契约测试，通过后才打包上传。

## 2. 端口

| 机器 | 端口 | 用途 |
| --- | --- | --- |
| 中间服务器 | `8851` | 管理界面 |
| 数据平台 | `8849` | 管理界面 |
| 数据平台 | `8850` | ingest 接收 |
| 数据平台 | `8848` | MCP HTTP |

## 3. 部署步骤

1. 数据平台解压 `d2a-portable-platform-<版本>.zip`。
2. 数据平台双击 `data2agent.exe`。
3. 浏览器打开 `/setup`,填写 ingest Token、管理 Token、MCP Token。
4. 中间服务器安装 ODBC Driver 18 for SQL Server。
5. 中间服务器解压 `d2a-portable-middle-<版本>.zip`。
6. 中间服务器双击 `data2agent.exe`。
7. 浏览器打开 `/config`,填写:
   - 平台 URL:`http://<平台IP>:8850` 或 `https://<平台域名>`
   - ERP 连接信息
   - 与平台一致的 ingest Token
   - 管理 Token
8. **首次选表（默认空清单，未选表不会访问 ERP 业务表）**:
   1. 在配置页「测试数据库连接」通过后，打开 `/metadata`（或点「下一步：元数据」）；
   2. 「刷新元数据」扫描表结构，打开详情「加入抽取计划」；
   3. 在 `/tables` 确认模式、业务键与水位，校验通过后保存；
   4. 重启抽取进程使新 `tables` 生效。
9. 确认两台机器托盘「运行状态」均为正常。

## 4. 验收

按 [push-validation.md](push-validation.md) 执行链路验收。

外部 Agent 通过 HTTP MCP 接入时,按 [mcp-http-agent-integration.md](mcp-http-agent-integration.md)
配置平台地址、Token 和工具调用流程。

## 5. 文件位置

解压目录内:

```text
data2agent.exe
runtime\
app\templates\
config\
data\
data\logs\
```

凭据写入 `config\secrets.env`,配置写入 `config\*.yaml`。新安装 `connect.yaml` 中
`tables` 默认为空对象 `{}`。
