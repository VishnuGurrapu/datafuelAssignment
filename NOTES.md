# Implementation Notes & Decisions

## 1. Key Architectural Decisions & Problem Handling

### Store Selection & Fleet Tracking
* **Roster Fetching:** We query all pages from `/v1/stores` to retrieve QuickMart's complete store network (30 stores total across Mumbai, Delhi, and Bengaluru).
* **Tracked Fleet:** We strictly filter for active stores (`is_active == True`), yielding **26 operational stores** (9 in Mumbai, 9 in Delhi, 8 in Bengaluru).
  * **Inactive Store Filtering:** Stores `MUM-009`, `DEL-010`, `BLR-004`, and `BLR-010` are decommissioned (`is_active: false`). Notably, `MUM-009` and `BLR-004` report `is_serviceable: true` despite being shut down—a classic operational data inconsistency where legacy serviceable flags were not cleared. Tracking shut-down stores would artificially dilute coverage and availability.
  * **Temporarily Unserviceable Stores:** Store `DEL-006` is `is_active: true` but `is_serviceable: false`. We **do track** `DEL-006` because it is an active dark store temporarily pausing delivery (e.g. monsoon rain, local maintenance, or temporary staffing shortages); its inventory snapshot remains accessible and representative of brand stocking.
  * **Pre-Launch Stores:** Store `BLR-007` launched on `2026-09-27T12:00:00Z`. In sweeps conducted prior to launch (`2026-09-27T04:30:00Z` and `2026-09-27T10:30:00Z`), the store returns an empty inventory list (`items: []`) with `partial: false`. This is recorded as a complete snapshot with 0 items, neither flagging an error nor distorting availability.

### Rate Limiting & Handling Step 2 Problems
* **Burst Limit (HTTP 429):** QuickMart throttles requests exceeding ~8 req/second with HTTP 429 and a `Retry-After: 2` header. We implement a client-side request throttle enforcing a minimum interval of 0.45s between requests (~2.2 req/s). If a 429 occurs, the client dynamically sleeps the duration specified in `Retry-After` plus a safety margin.
* **Transient Failures (HTTP 500 & 503):** The portal introduces random 7% 503 errors and deterministic 500 errors on `MUM-007` (which always fails its first 2 attempts). We implement exponential backoff with jitter (`0.5s`, `1.0s`, `2.0s`, `4.0s`) with up to 7 retries, easily absorbing consecutive 503/500 collisions.
* **Artificial Latency Spikes:** QuickMart injects an 8-second delay on ~3% of requests. We configured an explicit 12-second HTTP timeout, ensuring delayed requests complete without raising timeout exceptions or stalling subsequent requests.
* **Pagination Overlap Deduplication:** On ~35% of paginated inventory requests (`cursor > 0`), QuickMart repeats the previous item (`items[cursor - 1]`). We deduplicate items per store snapshot using a set of seen `sku_id` keys, guaranteeing every SKU is observed at most once per store sweep.
* **Ghost Inventory Handling:** For ~5% of in-stock items, `qty` is set to `0` despite `in_stock: true`. We strictly evaluate stock availability using the boolean `in_stock` field, never checking `qty > 0`.
* **Price & Timezone Inconsistencies:** Bengaluru stores format prices as 2-decimal strings (`"237.50"`), while Mumbai/Delhi return floats; Delhi stores return `observed_at` in IST offset (`+05:30`), while others use UTC (`Z`). All prices are cast to float, and timestamps are parsed as timezone-aware UTC timestamps.

### Soft Ban Detection & Prevention
* **Mechanism:** QuickMart enforces an undisclosed fair-use policy: making more than 30 inventory requests within a 10-second sliding window triggers a 20-second soft ban. During a soft ban, the server silently returns HTTP 200 with truncated/empty items, `next_cursor: null`, and `meta.source: "edge"`.
* **Prevention:** By throttling requests to 0.45s intervals (<2.22 req/s), our client issues at most ~22 requests per 10-second window, staying safely below the 30-request threshold and preventing the soft ban entirely under normal operation.
* **Detection & Recovery:** If `meta.get("source") == "edge"` is observed in any response, the client recognizes silent degradation, logs a warning, sleeps for 22 seconds to clear the server's cooldown window, and re-fetches the store inventory.

### Completeness & "The Golden Rule"
* QuickMart occasionally returns incomplete snapshots (e.g. `DEL-004` at `2026-09-28T10:30:00Z` returns `partial: true` with `items: []`).
* Adhering strictly to the golden rule (*"A wrong number is worse than no number. Never turn missing data into out of stock"*), incomplete store sweeps are recorded in `store_sweep_status` with `is_complete = 0` and `reason = "partial response from server"`. They are surfaced transparently in `coverage.incomplete` and excluded from product availability calculations so they do not falsely depress OSA.

---

## 2. Practical Engineering Scenarios

### Scenario A: Delhi Availability Drop (92% → 41%)
> *A brand says: "Your dashboard shows our Delhi availability fell from 92% to 41% yesterday." What would you check first, before replying?*

1. **Sweep Coverage & Incomplete Store Snapshots:**
   Inspect `coverage.incomplete` and `store_sweep_status` for Delhi across all sweeps for that day. Did multiple Delhi dark stores fail, timeout, or return `partial: true`? If an erroneous query counted incomplete store sweeps as zero stock, availability would artificially plummet.
2. **Execution Logs & Soft-Ban Telemetry:**
   Check whether the scraper was degraded by a soft ban or network outage during Delhi sweeps, leading to truncated catalog responses.
3. **Catalog & SKU Mapping Changes:**
   Verify if the brand's SKU names or IDs changed (e.g. SKU renames or delistings), causing observations to be attributed to an unrecognized identifier or new unmapped SKU.
4. **Physical Store Fleet Status:**
   Check if Delhi experienced severe weather or localized disruptions (e.g. flash flooding, municipal road closures) that caused multiple dark stores to flip `is_serviceable: false` or stop replenishment.

### Scenario B: City Total (₹4.20 lakh) vs Sum of Stores (₹4.61 lakh)
> *The app's own dashboard says a city sold ₹4.20 lakh yesterday, but adding up its store-level numbers gives ₹4.61 lakh. Which number would you show the brand, and why?*

* **Recommendation:** Display the **store-level gross sales (₹4.61 lakh)** alongside a **reconciliation breakdown showing net platform billing (₹4.20 lakh)**, rather than hiding either number.
* **Why:**
  * The ₹41,000 variance typically represents **post-checkout customer cancellations, returns, delivery rejections, or platform-level discount vouchers/subsidies** that are applied at the city/cart level rather than deducted from individual store order manifests.
  * Brands need **store-level numbers (₹4.61 lakh)** for physical inventory tracking, warehouse replenishment, and shrinkage audits (what actually left the shelf).
  * Brands need **net platform sales (₹4.20 lakh)** for financial accounting, settlement invoices, and revenue realization. Presenting both with an explicit reconciliation bridge ("Gross Store Dispatched: ₹4.61L, Less City-Level Promotions & In-Flight Returns: ₹0.41L, Net Realized: ₹4.20L") builds complete trust and prevents confusion.

### Scenario C: Where NOT to Use AI / LLMs in this Project
> *Where in this project would you not use an AI/LLM, and why?*

1. **Metric Aggregation & Statistical Formulas (OSA Calculation):**
   Computing availability requires strict relational math: `SUM(in_stock) / SUM(observations)`. LLMs are non-deterministic, frequently make arithmetic mistakes, struggle with Simpson's paradox (averaging store percentages instead of pooling observations), and can hallucinate edge-case totals. Deterministic SQL queries guarantee reproducible, bit-exact calculations.
2. **Date & Timezone Boundary Transformations:**
   Mapping UTC timestamps (`2026-09-27T19:00:00Z`) to IST calendar days (`2026-09-28`) must follow rigorous calendar logic. LLMs frequently misinterpret timezone offsets across midnight boundaries or default to substring string slicing (`substr(timestamp, 1, 10)`), which silently corrupts daily reporting.
3. **Network Rate Limiting & Retry State Machines:**
   Throttling request intervals, tracking exponential backoff jitter, parsing `Retry-After` headers, and cooldown timers require deterministic timing loops.

---

## 3. Scaling Architecture: 20,000 Stores Every 30 Minutes

To scale from 26 stores to 20,000 stores every 30 minutes (~40 million observations daily):

1. **Distributed Worker Architecture:**
   * Break each sweep into fine-grained tasks (one store per task) orchestrated by a distributed task queue (e.g. **Temporal**, **Celery**, or **AWS SQS** with autoscaled worker pods in Kubernetes).
   * 20,000 stores / 30 minutes = ~11.1 stores/second. With ~2 pages per store, this requires ~22–25 requests/second sustained throughput.
2. **Proxy Management & IP Rotation:**
   * A single egress IP cannot scrape 20,000 stores without triggering platform-wide IP rate limits and anti-bot bans.
   * Route scraper workers through an enterprise rotating proxy pool with geographic pinning matching the dark store's region.
3. **Asynchronous Non-Blocking I/O:**
   * Replace synchronous `requests` with asynchronous HTTP clients (`httpx` or `aiohttp`) utilizing `asyncio`, enabling a single worker instance to handle hundreds of concurrent I/O-bound store connections with minimal CPU overhead.
4. **Columnar / Analytical Database:**
   * SQLite is unsuitable for concurrent write throughput at this scale.
   * Ingest raw sweep observations into a distributed columnar data warehouse such as **ClickHouse** or **PostgreSQL with TimescaleDB / BigQuery**.
   * Use partition keys on `(ist_date, city_id)` and pre-aggregated Materialized Views for instantaneous sub-second OSA dashboard queries.
