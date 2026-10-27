# local-test:中间机本地端到端测试环境

`config/` 跟踪在 git(相对路径,任何机器可用);`data/` 为运行期生成物,不入库。
命令须在仓库根目录执行。

## 快速开始(本地落地)

```bash
python local-test/seed.py        # 重建源库并跑一轮 sync(可重复执行)
python -m data2agent.middle.extract status --landing local-test/data/landing.sqlite
```

`config/connect.yaml` 使用 `sink: local`,raw 落在 `data/landing.sqlite`,仅供开发调试。

## 通用 webhook 推送联调

```bash
python scripts/dev_webhook_receiver.py --port 8860     # 终端 1:本机接收端
python -m data2agent.middle.extract sync --config local-test/config/connect-push.yaml   # 终端 2
```

接收端会逐条打印 `sync.begin / table.begin / batch / table.complete / sync.complete`
事件与各表行数;协议说明见 [docs/runbook/webhook-push.md](../docs/runbook/webhook-push.md)。

## 说明

- `data/source.sqlite` 来自 `tests.fixtures.e10.seed`(E10-like 参考数据,与自动测试同源);
- `data/*.sqlite*`、`data/locks/` 全部可通过 seed.py 重建,已被 .gitignore 排除。
