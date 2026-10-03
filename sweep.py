#!/usr/bin/env python3
"""
QuickMart inventory scraper (sweep.py)

Usage:
    python sweep.py --as-of 2026-09-28T04:30:00Z
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from db import get_db_connection, init_db, record_sweep, save_store_sweep, upsert_stores

IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_PORTAL = "http://127.0.0.1:8765"
API_KEY = "dfhire-2026"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sweep")


def parse_and_validate_as_of(as_of_str: str) -> Tuple[datetime, str, str]:
    """
    Parses and validates ISO-8601 as_of timestamp.
    Returns (utc_datetime, canonical_utc_iso_string, ist_date_string).
    """
    clean_str = as_of_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(clean_str)
    except Exception as e:
        raise ValueError(f"Invalid ISO-8601 timestamp '{as_of_str}': {e}")

    if dt.tzinfo is None:
        raise ValueError(f"as_of must include a timezone: '{as_of_str}'")

    utc_dt = dt.astimezone(timezone.utc)
    utc_iso = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    ist_dt = utc_dt.astimezone(IST)
    ist_date = ist_dt.strftime("%Y-%m-%d")

    return utc_dt, utc_iso, ist_date


class QuickMartClient:
    """
    Robust HTTP client for QuickMart Partner Portal.
    Includes rate-limiting (to prevent 429 and fair-use soft-bans),
    automatic retries on transient errors, and soft-ban detection.
    """

    def __init__(self, base_url: str = DEFAULT_PORTAL, api_key: str = API_KEY):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": self.api_key})

        self.last_request_time = 0.0
        # Enforce minimum delay between requests to stay well within fair-use limit
        # (mock_portal soft ban triggers if > 30 inventory requests in 10s -> max 3 req/s.
        # 0.45s delay guarantees < 2.22 req/s).
        self.min_interval = 0.45

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self.last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request_time = time.monotonic()

    def get(self, endpoint: str, params: Optional[Dict] = None, max_retries: int = 7) -> Dict:
        url = f"{self.base_url}{endpoint}"
        attempt = 0
        backoff = 0.5

        while attempt < max_retries:
            attempt += 1
            self._throttle()

            try:
                # 12s timeout allows mock_portal's artificial 8s delay to complete
                resp = self.session.get(url, params=params, timeout=12)

                if resp.status_code == 200:
                    data = resp.json()
                    # Soft ban detection:
                    # When degraded, mock_portal sets meta.source = "edge"
                    meta = data.get("meta", {})
                    if meta.get("source") == "edge":
                        logger.warning(
                            "Detected fair-use soft ban (source: edge). Cooling down for 22s..."
                        )
                        time.sleep(22.0)
                        self.last_request_time = time.monotonic()
                        continue
                    return data

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 2.0))
                    logger.warning(f"Rate limited (429). Retrying after {retry_after}s...")
                    time.sleep(retry_after + 0.1)
                    continue

                if resp.status_code in (500, 503):
                    logger.warning(
                        f"Server error {resp.status_code} for {endpoint} (attempt {attempt}/{max_retries}). "
                        f"Retrying in {backoff:.1f}s..."
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 4.0)
                    continue

                # Non-retryable client errors (400, 401, 404, etc.)
                resp.raise_for_status()

            except requests.Timeout:
                logger.warning(
                    f"Request timeout for {endpoint} (attempt {attempt}/{max_retries}). Retrying..."
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 4.0)
            except requests.RequestException as e:
                logger.warning(
                    f"Network error on {endpoint} (attempt {attempt}/{max_retries}): {e}. Retrying..."
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 4.0)

        raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts.")

    def fetch_all_stores(self) -> List[Dict]:
        """Fetch complete store roster across all pages."""
        stores = []
        page = 1
        while page is not None:
            data = self.get("/v1/stores", params={"page": page})
            stores.extend(data.get("stores", []))
            page = data.get("next_page")
        return stores

    def fetch_store_inventory(
        self, store_id: str, as_of_iso: str
    ) -> Tuple[bool, Optional[str], List[Dict]]:
        """
        Fetches all pages of inventory for one store at as_of.
        Returns:
            (is_complete: bool, reason: Optional[str], items: List[Dict])
        Deduplicates items across pagination boundaries.
        """
        items_by_sku: Dict[str, Dict] = {}
        cursor: Optional[str] = "0"

        try:
            while cursor is not None:
                params = {"as_of": as_of_iso, "cursor": cursor}
                data = self.get(f"/v1/stores/{store_id}/inventory", params=params)

                # Check partial flag
                if data.get("partial") is True:
                    return False, "partial response from server", []

                page_items = data.get("items", [])
                for item in page_items:
                    sku_id = item["sku_id"]
                    # Retain first seen SKU (deduplicate page overlap injected by cursor bug)
                    if sku_id not in items_by_sku:
                        items_by_sku[sku_id] = item

                cursor = data.get("next_cursor")

            return True, None, list(items_by_sku.values())

        except Exception as e:
            return False, f"error: {str(e)}", []


def run_sweep(as_of_str: str, db_path: str = "osa.db", portal_url: str = DEFAULT_PORTAL) -> None:
    start_time = time.monotonic()
    utc_dt, as_of_utc, ist_date = parse_and_validate_as_of(as_of_str)

    logger.info(f"Starting sweep for as_of: {as_of_utc} (IST date: {ist_date})")

    init_db(db_path)
    client = QuickMartClient(base_url=portal_url)

    # 1. Fetch store roster
    all_stores = client.fetch_all_stores()
    conn = get_db_connection(db_path)

    try:
        upsert_stores(conn, all_stores)
        record_sweep(conn, as_of_utc, ist_date)

        # 2. Decide which stores to track:
        # We track all active stores (is_active == True).
        # Inactive stores are decommissioned/shut down and should not be tracked
        # even if is_serviceable flag is erroneously set to True.
        # Active stores that are temporarily unserviceable (e.g. DEL-006) are still tracked.
        tracked_stores = [s for s in all_stores if s.get("is_active")]
        logger.info(f"Tracking {len(tracked_stores)} active stores out of {len(all_stores)} total stores.")

        complete_count = 0
        incomplete_details: List[Tuple[str, str]] = []

        # 3. For each tracked store, fetch inventory
        for store in tracked_stores:
            sid = store["store_id"]
            is_complete, reason, items = client.fetch_store_inventory(sid, as_of_utc)

            save_store_sweep(conn, sid, as_of_utc, is_complete, reason, items)

            if is_complete:
                complete_count += 1
            else:
                incomplete_details.append((sid, reason or "unknown"))

        elapsed_sec = int(time.monotonic() - start_time)
        minutes, seconds = divmod(elapsed_sec, 60)
        duration_str = f"{minutes}m{seconds:02d}s"

        incomplete_count = len(incomplete_details)
        if incomplete_details:
            details_str = ", ".join(f"{sid}: {r}" for sid, r in incomplete_details)
            summary_str = (
                f"{len(tracked_stores)} stores: {complete_count} complete, "
                f"{incomplete_count} incomplete ({details_str}) · {duration_str}"
            )
        else:
            summary_str = (
                f"{len(tracked_stores)} stores: {complete_count} complete, "
                f"0 incomplete · {duration_str}"
            )

        print(summary_str.encode("utf-8", errors="replace").decode("utf-8"))

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Run QuickMart inventory sweep")
    parser.add_argument(
        "--as-of",
        required=True,
        help="Target timestamp in ISO-8601 with timezone (e.g. 2026-09-28T04:30:00Z)",
    )
    parser.add_argument(
        "--db",
        default="osa.db",
        help="Path to SQLite database (default: osa.db)",
    )
    parser.add_argument(
        "--portal",
        default=DEFAULT_PORTAL,
        help=f"QuickMart portal base URL (default: {DEFAULT_PORTAL})",
    )
    args = parser.parse_args()

    run_sweep(as_of_str=args.as_of, db_path=args.db, portal_url=args.portal)


if __name__ == "__main__":
    main()
