# 贡献指南

最欢迎的贡献,按价值排序:

1. **ERP binding 与表结构字典**(金蝶 KIS / 云星空、用友 T+ / 畅捷通、鼎捷 E10):
   - binding 作为新条目加进对应对象模板 `templates/objects/<对象>.yaml` 的 `bindings:` 段(按 `source` 区分,如 `digiwin_e10` / `kingdee_k3`),格式见现有 E10 binding;
   - 字典放 `docs/dict/`,**用你自己的语言描述实测行为,不要复制厂商官方文档原文,不要包含任何客户真实数据与自定义字段**;
2. 元模型与抽取框架的缺陷修复(附最小复现);
3. 文档改进。

## 规则

- 提交前:`pytest tests -q` 全绿 + `python -m data2agent.metamodel.validate templates` 通过;
- 所有示例 / 测试数据必须虚构;禁止提交任何真实企业的名称、IP、凭证、单据;
- 我们是小团队,PR 审核尽力一周内完成,请谅解节奏。
