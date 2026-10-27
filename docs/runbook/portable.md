# 便携包部署(中间机)

## 1. Release 附件

| 附件 | 部署位置 |
| --- | --- |
| `d2a-portable-middle-<版本>.zip` | 中间服务器(工厂内网,可只读访问 ERP 数据库) |

接收数据的平台不在本仓库内:可以是任何实现了 [d2a-push v1 webhook](webhook-push.md)
或 ingest v3 的数据平台。

### Windows 防火墙前置条件

启动前，Windows 防火墙或终端安全软件必须允许便携包中的
`data2agent.exe` 与 `runtime\python.exe` 访问本机回环网络，并允许出站访问接收端地址。
若 `runtime\python.exe` 连自己监听的 `127.0.0.1` 临时端口都会超时，
asyncio 会在监听管理端口前卡住，启动器最终提示 `8851` 启动超时。

遇到该现象时，先把上述两个程序加入防火墙/终端安全软件允许列表，再排查应用代码。
不要用替换 Python 版本、改用 `pythonw.exe`、修改子进程控制台模式或替换
`socket.socketpair` 的方式规避；这些措施无法修复被系统安全策略阻断的回环连接。

### 传输与解压前置条件

**必须传输 zip 原文件，到 Windows 后再解压**；禁止「在 Mac 上解压后把文件夹拷进 U 盘」。
macOS 向 FAT32/exFAT U 盘写入时会生成 `._*` AppleDouble 伴随文件，混入 `runtime\`
后可能导致 Python 包导入异常。
推荐解压工具：Windows 资源管理器或 7-Zip；避免使用会对文本文件「智能转码」的
国产压缩软件。

### 发布版本准备

正式发布前用本地脚本统一更新版本号并做校验:

```bash
python scripts/prepare_release.py 0.5.3
```

该命令会更新 `pyproject.toml`,并运行版本一致性检查和 `tests/*/test_version*.py`。
确认无误后可让脚本创建提交、tag 并推送:

```bash
python scripts/prepare_release.py 0.5.3 --commit --tag --push
```

推送 `v0.5.3` tag 后,GitHub `release` workflow 会自动构建中间机便携包并创建 Release。若只想先做测试包,不要打 tag,在 GitHub
Actions 手动运行 release workflow 且不勾选创建 Release。

### 升级与推送协议

中间机用新 zip 手工替换升级(保留 `config\` 与 `data\`)。推送协议与应用版本分离:

| `sink.type` | 协议 | 兼容性约束 |
| --- | --- | --- |
| `webhook` | d2a-push v1(事件信封带 `d2apushversion`) | 新增字段向后兼容;破坏性变更会升主版本并在 Release 正文说明 |
| `http` | ingest v3 | 中间机读取接收端 `/ingest/health` 的 `supported_ingest_protocol_versions`,本机发送协议不在列表内即 fail-fast;基线变更受 `deploy/ingest_protocol_compat.json` 门禁约束 |

中间机 `BUILD-INFO.json` 只声明自己发送的 ingest 协议:

```json
{
  "application_version": "0.6.5",
  "release_version": "v0.6.5",
  "role": "middle",
  "send_ingest_protocol_version": "3",
  "commit": "..."
}
```

Windows 端到端打包验收：构建完成后由 `deploy/build_portable.ps1` 调用
`scripts/check_portable_package.py`（扫描**整个**便携包根，禁止 `erp-configs` /
旧 profile 等路径，且禁止携带平台端代码）。打 `v*` tag 的 Release workflow 会先跑
`tests/integration/mssql` compose、ingest 兼容门禁与契约测试，通过后才打包上传。

## 2. 端口

| 机器 | 端口 | 用途 |
| --- | --- | --- |
| 中间服务器 | `8851` | 管理界面(仅监听 `127.0.0.1`) |

中间机只发起**出站**连接(ERP 数据库、接收端 HTTPS),不需要开放入站端口。

## 3. 部署步骤

1. 向接收方平台确认推送方式与地址:webhook 端点 URL + Token / 签名密钥,或 ingest v3 根地址 + Token。
2. 中间服务器安装 ODBC Driver 18 for SQL Server。
3. 中间服务器解压 `d2a-portable-middle-<版本>.zip`。
4. 中间服务器双击 `data2agent.exe`。
5. 浏览器打开 `/config`,填写:
   - 平台 URL:ingest v3 根地址,例如 `https://<平台域名>`
   - ERP 连接信息
   - 与平台一致的推送 Token
   - 管理 Token
6. **改用 webhook 推送时**:首配向导生成的是 `sink.type: http`。编辑 `config\connect.yaml`,
   按 [webhook-push.md §2](webhook-push.md#2-中间机配置) 把 `sink` 改为 `type: webhook`,
   在 `config\secrets.env` 写入 `token_env` / `hmac_secret_env` 指向的变量,并删除
   `reconcile_at` / `reconcile_deep_at`;保存后在 `/config` 点「测试平台连通」确认 ping 成功。
7. **首次选表（默认空清单，未选表不会访问 ERP 业务表）**:
   1. 在配置页「测试数据库连接」通过后，打开 `/metadata`（或点「下一步：元数据」）；
   2. 「刷新元数据」扫描表结构，打开详情「加入抽取计划」；
   3. 在 `/tables` 确认模式、业务键与水位，校验通过后保存；
   4. 保存后配置会在下一轮自动重载；首次配置落盘后 launcher 会自动拉起 connector，无需再次双击。
8. 在中间机以管理员 PowerShell 运行 `安装开机自启.ps1`：
   - 安装 `SYSTEM` 开机任务，重启主机后无需用户登录；
   - 以 `--headless` 常驻监控 admin / connector / maintenance；
   - 崩溃会自动重启，反复崩溃熵断 15 分钟后自动试探；
   - 卸载用 `卸载开机自启.ps1`，不会删数据或配置。
9. 确认中间机 `/api/status` 中 `process_status.connector_running=true`，并在接收端确认数据到达。

中间机 launcher 另外管理每日 maintenance：对 `data\middle-state.sqlite` 做 SQLite
Online Backup + integrity check，默认保留 14 份，清理 90 天运行历史、
365 天回执和超过 24 小时的孤儿 staging。可用空间低于 2 GiB 时任务失败并进入进程监控告警状态。

### 中间机状态库备份与恢复

`middle-state.sqlite` 是中间机的**控制状态库**，保存水位、运行步骤、推送回执、审计和管理状态，
不保存 ERP Raw，也不是接收端业务数据备份。受监管便携部署的备份默认位于
`data\backups\middle-state-<时间>.sqlite`。配置、`config\secrets.env`、程序文件和平台 Raw
必须分别备份；状态库备份不能替代完整节点灾备。备份中仍可能包含水位或运行键等业务标识，
应按敏感运维数据保护。

恢复前先在便携包根目录做只读完整性检查：

```powershell
.\runtime\python.exe -c "import sqlite3,sys; from pathlib import Path; u=Path(sys.argv[1]).resolve().as_uri()+'?mode=ro'; c=sqlite3.connect(u,uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()" .\data\backups\middle-state-<时间>.sqlite
```

只有输出 `ok` 才能继续。实际恢复必须离线执行：停止计划任务并确认 launcher、connector、
maintenance 全部退出；把当前数据库及 `-wal`/`-shm` 复制到新的故障留存目录；再把已校验备份
复制为 `data\middle-state.sqlite`，不要把旧 WAL/SHM 带回。重启后先核对配置 revision、进程、
水位和推送记录。控制状态回退可能造成重复推送，接收端依靠事件 id / batch / generation 幂等处理。

相同说明可在中间机管理页 `/recovery` 查看；页面故意不提供在线覆盖按钮。

## 4. 验收

按 [push-validation.md](push-validation.md) 执行链路验收。

## 5. 文件位置

解压目录内:

```text
data2agent.exe
安装开机自启.ps1
卸载开机自启.ps1
runtime\
config\
data\
data\logs\
```

凭据写入 `config\secrets.env`,配置写入 `config\connect.yaml`。新安装 `connect.yaml` 中
`tables` 默认为空对象 `{}`。
