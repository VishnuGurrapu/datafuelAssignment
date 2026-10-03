# AI Interaction Log (`AI_LOG.md`)

This log documents the AI tools utilized, key prompts, and specific instances where the AI generated incorrect code or assumptions, along with how those errors were identified and resolved.

---

## 1. AI Tools Used & Purpose

* **Tool:** Antigravity AI Assistant (powered by Gemini 3.8 Flash)
* **Purpose:**
  * End-to-end repository inspection and edge-case discovery in `mock_portal.py`.
  * Code review and flaw diagnosis for `review_me.py`.
  * Architecture design for rate limiting, retry backoff, database schema, and FastAPI `/osa` service.
  * Writing test cases covering high-risk statistical and timezone boundaries.

---

## 2. Key Prompts That Mattered

### Prompt 1: Initial Deep Inspection & Risk Assessment
> *"Before writing or modifying any code, inspect the entire repository and understand the assignment requirements from README.md, API.md, mock_portal.py, review_me.py... Identify the important edge cases deliberately included in mock_portal.py... Identify the most important correctness risks where an AI implementation could produce incorrect OSA numbers."*
* **Impact:** Led to discovering that `mock_portal.py` contains subtle failure modes: the fair-use soft ban (`meta.source == "edge"` with silent truncation), 35% cursor duplication, ghost stock (`in_stock=True` with `qty=0`), and most importantly, the UTC-to-IST midnight boundary shift where sweeps on Sept 27 at 19:00 UTC belong to Sept 28 IST.

### Prompt 2: Architectural Decoupling & Schema Design
> *"Propose a clean implementation architecture for sweep.py, SQLite database, app.py /osa API, and tests. Propose the database schema."*
* **Impact:** Established the composite primary key `(store_id, as_of_utc, sku_id)` to eliminate pagination duplicates, decoupled sweep completeness (`store_sweep_status`) from observation availability, and computed `ist_date` at sweep ingestion time for timezone-safe queries.

---

## 3. Instances Where the AI Was Wrong & How They Were Caught

### Mistake 1: Pytest TestClient Missing Dependency
* **The Error:** The AI initially used `from fastapi.testclient import TestClient` in `tests/test_osa.py`. In modern Starlette/FastAPI versions, `TestClient` requires the external `httpx` package, which was not pre-installed in the virtual environment.
* **How It Was Caught:** Running `pytest` immediately threw:
  `RuntimeError: The starlette.testclient module requires the httpx package to be installed.`
* **How It Was Resolved:** Instead of adding an unnecessary third-party dependency, the AI refactored `app.py` to return standard Python dictionaries and updated `tests/test_osa.py` to call `get_osa()` directly as a native function and inspect `fastapi.HTTPException`. This made tests run faster and completely eliminated external dependency issues.

### Mistake 2: Off-By-One Logic Error in Pooled OSA Test
* **The Error:** In `test_pooled_osa_vs_average_of_averages`, the AI intended to generate 90 items for Store 2 with exactly 45 in-stock items (50%). It wrote `items_2 = [{"sku_id": f"SKU-{i:04d}", ..., "in_stock": (i <= 45)} for i in range(11, 101)]`. Because the range started at 11, `11 <= i <= 45` only produced 35 in-stock items, not 45.
* **How It Was Caught:** Pytest execution failed:
  `AssertionError: Expected pooled 55.0% but got 45.0%`.
* **How It Was Resolved:** The AI corrected the condition to `idx < 45` using `enumerate(range(11, 101))`, ensuring exactly 45 in-stock items were generated and the pooled availability asserted to 55.0% as mathematically expected.

### Mistake 3: Deprecated Startup Event Handler
* **The Error:** In `app.py`, the AI initially used `@app.on_event("startup")` for database initialization.
* **How It Was Caught:** Pytest output captured a `DeprecationWarning: on_event is deprecated, use lifespan event handlers instead.`
* **How It Was Resolved:** Refactored the FastAPI application to use the modern `@asynccontextmanager` `lifespan` handler, ensuring zero deprecation warnings under Python 3.12 and FastAPI latest.
