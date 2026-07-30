"""M2 归因在关联字段未核对时必须保持空集。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from data2agent.middle.extract.adapters.sqlite import SqliteReadOnlyAdapter
from data2agent.shared.store.dataset_publish import build_dataset, resolve_published_snapshot
from data2agent.middle.extract.increment import incremental_sync
from tests.helpers import watermarks_from_pack
from data2agent.shared.store.landing import LandingStore, raw_table_name
from tests.helpers import whitelist_from_pack
from data2agent.shared.metamodel.loader import load_pack
from tests.fixtures.e10.seed import build, write_db

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "digiwin_e10"


def _published(tmp_path: Path) -> LandingStore:
    source_db = tmp_path / "e10.sqlite"
    write_db(source_db, build(seed=42, asof=date(2026, 7, 10)))
    pack = load_pack(ROOT / "templates")
    landing = LandingStore(tmp_path / "landing.sqlite")
    whitelist = whitelist_from_pack(pack, SOURCE)
    assert "ITEM_WAREHOUSE" in whitelist
    adapter = SqliteReadOnlyAdapter(str(source_db), whitelist)
    incremental_sync(adapter, landing, SOURCE, watermarks_from_pack(pack, SOURCE))
    assert build_dataset(landing, pack, SOURCE, auto_publish=True).published
    return landing


def test_m2_does_not_infer_purchase_or_production_relationships(tmp_path: Path) -> None:
    landing = _published(tmp_path)
    snap = resolve_published_snapshot(landing, SOURCE)
    assert {"DeadStockAttribution", "PurchaseOverbuyEvidence", "ProductionLossEvidence"} <= set(snap.objects)

    for logical_table in (
        "D2A_PURCHASE_OVERBUY_EVIDENCE", "D2A_PRODUCTION_LOSS_EVIDENCE", "D2A_DEAD_STOCK_ATTRIBUTION",
    ):
        table = raw_table_name(SOURCE, logical_table)
        assert landing.con.execute(
            f'SELECT COUNT(*) AS count FROM "{table}" WHERE _d2a_deleted_at IS NULL',
        ).fetchone()["count"] == 0
