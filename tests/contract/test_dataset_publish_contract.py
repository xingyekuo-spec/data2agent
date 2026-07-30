"""v0.3 M2-T01: 状态机、物理表名、publish/rollback 冲突语义契约。"""

from __future__ import annotations

import pytest

from data2agent.shared.metamodel.dataset_publish_contract import (
    ActionDecision,
    can_transition_dataset,
    can_transition_object,
    evaluate_publish,
    evaluate_rollback,
    is_dataset_ready,
    is_valid_build_table,
    make_build_table,
    validate_build_table,
)
from data2agent.shared.metamodel.versioning import DatasetVersionRecord, ObjectVersionRecord


def _ds(
    *,
    version: str = "ds-1",
    status: str = "building",
    previous: str | None = None,
    manifest: str | None = '["Customer","Order"]',
) -> DatasetVersionRecord:
    return DatasetVersionRecord(
        dataset_version=version,
        source="src_a",
        template_version="0.1.0",
        status=status,  # type: ignore[arg-type]
        built_at="2026-07-21T10:00:00",
        published_at="2026-07-21T10:00:00" if status in ("published", "retired") else None,
        previous_dataset_version=previous,
        object_manifest=manifest,
    )


def _obj(
    *,
    name: str,
    status: str = "built",
    dataset_version: str = "ds-1",
    build_table: str | None = "objv_aa_bb_cc",
) -> ObjectVersionRecord:
    return ObjectVersionRecord(
        dataset_version=dataset_version,
        object=name,
        object_version=f"{name}-v1",
        binding_hash="sha256:" + "ab" * 32,
        row_count=1,
        build_table=build_table,
        status=status,  # type: ignore[arg-type]
        built_at="2026-07-21T10:00:00",
        published_at="2026-07-21T10:00:00" if status in ("published", "retired") else None,
    )


# ---- physical table names ----


def test_make_build_table_uses_objv_prefix_and_hex_digests():
    name = make_build_table("src_a", "Customer", "deadbeef01")
    assert name.startswith("objv_")
    parts = name.split("_")
    assert len(parts) == 4
    assert parts[0] == "objv"
    assert all(c in "0123456789abcdef" for c in parts[1] + parts[2] + parts[3])
    assert is_valid_build_table(name)
    assert validate_build_table(name) == name


def test_build_table_rejects_illegal_identifiers():
    assert not is_valid_build_table("obj_Customer")
    assert not is_valid_build_table("objv_UPPER_aa_bb")
    assert not is_valid_build_table("objv_aa_bb")
    assert not is_valid_build_table("objv_aa_bb_gg")  # non-hex
    assert not is_valid_build_table("objv_aa;drop_bb_cc")
    with pytest.raises(ValueError):
        validate_build_table("obj_Customer")
    with pytest.raises(ValueError):
        make_build_table("src", "Obj", "NOTHEX")


# ---- state transitions ----


@pytest.mark.parametrize(
    ("frm", "to", "ok"),
    [
        ("building", "failed", True),
        ("building", "published", True),
        ("published", "retired", True),
        ("retired", "published", True),
        ("failed", "published", False),
        ("failed", "building", False),
        ("published", "building", False),
        ("retired", "failed", False),
        ("building", "retired", False),
    ],
)
def test_dataset_transition_table(frm, to, ok):
    assert can_transition_dataset(frm, to) is ok


@pytest.mark.parametrize(
    ("frm", "to", "ok"),
    [
        ("building", "built", True),
        ("building", "failed", True),
        ("built", "published", True),
        ("published", "retired", True),
        ("retired", "published", True),
        ("failed", "built", False),
        ("failed", "published", False),
        ("built", "failed", False),
        ("published", "built", False),
        ("building", "published", False),
    ],
)
def test_object_transition_table(frm, to, ok):
    assert can_transition_object(frm, to) is ok


def test_dataset_ready_requires_building_and_all_objects_built():
    objs = [_obj(name="Customer"), _obj(name="Order")]
    assert is_dataset_ready(_ds(status="building"), objs) is True
    assert is_dataset_ready(_ds(status="published"), objs) is False
    assert is_dataset_ready(
        _ds(status="building"),
        [_obj(name="Customer"), _obj(name="Order", status="building")],
    ) is False
    assert is_dataset_ready(
        _ds(status="building", manifest='["Customer"]'),
        objs,
    ) is False
    assert is_dataset_ready(_ds(status="building", manifest=None), objs) is False


# ---- publish / rollback decisions ----


def test_publish_idempotent_when_already_published():
    current = _ds(version="ds-1", status="published")
    decision = evaluate_publish(
        candidate=_ds(version="ds-1", status="published"),
        objects=[_obj(name="Customer", status="published"), _obj(name="Order", status="published")],
        current_published=current,
    )
    assert decision == ActionDecision(outcome="idempotent", reason_code=None, http_status=None)


def test_publish_execute_when_ready_building_and_previous_matches():
    candidate = _ds(version="ds-2", status="building", previous="ds-1")
    objs = [_obj(name="Customer", dataset_version="ds-2"), _obj(name="Order", dataset_version="ds-2")]
    decision = evaluate_publish(
        candidate=candidate,
        objects=objs,
        current_published=_ds(version="ds-1", status="published"),
    )
    assert decision.outcome == "execute"
    assert decision.http_status is None


def test_publish_conflict_when_failed_or_not_ready_or_stale_previous():
    failed = evaluate_publish(
        candidate=_ds(version="ds-2", status="failed", previous="ds-1"),
        objects=[_obj(name="Customer", status="failed"), _obj(name="Order", status="failed")],
        current_published=_ds(version="ds-1", status="published"),
    )
    assert failed.outcome == "conflict"
    assert failed.http_status == 409

    not_ready = evaluate_publish(
        candidate=_ds(version="ds-2", status="building", previous="ds-1"),
        objects=[
            _obj(name="Customer", dataset_version="ds-2"),
            _obj(name="Order", status="building", dataset_version="ds-2"),
        ],
        current_published=_ds(version="ds-1", status="published"),
    )
    assert not_ready.outcome == "conflict"
    assert not_ready.http_status == 409

    stale = evaluate_publish(
        candidate=_ds(version="ds-2", status="building", previous="ds-0"),
        objects=[
            _obj(name="Customer", dataset_version="ds-2"),
            _obj(name="Order", dataset_version="ds-2"),
        ],
        current_published=_ds(version="ds-1", status="published"),
    )
    assert stale.outcome == "conflict"
    assert stale.reason_code == "stale_previous"
    assert stale.http_status == 409


def test_publish_not_found_when_candidate_missing():
    decision = evaluate_publish(
        candidate=None,
        objects=[],
        current_published=_ds(version="ds-1", status="published"),
    )
    assert decision.outcome == "not_found"
    assert decision.http_status == 404


def test_publish_first_version_allows_null_previous():
    candidate = _ds(version="ds-1", status="building", previous=None)
    objs = [_obj(name="Customer"), _obj(name="Order")]
    decision = evaluate_publish(
        candidate=candidate,
        objects=objs,
        current_published=None,
    )
    assert decision.outcome == "execute"


def test_rollback_requires_direct_previous_only():
    current = _ds(version="ds-2", status="published", previous="ds-1")
    target = _ds(version="ds-1", status="retired", previous=None)
    ok = evaluate_rollback(target=target, current_published=current)
    assert ok.outcome == "execute"

    skip = evaluate_rollback(
        target=_ds(version="ds-0", status="retired"),
        current_published=current,
    )
    assert skip.outcome == "conflict"
    assert skip.reason_code == "not_direct_previous"
    assert skip.http_status == 409

    no_prev = evaluate_rollback(
        target=_ds(version="ds-0", status="retired"),
        current_published=_ds(version="ds-1", status="published", previous=None),
    )
    assert no_prev.outcome == "conflict"
    assert no_prev.http_status == 409


def test_rollback_rejects_non_retired_target():
    current = _ds(version="ds-2", status="published", previous="ds-1")
    building = evaluate_rollback(
        target=_ds(version="ds-1", status="building"),
        current_published=current,
    )
    assert building.outcome == "conflict"
    assert building.reason_code == "illegal_state"
    assert building.http_status == 409


def test_rollback_idempotent_and_not_found():
    current = _ds(version="ds-1", status="published", previous=None)
    idem = evaluate_rollback(target=current, current_published=current)
    assert idem.outcome == "idempotent"

    missing = evaluate_rollback(target=None, current_published=current)
    assert missing.outcome == "not_found"
    assert missing.http_status == 404


def test_apply_action_body_is_dedicated_and_defaults_publish_true():
    from data2agent.platform.console.contracts import ActionBody, ApplyActionBody, ApplyActionResult, RetryActionResult

    body = ApplyActionBody(source="src_a")
    assert body.publish is True
    assert ApplyActionBody(source="src_a", publish=False).publish is False
    assert not hasattr(ActionBody(source="src_a"), "publish") or "publish" not in ActionBody.model_fields

    result = ApplyActionResult(
        executed=True,
        results=[],
        aborted=[],
        dataset_version="ds-1",
        published=True,
        previous_dataset_version=None,
    )
    assert result.dataset_version == "ds-1"
    assert result.published is True

    retry = RetryActionResult(
        executed=True,
        object="Customer",
        total=1,
        mapped=1,
        quarantined=0,
        status="ok",
        run_id=1,
        step_id=1,
        detail_path="/api/runs/1",
        dataset_version="ds-1",
    )
    assert retry.dataset_version == "ds-1"


def test_run_types_include_publish_and_rollback():
    from data2agent.shared.store.landing import LandingStore
    from data2agent.platform.console import contracts

    assert "publish" in LandingStore.RUN_TYPES
    assert "rollback" in LandingStore.RUN_TYPES
    assert "publish" in contracts.RunType.__args__
    assert "rollback" in contracts.RunType.__args__
    assert "dataset" in contracts.StepKind.__args__
