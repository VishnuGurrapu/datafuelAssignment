# DataFuel: Backend Engineer (Fresher) take-home

Hi! Thanks for applying. Please read this whole page once before starting. It's written so
you shouldn't need to ask anything, but if you're stuck, email **vansh@datafuel.tech**.

---

## What you'll do, in one paragraph

DataFuel collects product data from quick-commerce platforms so brands know where their
products are in stock. We've given you a **fake quick-commerce
server** called **QuickMart** (`mock_portal.py`). You'll write a **scraper** that collects
inventory from it into a database, and a small **API** that reports "what % of the time was
each product in stock" for a city on a day. The server misbehaves on purpose, like real
platforms do. **Handling that correctly is the main thing we're testing.**

| | |
|---|---|
| Time | About **4–5 hours** of work. You have **72 hours** from our email to submit. |
| Language | **Python 3.10+**. Any libraries you like. |
| Database | **SQLite** is recommended (Postgres is fine too). |
| AI tools | **Allowed.** Use Claude, ChatGPT, Cursor, Copilot, whatever you normally use. |
| Recording | **Record your screen** the whole time (see step 6). |
| Submit | A **public GitHub repo** + recording links, by replying to our email. |

## Rules
1. **Use AI freely, but understand everything.** On the follow-up call we'll ask you to
   explain your code. "The AI wrote it" isn't an answer.
2. **Work alone.** No help from other people.
3. **Don't edit `mock_portal.py`.** Pretend it's someone else's server.
4. **Unfinished is OK.** If you run out of time, stop and write what's left in `NOTES.md`.

---

## Step 1: Set up (10 minutes)

```bash
python3 --version          # must be 3.10 or newer (on Windows, type `python` instead of `python3`)
python3 mock_portal.py     # starts QuickMart on http://127.0.0.1:8765. Keep this terminal open
```
If it says the port is in use: `PORT=9000 python3 mock_portal.py` (Windows PowerShell:
`$env:PORT=9000; python mock_portal.py`), and use port 9000 everywhere below.

In a **second terminal**, check it works:
```bash
curl http://127.0.0.1:8765/v1/health
curl -H "X-Api-Key: dfhire-2026" "http://127.0.0.1:8765/v1/stores?page=1"
curl -H "X-Api-Key: dfhire-2026" "http://127.0.0.1:8765/v1/stores/MUM-001/inventory?as_of=2026-09-28T04:30:00Z"
```
Now **read `API.md` fully**. It explains every endpoint and field.

Then create your own GitHub repo (e.g. `datafuel-take-home`), copy the 4 files from this
folder into it, and **start your screen recording** (step 6).

---

## Step 2: Know what the server will do to you

QuickMart behaves like a real app's server. Expect all of this:

- **Too many requests per second** → error `429`. It tells you how long to wait (`Retry-After`).
- **Random errors** (`500`, `503`). Trying again usually works.
- **Some requests are very slow.** Don't wait forever on one.
- **Going too fast for too long makes it quietly give you bad data.** No error, just
  empty or shortened results. This is called a "soft ban". `API.md` (section
  "Fair use") and the `meta` field will help you spot it.
- **Some data is missing or incomplete**, and the server tells you so in the response.
- **Times come in different formats and timezones.**

A few smaller surprises aren't listed here. Noticing them is part of the task.

**The golden rule:** a wrong number is worse than no number. If we don't know something,
our data must say "we don't know". **Never store a guess, and never turn missing data into
"out of stock".**

---

## Step 3: Build the scraper (`sweep.py`)

One run = one **sweep** = the inventory of every store we should track, at one timestamp.

```bash
python3 sweep.py --as-of 2026-09-28T04:30:00Z
```

It should:
1. Get the store list from `/v1/stores`, and decide which stores to track. Write down why in `NOTES.md`.
2. For each store, fetch **all pages** of `/v1/stores/{id}/inventory` for that `as_of`.
3. Save the products into your database.
4. Save, for **each store**, whether the data for this sweep is **complete** or **not** (and why not).
5. Handle everything in Step 2 without crashing, hanging, or hammering the server.
6. Be **safe to run twice** for the same timestamp. Running it again must not create duplicates.
7. Print a short summary at the end, e.g. `26 stores: 25 complete, 1 incomplete (DEL-XXX: reason) · 1m52s`.

**Run it for all six sweeps** (times are UTC):
```bash
python3 sweep.py --as-of 2026-09-27T04:30:00Z
python3 sweep.py --as-of 2026-09-27T10:30:00Z
python3 sweep.py --as-of 2026-09-27T19:00:00Z
python3 sweep.py --as-of 2026-09-28T04:30:00Z
python3 sweep.py --as-of 2026-09-28T10:30:00Z
python3 sweep.py --as-of 2026-09-28T18:40:00Z
```
A careful sweep takes **a minute or so each**. If yours finishes in a few seconds, check
your data; you've probably been soft-banned.

---

## Step 4: Build the report API (`GET /osa`)

Use **FastAPI or Flask** (either is fine).

```
GET /osa?city=Mumbai&date=2026-09-28
```

**OSA (on-shelf availability)** = out of all the times we saw a product listed in a city's
stores on that day, the % of times it was in stock.
*Made-up example:* a product was seen 20 times across Mumbai's stores and sweeps that day,
and in stock 17 times → **85.00%**. The city's overall OSA works the same way across all
products: total in-stock ÷ total seen.

Details:
- `city` is `Mumbai`, `Delhi` or `Bengaluru`. Anything else → error `400` with a message.
- **`date` means the Indian (IST) calendar day.** The sweep times are in UTC, so work out
  which sweeps belong to which IST day. (IST = UTC + 5:30.)
- If `date` is not given, use **yesterday in IST**. Depending on the day you do this, that
  day will have **little or no data** (the six sweeps only cover 27–29 Sep IST). Your
  response must make that obvious (e.g. `"status": "no_data"` and the coverage). Never
  return 0% for a day we have no data for.
- Use the server's own `in_stock` field to decide if a product was in stock.
- Round percentages to 2 decimals.

**Return this shape** (numbers made up):
```json
{
  "city": "Mumbai",
  "date": "2026-09-28",
  "osa_pct": 91.30,
  "observations": 812,
  "coverage": {
    "stores_expected": 14,
    "stores_complete": 14,
    "incomplete": []
  },
  "skus": [
    {"sku_id": "SKU-0002", "name": "Britannia Brown Bread 400g",
     "observations": 20, "in_stock": 17, "osa_pct": 85.00}
  ]
}
```
`coverage` tells the reader how complete the data is. List any store and sweep we couldn't
get properly, with the reason, e.g. `{"store_id": "...", "sweep": "...", "reason": "..."}`.
You may add more fields if you think they help.

---

## Step 5: Short written parts

**a) `REVIEW.md`: review `review_me.py`.** An AI wrote this file and nobody checked it.
Find **at least 5 real problems**, most serious first. For each, say what goes wrong with
a concrete example (e.g. "MUM-002's saved rows also contain MUM-001's products"). Fix the
2–3 most serious. Try running it (`pip install requests`, with the server running).

**b) `NOTES.md`**
1. Your main decisions and why (for example: which stores you track, how you handle each
   problem from Step 2, how you detect the soft ban).
2. Answer in a few lines each:
   - A brand says: *"Your dashboard shows our Delhi availability fell from 92% to 41%
     yesterday."* What would you check first, before replying?
   - The app's own dashboard says a city sold ₹4.20 lakh yesterday, but adding up its
     store-level numbers gives ₹4.61 lakh. Which number would you show the brand, and why?
   - Where in this project would you **not** use an AI/LLM, and why?
3. *(Optional bonus)* What would you change if this had to run for 20,000 stores every 30 minutes?

**c) `AI_LOG.md`**
- Which AI tools you used, and for what.
- 2–3 prompts that mattered.
- **At least 2 times the AI was wrong**, and how you noticed.

**d) Tests.** Add at least **3 tests** (pytest) for the cases where a mistake would give
a wrong number. Examples: the same product appearing twice, a store we couldn't read
completely, a sweep that falls on a different IST day.

---

## Step 6: Record your screen

We want to see **how** you work: reading, using AI, debugging. Getting stuck is normal
and fine to see on video.

1. Record your **whole screen** from when you start until your last commit.
2. Breaks are OK: stop and start a new part (`part-1`, `part-2`, …). **Don't edit or speed up.**
3. At the end, record a **5-minute talk-through** with your voice: what you built, the hardest
   bug, and what you'd improve.
4. Upload to **YouTube as "Unlisted"** or **Google Drive ("Anyone with the link can view")**.
5. **Open every link in an incognito window** to check it works.
6. Put the links in `RECORDING.md`, with a few timestamps of key moments.

Free recorders: **OBS Studio** (any OS), **QuickTime** (Mac: File → New Screen Recording).
720p is enough. Close personal tabs and turn off notifications first.

---

## Step 7: Submit

Your **public** GitHub repo should look like this:
```
README.md        ← how to install, run the sweeps, start the API, run tests (exact commands)
sweep.py
app.py           ← your API (any name, say it in README)
tests/
mock_portal.py   ← unchanged
API.md
review_me.py     ← with your fixes
REVIEW.md
NOTES.md
AI_LOG.md
RECORDING.md
.gitignore       ← don't commit the database file, venv, or __pycache__
```
**Commit as you go** (not one big commit at the end). We look at the history.

Then **reply to our email** with:
1. your repo link;
2. your recording links;
3. the `/osa` output for `date=2026-09-28` for **Mumbai, Delhi and Bengaluru**;
4. how many hours it took you.

### Final checklist
- [ ] Repo is public and opens in an incognito window
- [ ] Fresh clone + your README commands → everything runs
- [ ] All 6 sweeps done; running a sweep again doesn't change the numbers
- [ ] `/osa` works for all 3 cities and includes `coverage`
- [ ] At least 3 tests, runnable with one command
- [ ] `REVIEW.md`, `NOTES.md`, `AI_LOG.md`, `RECORDING.md` in the repo
- [ ] Recording links + 5-minute talk-through all open in incognito
- [ ] Email has all 4 items

---

## What happens next
We reply within **3 working days**. If it looks good, we'll do a **1-hour video call** where
you run your code, explain it, and make one small change with us (AI allowed). The job is
**in person at our Lower Parel, Mumbai office**.

## How we judge it
Most important first:
1. **Are your numbers right?** (and honest about what's missing)
2. **Does your scraper handle the server's problems?** Especially the soft ban.
3. **Can you explain your code and choices?** (recording, notes, call)
4. **How do you use AI?** Checking its output matters more than how much you used it.
5. **Is the code clean enough for someone else to work on?**

We **don't** care about UI, Docker, cloud deployment, or extra features.

## FAQ
- **SQLite or Postgres?** SQLite is fine.
- **FastAPI or Flask?** Either.
- **The server gave me an error / weird data. Is it broken?** No, that's on purpose. Handle it.
  (If it won't even start, email us.)
- **The brief doesn't say how to handle something.** Pick a sensible option, write it in
  `NOTES.md`, move on.
- **Can I look inside `mock_portal.py`?** Yes, you can read it, but build your code from
  `API.md` and what you observe, as you would with a real app whose code you can't see.
- **Do I need a camera?** No. Screen + voice is enough.
- **I can't finish in time.** Submit what you have, with `NOTES.md` explaining what's left.
- **Questions?** vansh@datafuel.tech. Asking is a good sign, not a bad one.
