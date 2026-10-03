"""
FastAPI application for QuickMart On-Shelf Availability (OSA) reporting.

Endpoint:
    GET /osa?city=Mumbai&date=2026-09-28
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from db import get_db_connection, init_db

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db("osa.db")
    yield

app = FastAPI(title="QuickMart OSA Reporting API", version="1.0.0", lifespan=lifespan)

IST = timezone(timedelta(hours=5, minutes=30))
VALID_CITIES = {"Mumbai", "Delhi", "Bengaluru"}


def get_yesterday_ist() -> str:
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    yesterday_ist = now_ist - timedelta(days=1)
    return yesterday_ist.strftime("%Y-%m-%d")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/osa")
def get_osa(
    city: str = Query(..., description="Target city: Mumbai, Delhi, or Bengaluru"),
    date: Optional[str] = Query(
        None, description="IST calendar date in YYYY-MM-DD format (defaults to yesterday in IST)"
    ),
    db_path: str = "osa.db",
):
    # 1. Validate city
    if city not in VALID_CITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid city '{city}'. Allowed cities are: {', '.join(sorted(VALID_CITIES))}",
        )

    # 2. Resolve date (IST calendar day)
    target_date = date.strip() if date else get_yesterday_ist()

    # Validate date format YYYY-MM-DD
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format '{target_date}'. Expected YYYY-MM-DD.",
        )

    conn = get_db_connection(db_path)
    try:
        # Check tracked stores in this city
        city_stores = conn.execute(
            "SELECT store_id, name FROM stores WHERE city = ? AND is_active = 1",
            (city,),
        ).fetchall()
        city_store_ids = [r["store_id"] for r in city_stores]

        # Sweeps conducted for this IST date
        sweeps = conn.execute(
            "SELECT as_of_utc FROM sweeps WHERE ist_date = ? ORDER BY as_of_utc ASC",
            (target_date,),
        ).fetchall()
        sweep_timestamps = [r["as_of_utc"] for r in sweeps]

        # Case 1: No sweeps found for this date
        if not sweep_timestamps or not city_store_ids:
            return {
                "city": city,
                "date": target_date,
                "status": "no_data",
                "osa_pct": None,
                "observations": 0,
                "coverage": {
                    "sweeps_conducted": len(sweep_timestamps),
                    "stores_expected": len(city_store_ids) * len(sweep_timestamps),
                    "stores_complete": 0,
                    "incomplete": [],
                },
                "skus": [],
            }

        # Case 2: Sweeps exist for this date
        total_expected_snapshots = len(city_store_ids) * len(sweep_timestamps)

        # Query completeness status for every expected (store_id, sweep) pair
        placeholders_stores = ",".join("?" for _ in city_store_ids)
        placeholders_sweeps = ",".join("?" for _ in sweep_timestamps)

        status_rows = conn.execute(
            f"""
            SELECT store_id, as_of_utc, is_complete, reason, items_count
            FROM store_sweep_status
            WHERE store_id IN ({placeholders_stores})
              AND as_of_utc IN ({placeholders_sweeps})
            """,
            city_store_ids + sweep_timestamps,
        ).fetchall()

        status_map = {(r["store_id"], r["as_of_utc"]): r for r in status_rows}

        complete_count = 0
        incomplete_list: List[Dict[str, Any]] = []

        for sid in city_store_ids:
            for as_of in sweep_timestamps:
                st = status_map.get((sid, as_of))
                if st and st["is_complete"] == 1:
                    complete_count += 1
                else:
                    reason = st["reason"] if st and st["reason"] else "missing from sweep"
                    incomplete_list.append(
                        {"store_id": sid, "sweep": as_of, "reason": reason}
                    )

        # Query all observations for COMPLETE store sweeps in this city and IST date
        # Note: We join with store_sweep_status and ensure is_complete = 1
        # to guarantee incomplete store sweeps are never counted as out of stock.
        obs_rows = conn.execute(
            f"""
            SELECT o.sku_id, o.name, o.in_stock, o.as_of_utc
            FROM inventory_observations o
            JOIN store_sweep_status s ON o.store_id = s.store_id AND o.as_of_utc = s.as_of_utc
            JOIN stores st ON o.store_id = st.store_id
            JOIN sweeps sw ON o.as_of_utc = sw.as_of_utc
            WHERE st.city = ?
              AND sw.ist_date = ?
              AND s.is_complete = 1
            ORDER BY o.sku_id ASC, o.as_of_utc DESC
            """,
            (city, target_date),
        ).fetchall()

        if not obs_rows:
            return {
                "city": city,
                "date": target_date,
                "status": "no_data",
                "osa_pct": None,
                "observations": 0,
                "coverage": {
                    "sweeps_conducted": len(sweep_timestamps),
                    "stores_expected": total_expected_snapshots,
                    "stores_complete": complete_count,
                    "incomplete": incomplete_list,
                },
                "skus": [],
            }

        # Aggregate metrics per SKU
        sku_data: Dict[str, Dict[str, Any]] = {}
        total_in_stock = 0
        total_obs = len(obs_rows)

        for row in obs_rows:
            sku_id = row["sku_id"]
            name = row["name"]
            in_stock = bool(row["in_stock"])

            if sku_id not in sku_data:
                sku_data[sku_id] = {
                    "sku_id": sku_id,
                    "name": name,
                    "observations": 0,
                    "in_stock": 0,
                }
            # Keep the latest observed name for display
            sku_data[sku_id]["name"] = name
            sku_data[sku_id]["observations"] += 1
            if in_stock:
                sku_data[sku_id]["in_stock"] += 1
                total_in_stock += 1

        skus_list = []
        for sku_id in sorted(sku_data.keys()):
            item = sku_data[sku_id]
            obs = item["observations"]
            stk = item["in_stock"]
            pct = round(100.0 * stk / obs, 2) if obs > 0 else 0.0
            skus_list.append(
                {
                    "sku_id": sku_id,
                    "name": item["name"],
                    "observations": obs,
                    "in_stock": stk,
                    "osa_pct": pct,
                }
            )

        overall_osa = round(100.0 * total_in_stock / total_obs, 2) if total_obs > 0 else 0.0

        return {
            "city": city,
            "date": target_date,
            "osa_pct": overall_osa,
            "observations": total_obs,
            "coverage": {
                "stores_expected": total_expected_snapshots,
                "stores_complete": complete_count,
                "incomplete": incomplete_list,
            },
            "skus": skus_list,
        }

    finally:
        conn.close()
