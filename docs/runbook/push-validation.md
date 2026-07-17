# 现场验证 Runbook · ERP → 中间服务器 → 数据平台(推送链路)

> 目标:验证 Pattern A 拆机部署的**同步链路** —— 中间服务器只读抽取生产 ERP,
> 出站推送 raw 批次到数据平台落地。对应设计 [02-extraction §12](../design/02-extraction.md)。
>
> 适用范围:E6a(推送 sink)已实现。**本 runbook 只验同步,不含对账**
> (E6b 跨机对账未实现,见 §5 边界)。
>
> **推荐现场形态:[便携包解压即用](portable.md)** —— 两台机各双击 `data2agent.exe`,
> 浏览器完成首次配置与管理界面验收。下文 §3A / §4A 是主路径。
>
> 安装细节外链:
> - 便携包:[portable.md](portable.md)
> - 备选(系统 Python + venv):[install-middle.md](install-middle.md) · [install-platform.md](install-platform.md)
> - NSSM / 深度排障:[windows-deploy.md](windows-deploy.md)
> - §3B / §4B 的手工 CLI 是便携包内部等价流程,用于理解协议与排障。

---

## 1. 拓扑与数据流

```
生产 ERP(内网,SQL Server / 鼎捷 E10)
   │ 只读账号:仅 SELECT / 白名单 / 限流 / 逐条 SQL 审计
   ▼
中间服务器(薄):connect serve + sink=http    ← 持有 ERP 只读凭据;本地只留水位/审计,不落 raw
   │ 出站推送 raw 批次(POST /ingest/batch)
   ▼
数据平台:ingest → apply → MCP              ← 落地 raw_* → 物化 obj_* → Agent 入口
```

**连接发起方向 = 中间 → 平台(出站)**。这是当前代码的唯一实现。
若客户网络只允许平台→中间方向,推送模式跑不通(需另建拉取实现,尚不存在)。

| 角色 | 管理界面 | 后台进程(便携包自动拉起) |
| --- | --- | --- |
| 中间机 | `:8851` | connector(`connect serve`) |
| 平台机 | `:8849` | ingest(`:8850`) + apply + mcp(`:8848`) |

便携包家目录(解压根,不必固定 `C:\d2a`):

```
config\connect.yaml | platform.yaml
config\secrets.env          # 凭据,不进 YAML
data\middle.sqlite          # 中间:水位/审计
data\factory.sqlite         # 平台:raw_* + obj_*
data\logs\                  # 各进程 + launcher 日志
```

---

## 2. 前置确认(进厂前,书面与 IT 敲定)

| # | 确认项 | 不满足的后果 |
| --- | --- | --- |
| ① | **中间 → 平台的出站已放行**(到平台 ingest 端口,默认 **8850**) | 推送直接连不上,链路不通 |
| ② | 生产 ERP 只对**中间服务器**授只读账号 + 白名单表 | 抽取被拒 / 违反安全承诺 |
| ③ | 中间服务器可常驻运行(便携包托盘常驻,或 NSSM 服务) | 无法持续同步 |
| ④ | 中间已装 [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)(64 位) | 适配器连不上 ERP |
| ⑤ | 两台机拿到**同版本**便携包 zip(或同版本 runtime 备选包) | 协议/模板不一致 |
| ⑥ | 允许的抽取窗口与限流上限(与 IT 书面确认) | 影响 `windows` / `rate` 配置 |

> 传输安全:`ingest` 自身跑明文 HTTP。验证阶段在**内网可信段**用「明文 + Bearer Token」可接受;
> 需要 TLS 时在平台侧加反向代理(nginx / caddy)终止 TLS,再把中间机「平台 URL」指到它。

---

## 3. 部署与启动

### 3A. 主路径(便携包 + 浏览器)

**顺序:先平台,后中间**(中间推送依赖平台 ingest 已监听)。

1. **平台机**:解压 `d2a-portable-platform-<版本>.zip` → 双击 `data2agent.exe`
   - 首次浏览器打开管理界面 → `/config`
   - 填写 **接收口令**(ingest Token,与中间机一致)及管理 Token;MCP Token 可留空自动生成
   - 保存后托盘常驻,自动拉起 ingest / apply / mcp
2. **中间机**:先装 ODBC Driver 18 → 解压同版本 middle zip → 双击 `data2agent.exe`
   - `/config` 填写:平台 URL(`http://<平台内网IP>:8850`)、ERP 连接、与平台相同的接收口令、管理 Token
   - 保存后自动拉起 connector
3. 托盘「运行状态」两侧均应健康;再次双击只会重开管理界面,不会重复启动。

细节(托盘菜单、崩溃自动重启、日志页)见 [portable.md](portable.md)。

### 3B. 等价 CLI(协议理解 / 排障)

路径以下为示意,便携包请换成解压根下的 `data\...`。

#### 平台侧

```bash
export D2A_INGEST_TOKEN='<生成一个随机长串>'
python -m data2agent.ingest \
  --landing data/factory.sqlite \
  --host 0.0.0.0 --port 8850
# 启动日志应显示 Token 认证与落地路径
```

健康检查(平台本机):
```bash
curl -s http://127.0.0.1:8850/ingest/health   # {"ok": true, ...}
```

管理界面(可选,备选安装时):
```bash
python -m data2agent.console --home <家目录> --host 127.0.0.1 --port 8849
```

#### 中间侧 `connect.yaml`

```yaml
templates: templates
landing: data/middle.sqlite          # 只存水位/审计,不落 raw
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN               # 连接串只从环境变量 / secrets.env 读,绝不落文件
    whitelist_from_bindings: true
    windows: []                        # 验证初期先不限窗口,尽快看到数据;稳定后按 IT 约定收窄
    rate: { batch_size: 5000, rows_per_second: 2000 }
    lookback: 3d
    sync_every: 30m
    apply_after_sync: true             # 推送模式下自动忽略(映射在平台侧)
    sink: { type: http, url: "http://<平台内网IP>:8850", token_env: D2A_INGEST_TOKEN }
    # 注意:推送模式下不要配 reconcile_at(config 校验会拒绝;E6b 未实现)
```

```bash
export D2A_E10_DSN='DRIVER={ODBC Driver 18 for SQL Server};SERVER=<ERP主机>,1433;UID=d2a_reader;PWD=<只读密码>;DATABASE=<E10库>;TrustServerCertificate=yes'
export D2A_INGEST_TOKEN='<与平台同一串>'

# 先跑一轮验证配置与连通性,不常驻:
python -m data2agent.connect serve --config connect.yaml --once
```

确认无误后去掉 `--once` 常驻运行。管理界面备选:
```bash
python -m data2agent.middle_admin --home <家目录> --host 127.0.0.1 --port 8851
```

---

## 4. 验收检查

### 4A. 主路径(管理界面)

一轮同步后(便携包常驻会自动跑;或中间管理界面触发一次同步 —— 注意若 connector 已在跑可能重叠):

| # | 检查 | 期望 |
| --- | --- | --- |
| 1 | 中间 `:8851` 状态页 | 有水位 / 最近运行成功;本地**无** raw 业务表 |
| 2 | 平台 `:8849` 仪表盘 | 出现 `raw_*` 表与行数;随后有 `obj_*`(apply 周期后) |
| 3 | 两侧「日志」页 | connector / ingest / apply / mcp 无持续 ERROR;launcher 无崩溃风暴 |
| 4 | 托盘「运行状态」 | 后台进程均为运行中(非标红) |
| 5 | 增量幂等 | 再等一轮 sync 后,平台 raw 行数不因重推而膨胀(upsert 幂等) |
| 6 | 只读守卫 | 确认抽取账号无写权限(尝试写操作应被 ERP 拒绝) |

平台侧数据到达 Agent 后,可用展厅同款离线链冒烟(在平台机、指向 `data\factory.sqlite`):
```bash
# 便携包 runtime 内 python,或开发机:
python -m data2agent.showroom.review_demo --db data/factory.sqlite
```
接单评审卡应能出数;数字侧仍带 draft 口径警示(见 §5)属预期,直到现场把 binding 置 `verified`。

### 4B. CLI 核对(可选)

```bash
# 平台已落地 raw
sqlite3 data/factory.sqlite \
  "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%';"
# 应出现 raw_digiwin_e10__CUSTOMER / __SALES_ORDER / ... 等

# 中间零 raw、有水位
sqlite3 data/middle.sqlite \
  "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%';"   # 应为空
sqlite3 data/middle.sqlite "SELECT source, table_name, high_water FROM d2a_sync_state;"

# 审计可查
sqlite3 data/middle.sqlite "SELECT COUNT(*) FROM d2a_audit_log;"   # > 0
```

若不用便携包自动拉起 apply/mcp,平台侧需另启:
```bash
python -m data2agent.connect apply --landing data/factory.sqlite --templates templates
python -m data2agent.mcp_server --db data/factory.sqlite --templates templates \
  --transport http --host 0.0.0.0 --port 8848
```
> `connect apply` **不接受** `--config`(纯落地库操作)。便携包会预建空 `factory.sqlite`,避免 mcp 因库文件不存在而空转重启。

---

## 5. 边界与已知缺口(本次验证跑不到的)

- **对账(E6b)未实现**:物理删除 / 不动水位的静默改动,拆机模式下无法对账修复。
  推送配置**禁用** `reconcile_at`(config 会拦)。这部分能力待 E6b 落地后由中间驱动。
- **binding 仍为 `draft`**:适配器机制已验证,但真实 E10 的表名 / 字段名 / 水位字段语义
  可能与参考表形有差异,数据正确性取决于按
  [02-extraction 附录·现场核对清单](../design/02-extraction.md) 逐 binding 核对并置 `verified`。
- **TLS 非内建**:`ingest` 明文 HTTP,TLS 需外部反代(见 §2)。

---

## 6. 常见故障排查

| 现象 | 可能原因 |
| --- | --- |
| 推送连接超时 / 拒绝 | 前置①出站未放行;`sink.url` / 平台 URL 主机端口错;平台 ingest 未监听 `0.0.0.0` |
| 平台返回 401 | 两侧接收口令(`D2A_INGEST_TOKEN`)不一致;改完未重启 connector / ingest |
| `config` 报错「推送模式下不能配 reconcile_at」 | 从中间 `connect.yaml` 移除 `reconcile_at` |
| 适配器导入失败 / ODBC 报错 | 中间缺 ODBC Driver 18(64 位);DSN 串格式;`secrets.env` 未加载 |
| 管理界面提示尚未首次配置 | 打开 `/config` 完成浏览器配置;勿只拷 `data2agent.exe` 而漏 `config\` |
| 托盘「运行状态」标红 | 60 秒内崩溃 ≥5 次已停重试;看 `data\logs\d2a-launcher.log` 与管理界面「日志」 |
| 平台有 raw、无 obj / MCP 空 | apply 未跑或周期未到;日志页看 apply;可手动触发物化 |
| `database is locked`(平台) | 已开 WAL + busy_timeout;若仍频发,检查是否有额外进程写同一落地库 |
| 抽取被 ERP 拒绝 | 只读账号无权限 / 表不在白名单 / 触发语句超时 |
