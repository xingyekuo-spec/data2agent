"""M5 contract tests: quarantine, templates, retry, and access audit extensions."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from data2agent.console.contracts import (
    AccessAuditItem,
    DerivedField,
    DeriveRule,
    JsonValue,
    QuarantineDetail,
    QuarantineGroup,
    QuarantineRecord,
    RetryActionError,
    RetryActionResult,
    TemplateBinding,
    TemplateMaterialization,
    TemplateMetric,
    TemplateObject,
    TemplateProperty,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# QuarantineRecord
# ---------------------------------------------------------------------------


def test_quarantine_record_construction():
    now = _utc_now()
    rec = QuarantineRecord(
        id=1,
        source="erp",
        object="SalesOrder",
        reason="duplicate key",
        created_at=now,
    )
    assert rec.id == 1
    assert rec.source == "erp"
    assert rec.keys_json is None
    assert rec.keys is None
    assert rec.reason == "duplicate key"
    assert rec.batch_id is None
    assert rec.created_at == now
    assert rec.age_seconds is None
    assert rec.warnings == []


def test_quarantine_record_with_keys_and_warnings():
    now = _utc_now()
    rec = QuarantineRecord(
        id=2,
        source="erp",
        object="SalesOrder",
        keys_json='{"code": "SO-001"}',
        keys={"code": "SO-001", "line": 1},
        reason="FK violation",
        batch_id="batch-abc",
        created_at=now,
        age_seconds=3600,
        warnings=["keys_json parse warning"],
    )
    # JsonObject = dict[str, JsonValue], so values are wrapped in JsonValue
    assert rec.keys is not None
    assert isinstance(rec.keys, dict)
    assert rec.keys["code"].root == "SO-001"
    assert rec.keys["line"].root == 1
    assert rec.keys_json == '{"code": "SO-001"}'
    assert rec.batch_id == "batch-abc"
    assert rec.age_seconds == 3600
    assert len(rec.warnings) == 1


def test_quarantine_record_created_at_is_datetime_with_tz():
    """M5: created_at must be tz-aware datetime, not legacy str."""
    now = _utc_now()
    rec = QuarantineRecord(
        id=3,
        source="erp",
        object="SalesOrder",
        reason="test",
        created_at=now,
    )
    assert isinstance(rec.created_at, datetime)
    assert rec.created_at.tzinfo is not None


def test_quarantine_record_accepts_iso_string_created_at():
    """M5: created_at is datetime typed. Pydantic v2 coerces ISO 8601 strings
    automatically; this is acceptable during the transition period before the
    backend emits tz-aware datetime values."""
    rec = QuarantineRecord(
        id=4,
        source="erp",
        object="SalesOrder",
        reason="test",
        created_at="2026-07-10T12:00:00+00:00",
    )
    assert isinstance(rec.created_at, datetime)


# ---------------------------------------------------------------------------
# QuarantineDetail (extends QuarantineRecord)
# ---------------------------------------------------------------------------


def test_quarantine_detail_extends_record():
    now = _utc_now()
    detail = QuarantineDetail(
        id=1,
        source="erp",
        object="SalesOrder",
        reason="FK violation",
        created_at=now,
        raw={"code": "SO-001", "status": "pending"},
        request_id="req-123",
    )
    # Inherited fields
    assert detail.id == 1
    assert detail.source == "erp"
    assert isinstance(detail.created_at, datetime)
    # Own fields: raw is JsonObject (dict[str, JsonValue]) | None
    assert detail.raw is not None
    assert isinstance(detail.raw, dict)
    assert detail.raw == {"code": JsonValue("SO-001"), "status": JsonValue("pending")}
    assert detail.truncations == []
    assert detail.request_id == "req-123"


def test_quarantine_detail_with_truncations():
    now = _utc_now()
    from data2agent.console.contracts import FieldTruncation

    detail = QuarantineDetail(
        id=2,
        source="erp",
        object="SalesOrder",
        reason="test",
        created_at=now,
        raw=None,
        truncations=[
            FieldTruncation(row_index=0, fields=["notes"]),
            FieldTruncation(row_index=5, fields=["address", "memo"]),
        ],
        request_id="req-456",
    )
    assert len(detail.truncations) == 2
    assert detail.truncations[0].row_index == 0
    assert detail.truncations[0].fields == ["notes"]


def test_quarantine_detail_has_no_raw_in_list():
    """QuarantineRecord (list) must NOT have raw field."""
    assert "raw" not in QuarantineRecord.model_fields
    assert "raw" in QuarantineDetail.model_fields


# ---------------------------------------------------------------------------
# QuarantineGroup
# ---------------------------------------------------------------------------


def test_quarantine_group_construction():
    now = _utc_now()
    group = QuarantineGroup(
        source="erp",
        object="SalesOrder",
        display_name="Sales Orders",
        pending=42,
        latest_created_at=now,
        latest_batch_id="batch-xyz",
        latest_reason="FK violation",
        quarantine_rate=0.03,
        breaker_threshold=0.05,
        rate_state="ok",
        serving_state="fresh",
        latest_apply_run_id=10,
        object_rows=10000,
        mapped_at=now,
    )
    assert group.source == "erp"
    assert group.object == "SalesOrder"
    assert group.pending == 42
    assert group.quarantine_rate == 0.03
    assert group.breaker_threshold == 0.05
    assert group.rate_state == "ok"
    assert group.serving_state == "fresh"
    assert group.latest_apply_run_id == 10
    assert group.object_rows == 10000
    assert group.warnings == []


def test_quarantine_group_rate_states():
    for state in ("ok", "warning", "tripped", "unknown"):
        now = _utc_now()
        group = QuarantineGroup(
            source="erp",
            object="Test",
            pending=0,
            breaker_threshold=0.05,
            rate_state=state,
            serving_state="unknown",
        )
        assert group.rate_state == state


def test_quarantine_group_serving_states():
    for state in ("fresh", "stale", "not_materialized", "unavailable", "unknown"):
        now = _utc_now()
        group = QuarantineGroup(
            source="erp",
            object="Test",
            pending=0,
            breaker_threshold=0.05,
            rate_state="unknown",
            serving_state=state,
        )
        assert group.serving_state == state


# ---------------------------------------------------------------------------
# RetryActionResult
# ---------------------------------------------------------------------------


def test_retry_action_result_construction():
    result = RetryActionResult(
        executed=True,
        object="SalesOrder",
        total=100,
        mapped=97,
        quarantined=3,
        status="ok",
        run_id=5,
        step_id=12,
        detail_path="/api/runs/5",
    )
    assert result.executed is True
    assert result.status == "ok"
    assert result.run_id == 5
    assert result.step_id == 12
    assert result.detail_path == "/api/runs/5"


def test_retry_action_result_status_is_literal_ok():
    """M5: status must be Literal["ok"], not arbitrary str."""
    result = RetryActionResult(
        executed=True,
        object="SalesOrder",
        total=100,
        mapped=97,
        quarantined=3,
        status="ok",
        run_id=5,
        step_id=12,
        detail_path="/api/runs/5",
    )
    assert result.status == "ok"

    # M5 breaking change: status "failed" no longer valid for success
    with pytest.raises(Exception):
        RetryActionResult(
            executed=True,
            object="SalesOrder",
            total=100,
            mapped=97,
            quarantined=3,
            status="failed",
            run_id=5,
            step_id=12,
            detail_path="/api/runs/5",
        )


# ---------------------------------------------------------------------------
# RetryActionError
# ---------------------------------------------------------------------------


def test_retry_action_error_circuit_broken():
    err = RetryActionError(
        detail="Circuit breaker tripped for SalesOrder",
        reason_code="circuit_broken",
        executed=False,
        object="SalesOrder",
        status="aborted",
    )
    assert err.reason_code == "circuit_broken"
    assert err.executed is False
    assert err.status == "aborted"
    assert err.total is None
    assert err.mapped is None


def test_retry_action_error_execution_failed():
    err = RetryActionError(
        detail="Apply failed: constraint violation",
        reason_code="execution_failed",
        executed=True,
        object="SalesOrder",
        total=100,
        mapped=50,
        quarantined=20,
        status="failed",
        run_id=5,
        step_id=12,
        detail_path="/api/runs/5",
        error_id="err-abc123",
    )
    assert err.reason_code == "execution_failed"
    assert err.executed is True
    assert err.status == "failed"
    assert err.run_id == 5
    assert err.error_id == "err-abc123"
    assert err.total == 100
    assert err.quarantined == 20


def test_retry_action_error_observation_failed():
    err = RetryActionError(
        detail="Unable to read quarantine state",
        reason_code="observation_failed",
        executed=False,
        object="SalesOrder",
        status="failed",
        error_id="err-xyz",
    )
    assert err.reason_code == "observation_failed"
    assert err.status == "failed"


def test_retry_action_error_all_reason_codes():
    for code in ("circuit_broken", "execution_failed", "observation_failed"):
        err = RetryActionError(
            detail=f"Error: {code}",
            reason_code=code,
            executed=False,
            object="SalesOrder",
            status="aborted",
        )
        assert err.reason_code == code


def test_retry_action_error_detail_must_be_safe():
    """detail must be a safe summary, no traceback/SQL/sensitive values."""
    err = RetryActionError(
        detail="Object apply failed after 3 retries",
        reason_code="execution_failed",
        executed=True,
        object="SalesOrder",
        status="failed",
    )
    assert isinstance(err.detail, str)
    assert len(err.detail) > 0
    # detail should not contain SQL keywords or traceback patterns
    assert "Traceback" not in err.detail
    assert "SELECT" not in err.detail.upper()


# ---------------------------------------------------------------------------
# TemplateProperty (extended)
# ---------------------------------------------------------------------------


def test_template_property_with_ref_and_enum():
    prop = TemplateProperty(
        name="status",
        type="string",
        desc="Order status",
        sensitive=False,
        ref="erp.orders.status",
        enum_values=["draft", "confirmed", "shipped"],
    )
    assert prop.name == "status"
    assert prop.ref == "erp.orders.status"
    assert prop.enum_values == ["draft", "confirmed", "shipped"]


def test_template_property_backward_compat():
    """Existing fields still work without M5 extensions."""
    prop = TemplateProperty(name="code", type="string", desc="Code", sensitive=True)
    assert prop.ref is None
    assert prop.enum_values == []


# ---------------------------------------------------------------------------
# DeriveRule / DerivedField
# ---------------------------------------------------------------------------


def test_derive_rule_construction():
    rule = DeriveRule(
        when={"source": "erp", "type": None},  # None = "any"
        value="ERP-derived",
    )
    assert rule.when == {"source": "erp", "type": None}
    assert rule.value == "ERP-derived"


def test_derived_field_with_rules_and_default():
    field = DerivedField(
        rules=[
            DeriveRule(when={"status": "active"}, value="ACTIVE"),
            DeriveRule(when={"status": "inactive"}, value="INACTIVE"),
        ],
        default="UNKNOWN",
    )
    assert len(field.rules) == 2
    assert field.rules[0].value == "ACTIVE"
    assert field.default == "UNKNOWN"


def test_derived_field_empty_rules():
    field = DerivedField()
    assert field.rules == []
    assert field.default is None


# ---------------------------------------------------------------------------
# TemplateBinding (extended)
# ---------------------------------------------------------------------------


def test_template_binding_with_enum_map():
    binding = TemplateBinding(
        source="erp",
        tables=["orders"],
        status="verified",
        enabled=True,
        enum_map={
            "status": {"A": "Active", "I": "Inactive"},
            "type": {"S": "Sale", "R": "Return"},
        },
    )
    assert binding.enabled is True
    assert binding.enum_map == {
        "status": {"A": "Active", "I": "Inactive"},
        "type": {"S": "Sale", "R": "Return"},
    }
    assert binding.derived == {}


def test_template_binding_with_derived():
    binding = TemplateBinding(
        source="erp",
        tables=["orders"],
        status="draft",
        derived={
            "display_status": DerivedField(
                rules=[
                    DeriveRule(when={"status": "A"}, value="Active"),
                    DeriveRule(when={"status": "I"}, value="Inactive"),
                ],
                default="Unknown",
            ),
        },
    )
    assert "display_status" in binding.derived
    assert len(binding.derived["display_status"].rules) == 2
    assert binding.derived["display_status"].default == "Unknown"


def test_template_binding_backward_compat():
    """Existing fields still work without M5 extensions (defaults)."""
    binding = TemplateBinding(
        source="erp",
        tables=["orders"],
        status="verified",
    )
    assert binding.enabled is True
    assert binding.enum_map == {}
    assert binding.derived == {}


# ---------------------------------------------------------------------------
# TemplateMaterialization
# ---------------------------------------------------------------------------


def test_template_materialization_materialized():
    now = _utc_now()
    mat = TemplateMaterialization(
        state="materialized",
        source="erp",
        rows=1000,
        mapped_at=now,
        batch_id="batch-001",
    )
    assert mat.state == "materialized"
    assert mat.source == "erp"
    assert mat.rows == 1000
    assert mat.mapped_at == now
    assert mat.batch_id == "batch-001"
    assert mat.warnings == []


def test_template_materialization_not_materialized():
    mat = TemplateMaterialization(state="not_materialized")
    assert mat.state == "not_materialized"
    assert mat.source is None
    assert mat.rows is None


def test_template_materialization_unknown():
    mat = TemplateMaterialization(
        state="unknown",
        warnings=["unable to detect materialization state"],
    )
    assert mat.state == "unknown"
    assert len(mat.warnings) == 1


def test_template_materialization_states():
    for state in ("materialized", "not_materialized", "unknown"):
        mat = TemplateMaterialization(state=state)
        assert mat.state == state


# ---------------------------------------------------------------------------
# TemplateObject (extended)
# ---------------------------------------------------------------------------


def test_template_object_with_m5_fields():
    now = _utc_now()
    obj = TemplateObject(
        object="SalesOrder",
        display_name="Sales Orders",
        description="Customer sales orders",
        domain="sales",
        keys=["order_id"],
        properties=[
            TemplateProperty(name="order_id", type="string"),
            TemplateProperty(name="status", type="string", ref="erp.orders.status"),
        ],
        bindings=[
            TemplateBinding(source="erp", tables=["orders"], status="verified"),
        ],
        source_of_truth="erp",
        knowledge_refs=["doc://sales/order-model"],
        materialized=TemplateMaterialization(
            state="materialized",
            source="erp",
            rows=5000,
            mapped_at=now,
            batch_id="batch-10",
        ),
        quarantine_pending=12,
        warnings=["3 properties have unknown classification"],
    )
    assert obj.source_of_truth == "erp"
    assert obj.knowledge_refs == ["doc://sales/order-model"]
    assert obj.materialized is not None
    assert obj.materialized.state == "materialized"
    assert obj.quarantine_pending == 12
    assert len(obj.warnings) == 1


def test_template_object_m5_defaults():
    obj = TemplateObject(
        object="Product",
        display_name="Products",
        keys=["product_id"],
        properties=[],
        bindings=[],
        source_of_truth="erp",
    )
    assert obj.knowledge_refs == []
    assert obj.materialized is None
    assert obj.quarantine_pending == 0
    assert obj.warnings == []


# ---------------------------------------------------------------------------
# TemplateMetric
# ---------------------------------------------------------------------------


def test_template_metric_construction():
    metric = TemplateMetric(
        metric="revenue_growth",
        display_name="Revenue Growth",
        status="certified",
        calibration_state="calibrated",
        formula="(current - previous) / previous * 100",
        grain=["month", "region"],
        dimensions=["product_category", "channel"],
        caveats="Excludes cancelled orders",
    )
    assert metric.metric == "revenue_growth"
    assert metric.display_name == "Revenue Growth"
    assert metric.grain == ["month", "region"]
    assert metric.dimensions == ["product_category", "channel"]
    assert metric.caveats == "Excludes cancelled orders"


def test_template_metric_status_values():
    for status in ("certified", "draft", "deprecated"):
        metric = TemplateMetric(
            metric="test",
            display_name="Test",
            status=status,
            calibration_state="uncalibrated",
            formula="1+1",
        )
        assert metric.status == status


def test_template_metric_calibration_states():
    for state in ("calibrated", "uncalibrated", "deprecated"):
        metric = TemplateMetric(
            metric="test",
            display_name="Test",
            status="draft",
            calibration_state=state,
            formula="1+1",
        )
        assert metric.calibration_state == state


# ---------------------------------------------------------------------------
# AccessAuditItem (extended resource_type)
# ---------------------------------------------------------------------------


def test_access_audit_item_accepts_quarantine_raw():
    now = _utc_now()
    item = AccessAuditItem(
        id=1,
        ts=now,
        subject="admin",
        resource_type="quarantine_raw",
        source="erp",
        resource="SalesOrder",
        allowed=True,
        reason_code="audit_m5_quarantine_detail",
        request_id="req-q-1",
    )
    assert item.resource_type == "quarantine_raw"


def test_access_audit_item_all_resource_types():
    now = _utc_now()
    for rt in ("raw", "object", "quarantine_raw"):
        item = AccessAuditItem(
            id=1,
            ts=now,
            subject="admin",
            resource_type=rt,
            source="erp",
            resource="SalesOrder",
            allowed=True,
            reason_code="test",
        )
        assert item.resource_type == rt


# ---------------------------------------------------------------------------
# JsonValue / JsonObject type
# ---------------------------------------------------------------------------


def test_json_value_construction():
    assert JsonValue("hello").root == "hello"
    assert JsonValue(42).root == 42
    assert JsonValue(3.14).root == 3.14
    assert JsonValue(True).root is True
    assert JsonValue(None).root is None


def test_json_value_nested():
    val = JsonValue({"a": 1, "b": [2, 3]})
    # JsonValue wraps container values recursively via RootModel.
    # Use model_dump() to get the unwrapped form.
    dumped = val.model_dump()
    assert dumped == {"a": 1, "b": [2, 3]}
