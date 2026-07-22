from __future__ import annotations

import pytest

from data2agent.mcp_server.evidence import (
    METRIC_SUMMARY_MAX_ITEMS,
    OBJECT_SUMMARY_MAX_ROWS,
    SUMMARY_MAX_BYTES,
    build_metric_result_summary,
    build_object_result_summary,
    build_result_envelope,
    canonical_json_bytes,
    canonical_json_dumps,
    normalize_query_metrics,
    normalize_query_objects,
    result_digest,
)


def test_normalize_query_objects_sorts_filters_and_clamps_limit():
    a = normalize_query_objects(
        source="digiwin_e10",
        object_name="Customer",
        filters={"b": "2", "a": "1"},
        order_by="customer_code",
        desc=False,
        limit=999,
    )
    b = normalize_query_objects(
        source="digiwin_e10",
        object_name="Customer",
        filters={"a": "1", "b": "2"},
        order_by="customer_code",
        desc=False,
        limit=200,
    )
    assert a == b
    assert list(a["filters"]) == ["a", "b"]
    assert a["limit"] == 200


def test_normalize_query_objects_rejects_non_scalar_filter_value():
    with pytest.raises(ValueError, match="filter value must be scalar"):
        normalize_query_objects(
            source="digiwin_e10",
            object_name="Customer",
            filters={"region": {"bad": 1}},
        )


def test_canonical_json_rejects_nan():
    with pytest.raises(ValueError, match="float must be finite"):
        canonical_json_dumps({"value": float("nan")})


def test_result_digest_ignores_query_runtime_metadata():
    envelope = build_result_envelope(
        tool="query_objects",
        source="digiwin_e10",
        target="Customer",
        normalized_query=normalize_query_objects(
            source="digiwin_e10",
            object_name="Customer",
            filters={"customer_code": "C001"},
            limit=20,
        ),
        dataset_version="ds_1",
        template_version="tpl_1",
        binding_hashes={"Customer": "sha256:abc"},
        response_payload={
            "rows": [{"customer_code": "C001", "contact": "***"}],
            "meta": {"query_id": "qry_1", "duration_ms": 12, "created_at": "ignored"},
        },
    )
    digest_a = result_digest(envelope)
    envelope["response_payload"] = {
        "rows": [{"customer_code": "C001", "contact": "***"}],
        "meta": {"query_id": "qry_999", "duration_ms": 999, "created_at": "changed"},
    }
    digest_b = result_digest(envelope)
    assert digest_a == digest_b


def test_result_digest_changes_when_payload_changes():
    base = build_result_envelope(
        tool="query_metrics",
        source="digiwin_e10",
        target="gross_margin_rate",
        normalized_query=normalize_query_metrics(
            source="digiwin_e10",
            metric="gross_margin_rate",
            group_by="月",
            limit=24,
        ),
        dataset_version="ds_1",
        template_version="tpl_1",
        binding_hashes={"SalesOrder": "sha256:abc"},
        response_payload={"rows": [{"group": "2026-07", "value": 0.3}]},
    )
    changed = build_result_envelope(
        tool="query_metrics",
        source="digiwin_e10",
        target="gross_margin_rate",
        normalized_query=normalize_query_metrics(
            source="digiwin_e10",
            metric="gross_margin_rate",
            group_by="月",
            limit=24,
        ),
        dataset_version="ds_2",
        template_version="tpl_1",
        binding_hashes={"SalesOrder": "sha256:abc"},
        response_payload={"rows": [{"group": "2026-07", "value": 0.3}]},
    )
    assert result_digest(base) != result_digest(changed)


def test_object_summary_truncates_after_20_rows():
    rows = [{"customer_code": f"C{i:03d}", "contact": "***"} for i in range(25)]
    summary = build_object_result_summary(columns=["customer_code", "contact"], rows=rows)
    assert summary["kind"] == "query_objects"
    assert summary["returned_row_count"] == 25
    assert len(summary["rows_preview"]) == OBJECT_SUMMARY_MAX_ROWS
    assert summary["preview_truncated"] is True


def test_metric_summary_truncates_after_50_items():
    rows = [{"group": f"g{i:03d}", "value": i} for i in range(60)]
    summary = build_metric_result_summary(
        metric="gross_margin_rate",
        status="draft",
        unit=None,
        group_by="月",
        rows=rows,
    )
    assert summary["kind"] == "query_metrics"
    assert summary["returned_row_count"] == 60
    assert len(summary["series_preview"]) == METRIC_SUMMARY_MAX_ITEMS
    assert summary["preview_truncated"] is True


def test_summary_is_bounded_to_32k():
    rows = [{"customer_code": f"C{i:03d}", "note": "x" * 4000} for i in range(30)]
    summary = build_object_result_summary(columns=["customer_code", "note"], rows=rows)
    size = len(canonical_json_bytes(summary))
    assert size <= SUMMARY_MAX_BYTES
    assert summary["preview_truncated"] is True


def test_summary_empty_result_is_stable():
    obj_summary = build_object_result_summary(columns=["customer_code"], rows=[])
    metric_summary = build_metric_result_summary(
        metric="gross_margin_rate",
        status="draft",
        unit=None,
        group_by="月",
        rows=[],
    )
    assert obj_summary["returned_row_count"] == 0
    assert obj_summary["rows_preview"] == []
    assert obj_summary["preview_truncated"] is False
    assert metric_summary["returned_row_count"] == 0
    assert metric_summary["series_preview"] == []
    assert metric_summary["preview_truncated"] is False
