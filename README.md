# data2agent

> **Data to Agent, for factories —— 把工厂 ERP 数据安全地接出来,推给任意数据平台。**

`data2agent` 是一台装在工厂内网的**只读抽取中间机**:直连 ERP 数据库(只读账号 + 白名单表),
按水位增量 / 全量快照抽取,然后把 raw 数据**推送到你指定的任何平台**。映射、对象层与 Agent
接入由接收方平台负责,不在本仓库内。

> ✅ 首个工厂生产试点已完成验证,当前处于公开准备阶段。

## 这是什么

中小制造企业的数据(鼎捷 / 金蝶 / 用友 ERP)大多锁在没有 API 的老系统里。`data2agent` 负责最难、
最敏感的那一段——**进内网、碰数据库、把数据安全地送出去**:

- **抽取框架**:只读直连(SQLite / SQL Server)、元数据发现 + 显式抽取计划、水位 + 回看 + 复合键续跑、
  `full_refresh` 快照原子替换、错峰窗口 / 限流、全量 SQL 审计;
- **通用推送(推荐)**:`sink.type: webhook`,基于 CloudEvents 的 [d2a-push v1](docs/runbook/webhook-push.md)
  ——接收方只需实现**一个** HTTPS 端点;事件 id 幂等、至少一次投递、HMAC 签名、快照整表原子替换;
- **强一致推送**:`sink.type: http`(ingest v3),generation 屏障 + 批次回执 + E6b 跨机对账,
  能发现物理删除与原地改动,适合愿意完整实现协议的平台;
- **AI Hub 推送**:`sink.type: ai_hub`,对象级 PUSH_AGENT v1(C1-B 已落地,生产启用属 C1-C);
- **管理界面** `middle_admin`(`:8851`):**运维**(状态总览 / 运行历史 / 推送记录 / 日志)与
  **配置**三步流程(1 连接 → 2 元数据选表 → 3 抽取计划);进程监管(launcher 拉起 / 崩溃重启 / 熔断)、
  生产就绪度检查、统一认证与严格 CSP、状态库只读恢复指引(`/recovery`);
- **数据驻留边界**:中间机只持久化控制状态(水位 / 运行 / 推送记录 / 对账),**不持久化业务 Raw**;
  生产默认 `strict_stream` 严格流式不落盘 spool;
- **ERP 表结构字典**:鼎捷 E10 参考字典持续积累于 [docs/dict](docs/dict/)。

**安全承诺**:装进你内网、碰你数据库的每一行代码都在这个仓库里 —— 只读账号、白名单表、限时限流、错峰窗口,
全部可审计,可逐行核对。

## 选择推送方式

| `sink.type` | 接收方工作量 | 删除检测 | 何时用 |
| --- | --- | --- | --- |
| `webhook` | 一个 HTTPS 端点 | 仅 `full_refresh` 表 | 对接任何新平台(默认) |
| `http` | 完整 ingest v3 | 增量表也可(E6b 对账) | 需要端到端强一致 |
| `ai_hub` | AI Hub 已实现 | — | 对接 AI Hub(非生产) |

接收方实现要点与参考代码见 [webhook 接入说明](docs/runbook/webhook-push.md)。

## 现场部署

现场使用 Windows [便携包](docs/runbook/portable.md):解压 `d2a-portable-middle-<版本>.zip`,
双击 `data2agent.exe`,在 `/config` 配连接与推送地址,经 `/metadata` 扫描选表、`/tables` 确认保存
抽取计划(默认空清单,未选表不会访问 ERP 业务表)。链路验收见 [push-validation](docs/runbook/push-validation.md)。

## 代码结构

```
data2agent/
├── protocol/    # 推送契约:ingest v3 模型与版本协商、AI Hub push 信封
├── middle/      # 中间机:admin(:8851 管理界面)+ extract(只读抽取 / 调度 / 推送 sink)
└── shared/      # 公共底座:store(控制状态库 / 本地落地)、config、admin(界面公共件)
```

依赖方向仅允许 `middle → shared → protocol`(由 `scripts/check_architecture_layers.py` 强制,
契约测试见 `tests/contract/test_architecture_layers.py`)。

## 开发者本地快速开始

完整步骤见 [docs/runbook/source-dev.md](docs/runbook/source-dev.md)。本地 `sink: local` 落地仅为开发 / 参考链路径。

```bash
pip install -e ".[dev,connect,middle_admin]"
python scripts/verify.py quick                    # 日常:按 Git 变更选择测试
python scripts/verify.py module push              # 推送出口回归
python scripts/verify.py full                     # 合并前:完整回归
python -m tests.fixtures.e10.seed --db /tmp/e10.sqlite   # 生成 E10-like 参考库(测试用)
python -m data2agent.middle.extract sync --config connect.example.yaml   # 抽取:水位增量 → 推送出口
python scripts/dev_webhook_receiver.py --port 8860       # 本机 webhook 接收端(联调用)
```

## 设计文档

产品定位、架构与各组件详设见 [docs/design](docs/design/00-overview.md)
(00 总览 → 02 抽取框架 → 04 参考数据链 → 06 抽取表配置 → 08 管理界面)。

## 边界(诚实声明)

- 本仓库只包含中间机。映射 / 对象层 / 数据集发布 / MCP 网关 / 平台控制台已随平台端移出,
  由接收方平台自行实现;
- webhook 推送没有远端对账,增量表无法发现 ERP 物理删除——需要时请用 `full_refresh` 或 `http`;
- 传输层加固(HTTPS 证书 / 反代、mTLS、凭据轮换)仍是生产扩大部署前的待办。
  详见[路线图](docs/roadmap.md)。

## 贡献与安全

- 欢迎贡献其他 ERP(金蝶 / 用友)的表结构字典与适配器,见 [CONTRIBUTING](CONTRIBUTING.md);
- 安全问题请走 [SECURITY.md](SECURITY.md),勿发公开 issue;
- 小团队维护,issue 响应尽力而为(通常一周内)。

Apache-2.0 © data2agent contributors
