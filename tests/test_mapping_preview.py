"""M3-T04: Preview 分析编排 — 聚合、diff、脱敏、草稿预检(无 HTTP)。"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from data2agent.connect.adapters.base import TableInfo
from data2agent.connect.landing import LandingStore
from data2agent.connect.mapping_preview import (
    MASKED,
    SAMPLE_DUPLICATE_WARNING,
    PreviewError,
    preview_mapping,
)
from data2agent.connect.mapping_transform import (
    DEFAULT_BREAKER_THRESHOLD,
    evaluate_object_rows,
    transform_object_rows,
    would_trip_breaker,
)
from data2agent.mapping import parse_field_expr
from data2agent.metamodel.schema import (
    DeriveRule,
    DerivedField,
    ObjectTemplate,
    Property,
    SourceBinding,
    TemplatePack,
)
from data2agent.metamodel.versioning import binding_hash

SOURCE = "demo_src"
ANCHOR = "ITEM"


def _pack(
    *,
    bindings: list[SourceBinding] | None = None,
    properties: list[Property] | None = None,
    keys: list[str] | None = None,
    derived: dict | None = None,
    extra_objects: list[ObjectTemplate] | None = None,
) -> TemplatePack:
    props = properties or [
        Property(name="code", type="string"),
        Property(name="name", type="string"),
        Property(name="status", type="enum", enum_values=["open", "closed"]),
        Property(name="phase", type="enum", enum_values=["A", "B", "C"]),
        Property(name="secret", type="string", sensitive=True),
    ]
    binding = (bindings or [None])[0]
    if bindings is None:
        binding = SourceBinding(
            source=SOURCE,
            tables=[ANCHOR],
            status="verified",
            key_map={"code": f"{ANCHOR}.CODE"},
            field_map={
                "code": f"{ANCHOR}.CODE",
                "name": f"{ANCHOR}.NAME",
                "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed)",
                "secret": f"{ANCHOR}.SECRET",
            },
            derived=derived or {},
        )
        bindings = [binding]
    tpl = ObjectTemplate(
        object="Item",
        display_name="物料",
        domain="辅助",
        keys=keys or ["code"],
        properties=props,
        bindings=bindings,
    )
    objects = [tpl, *(extra_objects or [])]
    return TemplatePack(version="0.3-test", objects=objects)


def _item_info() -> TableInfo:
    return TableInfo(
        name=ANCHOR,
        columns=[
            ("Id", "int"),
            ("CODE", "text"),
            ("NAME", "text"),
            ("STATUS", "text"),
            ("SECRET", "text"),
            ("ST", "text"),
        ],
        pk=["Id"],
    )


def _seed(tmp_path: Path, rows: list[dict], *, batch_id: str = "b1") -> LandingStore:
    landing = LandingStore(tmp_path / "landing.sqlite")
    info = _item_info()
    landing.ensure_raw_table(SOURCE, info)
    landing.upsert_rows(SOURCE, info, rows, batch_id)
    return landing


def _default_rows() -> list[dict]:
    return [
        {
            "Id": 1,
            "CODE": "A",
            "NAME": "alpha",
            "STATUS": "1",
            "SECRET": "s1",
            "ST": "X",
        },
        {
            "Id": 2,
            "CODE": "B",
            "NAME": "beta",
            "STATUS": "9",
            "SECRET": "s2",
            "ST": "Y",
        },
        {
            "Id": 3,
            "CODE": "C",
            "NAME": "gamma",
            "STATUS": "2",
            "SECRET": "s3",
            "ST": "Z",
        },
    ]


def test_preview_rows_match_evaluate_object_rows(tmp_path):
    pack = _pack()
    landing = _seed(tmp_path, _default_rows())
    tpl = pack.objects[0]
    binding = tpl.bindings[0]

    result = preview_mapping(
        landing, pack, object_name="Item", source=SOURCE, limit=50,
    )
    assert result.mode == "current"
    assert result.current is not None
    assert result.current.summary.total == 3
    assert result.candidate.summary.total == 3
    assert result.current.summary.mapped == result.candidate.summary.mapped
    assert result.current.summary.quarantined == 1  # STATUS=9 unmapped

    from data2agent.connect.mapping_preview import (
        _run_binding_eval,
        freeze_sample,
        preview_read_tx,
    )

    with preview_read_tx(landing) as store:
        sample = freeze_sample(
            store, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50,
        )
        core = _run_binding_eval(store, tpl, binding, sample, source=SOURCE)

    assert core.total == result.candidate.summary.total
    assert core.mapped == result.candidate.summary.mapped
    assert core.quarantined == result.candidate.summary.quarantined
    for row_view, row_eval in zip(result.candidate.rows, core.rows):
        assert row_view.status == row_eval.status
        if row_eval.issues:
            assert row_view.issues[0].reason_code == row_eval.issues[0].reason_code


def test_enum_gaps_and_sensitive_masking(tmp_path):
    pack = _pack(
        properties=[
            Property(name="code", type="string"),
            Property(name="status", type="enum", enum_values=["open", "closed"], sensitive=True),
            Property(name="secret", type="string", sensitive=True),
        ],
        bindings=[
            SourceBinding(
                source=SOURCE,
                tables=[ANCHOR],
                status="verified",
                key_map={"code": f"{ANCHOR}.CODE"},
                field_map={
                    "code": f"{ANCHOR}.CODE",
                    "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed)",
                    "secret": f"{ANCHOR}.SECRET",
                },
            ),
        ],
    )
    landing = _seed(
        tmp_path,
        [
            {"Id": 1, "CODE": "A", "NAME": "a", "STATUS": "9", "SECRET": "top", "ST": "X"},
            {"Id": 2, "CODE": "B", "NAME": "b", "STATUS": "9", "SECRET": "sec", "ST": "X"},
            {"Id": 3, "CODE": "C", "NAME": "c", "STATUS": "8", "SECRET": "x", "ST": "X"},
        ],
    )
    result = preview_mapping(landing, pack, object_name="Item", source=SOURCE)
    gaps = {(g.field, g.source_value, g.count) for g in result.candidate.enum_gaps}
    assert ("status", MASKED, 2) in gaps
    assert ("status", MASKED, 1) in gaps
    assert sum(g.count for g in result.candidate.enum_gaps if g.field == "status") == 3
    for row in result.candidate.rows:
        if row.output:
            assert row.output.get("secret") == MASKED or "secret" not in row.output
        for issue in row.issues:
            if issue.field == "status":
                assert issue.source_value == MASKED
                assert "9" not in issue.detail and "8" not in issue.detail


def test_derived_coverage_identity_and_hits(tmp_path):
    derived = {
        "phase": DerivedField(
            rules=[
                DeriveRule(when={"ST": "X"}, value="A"),
                DeriveRule(when={"ST": "Y"}, value="B"),
            ],
            default="C",
        ),
    }
    pack = _pack(derived=derived)
    # 覆盖 field_map 不含 phase(由 derived 填)
    pack.objects[0].bindings[0].field_map = {
        "code": f"{ANCHOR}.CODE",
        "name": f"{ANCHOR}.NAME",
        "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed)",
        "secret": f"{ANCHOR}.SECRET",
    }
    landing = _seed(
        tmp_path,
        [
            {"Id": 1, "CODE": "A", "NAME": "a", "STATUS": "1", "SECRET": "s", "ST": "X"},
            {"Id": 2, "CODE": "B", "NAME": "b", "STATUS": "1", "SECRET": "s", "ST": "Y"},
            {"Id": 3, "CODE": "C", "NAME": "c", "STATUS": "1", "SECRET": "s", "ST": "Z"},
            {"Id": 4, "CODE": "D", "NAME": "d", "STATUS": "9", "SECRET": "s", "ST": "X"},
        ],
    )
    result = preview_mapping(landing, pack, object_name="Item", source=SOURCE)
    cov = {c.field: c for c in result.candidate.derived_coverage}
    assert "phase" in cov
    c = cov["phase"]
    # STATUS=9 行在 derived 前隔离 → 不计入 eligible
    assert c.eligible_rows == 3
    assert c.matched_rows == 2
    assert c.default_hits == 1
    assert c.unmatched_rows == 0
    assert c.matched_rows + c.default_hits + c.unmatched_rows == c.eligible_rows
    assert c.row_coverage == pytest.approx(1.0)
    assert c.rules_total == 2
    assert c.rules_hit == 2
    assert c.rules[0].hit_count == 1
    assert c.rules[1].hit_count == 1

    # unmatched + null coverage when no eligible
    pack2 = _pack(
        derived={
            "phase": DerivedField(rules=[DeriveRule(when={"ST": "X"}, value="A")]),
        },
    )
    pack2.objects[0].bindings[0].field_map = {
        "code": f"{ANCHOR}.CODE",
        "name": f"{ANCHOR}.NAME",
        "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed)",
        "secret": f"{ANCHOR}.SECRET",
    }
    landing2 = _seed(
        tmp_path / "u",
        [{"Id": 1, "CODE": "A", "NAME": "a", "STATUS": "1", "SECRET": "s", "ST": "Z"}],
    )
    r2 = preview_mapping(landing2, pack2, object_name="Item", source=SOURCE)
    c2 = r2.candidate.derived_coverage[0]
    assert c2.unmatched_rows == 1
    assert c2.matched_rows == 0
    assert c2.default_hits == 0
    assert c2.eligible_rows == 1
    assert c2.row_coverage == pytest.approx(0.0)

    # empty eligible → row_coverage null
    landing3 = _seed(tmp_path / "e", [])
    r3 = preview_mapping(landing3, pack2, object_name="Item", source=SOURCE)
    c3 = r3.candidate.derived_coverage[0]
    assert c3.eligible_rows == 0
    assert c3.row_coverage is None


def test_business_key_sample_scope_and_warning(tmp_path):
    pack = _pack()
    landing = _seed(
        tmp_path,
        [
            {"Id": 1, "CODE": "A", "NAME": "a", "STATUS": "1", "SECRET": "s", "ST": "X"},
            {"Id": 2, "CODE": "A", "NAME": "a2", "STATUS": "1", "SECRET": "s", "ST": "X"},
            {"Id": 3, "CODE": None, "NAME": "n", "STATUS": "1", "SECRET": "s", "ST": "X"},
        ],
    )
    # CODE None 需要允许 — upsert 用空串? 用 SQL 直接改
    result = preview_mapping(landing, pack, object_name="Item", source=SOURCE)
    # 先确认 duplicate;missing 另测
    assert result.candidate.business_key_issues.duplicate >= 1
    assert result.candidate.business_key_issues.scope == "sample"
    assert SAMPLE_DUPLICATE_WARNING in result.warnings

    landing.con.execute(
        'UPDATE "raw_demo_src__ITEM" SET "CODE" = NULL WHERE "Id" = 3'
    )
    landing.con.commit()
    result2 = preview_mapping(landing, pack, object_name="Item", source=SOURCE)
    assert result2.candidate.business_key_issues.missing == 1
    assert result2.candidate.business_key_issues.duplicate == 1


def test_would_trip_breaker_edges(tmp_path):
    # 1/20 == 0.05 → False; 2/20 → True
    rows = [
        {
            "Id": i,
            "CODE": f"C{i}",
            "NAME": "n",
            "STATUS": "9" if i <= 2 else "1",
            "SECRET": "s",
            "ST": "X",
        }
        for i in range(1, 21)
    ]
    pack = _pack()
    landing = _seed(tmp_path, rows)
    # only first quarantined → rate 1/20
    rows1 = copy.deepcopy(rows)
    for r in rows1:
        r["STATUS"] = "1"
    rows1[0]["STATUS"] = "9"
    landing1 = _seed(tmp_path / "t1", rows1)
    r1 = preview_mapping(landing1, pack, object_name="Item", source=SOURCE)
    assert r1.candidate.summary.quarantined == 1
    assert r1.candidate.summary.total == 20
    assert r1.candidate.summary.would_trip_breaker is False
    assert would_trip_breaker(1, 20, DEFAULT_BREAKER_THRESHOLD) is False

    landing2 = _seed(tmp_path / "t2", rows)
    r2 = preview_mapping(landing2, pack, object_name="Item", source=SOURCE)
    assert r2.candidate.summary.quarantined == 2
    assert r2.candidate.summary.would_trip_breaker is True


def test_diff_available_and_unavailable(tmp_path):
    pack = _pack()
    landing = _seed(tmp_path, _default_rows())

    # current vs draft map change
    draft = {
        "tables": [ANCHOR],
        "key_map": {"code": f"{ANCHOR}.CODE"},
        "field_map": {
            "code": f"{ANCHOR}.CODE",
            "name": f"{ANCHOR}.NAME",
            "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed / 9→open)",
            "secret": f"{ANCHOR}.SECRET",
        },
        "derived": {},
        "watermark": None,
        "notes": "",
    }
    result = preview_mapping(
        landing, pack, object_name="Item", source=SOURCE, draft_binding=draft,
    )
    assert result.mode == "draft"
    assert result.diff.state == "available"
    assert result.diff.reason is None
    assert result.diff.summary.rows_changed >= 1
    assert result.diff.summary.status_changed >= 1
    assert result.current is not None
    assert result.current.summary.quarantined == 1
    assert result.candidate.summary.quarantined == 0

    # no current → unavailable
    pack_no = TemplatePack(
        version="0.3-test",
        objects=[
            ObjectTemplate(
                object="Item",
                display_name="物料",
                domain="辅助",
                keys=["code"],
                properties=[
                    Property(name="code", type="string"),
                    Property(name="name", type="string"),
                ],
                bindings=[
                    # whitelist table via another object's binding
                ],
            ),
            ObjectTemplate(
                object="Other",
                display_name="其它",
                domain="辅助",
                keys=["code"],
                properties=[Property(name="code", type="string")],
                bindings=[
                    SourceBinding(
                        source=SOURCE,
                        tables=[ANCHOR],
                        status="verified",
                        field_map={"code": f"{ANCHOR}.CODE"},
                    ),
                ],
            ),
        ],
    )
    draft2 = {
        "tables": [ANCHOR],
        "key_map": {"code": f"{ANCHOR}.CODE"},
        "field_map": {
            "code": f"{ANCHOR}.CODE",
            "name": f"{ANCHOR}.NAME",
        },
        "derived": {},
        "notes": "",
    }
    result2 = preview_mapping(
        landing, pack_no, object_name="Item", source=SOURCE, draft_binding=draft2,
    )
    assert result2.current is None
    assert result2.current_binding_hash is None
    assert result2.diff.state == "unavailable"
    assert result2.diff.reason == "no_current_binding"
    assert result2.diff.summary.rows_changed == 0


def test_masking_output_issues_diff_draft_cannot_unmask(tmp_path):
    pack = _pack()
    landing = _seed(tmp_path, _default_rows())
    draft = {
        "tables": [ANCHOR],
        "key_map": {"code": f"{ANCHOR}.CODE"},
        "field_map": {
            "code": f"{ANCHOR}.CODE",
            "name": f"{ANCHOR}.NAME",
            "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed / 9→open)",
            "secret": f"{ANCHOR}.SECRET",
        },
        "derived": {},
        "notes": "",
    }
    result = preview_mapping(
        landing, pack, object_name="Item", source=SOURCE, draft_binding=draft,
    )
    for side in (result.current, result.candidate):
        assert side is not None
        for row in side.rows:
            if row.status == "mapped":
                assert row.output.get("secret") == MASKED

    for drow in result.diff.rows:
        for f in drow.fields:
            if f.field == "secret":
                if f.before is not None:
                    assert f.before == MASKED
                if f.after is not None:
                    assert f.after == MASKED
                assert f.before != "s1" and f.after != "s1"


def test_draft_remap_sensitive_or_unknown_col_onto_nonsensitive_is_masked(tmp_path):
    """草稿把敏感/未分类 raw 列映到非敏感属性时仍须遮罩,不能降敏。"""
    pack = _pack()
    info = _item_info()
    # 追加未分类列 PHONE(不在 current field_map)
    info = TableInfo(
        name=ANCHOR,
        columns=[*info.columns, ("PHONE", "text")],
        pk=list(info.pk),
    )
    landing = LandingStore(tmp_path / "landing.sqlite")
    landing.ensure_raw_table(SOURCE, info)
    rows = []
    for row in _default_rows():
        rows.append({**row, "PHONE": f"phone-{row['CODE']}"})
    landing.upsert_rows(SOURCE, info, rows, "b1")

    # 1) 敏感列 SECRET → name
    draft_secret = {
        "tables": [ANCHOR],
        "key_map": {"code": f"{ANCHOR}.CODE"},
        "field_map": {
            "code": f"{ANCHOR}.CODE",
            "name": f"{ANCHOR}.SECRET",
            "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed / 9→open)",
            "secret": f"{ANCHOR}.SECRET",
        },
        "derived": {},
        "notes": "",
    }
    result = preview_mapping(
        landing, pack, object_name="Item", source=SOURCE, draft_binding=draft_secret,
    )
    for row in result.candidate.rows:
        if row.status == "mapped":
            assert row.output.get("name") == MASKED
            assert row.output.get("secret") == MASKED
            assert row.output.get("name") != "s1"
    for drow in result.diff.rows:
        for f in drow.fields:
            if f.field == "name" and f.after is not None:
                assert f.after == MASKED

    # 2) 未分类列 PHONE → name
    draft_phone = {
        "tables": [ANCHOR],
        "key_map": {"code": f"{ANCHOR}.CODE"},
        "field_map": {
            "code": f"{ANCHOR}.CODE",
            "name": f"{ANCHOR}.PHONE",
            "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed / 9→open)",
            "secret": f"{ANCHOR}.SECRET",
        },
        "derived": {},
        "notes": "",
    }
    result2 = preview_mapping(
        landing, pack, object_name="Item", source=SOURCE, draft_binding=draft_phone,
    )
    for row in result2.candidate.rows:
        if row.status == "mapped":
            assert row.output.get("name") == MASKED
            assert not str(row.output.get("name", "")).startswith("phone-")


def test_derived_unmatched_does_not_leak_raw_cols_or_values(tmp_path):
    """derived_unmatched 不得把 raw 列名/敏感原值打进 detail/source_value。"""
    pack = _pack(
        derived={
            "phase": DerivedField(
                rules=[DeriveRule(when={"ST": "MATCH_NEVER"}, value="A")],
                default=None,
            ),
        },
    )
    # current field_map 不含 phase(由 derived 填);ST 未出现在 field_map → 未分类
    pack.objects[0].bindings[0].field_map = {
        "code": f"{ANCHOR}.CODE",
        "name": f"{ANCHOR}.NAME",
        "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed)",
        "secret": f"{ANCHOR}.SECRET",
    }
    pack.objects[0].bindings[0].derived = {
        "phase": DerivedField(
            rules=[DeriveRule(when={"SECRET": "__no_match__"}, value="A")],
            default=None,
        ),
    }
    landing = _seed(tmp_path, _default_rows())
    result = preview_mapping(landing, pack, object_name="Item", source=SOURCE)
    blob_parts: list[str] = []
    for row in result.candidate.rows:
        for issue in row.issues:
            if issue.reason_code != "derived_unmatched":
                continue
            assert "SECRET" not in (issue.detail or "")
            assert "s1" not in (issue.detail or "")
            assert issue.source_value is None or "s1" not in issue.source_value
            assert issue.source_value is None or "SECRET" not in issue.source_value
            blob_parts.append(issue.detail or "")
            blob_parts.append(issue.source_value or "")
    assert any("派生规则无匹配" in p for p in blob_parts)


def test_empty_sample_success_zeros(tmp_path):
    pack = _pack()
    landing = _seed(tmp_path, [])
    result = preview_mapping(landing, pack, object_name="Item", source=SOURCE)
    assert result.candidate.summary.total == 0
    assert result.candidate.summary.mapped == 0
    assert result.candidate.summary.quarantined == 0
    assert result.candidate.summary.quarantine_rate == 0.0
    assert result.candidate.summary.would_trip_breaker is False
    assert result.candidate.rows == []
    assert result.candidate.enum_gaps == []
    assert result.candidate.business_key_issues.missing == 0
    assert result.sample.sampled_rows == 0
    assert result.diff.state == "available"
    assert result.diff.summary.rows_changed == 0


def test_draft_does_not_persist(tmp_path):
    pack = _pack()
    original = copy.deepcopy(pack.model_dump())
    landing = _seed(tmp_path, _default_rows())
    draft = {
        "tables": [ANCHOR],
        "key_map": {"code": f"{ANCHOR}.CODE"},
        "field_map": {
            "code": f"{ANCHOR}.CODE",
            "name": f"{ANCHOR}.NAME",
            "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed / 9→open)",
            "secret": f"{ANCHOR}.SECRET",
        },
        "derived": {},
        "notes": "temp",
    }
    preview_mapping(
        landing, pack, object_name="Item", source=SOURCE, draft_binding=draft,
    )
    assert pack.model_dump() == original
    # binding hash of template current unchanged
    assert binding_hash(pack.objects[0].bindings[0]) == binding_hash(
        SourceBinding.model_validate(original["objects"][0]["bindings"][0])
    )


def test_preview_errors_object_source_anchor_draft(tmp_path):
    pack = _pack()
    landing = _seed(tmp_path, _default_rows())

    with pytest.raises(PreviewError) as ei:
        preview_mapping(landing, pack, object_name="Nope", source=SOURCE)
    assert ei.value.reason_code == "object_not_found"

    with pytest.raises(PreviewError) as ei:
        preview_mapping(landing, pack, object_name="Item", source="missing_src")
    assert ei.value.reason_code == "source_not_found"

    pack2 = TemplatePack(
        version="x",
        objects=[
            ObjectTemplate(
                object="Item",
                display_name="物料",
                domain="辅助",
                keys=["code"],
                properties=[Property(name="code", type="string")],
                bindings=[],
            ),
            ObjectTemplate(
                object="Other",
                display_name="其它",
                domain="辅助",
                keys=["code"],
                properties=[Property(name="code", type="string")],
                bindings=[
                    SourceBinding(
                        source=SOURCE,
                        tables=[ANCHOR],
                        status="verified",
                        field_map={"code": f"{ANCHOR}.CODE"},
                    ),
                ],
            ),
        ],
    )
    with pytest.raises(PreviewError) as ei:
        preview_mapping(landing, pack2, object_name="Item", source=SOURCE)
    assert ei.value.reason_code == "current_binding_unavailable"

    with pytest.raises(PreviewError) as ei:
        preview_mapping(
            landing,
            pack,
            object_name="Item",
            source=SOURCE,
            draft_binding={
                "tables": ["OTHER_TBL"],
                "field_map": {"code": "OTHER_TBL.CODE"},
            },
        )
    assert ei.value.reason_code in ("draft_invalid", "anchor_changed")

    with pytest.raises(PreviewError) as ei:
        preview_mapping(
            landing,
            pack,
            object_name="Item",
            source=SOURCE,
            draft_binding={
                "tables": ["NOT_IN_WHITELIST"],
                "field_map": {"code": f"{ANCHOR}.CODE"},
            },
        )
    assert ei.value.reason_code == "draft_invalid"


def test_summary_identity(tmp_path):
    pack = _pack()
    landing = _seed(tmp_path, _default_rows())
    result = preview_mapping(landing, pack, object_name="Item", source=SOURCE)
    s = result.candidate.summary
    assert s.mapped + s.quarantined == s.total
    assert s.quarantine_rate == pytest.approx(s.quarantined / s.total)


def test_transform_wrapper_still_matches_after_derived_hits(tmp_path):
    """回归:derived hit 元数据不得改变隔离文案。"""
    del tmp_path
    pack = _pack(
        derived={
            "phase": DerivedField(
                rules=[DeriveRule(when={"ST": "X"}, value="A")],
                default="C",
            ),
        },
    )
    tpl = pack.objects[0]
    binding = tpl.bindings[0]
    binding.field_map = {
        "code": f"{ANCHOR}.CODE",
        "status": f"{ANCHOR}.STATUS (map 1→open / 2→closed)",
    }
    exprs = {p: parse_field_expr(v) for p, v in binding.field_map.items()}
    rows = [
        {"code": "A", "status": "1", "__ST": "X"},
        {"code": "B", "status": "1", "__ST": "Z"},
    ]
    good, quarantined = transform_object_rows(tpl, binding, rows, exprs)
    evaluated = evaluate_object_rows(tpl, binding, rows, exprs)
    assert len(good) == evaluated.mapped
    assert len(quarantined) == evaluated.quarantined
    assert evaluated.rows[0].derived_hits[0].outcome == "rule"
    assert evaluated.rows[1].derived_hits[0].outcome == "default"
