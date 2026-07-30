"""M3-T03: 确定性只读样本冻结(主键排序、批次、fingerprint、双跑同锚)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from data2agent.middle.extract.adapters.base import TableInfo
from data2agent.shared.store.landing import LandingStore, raw_table_name
from data2agent.shared.store.mapping_preview import (
    FrozenSample,
    PreviewSampleError,
    freeze_sample,
    load_sample_rows,
    make_sample_row_id,
    preview_read_tx,
)
from data2agent.shared.mapping import build_select
from data2agent.shared.metamodel.schema import ObjectTemplate, SourceBinding

SOURCE = "demo_src"
ANCHOR = "ITEM"


def _tpl_binding(*, tables=None, field_map=None) -> tuple[ObjectTemplate, SourceBinding]:
    tables = tables or [ANCHOR]
    field_map = field_map or {"code": f"{ANCHOR}.CODE", "name": f"{ANCHOR}.NAME"}
    tpl = ObjectTemplate(
        object="Item",
        display_name="物料",
        domain="辅助",
        keys=["code"],
        properties=[
            {"name": "code", "type": "string"},
            {"name": "name", "type": "string"},
        ],
    )
    binding = SourceBinding(
        source=SOURCE,
        tables=tables,
        field_map=field_map,
    )
    return tpl, binding


def _item_info(*, composite: bool = False) -> TableInfo:
    if composite:
        return TableInfo(
            name=ANCHOR,
            columns=[
                ("ORG", "text"),
                ("CODE", "text"),
                ("NAME", "text"),
            ],
            pk=["ORG", "CODE"],
        )
    return TableInfo(
        name=ANCHOR,
        columns=[("Id", "int"), ("CODE", "text"), ("NAME", "text")],
        pk=["Id"],
    )


def _seed(
    tmp_path: Path,
    *,
    rows: list[dict],
    batch_id: str = "batch-a",
    composite: bool = False,
    soft_delete_pks: set | None = None,
) -> LandingStore:
    landing = LandingStore(tmp_path / "landing.sqlite")
    info = _item_info(composite=composite)
    landing.ensure_raw_table(SOURCE, info)
    landing.upsert_rows(SOURCE, info, rows, batch_id)
    if soft_delete_pks:
        pk_col = "Id" if not composite else None
        if composite:
            # mark_deleted is single-col; soft-delete via SQL for composite
            physical = raw_table_name(SOURCE, ANCHOR)
            for org, code in soft_delete_pks:
                landing.con.execute(
                    f'UPDATE "{physical}" SET "_d2a_deleted_at" = ? '
                    'WHERE "ORG" = ? AND "CODE" = ?',
                    ("2026-07-21T00:00:00", org, code),
                )
            landing.con.commit()
        else:
            landing.mark_deleted(SOURCE, ANCHOR, pk_col, soft_delete_pks)
    return landing


def test_freeze_offset_limit_stable_pk_order(tmp_path):
    landing = _seed(
        tmp_path,
        rows=[
            {"Id": 3, "CODE": "C", "NAME": "c"},
            {"Id": 1, "CODE": "A", "NAME": "a"},
            {"Id": 2, "CODE": "B", "NAME": "b"},
            {"Id": 4, "CODE": "D", "NAME": "d"},
        ],
    )
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        frozen = freeze_sample(
            ro, source=SOURCE, anchor_table=ANCHOR, offset=1, limit=2,
        )
    assert frozen.source == SOURCE
    assert frozen.pk_cols == ("Id",)
    assert frozen.pk_tuples == ((2,), (3,))
    assert frozen.sampled_rows == 2
    assert frozen.offset == 1 and frozen.limit == 2
    assert frozen.sample_batch_ids == ("batch-a",)
    assert len(frozen.fingerprint) == 64


def test_freeze_composite_pk(tmp_path):
    landing = _seed(
        tmp_path,
        composite=True,
        rows=[
            {"ORG": "B", "CODE": "2", "NAME": "b2"},
            {"ORG": "A", "CODE": "2", "NAME": "a2"},
            {"ORG": "A", "CODE": "1", "NAME": "a1"},
        ],
    )
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        frozen = freeze_sample(
            ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=10,
        )
    assert frozen.source == SOURCE
    assert frozen.pk_cols == ("ORG", "CODE")
    assert frozen.pk_tuples == (("A", "1"), ("A", "2"), ("B", "2"))


def test_composite_pk_freeze_then_load(tmp_path):
    landing = _seed(
        tmp_path,
        composite=True,
        rows=[
            {"ORG": "B", "CODE": "2", "NAME": "b2"},
            {"ORG": "A", "CODE": "2", "NAME": "a2"},
            {"ORG": "A", "CODE": "1", "NAME": "a1"},
            {"ORG": "C", "CODE": "9", "NAME": "c9"},
        ],
    )
    tpl, binding = _tpl_binding()
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        frozen = freeze_sample(
            ro, source=SOURCE, anchor_table=ANCHOR, offset=1, limit=2,
        )
        rows = load_sample_rows(ro, tpl, binding, frozen, source=SOURCE)
    assert frozen.pk_tuples == (("A", "2"), ("B", "2"))
    assert [r["code"] for r in rows] == ["2", "2"]
    assert [r["name"] for r in rows] == ["a2", "b2"]


def test_soft_deleted_rows_excluded(tmp_path):
    landing = _seed(
        tmp_path,
        rows=[
            {"Id": 1, "CODE": "A", "NAME": "a"},
            {"Id": 2, "CODE": "B", "NAME": "b"},
            {"Id": 3, "CODE": "C", "NAME": "c"},
        ],
        soft_delete_pks={2},
    )
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        frozen = freeze_sample(
            ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50,
        )
    assert frozen.pk_tuples == ((1,), (3,))
    assert frozen.sampled_rows == 2


def test_missing_batch_raises_typed_error(tmp_path):
    landing = _seed(
        tmp_path,
        rows=[{"Id": 1, "CODE": "A", "NAME": "a"}],
        batch_id="batch-a",
    )
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        with pytest.raises(PreviewSampleError) as exc:
            freeze_sample(
                ro,
                source=SOURCE,
                anchor_table=ANCHOR,
                offset=0,
                limit=50,
                batch_id="batch-missing",
            )
    assert exc.value.reason_code == "sample_batch_not_found"


def test_batch_filter_exact(tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    info = _item_info()
    landing.ensure_raw_table(SOURCE, info)
    landing.upsert_rows(
        SOURCE, info,
        [{"Id": 1, "CODE": "A", "NAME": "a"}, {"Id": 2, "CODE": "B", "NAME": "b"}],
        "batch-a",
    )
    landing.upsert_rows(
        SOURCE, info,
        [{"Id": 3, "CODE": "C", "NAME": "c"}],
        "batch-b",
    )
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        frozen = freeze_sample(
            ro, source=SOURCE, anchor_table=ANCHOR,
            offset=0, limit=50, batch_id="batch-b",
        )
    assert frozen.pk_tuples == ((3,),)
    assert frozen.sample_batch_ids == ("batch-b",)
    assert frozen.requested_batch_id == "batch-b"


def test_empty_table_success_stable_fingerprint(tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    landing.ensure_raw_table(SOURCE, _item_info())
    landing.con.commit()
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        a = freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
        b = freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
    assert a.pk_tuples == ()
    assert a.sampled_rows == 0
    assert a.sample_batch_ids == ()
    assert a.fingerprint == b.fingerprint
    assert len(a.fingerprint) == 64


def test_fingerprint_stable_and_sensitive(tmp_path):
    landing = _seed(
        tmp_path,
        rows=[
            {"Id": 1, "CODE": "A", "NAME": "a"},
            {"Id": 2, "CODE": "B", "NAME": "b"},
        ],
        batch_id="batch-a",
    )
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        base = freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
        again = freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
    assert base.fingerprint == again.fingerprint

    # PK set change (offset)
    with preview_read_tx(ro):
        shifted = freeze_sample(
            ro, source=SOURCE, anchor_table=ANCHOR, offset=1, limit=50,
        )
    assert shifted.fingerprint != base.fingerprint

    # row hash change
    physical = raw_table_name(SOURCE, ANCHOR)
    landing.con.execute(
        f'UPDATE "{physical}" SET "NAME" = ?, "_d2a_row_hash" = ? WHERE "Id" = 1',
        ("a2", "hash-changed"),
    )
    landing.con.commit()
    ro2 = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro2):
        after_hash = freeze_sample(
            ro2, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50,
        )
    assert after_hash.fingerprint != base.fingerprint

    # batch_id change on sampled row
    landing.con.execute(
        f'UPDATE "{physical}" SET "_d2a_batch_id" = ? WHERE "Id" = 2',
        ("batch-z",),
    )
    landing.con.commit()
    ro3 = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro3):
        after_batch = freeze_sample(
            ro3, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50,
        )
    assert after_batch.fingerprint != after_hash.fingerprint
    assert after_batch.sample_batch_ids == ("batch-a", "batch-z")


def test_concurrent_writer_does_not_diverge_current_candidate(tmp_path):
    landing = _seed(
        tmp_path,
        rows=[
            {"Id": 10, "CODE": "X", "NAME": "x"},
            {"Id": 20, "CODE": "Y", "NAME": "y"},
        ],
        batch_id="batch-a",
    )
    writer = LandingStore(landing.db_path)
    info = _item_info()
    ro = LandingStore.open_readonly(landing.db_path)
    tpl, binding_current = _tpl_binding()
    _, binding_draft = _tpl_binding(
        field_map={"code": f"{ANCHOR}.CODE", "name": f"{ANCHOR}.NAME"},
    )

    with preview_read_tx(ro):
        frozen = freeze_sample(
            ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50,
        )
        assert frozen.pk_tuples == ((10,), (20,))

        # Concurrent writer inserts a row that would sort into the sample window
        # and mutates an existing sampled row — must not affect this snapshot.
        writer.upsert_rows(
            SOURCE, info,
            [
                {"Id": 1, "CODE": "NEW", "NAME": "new"},
                {"Id": 10, "CODE": "X", "NAME": "mutated"},
            ],
            "batch-writer",
        )

        current_rows = load_sample_rows(
            ro, tpl, binding_current, frozen, source=SOURCE,
        )
        candidate_rows = load_sample_rows(
            ro, tpl, binding_draft, frozen, source=SOURCE,
        )

    assert [r["code"] for r in current_rows] == ["X", "Y"]
    assert [r["code"] for r in candidate_rows] == ["X", "Y"]
    assert [r["name"] for r in current_rows] == ["x", "y"]
    assert [r["name"] for r in candidate_rows] == ["x", "y"]
    assert frozen.pk_tuples == ((10,), (20,))


def test_no_pk_table_fail_closed(tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    physical = raw_table_name(SOURCE, ANCHOR)
    landing.con.execute(
        f'CREATE TABLE "{physical}" ('
        '"CODE" TEXT, "NAME" TEXT, '
        '"_d2a_batch_id" TEXT, "_d2a_extracted_at" TEXT, '
        '"_d2a_row_hash" TEXT, "_d2a_deleted_at" TEXT)'
    )
    landing.con.commit()
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        with pytest.raises(PreviewSampleError) as exc:
            freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
    assert exc.value.reason_code == "sample_invalid"


def test_same_frozen_keys_feed_two_build_selects(tmp_path):
    landing = _seed(
        tmp_path,
        rows=[
            {"Id": 1, "CODE": "A", "NAME": "a"},
            {"Id": 2, "CODE": "B", "NAME": "b"},
            {"Id": 3, "CODE": "C", "NAME": "c"},
        ],
    )
    ro = LandingStore.open_readonly(landing.db_path)
    tpl, binding_a = _tpl_binding()
    _, binding_b = _tpl_binding(
        field_map={"code": f"{ANCHOR}.CODE", "name": f"{ANCHOR}.NAME"},
    )
    with preview_read_tx(ro):
        frozen = freeze_sample(
            ro, source=SOURCE, anchor_table=ANCHOR, offset=1, limit=2,
        )
        rows_a = load_sample_rows(ro, tpl, binding_a, frozen, source=SOURCE)
        rows_b = load_sample_rows(ro, tpl, binding_b, frozen, source=SOURCE)
    assert frozen.pk_tuples == ((2,), (3,))
    assert [r["code"] for r in rows_a] == [r["code"] for r in rows_b] == ["B", "C"]


def test_build_select_anchor_pk_filter_default_unchanged():
    tpl, binding = _tpl_binding()
    sql, params, _ = build_select(tpl, binding, filters={"code": "A"}, limit=5)
    assert "IN (" not in sql
    assert params == ["A"]
    assert "LIMIT 5" in sql


def test_build_select_anchor_pk_values_parameterized():
    tpl, binding = _tpl_binding()
    sql, params, _ = build_select(
        tpl, binding,
        limit=None,
        anchor_pk_cols=["Id"],
        anchor_pk_values=[(2,), (3,)],
        physical=lambda t: raw_table_name(SOURCE, t),
        active_col="_d2a_deleted_at",
    )
    assert 'a."Id" IN (?, ?)' in sql
    assert params == [2, 3]
    assert "LIMIT" not in sql
    assert 'a."_d2a_deleted_at" IS NULL' in sql


def test_build_select_composite_pk_row_values():
    tpl, binding = _tpl_binding(
        field_map={"code": f"{ANCHOR}.CODE", "name": f"{ANCHOR}.NAME"},
    )
    sql, params, _ = build_select(
        tpl, binding,
        limit=None,
        anchor_pk_cols=["ORG", "CODE"],
        anchor_pk_values=[("A", "1"), ("B", "2")],
    )
    assert '(a."ORG", a."CODE") IN ((?, ?), (?, ?))' in sql
    assert params == ["A", "1", "B", "2"]


def test_build_select_empty_pk_values_force_empty():
    tpl, binding = _tpl_binding()
    sql, params, _ = build_select(
        tpl, binding,
        limit=None,
        anchor_pk_cols=["Id"],
        anchor_pk_values=[],
    )
    assert "1 = 0" in sql
    assert params == []


def test_make_sample_row_id_stub():
    rid = make_sample_row_id(0, ("1",))
    assert rid.startswith("0:")
    assert len(rid.split(":", 1)[1]) >= 8
    assert make_sample_row_id(1, ("1",)) != make_sample_row_id(0, ("1",))


def test_freeze_rejects_out_of_bounds(tmp_path):
    landing = _seed(tmp_path, rows=[{"Id": 1, "CODE": "A", "NAME": "a"}])
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        with pytest.raises(PreviewSampleError) as exc:
            freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=0)
    assert exc.value.reason_code == "sample_invalid"
    with preview_read_tx(ro):
        with pytest.raises(PreviewSampleError) as exc:
            freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=-1, limit=10)
    assert exc.value.reason_code == "sample_invalid"
    with preview_read_tx(ro):
        with pytest.raises(PreviewSampleError) as exc:
            freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=True, limit=10)
    assert exc.value.reason_code == "sample_invalid"


def test_raw_table_not_found(tmp_path):
    landing = LandingStore(tmp_path / "landing.sqlite")
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        with pytest.raises(PreviewSampleError) as exc:
            freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
    assert exc.value.reason_code == "raw_table_not_found"


def test_requires_preview_read_tx(tmp_path):
    landing = _seed(tmp_path, rows=[{"Id": 1, "CODE": "A", "NAME": "a"}])
    ro = LandingStore.open_readonly(landing.db_path)
    with pytest.raises(PreviewSampleError) as exc:
        freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
    assert exc.value.reason_code == "sample_invalid"

    tpl, binding = _tpl_binding()
    sample = FrozenSample(
        source=SOURCE,
        anchor_table=ANCHOR,
        pk_cols=("Id",),
        pk_tuples=((1,),),
        sample_batch_ids=("batch-a",),
        fingerprint="a" * 64,
        sampled_rows=1,
        offset=0,
        limit=50,
        requested_batch_id=None,
    )
    with pytest.raises(PreviewSampleError) as exc:
        load_sample_rows(ro, tpl, binding, sample, source=SOURCE)
    assert exc.value.reason_code == "sample_invalid"


def test_load_rejects_source_mismatch(tmp_path):
    landing = _seed(tmp_path, rows=[{"Id": 1, "CODE": "A", "NAME": "a"}])
    tpl, binding = _tpl_binding()
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        frozen = freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
        with pytest.raises(PreviewSampleError) as exc:
            load_sample_rows(ro, tpl, binding, frozen, source="other_src")
    assert exc.value.reason_code == "sample_invalid"


def test_load_fail_closed_on_pk_drift(tmp_path):
    landing = _seed(
        tmp_path,
        rows=[
            {"Id": 1, "CODE": "A", "NAME": "a"},
            {"Id": 2, "CODE": "B", "NAME": "b"},
        ],
    )
    tpl, binding = _tpl_binding()
    ro = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro):
        frozen = freeze_sample(ro, source=SOURCE, anchor_table=ANCHOR, offset=0, limit=50)
    assert frozen.pk_tuples == ((1,), (2,))

    landing.mark_deleted(SOURCE, ANCHOR, "Id", {2})
    ro2 = LandingStore.open_readonly(landing.db_path)
    with preview_read_tx(ro2):
        with pytest.raises(PreviewSampleError) as exc:
            load_sample_rows(ro2, tpl, binding, frozen, source=SOURCE)
    assert exc.value.reason_code == "anchor_changed"


def test_frozen_sample_dataclass_fields():
    sample = FrozenSample(
        source=SOURCE,
        anchor_table=ANCHOR,
        pk_cols=("Id",),
        pk_tuples=((1,),),
        sample_batch_ids=("batch-a",),
        fingerprint="a" * 64,
        sampled_rows=1,
        offset=0,
        limit=50,
        requested_batch_id=None,
    )
    assert sample.source == SOURCE
    assert sample.sampled_rows == 1
