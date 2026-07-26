# 便携包部署

## 1. Release 附件

| 附件 | 部署位置 |
| --- | --- |
| `d2a-portable-platform-<版本>.zip` | 数据平台 |
| `d2a-portable-middle-<版本>.zip` | 中间服务器 |

### 发布版本准备

正式发布前用本地脚本统一更新版本号并做校验:

```bash
python scripts/prepare_release.py 0.5.3
```

该命令会同步更新 `pyproject.toml`、`console-ui/package.json` 和
`console-ui/package-lock.json`,并运行版本一致性检查和 `tests/test_version*.py`。
确认无误后可让脚本创建提交、tag 并推送:

```bash
python scripts/prepare_release.py 0.5.3 --commit --tag --push
```

推送 `v0.5.3` tag 后,GitHub `release` workflow 会自动构建平台/中间机便携包、
生成 `latest.json` 并创建 Release。若只想先做测试包,不要打 tag,在 GitHub
Actions 手动运行 release workflow 且不勾选创建 Release。

### 升级策略（现场）

典型现场：**数据平台定期升级，中间机很少动。**

| 概念 | 含义 |
| --- | --- |
| `application_version` | 应用包版本（平台/中间机可各自独立升级） |
| `send_ingest_protocol_version` | **中间机包**实际发送的协议（写入中间机 `BUILD-INFO.json`） |
| `supported_ingest_protocol_versions` | **平台**接受的协议列表（健康接口 + 平台 `BUILD-INFO.json`） |
| `ingest_protocol_version` | 为旧中间机保留的 health 字段；在旧协议仍受支持时保持其最早基线值，不等于 active |

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
平台即使 active 已升至 v3，只要仍支持 v2 也会继续返回 v2，因此仍可推送。

包内 `BUILD-INFO.json`（按角色不同字段）:

```json
// 平台
{
  "application_version": "0.5.1",
  "release_version": "v0.5.1",
  "role": "platform",
  "active_ingest_protocol_version": "2",
  "legacy_health_ingest_protocol_version": "2",
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
- **破坏性协议变更**（平台不再接受某基线发送协议）：基线
  `field_baseline_send_protocols` **只增不减**（相对上一正式版本 / CI 基线 ref）；
  须在同一提交的 `deploy/ingest_protocol_compat.json` 中把该协议留在基线并写入
  `unsupported`（`reason` / `since_release`）。CI 与 tag Release 会与上一基线比对，
  禁止把基线从 `["2"]` 静默改成 `["3"]` 来绕过声明；Release 正文会提示中间机必须升级。
- 两个 zip **不必**来自同一次 Release；以各自 `BUILD-INFO.json` 与平台 health 为准。
- 每个正式 tag 仍会打出两个便携包，便于按需取用。

以 Release 正文中的「ingest 协议兼容性」段落与 `deploy/ingest_protocol_compat.json` 为准。

Windows 端到端打包验收：构建完成后由 `deploy/build_portable.ps1` 调用
`scripts/check_portable_package.py`（扫描**整个**便携包根，禁止 `erp-configs` /
旧 profile 等路径）。打 `v*` tag 的 Release workflow 会先跑
`tests/integration/mssql` compose、ingest 兼容门禁与契约测试，通过后才打包上传。

### 平台端在线升级

平台便携包支持在管理界面一键准备更新、退出后换包：

1. 升级前确认平台处于空闲时段（无推送高峰）；升级全程不触碰 `config\` 与 `data\`。
2. 打开管理界面「设置」页 →「检查更新」：
   - 更新源由环境变量 `D2A_UPDATE_URL` 指定（写入 `config\secrets.env`），
     指向 Release 附件 `latest.json`，例如
     `https://github.com/<org>/<repo>/releases/latest/download/latest.json`；
     私有仓库另需 `D2A_UPDATE_TOKEN`（只读 PAT）；也可指向内网文件服务器上的同名清单。
   - 发现新版本时自动做 **ingest 协议预检**：新平台若不再支持现场中间机协议，
     会直接拦截并提示「需先升级中间机」。
3. 点「下载更新」：后台下载、sha256 校验、解压到 `data\updates\staging`，
   并生成 `data\updates\apply-update.ps1`、便携包根目录的「升级.bat」和「启动平台.bat」。
4. 界面提示就绪后：右键托盘图标 →「退出」，双击「升级.bat」。
   脚本自动完成换包（旧版本改名 `.old` 保留）→ 启动新版本 → 健康检查；
   **新版本起不来会自动回滚到旧版本**，日志见 `data\logs\d2a-update.log`。
5. 确认新版本运行正常后，现场日常启动优先双击「启动平台.bat」；它直接执行
   `runtime\python.exe -m data2agent.console ...`，是最接近命令行手动启动的稳定入口。
6. 可手动删除 `runtime.old` / `app.old` / `data2agent.exe.old`。

中间机不使用该功能；按本节顶部策略，中间机很少升级，仍用新 zip 手工替换。

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
升级.bat              # 仅平台包:在线升级入口(详见「平台端在线升级」)
启动平台.bat          # 仅平台包:稳定启动入口,直接运行 runtime\python.exe
runtime\
app\templates\
config\
data\
data\logs\
```

凭据写入 `config\secrets.env`,配置写入 `config\*.yaml`。新安装 `connect.yaml` 中
`tables` 默认为空对象 `{}`。
