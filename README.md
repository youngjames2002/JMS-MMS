# JMS Material Management System

A Streamlit app for tracking flat (sheet) stock at JMS Metaltec — what's on the shelf, what's committed to upcoming jobs, and what's on order.

It pulls from three systems and reconciles them against a single canonical list of stock descriptions:

| Source | Via | What it provides |
|---|---|---|
| SharePoint | Microsoft Graph API | Bundle staging sheet, provisional nest PDFs |
| Statii (ERP) | REST API | Purchase order lines — incoming material |
| PostgreSQL | SQLAlchemy | Stock take submissions, live stock |

## Status

**Early development, but running end to end.** [`data.py`](data.py) covers SharePoint, Statii, nest parsing, stock takes and live stock. Every page is built: the dashboard, material usage and delivery, performing a stock take, and stock take history. [`debug.py`](debug.py) remains the harness that exercises each data function on its own.

Start it with **`streamlit run login.py`** — that is the entry script, not `debug.py`.

### What the numbers depend on

Three feeds bound what the app can show. None of them is a code problem, and the dashboard states each one on the page rather than letting an empty table read as good news:

- **The projection reaches only as far as the schedule does.** Every incomplete flat bundle on the staging sheet sits inside a five week window, so the dashboard shows five weeks. Widening the view would add empty columns; what limits it is bundles not being scheduled further out.
- **A bundle with no nest is invisible to demand.** Material it needs looks available, because nothing says otherwise. Roughly a quarter of incomplete bundles are in that state at any time, which is the largest blind spot in the figures.
- **Purchase orders count only if they are in the future.** `get_po_lines()` filters to `date_promised > today`, so when Statii holds nothing promised ahead the on-order columns read zero for real.

## How it works

### Login

Streamlit's own OpenID Connect support does the work — there is no MSAL flow to maintain. [`login.py`](login.py) is the entry script and calls `st.login("microsoft")`; `st.user` carries whoever comes back. It needs `Authlib` installed and the `[auth]` / `[auth.microsoft]` blocks in secrets.

Every page begins with `page_setup()` from [`ui.py`](ui.py), which sets the page config, gates on auth, then draws the logo and title row. The gate is **per page** deliberately: a page reached by pasting its URL has to pass the same check as one reached from the sidebar.

Those helpers live in `ui.py` rather than `login.py` for a reason worth keeping. Importing `login.py` from a page executes that file's whole body, so the login screen drew itself on top of whichever page imported it first — and because Python caches the module, *which* page that was varied with what the server had already loaded. `ui.py` renders nothing when imported.

`TEST_MODE = true` in local secrets skips the login entirely. Leave it out of the deployed secrets so auth runs live there.

### The weekly projection

The dashboard answers when each material runs short and which bundles are responsible. It models what the `FlatStock_Report` macro on the Steel Stock workbook produces, one sheet per week.

Demand is collapsed to one row per bundle per material first, so a bundle split across several nest PDFs counts once. Those rows are bucketed into weeks ending Thursday (`pd.offsets.Week(weekday=3).rollforward`), and a cumulative sum down the weeks is subtracted from stock on hand. The week where that balance crosses zero is when the material runs out; the bundle refs carried alongside are why.

Every material is projected against every week rather than only the weeks it appears in, so a quiet week repeats the previous level instead of leaving a hole, and a material nothing is nested against still shows what is on the shelf.

The bundle view groups the same ledger the other way round — by bundle rather than by material — so the two cannot disagree.

### Canonical stock descriptions

[`flat_stock_standard_descriptions.json`](flat_stock_standard_descriptions.json) holds 164 standard sheet descriptions in the form `LENGTHxWIDTHxTHICKNESS grade` — e.g. `3000x1500x3 cr`. Every other data source is normalised onto this list so PO lines, nest consumption and stock takes can be compared.

### Nest PDF parsing

Provisional nests are exported from the cutting machine as PDFs and dropped in SharePoint. `parse_used_material_from_pdf()` finds the *Used Material Info* table in each one (matched by its `Sheet Code` / `Material Name` header row) and reads off the raw material consumed.

Two things about these PDFs shape how that function works:

- **Only the front matter is scanned.** The *Used Material Info* table is a summary page at the front; the sheet layout pages behind it hold the cutting geometry — tens of thousands of vector edges each, which pdfminer needs *seconds per page* to load. The parser stops at the first page holding the table and reads at most `max_pages` (default 10). Scanning every page instead cost 55–130s per nest, so a folder of half a dozen made the read take over five minutes.
- **Merged rows are split back out.** The PDF only rules a line between two rows when the material name changes, so consecutive rows for the same material arrive as one row with newlines inside each cell (`SS400-8.0\nSS400-8.0`). `split_merged_rows()` separates them; without it those sheets were collapsed into one unrecognisable name and dropped from the demand figures.

Machine material names don't match the standard descriptions, so `standardise_material()` bridges them:

- [`nest_material_grades.json`](nest_material_grades.json) maps machine names to grades — `SPH5.0` → `s275`, `ALU-5251-H22-3MM` → `aluminium 5251h22`. Matching is on the **full** name, not a prefix, so `SPH5.0-355-GRADE` resolves to `s355` rather than being mistaken for a plain SPH.
- Nests often run off remnants. A 1500×1500 piece cut from a 3000×1500 sheet is recorded as **half a sheet**, not a whole one. The function picks the smallest standard sheet the piece could have come from and returns that fractional share, which is multiplied through the process quantity.

Materials that resolve to no standard description are surfaced separately in `debug.py` — they're either cut from a remnant or missing from the mapping.

### Nest caching

`read_material_from_nest()` reads a whole SharePoint folder, so it caches per file rather than all-or-nothing.

`list_files_in_sharepoint_folder()` selects `eTag`, `lastModifiedDateTime` and `size` alongside the id and name. Those form a change key, and `nest_material_cache()` — an `st.cache_resource` dict, so one shared instance rather than a copy per session — holds `{item id: (change key, materials)}`. A nest whose change key still matches is never downloaded again; only new or edited files are fetched. Files that leave the folder are dropped from the cache so it can't grow forever.

The `ttl=900` on `read_material_from_nest()` is now just how often the *folder listing* is refreshed — one cheap Graph call — rather than a full re-download of everything.

Stale files are downloaded through a `ThreadPoolExecutor`, since that half is pure network. Workers call `graph_download_item()`, which deliberately takes pre-resolved headers and site id and makes no Streamlit calls — a `st.cache_data` function called off the main thread has no `ScriptRunContext`. Parsing stays on the main thread: it's CPU bound, so threads wouldn't help.

Two failure modes are contained rather than fatal, because anyone can drop a file in the folder: a download that fails is skipped, and a file pdfplumber can't open is logged and cached as empty so it isn't retried on every read.

### Bundle joining

Bundle references appear inconsistently across systems — `Provisional B385 (do not cut) 1.pdf` in nest filenames, `B343 (355 & GALV)` in the staging sheet. `get_bundle_ref()` regexes out the `B<digits>` token from both sides so they join. A bundle split across several nest PDFs collects rather than overwrites.

`get_flat_bundles()` filters the staging sheet to `Type == "FLAT"` and `Completed? == "No"`, then left-joins the nest materials — so bundles with no nest yet keep their row.

### Purchase orders

`get_po_lines()` pulls the `purchase_order_lines` report from Statii and filters to future arrivals only (`date_promised > today`), flat stock only, excluding cancelled orders. Current stock is expected to come from stock takes and usage forms rather than PO history.

Statii keeps **one live session per set of credentials**. Anything else that authenticates with them — a second deployment, someone running a script against the API — logs the app out, and the token cached by `statii_get_session_token()` then looks fine for the rest of its 25 minute TTL while every request comes back `401 Session Invalid` (code 1005).

`statii_report()` wraps the call and treats a 401 as a session that has been taken away rather than a failure: clear the token cache, re-authenticate, retry once. The retry is not itself retried, so bad credentials fail rather than loop. **Call reports through `statii_report()`, not `statii_get_request()` directly**, or they will break the next time something else logs in. Note that re-authenticating also logs out whatever else was holding a session, so two things polling Statii on a timer will fight.

## Setup

```bash
pip install -r requirements.txt
```

Create `.streamlit/secrets.toml` (gitignored — never commit it):

```toml
# skips the microsoft login while developing locally - omit it from deployed secrets
TEST_MODE = true

[auth]
# must match a redirect uri registered on the azure app registration, ending /oauth2callback
redirect_uri = "http://localhost:8501/oauth2callback"
cookie_secret = "..."   # any long random string; streamlit signs the login cookie with it

[auth.microsoft]
client_id = "..."
client_secret = "..."
server_metadata_url = "https://login.microsoftonline.com/<TENANT_ID>/v2.0/.well-known/openid-configuration"

[sharepoint]
TENANT_ID = "..."
CLIENT_ID = "..."
CLIENT_SECRET = "..."
SHAREPOINT_SITE = "..."

[statii]
BASE_URL = "..."
CLIENT_ID = "..."
CLIENT_SECRET = "..."

[connections.sql]
dialect = "postgresql"
host = "..."
username = "..."
password = "..."
database = "..."
```

The database needs three tables:

- `stock_takes` — `created_by` and `data` columns, plus a `created_at` default; `data` holds the whole stock take as JSON.
- `live_stock` — `material` (primary key), `quantity` and `location`. This is the running total: a stock take overwrites it, and the usage and delivery forms move it from either direction.
- `material_movements` — `created_by`, `material`, `quantity` (signed: negative for usage, positive for delivery) and a `created_at` default. Every usage and delivery form submission logs one row here, so who moved what is recorded even though `live_stock` only keeps the running total. Create it with:

  ```sql
  CREATE TABLE material_movements (
      id         SERIAL PRIMARY KEY,
      created_at TIMESTAMP NOT NULL DEFAULT now(),
      created_by TEXT,
      material   TEXT,
      quantity   NUMERIC
  );
  ```

Run the app:

```bash
streamlit run login.py
```

Or the data harness on its own:

```bash
streamlit run debug.py
```

## Layout

```
login.py                                entry script and login page
ui.py                                   page_setup: auth gate, logo, title row
data.py                                 all data access and parsing logic
debug.py                                harness exercising every data function
assets/logo.jpg                         drawn by ui.render_logo
flat_stock_standard_descriptions.json   164 canonical stock descriptions
nest_material_grades.json               machine material name -> grade
pages/
  2_Dashboard.py                        weekly projection, bundles, locations, movement
  3_Report_Material_Usage.py            takes sheets off live stock
  4_Report_Material_Delivery.py         puts sheets back on
  5_Perform_Full_Stock_Take.py          count every line, overwrites live stock
  6_View_Stock_Takes.py                 latest count, history per material, past takes
```

## Notes

- Graph and Statii calls are wrapped in `st.cache_data` with a 15–25 minute TTL to keep the app responsive against slow upstream APIs.
- `data.py` reads Statii secrets at import time, so the app will fail on startup rather than at first use if they're missing.
- The database reads (`live_stock_read_from_db`, `flat_stock_take_read_from_db`) pass `ttl=0` rather than caching. Stock on hand is the one number that has to be current, and both are cheap queries.
- **A blank cell in the stock take editor is a counted zero**, and `flat_stock_take_save_to_db()` converts it before writing. This matters more than it looks: a blank arrives as a pandas `NaN`, and `NaN` is not `NULL` — it stores as a real numeric value that then swallows every later `quantity + delta`, so usage and delivery forms wrote nothing while still reporting success. `COALESCE` does not help. Sanitise at the point of writing.
- `record_material_usage()` returns the new level via `RETURNING` and raises when no row matched, rather than committing nothing and reporting success.
- `.streamlit/config.toml` pins `[theme] base = "light"`, matching the Data Visualiser. Viewers can still switch themes in their own browser settings menu; `[client] toolbarMode` is what hides that.
- `demo_data/` is gitignored and exists only on the machine that created it. It holds scripts that load sample stock and stock takes into the database for reviewing the dashboard, plus the backup they restore from.
