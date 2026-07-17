# Windows 部署 Runbook · 中间服务器 + 数据平台(SQL 账号)

> 适用:中间服务器 / 数据平台均为 Windows;E10 用 SQL Server 账号+密码认证
> (不用集成认证)。原生 Windows 服务部署,不用 Docker,不拆包。
> 前置概念与拓扑见 [push-validation.md](push-validation.md);本文档只讲 Windows 落地细节。
>
> **推荐现场形态:[便携包解压即用](portable.md)**(内嵌 runtime,双击 `data2agent.exe`)。  
> 下文大量章节描述旧版 `C:\d2a` + 系统 Python + 离线 wheels,作深度排障与 NSSM 参考。
> 只要操作清单:中间机 [install-middle.md](install-middle.md)·平台机 [install-platform.md](install-platform.md)。

---

## 0. 为什么不用 Docker、不拆包

- 工厂内网多数不批 Docker Desktop(要 WSL2/Hyper-V + License);Windows 原生服务更容易过 IT 审批。
- `data2agent` 是单一 Python 包,中间/平台角色差异只在**依赖组 + 配置 + 命令行参数**,
  拆包只会多一层版本协调成本,不省实质工作量。
- 两台机器装**同一份代码**,用 venv 隔离、NSSM 托管进程即可。

---

## 1. Python 环境:统一用独立 venv(不装系统级)

两台机器都按此约定,路径固定,便于 NSSM 服务和排障对齐:

```
C:\d2a\app\            # 运行包(Release zip 解压:代码 + templates/ + wheels/ + 配置样例 + setup-*.ps1)
C:\d2a\venv\           # 独立虚拟环境,不污染系统 Python
C:\d2a\config\         # connect.yaml / platform.yaml(由 setup-*.ps1 生成,也可手工写)
C:\d2a\data\           # 落地库、日志
```

**为什么选 venv 而不是系统级安装**:
- 工厂 Windows 机常年不重装,系统 Python 可能被别的工具占用或版本冲突,venv 完全隔离;
- 升级/回滚只是切换 venv 目录或重新 `pip install`,不影响系统;
- NSSM 服务直接指向 `C:\d2a\venv\Scripts\python.exe`,不依赖 PATH,权限最小化(服务账号不需要改系统环境变量)。

建立 venv(两台机各自执行,前提已装 **Python 3.14** 官方 64 位安装包;两台机版本必须一致 ——
离线编译 wheel 绑定 Python 版本,版本不符会报 "No matching distribution"):

```powershell
python -m venv C:\d2a\venv
C:\d2a\venv\Scripts\python.exe -m pip install --upgrade pip
```

---

## 2. 离线分发:从 GitHub Release 下载运行包(推荐)

打包由 GitHub Actions 自动完成(`.github/workflows/release.yml`):打 tag(如 `v0.1.6`)后,
CI 测试通过即产出两台机各自的**离线运行包**并附到 Release。运行包内已含 `data2agent/` +
`templates/` + 配置样例 + **Windows(py3.14)原生离线依赖 wheel** + `INSTALL-*.txt` +
`setup-middle.ps1` / `setup-platform.ps1`(配置生成脚本),无需在生产机联网、也无需手工
`pip download`。打包 job 跑在 windows runner 上,确保 Windows 专属条件依赖(如 `tzdata`)被带上。

> 私有仓库:Release 附件仅有仓库权限者可下载。工厂内网**不需要**能访问 GitHub —— 在一台
> 联网机(公司办公网即可)下载附件,U 盘 / 内网文件共享拷到两台生产机。

### 2.1 下载(联网机,一次)

到仓库 **Releases** 页面,下载目标版本的两个附件:

| 附件 | 拷到哪台 |
| --- | --- |
| `d2a-runtime-connect-<版本>.zip` | 中间服务器 |
| `d2a-runtime-platform-<版本>.zip` | 数据平台 |

> 想在打 tag 前先验证打包,可在 Actions 页手动运行 `release` workflow(workflow_dispatch),
> 产物作为 Artifact 下载(不创建 Release)。

### 2.2 安装(各生产机,离线)

把对应 zip 解压到 `C:\d2a\app`,然后:

**中间机**(connect + middle_admin 管理界面):
```powershell
Expand-Archive d2a-runtime-connect-<版本>.zip C:\d2a\app
C:\d2a\venv\Scripts\pip.exe install --no-index --find-links=C:\d2a\app\wheels -e C:\d2a\app[connect,middle_admin]
```

**平台机**(装全套接收端):
```powershell
Expand-Archive d2a-runtime-platform-<版本>.zip C:\d2a\app
C:\d2a\venv\Scripts\pip.exe install --no-index --find-links=C:\d2a\app\wheels -e C:\d2a\app[ingest,connect,mcp,console]
```

> 用 `-e`(可编辑安装)是因为 `templates/` 随包在 `C:\d2a\app\templates`、不在 wheel 内 ——
> 指向该目录保证 `templates` 与代码始终配对。运行包内每台机的 `wheels\` 已是对应角色的完整依赖集。

版本一致性:两台机必须解压**同一个 Release 版本**的 zip(即同一 git tag),升级时两台一起换(见 §8)。

### 2.3 备选:本地手工打包(无 GitHub 时)

若暂不走 CI,**必须在一台联网的 Windows + Python 3.14 64 位**机器上下载依赖
(与 CI 一致)。不要在 Linux/macOS 上用 `--platform win_amd64` —— 那只会改 wheel 兼容标签,
**不会**按目标平台评估环境标记,会漏掉 `tzdata`/`colorama` 等 Windows 专属条件依赖。

```powershell
git clone <repo> d2a-src; cd d2a-src
# 中间机依赖(在本机 Windows 上原生 download,勿加 --platform)
pip download -d wheels --only-binary=:all: `
  "setuptools>=68" wheel "pydantic>=2.7" "pyyaml>=6.0" "pyodbc>=5.1" "apscheduler>=3.10" `
  "fastapi>=0.110" "uvicorn>=0.29" "jinja2>=3.0" "markupsafe"
# 平台机依赖
pip download -d wheels-full --only-binary=:all: `
  "setuptools>=68" wheel "pydantic>=2.7" "pyyaml>=6.0" "pyodbc>=5.1" "apscheduler>=3.10" `
  "mcp>=1.0" "fastapi>=0.110" "uvicorn>=0.29" "jinja2>=3.0" "markupsafe"
```
把 `d2a-src`(含 `templates/`、`deploy/setup-*.ps1`)+ 对应 `wheels*` 拷到目标机,安装命令同 §2.2。

---

## 3. ODBC 驱动(只装在中间机)

平台机推送模式下不连 ERP,**不需要装 ODBC Driver**。中间机需要:

1. 下载安装 [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)(MSI,静默安装 `msiexec /i msodbcsql.msi /quiet /qn IACCEPTMSODBCSQLLICENSETERMS=YES`);
2. 确认:`Get-OdbcDriver -Name "ODBC Driver 18 for SQL Server"`。

---

## 4. 配置与凭据(SQL 账号 + 密码)

### 4.1 中间机 `C:\d2a\config\connect.yaml`

> **推荐:用脚本生成,免手工填连接串**。运行包内含 `setup-middle.ps1`(在 `C:\d2a\app\setup-middle.ps1`)。
> 以**管理员身份**打开 PowerShell 执行(密码/token 安全提示输入,不落文件、不进历史):
> ```powershell
> C:\d2a\app\setup-middle.ps1 -PlatformIP <平台机内网IP> -ErpServer <ERP主机> -ErpDatabase <E10库> -ErpUser d2a_reader
> ```
> 脚本会:生成 `C:\d2a\config\connect.yaml`(已存在则备份)、设置机器级环境变量 `D2A_E10_DSN`/`D2A_INGEST_TOKEN`/`D2A_MIDDLE_ADMIN_TOKEN`、并调 `load_config` 自检。
> 完成后**新开窗口**再跑服务。下面的手工模板仅供参考/排错。

```yaml
templates: C:\d2a\app\templates
landing: C:\d2a\data\middle.sqlite     # 只存水位/审计,不落 raw
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN
    whitelist_from_bindings: true
    windows: []                        # 验证期不限;稳定后按 IT 约定收窄
    rate: { batch_size: 5000, rows_per_second: 2000 }
    lookback: 3d
    sync_every: 30m
    # reconcile_at 留空 —— 推送模式下配了会被 config 校验直接拒绝(见 push-validation.md §5)
    sink: { type: http, url: "http://<平台机内网IP>:8850", token_env: D2A_INGEST_TOKEN }
```

### 4.2 SQL 账号连接串(密码只放系统环境变量,不落文件)

> **用了 §4.1 的 `setup-middle.ps1` 可跳过本节** —— 脚本已写入机器级 `D2A_E10_DSN` /
> `D2A_INGEST_TOKEN`。本节仅供排错或手工回退。

密码含在连接串里,因此**连接串本身**只能作为环境变量存在,`connect.yaml` 只存环境变量名(`dsn_env`)——这是代码强制的凭据纪律(`config.py` 校验 mssql 必须配 `dsn_env`)。

用 PowerShell 设置**机器级**环境变量(重启后仍生效,服务账号可读):
```powershell
[Environment]::SetEnvironmentVariable(
  "D2A_E10_DSN",
  "DRIVER={ODBC Driver 18 for SQL Server};SERVER=<ERP主机>,1433;UID=d2a_reader;PWD=<只读密码>;DATABASE=<E10库>;TrustServerCertificate=yes",
  "Machine")

[Environment]::SetEnvironmentVariable("D2A_INGEST_TOKEN", "<随机长串>", "Machine")
[Environment]::SetEnvironmentVariable("D2A_MIDDLE_ADMIN_TOKEN", "<随机长串>", "Machine")
```

> `d2a_reader` 必须是 SQL Server 里专门建的**只读账号**(仅 SELECT 权限,限定到白名单表所在 schema)。
> 密码定期轮换时,只需改这一处机器级环境变量 + 重启 NSSM 服务,不用碰代码或配置文件。
> 也可用脚本重跑覆盖:`setup-middle.ps1 ...`(会备份旧 yaml 后重写)。

### 4.3 平台机 `C:\d2a\config\platform.yaml`

> **推荐:用脚本生成**。运行包内含 `setup-platform.ps1`(`C:\d2a\app\setup-platform.ps1`)。
> 以**管理员身份**执行(ingest token 提示输入、须与中间机一致;mcp/console token 自动生成并显示):
> ```powershell
> C:\d2a\app\setup-platform.ps1
> ```
> 脚本会生成 `platform.yaml`、设置三个 token 环境变量、自检配置,并打印四个服务的 `AppParameters`。
> 下面的手工模板仅供参考/排错。

```yaml
templates: C:\d2a\app\templates
landing: C:\d2a\data\factory.sqlite    # 与 ingest 的 --landing 指向同一个文件
sources:
  digiwin_e10:
    adapter: mssql_readonly            # 仅用于 apply 读取 binding,不会真的连 ERP
    dsn_env: D2A_E10_DSN_PLACEHOLDER   # 平台机不设该变量也无妨:apply 不建 adapter
```

平台机环境变量(用了上面的 `setup-platform.ps1` 可跳过 —— 脚本已写入三个 token):
```powershell
[Environment]::SetEnvironmentVariable("D2A_INGEST_TOKEN", "<与中间机同一串>", "Machine")
[Environment]::SetEnvironmentVariable("D2A_MCP_TOKEN", "<随机长串>", "Machine")
[Environment]::SetEnvironmentVariable("D2A_CONSOLE_TOKEN", "<随机长串>", "Machine")
```

设置机器级环境变量后需要**重启 PowerShell / 重启服务**才会生效。

---

## 5. 用 NSSM 把进程包成 Windows 服务

下载 [NSSM](https://nssm.cc/) 解压到 `C:\d2a\nssm\nssm.exe`。每个服务命令模式一致:

```powershell
C:\d2a\nssm\nssm.exe install <服务名> C:\d2a\venv\Scripts\python.exe
C:\d2a\nssm\nssm.exe set <服务名> AppParameters '-m data2agent.<模块> <参数...>'
C:\d2a\nssm\nssm.exe set <服务名> AppDirectory C:\d2a\app
C:\d2a\nssm\nssm.exe set <服务名> AppStdout C:\d2a\data\logs\<服务名>.log
C:\d2a\nssm\nssm.exe set <服务名> AppStderr C:\d2a\data\logs\<服务名>.log
C:\d2a\nssm\nssm.exe set <服务名> AppExit Default Restart   # 崩溃自动重启
C:\d2a\nssm\nssm.exe start <服务名>
```

### 5.1 中间机:2 个服务

| 服务名 | AppParameters |
| --- | --- |
| `d2a-connector` | `-m data2agent.connect serve --config C:\d2a\config\connect.yaml` |
| `d2a-middle-admin` | `-m data2agent.middle_admin --config C:\d2a\config\connect.yaml --host 0.0.0.0 --port 8851 --log-path C:\d2a\data\logs\d2a-connector.log` |

> **Token 纪律:** `D2A_MIDDLE_ADMIN_TOKEN` 只设机器级环境变量,**不要**在 NSSM `AppParameters` 里写 `%D2A_MIDDLE_ADMIN_TOKEN%` 或 `--token ...` —— 服务进程继承 Machine env,`middle_admin` 自动读取。
> 管理界面 `http://<中间机IP>:8851`,浏览器登录时用 setup 脚本输出的 Token。
> 便携包请双击目录内唯一入口 `data2agent.exe`(见 [portable.md](portable.md))。
> 防火墙:内网放行入站 **8851**(仅运维网段,不对公网)。

先手动验证一轮再装服务:
```powershell
C:\d2a\venv\Scripts\python.exe -m data2agent.connect serve --config C:\d2a\config\connect.yaml --once
```

### 5.2 平台机:4 个服务(同一 venv,不同命令)

| 服务名 | AppParameters |
| --- | --- |
| `d2a-ingest` | `-m data2agent.ingest --landing C:\d2a\data\factory.sqlite --host 0.0.0.0 --port 8850` |
| `d2a-apply`  | `-m data2agent.connect apply --config C:\d2a\config\platform.yaml --landing C:\d2a\data\factory.sqlite --every 1800` |
| `d2a-mcp`    | `-m data2agent.mcp_server --db C:\d2a\data\factory.sqlite --transport http --host 0.0.0.0 --port 8848` |
| `d2a-console`| `-m data2agent.console --config C:\d2a\config\platform.yaml --host 0.0.0.0 --port 8849 --log-dir C:\d2a\data\logs` |

> 平台管理界面 `http://<平台机IP>:8849`,登录 Token 为机器级 `D2A_CONSOLE_TOKEN`(setup-platform 生成并显示)。旧版 JSON API 仍在 `/v0`。
> 便携包请双击目录内唯一入口 `data2agent.exe`(见 [portable.md](portable.md))。

`d2a-apply` 用的 `--every 1800` 是本次新加的常驻循环参数(每 30 分钟跑一轮 `raw_* → obj_*`)——
拆机部署下 `ingest` 只负责接收落地,没有进程会周期性物化对象层,这个服务补上这个缺口。
单次验证也可以先不带 `--every` 手动跑一次看效果:
```powershell
C:\d2a\venv\Scripts\python.exe -m data2agent.connect apply --config C:\d2a\config\platform.yaml --landing C:\d2a\data\factory.sqlite
```

---

## 6. 服务账号权限

NSSM 服务默认用 `Local System` 运行即可读写 `C:\d2a\data`;若公司策略要求专用服务账号,
在 `nssm set <服务名> ObjectName .\<域账号> <密码>` 指定,并确保该账号对 `C:\d2a` 有读写权限。
Windows 防火墙需放行:平台机入站 8850(ingest,仅对中间机 IP)、8848/8849(按需对内网开放);中间机入站 8851(管理界面,仅运维网段)。

---

## 7. 验收(对应 push-validation.md §4,Windows 版检查命令)

```powershell
# 平台机:确认 raw 已落地
C:\d2a\venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('C:/d2a/data/factory.sqlite'); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%'\")])"

# 中间机:确认零 raw、有水位
C:\d2a\venv\Scripts\python.exe -m data2agent.connect status --landing C:\d2a\data\middle.sqlite

# 服务状态
Get-Service d2a-*
```

---

## 8. 升级流程

1. 下载新版本 Release 的运行包 zip(§2.1),解压到 `C:\d2a\app-new`,验证后再切换目录名,方便回滚;
2. 两台机的服务顺序:先停平台机服务,再停中间机服务;
3. `pip install --no-index --find-links=... -e C:\d2a\app[...]` 覆盖安装;
4. 按 §5 顺序反过来启动(先平台,后中间),用 `--once` 先跑一轮确认再转常驻。

---

## 9. 常见故障排查(Windows 特有)

| 现象 | 排查 |
| --- | --- |
| 安装报 `No matching distribution found for setuptools>=68` | `-e` 安装触发离线构建,需 `setuptools`/`wheel` wheel 在 `wheels\` 内(v0.1.0 运行包漏打;临时解法见下)。<br>临时解法:联网机 `pip download -d fix "setuptools>=68" wheel`(纯 Python,无需平台参数),把产出的 2 个 whl 拷进 `C:\d2a\app\wheels` 后重跑安装 |
| 安装报 `No matching distribution found for tzdata`(来自 tzlocal)或 `colorama` | Windows 专属条件依赖漏打(v0.1.3 及更早在 Linux 上用 `--platform` 下载,不会评估 `platform_system=="Windows"` 标记)。v0.1.4 起改为在 windows runner 上原生打包修复。<br>临时解法:联网 **Windows** 机 `pip download -d fix tzdata colorama`,把产出 whl 拷进 `C:\d2a\app\wheels` 后重跑安装 |
| 运行 `setup-*.ps1` 报「在此系统上禁止运行脚本」 | 默认 ExecutionPolicy 禁脚本。管理员窗口执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`,或单次:`powershell -ExecutionPolicy Bypass -File C:\d2a\app\setup-middle.ps1 ...` |
| 运行 `setup-*.ps1` 出现大量「意外的标记」/中文乱码 | 旧版脚本含 UTF-8 中文,Windows PowerShell 5.x 无 BOM 会误解析。换用仓库最新 ASCII 版脚本覆盖 `C:\d2a\app\setup-*.ps1` 后重跑 |
| NSSM 服务启动即退出 | 看 `AppStdout`/`AppStderr` 日志;多是环境变量未生效(机器级变量需重启服务进程) |
| pyodbc 报 `IM002` 找不到驱动 | ODBC Driver 18 未装或架构不匹配(确认 64 位 Python 配 64 位驱动) |
| 密码含特殊字符导致连接串解析错 | 连接串里 `;`/`=` 等符号需按 ODBC 连接串规则处理,必要时整串加引号 |
| 防火墙拦截推送 | `Test-NetConnection <平台IP> -Port 8850` 从中间机测试连通性 |
| `d2a-apply` 服务一直空转无变化 | 确认 `--landing` 与 `d2a-ingest` 落地路径一致;检查 `factory.sqlite` 是否真的有新 raw 行 |
