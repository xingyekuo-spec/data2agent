# 贡献指南

最欢迎的贡献,按价值排序:

1. **ERP binding 与表结构字典**(金蝶 KIS / 云星空、用友 T+ / 畅捷通、鼎捷 E10):
   - binding 作为新条目加进对应对象模板 `templates/objects/<对象>.yaml` 的 `bindings:` 段(按 `source` 区分,如 `digiwin_e10` / `kingdee_k3`),格式见现有 E10 binding;
   - 字典放 `docs/dict/`,**用你自己的语言描述实测行为,不要复制厂商官方文档原文,不要包含任何客户真实数据与自定义字段**;
2. 元模型与抽取框架的缺陷修复(附最小复现);
3. 文档改进。

## 规则

- 开发过程中运行 `python scripts/verify.py quick`;它会根据 `git diff` 选择受影响测试,无法可靠判断时自动回退到完整检查;
- 功能模块完成后运行 `python scripts/verify.py module <模块>`;可选模块为 `backend`、`erp`、`console`、`mcp`、`metamodel`、`scenario`、`frontend`;
- 合并前必须运行 `python scripts/verify.py full`;发布验收使用 `python scripts/verify.py release`(需要 Docker);
- 所有示例 / 测试数据必须虚构;禁止提交任何真实企业的名称、IP、凭证、单据;
- 我们是小团队,PR 审核尽力一周内完成,请谅解节奏。

Python 测试统一分为 `unit`、`contract`、`integration` 和 `slow`。日常排障也可直接运行:

```bash
python -m pytest -m "unit or contract" -q
python -m pytest --lf -q
```

完整 Python 回归由验证脚本通过 `pytest-xdist` 并行执行。设置
`D2A_PYTEST_WORKERS=2` 可限制 worker 数;遇到并发相关问题时加 `--serial`
只会串行运行 Python/前端两个任务组,pytest worker 数仍通过前述环境变量控制。
