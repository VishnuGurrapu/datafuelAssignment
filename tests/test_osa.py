"""
Unit and integration tests for QuickMart scraper, database, and OSA calculation.
Run with: pytest
"""
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
import pytest
from fastapi import HTTPException

from db import init_db, get_db_connection, upsert_stores, record_sweep, save_store_sweep
from sweep import parse_and_validate_as_of, QuickMartClient
from app import get_osa


@pytest.fixture
def test_db(tmp_path):
    db_file = str(tmp_path / "test_osa.db")
    init_db(db_file)
    return db_file


def test_ist_midnight_crossing_date_mapping():
    """
    Ensure sweeps crossing UTC/IST midnight are correctly attributed to their IST calendar day.
    IST = UTC + 5:30.
    """
    # 2026-09-27 19:00:00 UTC + 5:30 = 2026-09-28 00:30:00 IST -> Date must be 2026-09-28
    _, _, ist_date_1 = parse_and_validate_as_of("2026-09-27T19:00:00Z")
    assert ist_date_1 == "2026-09-28", f"Expected 2026-09-28 but got {ist_date_1}"

    # 2026-09-28 18:40:00 UTC + 5:30 = 2026-09-29 00:10:00 IST -> Date must be 2026-09-29
    _, _, ist_date_2 = parse_and_validate_as_of("2026-09-28T18:40:00Z")
    assert ist_date_2 == "2026-09-29", f"Expected 2026-09-29 but got {ist_date_2}"

    # 2026-09-28 04:30:00 UTC + 5:30 = 2026-09-28 10:00:00 IST -> Date must be 2026-09-28
    _, _, ist_date_3 = parse_and_validate_as_of("2026-09-28T04:30:00Z")
    assert ist_date_3 == "2026-09-28", f"Expected 2026-09-28 but got {ist_date_3}"


def test_ghost_stock_considered_in_stock(test_db):
    """
    Ghost stock occurs when in_stock == True but qty == 0.
    Must be treated as in-stock (OSA 100%), NOT out of stock.
    """
    conn = get_db_connection(test_db)
    stores = [{"store_id": "MUM-001", "city": "Mumbai", "name": "QuickMart Mumbai #1", "is_active": True, "is_serviceable": True}]
    upsert_stores(conn, stores)
    record_sweep(conn, "2026-09-28T04:30:00Z", "2026-09-28")

    ghost_items = [
        {
            "sku_id": "SKU-0001",
            "name": "Amul Taaza Toned Milk 500ml",
            "in_stock": True,
            "qty": 0,  # Ghost stock
            "price": 27.0,
            "observed_at": "2026-09-28T04:30:00Z",
        }
    ]
    save_store_sweep(conn, "MUM-001", "2026-09-28T04:30:00Z", is_complete=True, reason=None, items=ghost_items)
    conn.close()

    data = get_osa(city="Mumbai", date="2026-09-28", db_path=test_db)
    assert data["observations"] == 1
    assert data["osa_pct"] == 100.0, "Ghost stock with in_stock=True must be counted as 100% in stock"
    assert data["skus"][0]["in_stock"] == 1


def test_partial_store_never_counts_as_out_of_stock(test_db):
    """
    When a store returns partial: True (like DEL-004), its snapshot is incomplete.
    It must be reported under coverage.incomplete and MUST NOT contribute zero stock or skew OSA.
    """
    conn = get_db_connection(test_db)
    stores = [
        {"store_id": "DEL-001", "city": "Delhi", "name": "QuickMart Delhi #1", "is_active": True, "is_serviceable": True},
        {"store_id": "DEL-004", "city": "Delhi", "name": "QuickMart Delhi #4", "is_active": True, "is_serviceable": True},
    ]
    upsert_stores(conn, stores)
    record_sweep(conn, "2026-09-28T10:30:00Z", "2026-09-28")

    # DEL-001 is complete with 1 in-stock item
    del1_items = [{"sku_id": "SKU-0002", "name": "Bread", "in_stock": True, "qty": 10, "price": 40.0, "observed_at": "2026-09-28T10:30:00+05:30"}]
    save_store_sweep(conn, "DEL-001", "2026-09-28T10:30:00Z", is_complete=True, reason=None, items=del1_items)

    # DEL-004 is incomplete (partial response from server)
    save_store_sweep(conn, "DEL-004", "2026-09-28T10:30:00Z", is_complete=False, reason="partial response from server", items=[])
    conn.close()

    data = get_osa(city="Delhi", date="2026-09-28", db_path=test_db)

    # Coverage verification
    coverage = data["coverage"]
    assert coverage["stores_expected"] == 2
    assert coverage["stores_complete"] == 1
    assert len(coverage["incomplete"]) == 1
    assert coverage["incomplete"][0]["store_id"] == "DEL-004"
    assert coverage["incomplete"][0]["reason"] == "partial response from server"

    # Availability verification: DEL-004 must NOT lower the score to 50% or 0%
    assert data["observations"] == 1
    assert data["osa_pct"] == 100.0


def test_pooled_osa_vs_average_of_averages(test_db):
    """
    Overall city OSA must be total_in_stock / total_observations * 100.
    It must NOT be the arithmetic average of store-level percentages.
    """
    conn = get_db_connection(test_db)
    stores = [
        {"store_id": "MUM-001", "city": "Mumbai", "name": "QuickMart Mumbai #1", "is_active": True, "is_serviceable": True},
        {"store_id": "MUM-002", "city": "Mumbai", "name": "QuickMart Mumbai #2", "is_active": True, "is_serviceable": True},
    ]
    upsert_stores(conn, stores)
    record_sweep(conn, "2026-09-28T04:30:00Z", "2026-09-28")

    # Store 1 has 10 items, 10 in stock (100%)
    items_1 = [
        {"sku_id": f"SKU-{i:04d}", "name": f"Prod {i}", "in_stock": True, "qty": 5, "price": 50.0, "observed_at": "2026-09-28T04:30:00Z"}
        for i in range(1, 11)
    ]
    # Store 2 has 90 items, exactly 45 in stock (50%)
    items_2 = [
        {"sku_id": f"SKU-{i:04d}", "name": f"Prod {i}", "in_stock": (idx < 45), "qty": 5 if idx < 45 else 0, "price": 50.0, "observed_at": "2026-09-28T04:30:00Z"}
        for idx, i in enumerate(range(11, 101))
    ]
    save_store_sweep(conn, "MUM-001", "2026-09-28T04:30:00Z", is_complete=True, reason=None, items=items_1)
    save_store_sweep(conn, "MUM-002", "2026-09-28T04:30:00Z", is_complete=True, reason=None, items=items_2)
    conn.close()

    data = get_osa(city="Mumbai", date="2026-09-28", db_path=test_db)

    # Total in stock: 10 + 45 = 55. Total observations: 10 + 90 = 100.
    # Pooled OSA: 55 / 100 = 55.0%
    # (Average of averages would give (100% + 50%) / 2 = 75.0%, which is incorrect!)
    assert data["observations"] == 100
    assert data["osa_pct"] == 55.0, f"Expected pooled 55.0% but got {data['osa_pct']}%"


def test_idempotency_rerun(test_db):
    """
    Running a sweep twice for the same timestamp must be strictly idempotent.
    """
    conn = get_db_connection(test_db)
    stores = [{"store_id": "MUM-001", "city": "Mumbai", "name": "QuickMart Mumbai #1", "is_active": True, "is_serviceable": True}]
    upsert_stores(conn, stores)
    record_sweep(conn, "2026-09-28T04:30:00Z", "2026-09-28")

    items = [{"sku_id": "SKU-0001", "name": "Milk", "in_stock": True, "qty": 10, "price": 30.0, "observed_at": "2026-09-28T04:30:00Z"}]
    # Run 1
    save_store_sweep(conn, "MUM-001", "2026-09-28T04:30:00Z", is_complete=True, reason=None, items=items)
    # Run 2 (same data)
    save_store_sweep(conn, "MUM-001", "2026-09-28T04:30:00Z", is_complete=True, reason=None, items=items)

    count = conn.execute("SELECT COUNT(*) FROM inventory_observations WHERE store_id = 'MUM-001'").fetchone()[0]
    conn.close()

    assert count == 1, f"Expected 1 observation after rerun, but got {count}"


def test_invalid_city_validation():
    with pytest.raises(HTTPException) as excinfo:
        get_osa(city="Kolkata", date="2026-09-28")
    assert excinfo.value.status_code == 400
    assert "Invalid city" in excinfo.value.detail


def test_no_data_date_response(test_db):
    data = get_osa(city="Mumbai", date="2026-01-01", db_path=test_db)
    assert data["status"] == "no_data"
    assert data["osa_pct"] is None
    assert data["observations"] == 0
    assert data["skus"] == []


def test_rerun_does_not_overwrite_complete_with_incomplete(test_db):
    """
    Issue 1 (Case B):
    If a store sweep is already COMPLETE with observations, a subsequent rerun
    that results in INCOMPLETE must NOT overwrite the complete status or delete observations.
    """
    conn = get_db_connection(test_db)
    stores = [{"store_id": "MUM-001", "city": "Mumbai", "name": "QuickMart Mumbai #1", "is_active": True, "is_serviceable": True}]
    upsert_stores(conn, stores)
    as_of = "2026-09-28T04:30:00Z"
    record_sweep(conn, as_of, "2026-09-28")

    valid_items = [
        {"sku_id": "SKU-0001", "name": "Milk", "in_stock": True, "qty": 10, "price": 30.0, "observed_at": as_of},
        {"sku_id": "SKU-0002", "name": "Bread", "in_stock": True, "qty": 5, "price": 40.0, "observed_at": as_of},
    ]

    # Run 1: Store sweep succeeds (complete)
    save_store_sweep(conn, "MUM-001", as_of, is_complete=True, reason=None, items=valid_items)

    status_row = conn.execute("SELECT is_complete, reason, items_count FROM store_sweep_status WHERE store_id = 'MUM-001'").fetchone()
    assert status_row["is_complete"] == 1
    assert status_row["items_count"] == 2

    obs_count = conn.execute("SELECT COUNT(*) FROM inventory_observations WHERE store_id = 'MUM-001'").fetchone()[0]
    assert obs_count == 2

    # Run 2: Rerun fails (incomplete due to timeout / partial / network error)
    save_store_sweep(conn, "MUM-001", as_of, is_complete=False, reason="network timeout", items=[])

    # Case B check: complete status and observations must be PRESERVED
    status_after = conn.execute("SELECT is_complete, reason, items_count FROM store_sweep_status WHERE store_id = 'MUM-001'").fetchone()
    assert status_after["is_complete"] == 1, "Complete status must not be overwritten by incomplete run"
    assert status_after["reason"] is None
    assert status_after["items_count"] == 2

    obs_after = conn.execute("SELECT COUNT(*) FROM inventory_observations WHERE store_id = 'MUM-001'").fetchone()[0]
    assert obs_after == 2, "Valid observations must not be deleted on incomplete rerun"

    conn.close()


def test_scraper_handles_429_retry_after(monkeypatch):
    """
    Issue 2.1:
    Verify that QuickMartClient respects Retry-After header and eventually succeeds.
    """
    client = QuickMartClient(base_url="http://mock-portal")

    # First call returns 429 with Retry-After: 3
    resp_429 = Mock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "3"}

    # Second call returns 200 OK
    resp_200 = Mock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"ok": True, "meta": {"source": "origin"}}

    mock_get = Mock(side_effect=[resp_429, resp_200])
    monkeypatch.setattr(client.session, "get", mock_get)

    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    result = client.get("/v1/test")
    assert result == {"ok": True, "meta": {"source": "origin"}}
    assert mock_get.call_count == 2
    # Verify that sleep was called with duration >= 3.0 seconds (respecting Retry-After)
    assert any(s >= 3.0 for s in sleep_calls), f"Expected sleep >= 3.0s, got: {sleep_calls}"


def test_scraper_retries_transient_500_503_and_is_bounded(monkeypatch):
    """
    Issue 2.2:
    Verify that transient 500 and 503 errors are retried, and that retries are bounded.
    """
    client = QuickMartClient(base_url="http://mock-portal")
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    # Part A: Transient 500 then 503 then success
    resp_500 = Mock()
    resp_500.status_code = 500

    resp_503 = Mock()
    resp_503.status_code = 503

    resp_200 = Mock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"recovered": True, "meta": {"source": "origin"}}

    mock_transient = Mock(side_effect=[resp_500, resp_503, resp_200])
    monkeypatch.setattr(client.session, "get", mock_transient)

    res = client.get("/v1/test", max_retries=5)
    assert res["recovered"] is True
    assert mock_transient.call_count == 3

    # Part B: Unbounded failure raises RuntimeError when max_retries exhausted
    mock_permanent_fail = Mock(return_value=resp_503)
    monkeypatch.setattr(client.session, "get", mock_permanent_fail)

    with pytest.raises(RuntimeError) as excinfo:
        client.get("/v1/test", max_retries=3)
    assert "Failed to fetch" in str(excinfo.value)
    assert "after 3 attempts" in str(excinfo.value)
    assert mock_permanent_fail.call_count == 3


def test_soft_ban_detection_rejects_edge_response(monkeypatch):
    """
    Issue 2.3:
    Verify that when meta.source == "edge" is detected (soft ban), the degraded
    response is rejected/not accepted as valid inventory, and the client cools down before retrying.
    """
    client = QuickMartClient(base_url="http://mock-portal")
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    # 1st response: Degraded edge response from soft ban
    degraded_resp = Mock()
    degraded_resp.status_code = 200
    degraded_resp.json.return_value = {
        "store_id": "MUM-001",
        "items": [],  # Degraded/empty
        "meta": {"source": "edge", "generated_at": "2026-09-28T04:30:00Z"},
    }

    # 2nd response: Clean origin response after cooldown
    origin_resp = Mock()
    origin_resp.status_code = 200
    origin_resp.json.return_value = {
        "store_id": "MUM-001",
        "items": [{"sku_id": "SKU-0001", "name": "Milk", "in_stock": True, "qty": 10, "price": 30.0, "observed_at": "..."}],
        "meta": {"source": "origin", "generated_at": "2026-09-28T04:30:00Z"},
    }

    mock_get = Mock(side_effect=[degraded_resp, origin_resp])
    monkeypatch.setattr(client.session, "get", mock_get)

    result = client.get("/v1/stores/MUM-001/inventory")
    # Verify the degraded edge response was NOT accepted as the final result
    assert result["meta"]["source"] == "origin"
    assert len(result["items"]) == 1
    assert mock_get.call_count == 2
    # Verify cooldown sleep (22s) was triggered
    assert any(s >= 20.0 for s in sleep_calls), f"Expected cooldown sleep >= 20s, got: {sleep_calls}"


def test_pagination_duplicates_are_stored_only_once(test_db, monkeypatch):
    """
    Issue 2.4:
    Verify that duplicate sku_id appearing across pages (cursor bug) is stored only once.
    """
    conn = get_db_connection(test_db)
    stores = [{"store_id": "MUM-001", "city": "Mumbai", "name": "QuickMart Mumbai #1", "is_active": True, "is_serviceable": True}]
    upsert_stores(conn, stores)
    as_of = "2026-09-28T04:30:00Z"
    record_sweep(conn, as_of, "2026-09-28")

    client = QuickMartClient(base_url="http://mock-portal")

    # Page 1 returns SKU-0001 and SKU-0002
    page1 = {
        "items": [
            {"sku_id": "SKU-0001", "name": "Milk", "in_stock": True, "qty": 10, "price": 30.0, "observed_at": as_of},
            {"sku_id": "SKU-0002", "name": "Bread", "in_stock": True, "qty": 5, "price": 40.0, "observed_at": as_of},
        ],
        "partial": False,
        "next_cursor": "15",
    }
    # Page 2 duplicates SKU-0002 (simulating cursor bug) and introduces SKU-0003
    page2 = {
        "items": [
            {"sku_id": "SKU-0002", "name": "Bread", "in_stock": True, "qty": 5, "price": 40.0, "observed_at": as_of},
            {"sku_id": "SKU-0003", "name": "Butter", "in_stock": False, "qty": 0, "price": 50.0, "observed_at": as_of},
        ],
        "partial": False,
        "next_cursor": None,
    }

    mock_get = Mock(side_effect=[page1, page2])
    monkeypatch.setattr(client, "get", mock_get)

    is_complete, reason, items = client.fetch_store_inventory("MUM-001", as_of)
    assert is_complete is True
    # In-memory deduplication check: 3 unique items, not 4
    assert len(items) == 3
    sku_ids = [it["sku_id"] for it in items]
    assert sku_ids == ["SKU-0001", "SKU-0002", "SKU-0003"]

    # Save to database
    save_store_sweep(conn, "MUM-001", as_of, is_complete, reason, items)

    # Database check: exactly 3 rows in inventory_observations
    obs_rows = conn.execute("SELECT sku_id FROM inventory_observations WHERE store_id = 'MUM-001'").fetchall()
    db_sku_ids = [r["sku_id"] for r in obs_rows]
    assert len(db_sku_ids) == 3
    assert set(db_sku_ids) == {"SKU-0001", "SKU-0002", "SKU-0003"}

    conn.close()

