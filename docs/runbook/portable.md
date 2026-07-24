# 便携包部署

## 1. Release 附件

| 附件 | 部署位置 |
| --- | --- |
| `d2a-portable-platform-<版本>.zip` | 数据平台 |
| `d2a-portable-middle-<版本>.zip` | 中间服务器 |

### 升级策略（现场）

典型现场：**数据平台会定期升级，中间机很少动。**

硬约束只有一条——两边的 **`ingest_protocol_version` 必须一致**（当前为 `"2"`）。
中间机同步前会校验平台健康接口；协议不一致时 fail-fast，不会产生半份数据。

因此：

- **协议未变**（Release 说明未写协议 bump）：可以只升级数据平台，中间机继续用现有包。
- **协议 bump**（例如 `"2"` → `"3"`）：平台与中间机必须一起换到支持新协议号的包。
- 首次安装仍建议从**同一次 Release**各取一个 zip，减少首装排障变量。

每个正式 Release 仍会同时产出两个便携包（便于按需取用），不等于每次都必须两边都装。

Windows 端到端打包验收：构建完成后由 `deploy/build_portable.ps1` 调用
`scripts/check_portable_package.py`（扫描**整个**便携包根，禁止 `erp-configs` /
旧 profile 等路径）。打 `v*` tag 的 Release workflow 会先跑
`tests/integration/mssql` compose 集成（真实 SQL Server），通过后才打包上传。
该检查是 Release 门槛。

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
