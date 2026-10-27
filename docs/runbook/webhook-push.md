# 通用 Webhook 推送(d2a-push v1)接入说明

> 适用:`sink.type: webhook`。面向**接收方平台**的实现者,以及配置中间机的现场人员。
> 实现:[`webhook_sink.py`](../../data2agent/middle/extract/webhook_sink.py) ·
> 参考接收端:[`tests/support/webhook_receiver.py`](../../tests/support/webhook_receiver.py) ·
> 本机联调:[`scripts/dev_webhook_receiver.py`](../../scripts/dev_webhook_receiver.py)

## 1. 选哪种推送出口

| `sink.type` | 接收方要做什么 | 一致性 | 适用 |
| --- | --- | --- | --- |
| `webhook`(本文) | 实现**一个** HTTPS POST 端点,按事件类型处理 JSON | 至少一次 + 幂等键;全量快照整表原子替换;**无远端对账** | 对接任意数据平台 / 数据湖 / 自研服务,默认推荐 |
| `http`(ingest v3) | 实现完整 ingest v3:健康握手、generation 屏障、批次回执、E6b 对账端点 | 端到端强一致,能发现物理删除与原地改动 | 接收方愿意完整实现协议(参考实现见 `tests/support/reference_ingest.py`) |
| `ai_hub` | AI Hub PUSH_AGENT v1 | 对象级推送 | 仅 AI Hub;生产启用属 C1-C |

webhook 没有对账:增量表**发现不了 ERP 物理删除**。需要删除语义的表请配置为
`mode: full_refresh`(每轮整表快照替换),或改用 `sink.type: http`。

## 2. 中间机配置

```yaml
sources:
  digiwin_e10:
    # ... adapter / tables / windows 同其他 sink
    sink:
      type: webhook
      url: "https://data-platform.example.com/d2a/events"   # 完整端点 URL
      token_env: D2A_WEBHOOK_TOKEN          # 可选:Authorization: Bearer <token>
      hmac_secret_env: D2A_WEBHOOK_SECRET   # 可选:HMAC-SHA256 签名(推荐)
      timeout_seconds: 30
      retries: 3
      # ca_bundle: C:\d2a\config\ca.pem     # 私有 CA 时配置
    spool: { policy: strict_stream }
    # 不得配置 reconcile_at / reconcile_deep_at(加载即报错)
```

规则:

- 非本机回环地址必须 HTTPS(受控开发环境可显式 `allow_insecure_http: true`);
- 非本机地址必须至少配置 `token_env` 或 `hmac_secret_env` 之一(开发环境可显式 `allow_unauthenticated: true`);
- `deployment_mode: production` 允许 `webhook` 与 `http`,但 `url` 不得指向本机回环;
- 凭据只写在 `secrets.env` / 环境变量,配置文件只放变量名。

也可以不手写 YAML:首配向导第 1 步或 `/config`「推送目标」页签选择「通用 Webhook」,填写端点 URL、
Bearer Token 与 / 或 HMAC 签名密钥。凭据写入 `secrets.env` 的 `D2A_WEBHOOK_TOKEN` /
`D2A_WEBHOOK_SECRET`,YAML 只记录变量名;切换时自动清除对账计划。

管理页「测试接收端连通」会发送一条 `ping` 事件,接收端返回 2xx 即视为可达且鉴权通过。

## 3. 传输约定

- 方法与路径:`POST <url>`,每个请求一条事件;
- `Content-Type: application/cloudevents+json; charset=utf-8`(CloudEvents 1.0 结构化模式);
- `Idempotency-Key: <事件 id>`,与 body 中 `id` 相同;
- 鉴权(按配置出现):`Authorization: Bearer <token>`;
  `X-D2A-Signature: t=<unix 秒>,v1=<hex>`,其中
  `hex = HMAC-SHA256(secret, "<t>." + 原始 body 字节)`;
- 接收方返回任意 **2xx** 即确认,响应体可为空;
- 中间机对 408 / 425 / 429 / 5xx / 网络错误按指数退避重试(1s、2s、4s… 上限 10s),
  **重试复用同一 id 与 body**(签名时间戳每次更新);其他 4xx 视为确定性拒绝,不重试,本轮失败;
- 串行按序投递:上一事件确认后才发送下一事件。

## 4. 事件信封

```json
{
  "specversion": "1.0",
  "id": "sync-6f0c…:7d2e91a4b3c0-0",
  "source": "data2agent/digiwin_e10",
  "type": "io.data2agent.push.v1.batch",
  "subject": "SALES_ORDER",
  "time": "2026-10-27T02:00:13+00:00",
  "datacontenttype": "application/json",
  "d2apushversion": "1",
  "data": { "…": "见下文" }
}
```

`id` 全局唯一,以本轮 `sync_id` 开头;接收方应**长期**按 `id` 去重(至少保留 7 天)。

## 5. 事件类型与顺序

```
sync.begin
  ( table.begin → batch* → table.complete | table.abort )*
sync.complete | sync.abort
```

| `type` 后缀 | `data` 字段 | 接收方处理 |
| --- | --- | --- |
| `sync.begin` | `sync_id`, `tables`(本轮表清单) | 记录一轮开始,可选 |
| `table.begin` | 表描述(见下) | 全量模式:为 `snapshot_id` 新建 staging |
| `batch` | 表描述 + `table_run_id`, `batch_id`, `row_count`, `rows`, `binary_encoding` | 增量:按 `pk` upsert,立即生效;全量:写入 staging |
| `table.complete` | 表描述 + `table_run_id`, `rows`, `batches` | 全量:核对 staging 行数 / 批次数一致后**原子替换**整表;增量:可核对批次数 |
| `table.abort` | 表描述 | 丢弃该 `snapshot_id` 的 staging(仅全量;增量批次已生效) |
| `sync.complete` / `sync.abort` | `sync_id` | 一轮结束;abort 表示窗口越界暂停或失败,下一轮会从水位续跑 |
| `ping` | `{}` | 连通测试,直接 2xx |

表描述字段:

| 字段 | 说明 |
| --- | --- |
| `sync_id` | 本轮标识(`sync-<uuid>`) |
| `table` / `schema` | 源表名与 schema |
| `mode` | `incremental` 或 `full_refresh` |
| `columns` | `[[列名, 类型], …]`,类型 ∈ `int` / `real` / `text` / `blob` |
| `pk` | 运行键列(增量必有;全量可能为空,此时**不得去重**,快照按原样整表替换,重复行计入行数) |
| `snapshot_id` | 全量快照标识;增量为 `null` |

值的表示:日期时间为 ISO 文本(带时区的统一转 UTC),Decimal 为定点文本;
`blob` 列编码为 `{"$d2a_base64": "<base64>"}`(`binary_encoding: "base64-v1"`)。
传输完整性以 HMAC 签名(覆盖原始 body 字节)为准。

## 6. 接收方实现清单

1. 校验 Bearer Token 与 / 或 HMAC 签名,签名时间戳偏差超过 5 分钟拒绝(防重放);
   签名覆盖原始 body 字节,即完整性校验;
2. 以 `id` 幂等:已处理过的 `id` 直接返回 2xx,不重复生效;
3. 在**持久化完成后**才返回 2xx;处理失败返回 5xx 让中间机重试,数据错误返回 4xx;
4. 全量快照只在 `table.complete` 且核对一致时替换,`table.abort` 或核对失败时保留上一版;
5. 不要假设同一张表每轮都出现:窗口越界时本轮会 `sync.abort`,剩余表下一轮继续。

参考实现(约 150 行,含以上全部要点)见
[`tests/support/webhook_receiver.py`](../../tests/support/webhook_receiver.py);
签名校验可直接参考 `data2agent.middle.extract.webhook_sink.verify_signature`。

## 7. 本机联调

```bash
python scripts/dev_webhook_receiver.py --port 8860 --secret dev-secret
```

`local-test/config/connect-push.yaml` 已指向 `http://127.0.0.1:8860/events`;若启用了 `--secret`,
在配置中加 `hmac_secret_env` 并导出同名环境变量后执行:

```bash
python -m data2agent.middle.extract sync --config local-test/config/connect-push.yaml
```
