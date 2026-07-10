"""模板元模型(薄版):定义"什么是一个合法的对象模板 / 指标定义"。

单一事实来源:连接器映射、对账工具、query_objects 参数、网关档位校验
全部消费本模块定义的结构。设计依据见 docs/03 §3.1。
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

PropertyType = Literal[
    "string", "text", "int", "decimal", "money",
    "date", "datetime", "bool", "ref", "enum",
]
Tier = Literal["看", "说", "做"]
Cardinality = Literal["1", "0..1", "1..n", "0..n"]
Domain = Literal["销售", "产品", "供应", "生产", "资源", "辅助"]


class Property(BaseModel):
    name: str
    type: PropertyType
    desc: str = ""
    ref: Optional[str] = None          # type=ref 时指向的对象名
    enum_values: list[str] = []
    sensitive: bool = False            # 默认脱敏字段(出网前置脱敏依据)

    @model_validator(mode="after")
    def check_type_extras(self) -> "Property":
        if self.type == "ref" and not self.ref:
            raise ValueError(f"属性 {self.name}: type=ref 必须声明 ref 目标对象")
        if self.type == "enum" and not self.enum_values:
            raise ValueError(f"属性 {self.name}: type=enum 必须声明 enum_values")
        return self


class Relation(BaseModel):
    name: str
    target: str
    cardinality: Cardinality = "0..n"
    desc: str = ""


class Action(BaseModel):
    """对象上允许 Agent 参与的动作;tier 是网关硬校验的治理档位。"""

    name: str
    desc: str
    tier: Tier


class SourceBinding(BaseModel):
    """源系统映射;status=verified 表示已经现场数据字典核对固化。

    约定:tables[0] 为锚表(对象一行 = 锚表一行);key_map / field_map 的值
    遵循映射表达式文法(解析器见 data2agent/mapping.py):
    "表.字段"、"表.字段 (join 锚表.外键)"、"表.字段 (map 源值→对象值 / ...)"。
    """

    source: str                        # digiwin_yifei / digiwin_e10 / excel_* ...
    tables: list[str] = []
    status: Literal["draft", "verified"] = "draft"
    key_map: dict[str, str] = {}       # 对象 key -> 源字段
    field_map: dict[str, str] = {}     # 属性 -> 源字段/表达式
    watermark: Optional[str] = None    # 增量水位字段
    notes: str = ""


class ObjectTemplate(BaseModel):
    object: str
    display_name: str
    description: str = ""
    domain: Domain
    source_of_truth: str = "ERP"
    keys: list[str]
    properties: list[Property]
    states: list[str] = []
    relations: list[Relation] = []
    actions: list[Action] = []
    bindings: list[SourceBinding] = []
    knowledge_refs: list[str] = []
    custom_field_slots: int = 20       # 客户个性化字段槽位,模板不分叉

    @model_validator(mode="after")
    def check_integrity(self) -> "ObjectTemplate":
        prop_names = [p.name for p in self.properties]
        dupes = {n for n in prop_names if prop_names.count(n) > 1}
        if dupes:
            raise ValueError(f"{self.object}: 属性名重复 {sorted(dupes)}")
        for k in self.keys:
            if k not in prop_names:
                raise ValueError(f"{self.object}: key '{k}' 不在属性列表中")
        return self


class MetricDef(BaseModel):
    metric: str
    display_name: str
    status: Literal["certified", "draft", "deprecated"] = "draft"
    formula: str
    grain: list[str]
    dimensions: list[str] = []
    caveats: str = ""
    freshness_sla: str = "T+1"

    @field_validator("metric")
    @classmethod
    def metric_id_snake_case(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", v):
            raise ValueError(f"指标 id 须为 snake_case: {v}")
        return v


class TemplatePack(BaseModel):
    version: str
    objects: list[ObjectTemplate] = []
    metrics: list[MetricDef] = []

    def object_names(self) -> set[str]:
        return {o.object for o in self.objects}

    def cross_validate(self) -> list[str]:
        """跨对象引用完整性:relation / ref 必须指向已定义对象;指标 id 唯一。"""
        errors: list[str] = []
        names = self.object_names()
        for o in self.objects:
            for r in o.relations:
                if r.target not in names:
                    errors.append(f"{o.object}.relations.{r.name}: 目标对象 {r.target} 未定义")
            for p in o.properties:
                if p.type == "ref" and p.ref not in names:
                    errors.append(f"{o.object}.{p.name}: ref 目标 {p.ref} 未定义")
        ids = [m.metric for m in self.metrics]
        for dup in sorted({i for i in ids if ids.count(i) > 1}):
            errors.append(f"指标 id 重复: {dup}")
        return errors
