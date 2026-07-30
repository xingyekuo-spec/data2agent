"""模板包加载与校验:templates/ 目录 -> TemplatePack。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .schema import MetricDef, ObjectTemplate, TemplatePack


class TemplateLoadError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def load_pack(root: str | Path) -> TemplatePack:
    root = Path(root)
    errors: list[str] = []
    objects: list[ObjectTemplate] = []
    metrics: list[MetricDef] = []

    pack_meta: dict = {}
    pack_file = root / "pack.yaml"
    if pack_file.exists():
        pack_meta = yaml.safe_load(pack_file.read_text(encoding="utf-8")) or {}

    obj_dir = root / "objects"
    if obj_dir.is_dir():
        for f in sorted(obj_dir.glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            try:
                objects.append(ObjectTemplate(**data))
            except ValidationError as e:
                errors.append(f"{f.name}: {e}")

    metric_dir = root / "metrics"
    if metric_dir.is_dir():
        for f in sorted(metric_dir.glob("*.yaml")):
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            for m in data.get("metrics", []):
                try:
                    metrics.append(MetricDef(**m))
                except ValidationError as e:
                    errors.append(f"{f.name}: {e}")

    if errors:
        raise TemplateLoadError(errors)

    pack = TemplatePack(
        version=str(pack_meta.get("version", "0.0.0")),
        objects=objects,
        metrics=metrics,
    )
    cross = pack.cross_validate()
    if cross:
        raise TemplateLoadError(cross)
    return pack
