from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data2agent.connect.landing import LandingStore
from data2agent.mcp_server.evidence import (
    EvidenceStore,
    GatewayAuditRecord,
    ProposalEvidenceRecord,
    ProposalRecord,
    QueryEvidenceRecord,
    canonical_json_dumps,
)


def _make_store(tmp_path: Path) -> tuple[LandingStore, EvidenceStore]:
    landing = LandingStore(tmp_path / "landing.sqlite")
    return landing, EvidenceStore(landing)


def _query_record(query_id: str = "qry_test") -> QueryEvidenceRecord:
    return QueryEvidenceRecord(
        query_id=query_id,
        principal="console:configured",
        session_id="d2a_session_0123456789",
        channel="console",
        source="digiwin_e10",
        tool="query_objects",
        target="Customer",
        normalized_query_json=canonical_json_dumps(
            {"tool": "query_objects", "source": "digiwin_e10", "object": "Customer"}
        ),
        dataset_version="ds_001",
        template_version="tv_001",
        binding_hashes_json=canonical_json_dumps({"Customer": "sha256:abc"}),
        result_digest="sha256:" + "a" * 64,
        result_summary_json=canonical_json_dumps(
            {"kind": "query_objects", "returned_row_count": 1, "rows_preview": []}
        ),
        warnings_json=canonical_json_dumps(["draft binding"]),
        row_count=1,
        created_at="2026-07-22T10:00:00+00:00",
        expires_at="2026-07-23T10:00:00+00:00",
    )


def _proposal_record(proposal_id: str = "prp_test") -> ProposalRecord:
    return ProposalRecord(
        proposal_id=proposal_id,
        principal="console:configured",
        session_id="d2a_session_0123456789",
        channel="console",
        source="digiwin_e10",
        object="Quotation",
        action="quote_review",
        action_desc="报价复核",
        tier="说",
        conclusion="谨慎接",
        governance="「说」档建议卡:未执行任何写操作;落地执行(做档)需审批治理",
        dataset_version="ds_001",
        created_at="2026-07-22T10:05:00+00:00",
    )


def _proposal_evidence_record(
    proposal_id: str = "prp_test", ordinal: int = 0,
) -> ProposalEvidenceRecord:
    return ProposalEvidenceRecord(
        proposal_id=proposal_id,
        evidence_ordinal=ordinal,
        claim="客户账期较长",
        query_id="qry_test",
        query_tool="query_objects",
        query_target="Customer",
        normalized_query_json=canonical_json_dumps(
            {"tool": "query_objects", "source": "digiwin_e10", "object": "Customer"}
        ),
        dataset_version="ds_001",
        template_version="tv_001",
        binding_hashes_json=canonical_json_dumps({"Customer": "sha256:abc"}),
        result_digest="sha256:" + "a" * 64,
        result_summary_json=canonical_json_dumps(
            {"kind": "query_objects", "returned_row_count": 1, "rows_preview": []}
        ),
        warnings_json=canonical_json_dumps(["draft binding"]),
        query_created_at="2026-07-22T10:00:00+00:00",
    )


def _audit_record(event_id: str = "evt_test") -> GatewayAuditRecord:
    return GatewayAuditRecord(
        event_id=event_id,
        created_at="2026-07-22T10:05:30+00:00",
        principal="console:configured",
        session_id="d2a_session_0123456789",
        channel="console",
        source="digiwin_e10",
        operation="proposal_create",
        target="Quotation.quote_review",
        outcome="ok",
        reason_code="ok",
        query_id="qry_test",
        proposal_id="prp_test",
        dataset_version="ds_001",
        result_digest="sha256:" + "a" * 64,
        detail_json=canonical_json_dumps({"evidence_count": 1}),
    )


def test_fresh_db_creates_m5_evidence_tables_and_indexes(tmp_path):
    landing, _store = _make_store(tmp_path)
    tables = {
        r[0]
        for r in landing.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    indexes = {
        r[0]
        for r in landing.con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "d2a_gateway_query_evidence" in tables
    assert "d2a_gateway_proposal" in tables
    assert "d2a_gateway_proposal_evidence" in tables
    assert "d2a_gateway_audit" in tables
    assert "idx_d2a_gateway_query_session" in indexes
    assert "idx_d2a_gateway_proposal_session" in indexes
    assert "idx_d2a_gateway_audit_session" in indexes


def test_old_db_migrates_m5_evidence_tables(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE d2a_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL,
            sql TEXT NOT NULL, rows INTEGER, duration_ms REAL, batch_id TEXT
        );
        CREATE TABLE d2a_sync_run (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
            tables INTEGER, rows INTEGER, status TEXT, detail TEXT
        );
        CREATE TABLE d2a_dataset_version (
            dataset_version TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            template_version TEXT NOT NULL,
            status TEXT NOT NULL,
            built_at TEXT NOT NULL,
            published_at TEXT
        );
        CREATE TABLE d2a_object_version (
            dataset_version TEXT NOT NULL,
            object TEXT NOT NULL,
            object_version TEXT NOT NULL,
            binding_hash TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            build_table TEXT,
            status TEXT NOT NULL,
            built_at TEXT NOT NULL,
            published_at TEXT,
            PRIMARY KEY (dataset_version, object)
        );
        """
    )
    con.commit()
    con.close()

    landing = LandingStore(db)
    tables = {
        r[0]
        for r in landing.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "d2a_gateway_query_evidence" in tables
    assert "d2a_gateway_proposal" in tables
    assert "d2a_gateway_proposal_evidence" in tables
    assert "d2a_gateway_audit" in tables


def test_evidence_store_roundtrip_and_readonly_reopen(tmp_path):
    landing, store = _make_store(tmp_path)
    store.insert_query(_query_record())
    store.insert_proposal(_proposal_record())
    store.insert_proposal_evidence([_proposal_evidence_record()])
    store.insert_audit(_audit_record())

    query = store.get_query("qry_test")
    proposal = store.get_proposal("prp_test")
    evidence = store.list_proposal_evidence("prp_test")
    audit = store.list_audit(principal="console:configured")
    assert query is not None and query.result_digest.startswith("sha256:")
    assert proposal is not None and proposal.object == "Quotation"
    assert len(evidence) == 1 and evidence[0].claim == "客户账期较长"
    assert len(audit) == 1 and audit[0].proposal_id == "prp_test"

    landing.con.close()
    ro = LandingStore.open_readonly(tmp_path / "landing.sqlite")
    reread = ro.get_gateway_query_evidence("qry_test")
    assert reread is not None
    assert reread.session_id == "d2a_session_0123456789"
    ro.con.close()


def test_proposal_evidence_fk_requires_parent_proposal(tmp_path):
    _landing, store = _make_store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_proposal_evidence([_proposal_evidence_record(proposal_id="missing")])


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "UPDATE d2a_gateway_query_evidence SET target = 'X' WHERE query_id = ?",
            ("qry_test",),
        ),
        (
            "DELETE FROM d2a_gateway_query_evidence WHERE query_id = ?",
            ("qry_test",),
        ),
        (
            "UPDATE d2a_gateway_proposal SET conclusion = 'X' WHERE proposal_id = ?",
            ("prp_test",),
        ),
        (
            "DELETE FROM d2a_gateway_proposal WHERE proposal_id = ?",
            ("prp_test",),
        ),
        (
            "UPDATE d2a_gateway_proposal_evidence SET claim = 'X' WHERE proposal_id = ?",
            ("prp_test",),
        ),
        (
            "DELETE FROM d2a_gateway_proposal_evidence WHERE proposal_id = ?",
            ("prp_test",),
        ),
        (
            "UPDATE d2a_gateway_audit SET outcome = 'fail' WHERE event_id = ?",
            ("evt_test",),
        ),
        (
            "DELETE FROM d2a_gateway_audit WHERE event_id = ?",
            ("evt_test",),
        ),
    ],
)
def test_gateway_tables_are_immutable(tmp_path, sql, params):
    landing, store = _make_store(tmp_path)
    store.insert_query(_query_record())
    store.insert_proposal(_proposal_record())
    store.insert_proposal_evidence([_proposal_evidence_record()])
    store.insert_audit(_audit_record())
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        landing.con.execute(sql, params)


def test_commit_false_allows_atomic_rollback(tmp_path):
    landing, store = _make_store(tmp_path)
    record = _query_record()
    landing.con.execute("BEGIN IMMEDIATE")
    try:
        store.insert_query(record, commit=False)
        store.insert_query(record, commit=False)
        landing.con.commit()
    except sqlite3.IntegrityError:
        landing.con.rollback()
    assert store.get_query(record.query_id) is None
