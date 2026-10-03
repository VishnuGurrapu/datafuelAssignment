# Code Review: `review_me.py`

This document details the critical defects identified in `review_me.py`, ordered from most serious to least serious, with concrete examples of failures.

---

### Problem 1: Mutable Default Argument Causes Cross-Store Data Leakage
- **Location:** Line 20: `def fetch_inventory(store_id, as_of, cursor="0", results=[]):`
- **What goes wrong:** In Python, default parameter expressions are evaluated once when the function is defined, not every time it is called. The list `results` persists in memory across separate top-level calls to `fetch_inventory`.
- **Concrete Example:**
  When `fetch_inventory("MUM-001", ...)` runs, it populates `results` with all ~30 products from `MUM-001`. When the script subsequently calls `fetch_inventory("MUM-002", ...)`, the default `results` parameter still holds all of `MUM-001`'s items. As `MUM-002`'s pages are fetched and appended, `save(conn, "MUM-002", ...)` writes all of `MUM-001`'s items into the database tagged under `store_id = "MUM-002"`, duplicating inventory and corrupting store-level data.
- **Fix:** Set `results=None` by default and initialize `if results is None: results = []` inside the function body.

---

### Problem 2: SQL Injection and Crash on Quoted Product Names
- **Location:** Lines 43–46:
  ```python
  conn.execute(
      f"INSERT INTO inventory VALUES ('{store_id}', '{it['sku_id']}', '{it['name']}', "
      f"{int(it['in_stock'])}, {it['qty']}, '{it['observed_at']}')"
  )
  ```
- **What goes wrong:** Data values are interpolated directly into SQL statements via f-strings rather than using parameterized queries (`?`). If any string contains a single quote or special character, the SQL parser breaks. It also leaves the database open to SQL injection.
- **Concrete Example:**
  If a SKU is named `"Haldiram's Bhujia 200g"` or `"Lay's Classic Salted"`, the generated SQL statement becomes:
  `INSERT INTO inventory VALUES ('MUM-001', 'SKU-0015', 'Haldiram's Bhujia 200g', ...)`
  SQLite will immediately crash with:
  `sqlite3.OperationalError: near "s": syntax error`, aborting the entire save transaction.
- **Fix:** Use parameterized queries:
  `conn.execute("INSERT INTO inventory VALUES (?, ?, ?, ?, ?, ?)", (store_id, it['sku_id'], it['name'], int(it['in_stock']), it['qty'], it['observed_at']))`.

---

### Problem 3: Stock Availability Logic Confuses `qty > 0` with `in_stock`
- **Location:** Line 61: `in_stock = sum(1 for (qty,) in rows if qty > 0)`
- **What goes wrong:** The script calculates stock availability based on whether `qty > 0` rather than the API's boolean `in_stock` flag. In real-world quick commerce (and explicitly in `mock_portal.py`), inventory can have `in_stock = True` while `qty = 0` (e.g. buffer stock, items reserved in carts, or platform ghost stock).
- **Concrete Example:**
  In `mock_portal.py` (lines 132–133), 5% of in-stock items have their `qty` set to `0`. If a store has 20 in-stock items, one of which has `qty = 0`, `review_me.py` counts only 19 as in stock, falsely reporting 95% availability instead of 100%. Conversely, if an item is out of stock (`in_stock = False`), relying on quantity could lead to discrepancies if leftover counts exist.
- **Fix:** Query `in_stock` from the database and check `if bool(in_stock)` instead of `qty > 0`.

---

### Problem 4: Average-of-Averages Distorts City-Wide Availability
- **Location:** Lines 62–63:
  ```python
  per_store.append(in_stock / len(rows) if rows else 0.0)
  return round(100 * sum(per_store) / len(per_store), 2)
  ```
- **What goes wrong:** The script calculates the OSA for each store individually, then averages those percentages (`sum(per_store) / len(per_store)`). This is the "average of averages" statistical fallacy (Simpson's Paradox). Stores with 5 observations are weighted identically to stores with 50 observations. Even worse, if a store in `stores` has 0 rows in the database (e.g. unserviceable or not yet fetched), it appends `0.0`, dragging down the entire city's metric.
- **Concrete Example:**
  Suppose Store A has 10 items, all 10 in stock (100%). Store B has 90 items, 45 in stock (50%). Total items: 100, total in stock: 55. True overall OSA is `55 / 100 = 55.0%`.
  However, `review_me.py` calculates `(100% + 50%) / 2 = 75.0%`, reporting an inflated number that is 20% higher than reality.
- **Fix:** Aggregate all observations across all stores in the city: `total_in_stock / total_observations * 100`.

---

### Problem 5: Infinite Retries on Non-Transient Errors, Missing Timeouts & Soft Ban Trigger
- **Location:** Lines 22–33:
  ```python
  while True:
      try:
          r = requests.get(..., params=..., headers=...)
          r.raise_for_status()
          break
      except Exception:
          time.sleep(0.1)
          continue
  ```
- **What goes wrong:**
  1. `requests.get` has no `timeout` parameter. If `mock_portal.py` triggers an 8-second delay or the connection hangs, the script stalls.
  2. `while True` retries indiscriminately on any exception, including client errors (HTTP 400, 401, 404). If `as_of` is missing a timezone, the server returns 400 Bad Request, causing an infinite loop.
  3. Retrying every 100ms (`time.sleep(0.1)`) hammers the server at 10 requests per second, immediately blowing past the 8 req/s burst limit (HTTP 429) and triggering the fair-use soft ban (>30 requests in 10s).
- **Concrete Example:**
  In line 72, `fetch_inventory(sid, datetime.utcnow().isoformat())` passes a timestamp like `2026-09-28T07:30:00.123456` without a timezone `Z` or `+00:00`. `mock_portal.py` returns HTTP 400 `{"error": "as_of must include a timezone"}`. `review_me.py` enters an infinite loop, spamming the server every 100ms and never completing.
- **Fix:** Add a request timeout (e.g., `timeout=10`), inspect HTTP status codes (only retry 429, 500, 503), respect `Retry-After`, and implement exponential backoff with a maximum attempt limit.

---

### Problem 6: Timezone Slicing Bug in Date Filtering
- **Location:** Line 58:
  ```python
  "SELECT qty FROM inventory WHERE store_id = ? AND substr(observed_at, 1, 10) = ?"
  ```
- **What goes wrong:** In `mock_portal.py`, Delhi stores (`DEL-*`) format `observed_at` in IST (`+05:30`), while Mumbai (`MUM-*`) and Bengaluru (`BLR-*`) format `observed_at` in UTC (`Z`). Slicing the first 10 characters `substr(observed_at, 1, 10)` compares UTC calendar dates for Mumbai with IST calendar dates for Delhi. Furthermore, sweeps conducted in UTC at 19:00Z correspond to the next calendar day in IST (00:30 AM IST).
- **Concrete Example:**
  A sweep at `2026-09-27T19:00:00Z` occurs on September 28 in India (00:30 IST). Slicing `substr("2026-09-27T19:00:00Z", 1, 10)` evaluates to `2026-09-27`, completely omitting these observations from September 28 queries.

---

### Problem 7: Failure to Deduplicate Cursor Overlaps
- **Location:** Line 35: `results.extend(body["items"])`
- **What goes wrong:** `mock_portal.py` deliberately injects duplicate items across pagination boundaries (lines 239–240: 35% chance that `cursor > 0` repeats `items[cursor - 1]`).
- **Concrete Example:**
  If Page 1 ends with `SKU-0015` and Page 2 prepends `SKU-0015`, `results.extend()` includes `SKU-0015` twice for the same snapshot. Both copies are saved into the database, skewing observation counts and biasing the OSA score.
