# local-test:本地端到端测试环境

`config/` 跟踪在 git(相对路径,任何机器可用);`data/` 为运行期生成物,不入库。

## 快速开始

```bash
python local-test/seed.py        # 重建源库 + 落地库(可重复执行)
python -m data2agent.platform.console --landing local-test/data/landing.sqlite --templates templates
```

浏览器打开 http://127.0.0.1:8849/ 即可看到已有数据的控制台。

## 重新跑一轮抽取

```bash
python -m data2agent.middle.extract sync --config local-test/config/connect.yaml
python -m data2agent.middle.extract apply --landing local-test/data/landing.sqlite --templates templates
```

## 说明

- `data/source.sqlite` 来自 `tests.fixtures.e10.seed`(E10-like 参考数据,与自动测试同源);
- `data/*.sqlite*`、`data/locks/` 全部可通过 seed.py 重建,已被 .gitignore 排除;
- `config/connect.yaml` 使用相对路径,命令须在仓库根目录执行。
