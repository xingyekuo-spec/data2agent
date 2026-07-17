# 现场验证 Runbook · ERP → 中间服务器 → 数据平台(推送链路)

> 目标:验证 Pattern A 拆机部署的**同步链路** —— 中间服务器只读抽取生产 ERP,
> 出站推送 raw 批次到数据平台落地。对应设计 [02-extraction §12](../design/02-extraction.md)。
>
> 适用范围:E6a(推送 sink)已实现。**本 runbook 只验同步,不含对账**
> (E6b 跨机对账未实现,见 §5 边界)。
>
> 本文档讲协议层(拓扑/配置/验收);两台机若是 **Windows**,进程托管与离线分发
> 的具体命令见 [windows-deploy.md](windows-deploy.md)。
>
> **推荐现场形态:[便携包解压即用](portable.md)** —— 两台机各双击 `data2agent.exe`,
> 浏览器完成首次配置(平台填「接收口令」,中间机填 ERP 连接与平台地址),
> 之后自动拉起后台进程(中间机 connector;平台 ingest + apply + mcp)。
> 本文的手工命令是便携包内部等价流程,用于理解协议、验收与排障。

---

## 1. 拓扑与数据流

```
生产 ERP(内网,SQL Server / 鼎捷 E10)
   │ 只读账号:仅 SELECT / 白名单 / 限流 / 逐条 SQL 审计
   ▼
中间服务器(薄):connect serve + sink=http    ← 持有 ERP 只读凭据;本地只留水位/审计,不落 raw
   │ 出站推送 raw 批次(POST /ingest/batch)
   ▼
数据平台:python -m data2agent.ingest           ← 落地 raw_*(→ 平台侧 apply → MCP)
```

**连接发起方向 = 中间 → 平台(出站)**。这是当前代码的唯一实现。
若客户网络只允许平台→中间方向,推送模式跑不通(需另建拉取实现,尚不存在)。

---

## 2. 前置确认(进厂前,书面与 IT 敲定)

| # | 确认项 | 不满足的后果 |
| --- | --- | --- |
| ① | **中间 → 平台的出站已放行**(到平台 ingest 端口,默认 8850) | 推送直接连不上,链路不通 |
| ② | 生产 ERP 只对**中间服务器**授只读账号 + 白名单表 | 抽取被拒 / 违反安全承诺 |
| ③ | 中间服务器可跑常驻进程(`connect serve`) | 无法持续同步 |
| ④ | 中间已装 ODBC Driver 18 for SQL Server + `pip install -e ".[connect]"` | 适配器导入失败 |
| ⑤ | 平台已装 `pip install -e ".[ingest]"` | 接收端起不来 |
| ⑥ | 允许的抽取窗口与限流上限(与 IT 书面确认) | 影响 `windows` / `rate` 配置 |

> 传输安全:`ingest` 自身跑明文 HTTP。验证阶段在**内网可信段**用「明文 + Bearer Token」可接受;
> 需要 TLS 时在平台侧加反向代理(nginx / caddy)终止 TLS,再把 `sink.url` 指向它。

---

## 3. 部署与启动

### 3.1 平台侧(数据平台)

```bash
export D2A_INGEST_TOKEN='<生成一个随机长串>'
python -m data2agent.ingest \
  --landing /data/factory.sqlite \
  --host 0.0.0.0 --port 8850
# 启动日志应显示 "Token 认证;落地 /data/factory.sqlite"
```

健康检查(平台本机):
```bash
curl -s http://127.0.0.1:8850/ingest/health   # {"ok": true, ...}
```

### 3.2 中间侧(中间服务器)`connect.yaml`

```yaml
templates: templates
landing: state/middle.sqlite          # 只存水位/审计,不落 raw
sources:
  digiwin_e10:
    adapter: mssql_readonly
    dsn_env: D2A_E10_DSN               # 连接串只从环境变量读,绝不落文件
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

确认无误后去掉 `--once` 常驻运行。

---

## 4. 验收检查

一轮 `--once` 后逐项核对:

1. **平台已落地 raw**
   ```bash
   sqlite3 /data/factory.sqlite \
     "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%';"
   # 应出现 raw_digiwin_e10__CUSTOMER / __SALES_ORDER / ... 等
   ```
2. **行数合理**(与 ERP 侧 `SELECT COUNT(*)` 抽样比对若干表)。
3. **中间零 raw、有水位**
   ```bash
   sqlite3 state/middle.sqlite \
     "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%';"   # 应为空
   sqlite3 state/middle.sqlite "SELECT source, table_name, high_water FROM d2a_sync_state;"  # 有水位
   ```
4. **审计可查**:中间 `state/middle.sqlite` 的 `d2a_audit_log` 每条源 SQL 有记录(语句 / 行数 / 耗时)。
5. **增量幂等**:再跑一轮 `--once`,平台行数不变(回看窗口内只重推增量,upsert 幂等)。
6. **只读守卫**:确认抽取账号无写权限(尝试写操作应被 ERP 拒绝)。

平台侧要让数据到达 MCP 网关,另需在**平台**跑物化与网关(便携包 `data2agent.exe` 会自动拉起这两个):
```bash
# apply 不接受 --config(纯落地库操作),须给 --landing / --templates:
python -m data2agent.connect apply --landing /data/factory.sqlite --templates templates
# mcp 启动要求落地库已存在;首个批次到达前 /data/factory.sqlite 可能还没生成:
python -m data2agent.mcp_server --db /data/factory.sqlite --templates templates \
  --transport http --host 0.0.0.0 --port 8848
```

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
| 推送连接超时 / 拒绝 | 前置①出站未放行;`sink.url` 主机/端口错;平台 ingest 未监听 `0.0.0.0` |
| 平台返回 401 | 两侧 `D2A_INGEST_TOKEN` 不一致 |
| `config` 报错「推送模式下不能配 reconcile_at」 | 从中间 `connect.yaml` 移除 `reconcile_at` |
| 适配器导入失败 / ODBC 报错 | 中间缺 `.[connect]` 或 ODBC Driver 18;DSN 串格式 |
| `database is locked`(平台) | 已开 WAL + busy_timeout;若仍频发,检查是否有额外进程写同一落地库 |
| 抽取被 ERP 拒绝 | 只读账号无权限 / 表不在白名单 / 触发语句超时 |
