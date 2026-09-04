# JMS Material Management System

A Streamlit app for tracking flat (sheet) stock at JMS Metaltec — what's on the shelf, what's committed to upcoming jobs, and what's on order.

It pulls from three systems and reconciles them against a single canonical list of stock descriptions:

| Source | Via | What it provides |
|---|---|---|
| SharePoint | Microsoft Graph API | Bundle staging sheet, provisional nest PDFs |
| Statii (ERP) | REST API | Purchase order lines — incoming material |
| PostgreSQL | SQLAlchemy | Stock take submissions, live stock |

## Status

**Early development.** The data layer in [`data.py`](data.py) is working and covers SharePoint, Statii, nest parsing, stock takes and live stock. The pages that record and read stock are built: material usage, material delivery, performing a stock take, and stock take history. [`debug.py`](debug.py) is still the harness that exercises each data function end to end.

Not built yet: [`pages/1_login.py`](pages/1_login.py) is empty, so stock takes are attributed to a hardcoded `"username here"` rather than a real user.

### The dashboard lives on a branch

The live stock dashboard is on **`claude-dashboard`**, not on `main`:

```bash
git checkout claude-dashboard
```

It adds one page, `pages/2_Live_Stock_Dashboard.py`, which puts stock on hand, upcoming bundle demand and incoming purchase orders on a single timeline per material to work out when each one runs short. All of its projection and KPI logic is self-contained in that page — it reads `data.py` but does not change it, so the branch is only that one file.

Two things to know before judging what it shows:

- **The nest folder is stale.** SharePoint currently holds nests for B371–B387, while the incomplete bundles on the staging sheet are B393 onwards, so no bundle joins to a nest and every demand figure is zero. The page reports this rather than showing an empty shortage table as good news.
- **Almost nothing has been counted.** 163 of the 164 lines in `live_stock` have never had a stock take against them, so there is nothing for the projection to start from.

Neither is a fault in the page; both are the data feeds not flowing yet.

### Known defect: the usage and delivery forms silently do nothing

`live_stock.quantity` is `NOT NULL numeric`, and a blank cell in the stock take editor arrives as a pandas `NaN`, which stores as a literal `'NaN'::numeric`. 163 of the 164 rows currently hold that value.

`record_material_usage()` does `SET quantity = quantity + :delta`, and in Postgres `NaN + 10` is `NaN` — so for those rows the write changes nothing while the form still reports success. `COALESCE` is not the fix: `NaN` is not `NULL`.

Fixing it means converting `NaN` to `None` on the stock take write, backfilling the existing rows, and checking the row count on the update rather than assuming it landed. **Worth doing before the next real stock take**, or the count will write the same values back in.

## How it works

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

The database needs two tables:

- `stock_takes` — `created_by` and `data` columns, plus a `created_at` default; `data` holds the whole stock take as JSON.
- `live_stock` — `material` (primary key), `quantity` and `location`. This is the running total: a stock take overwrites it, and the usage and delivery forms move it from either direction.

Run the debug harness:

```bash
streamlit run debug.py
```

## Layout

```
data.py                                 all data access and parsing logic
debug.py                                harness exercising every function
flat_stock_standard_descriptions.json   164 canonical stock descriptions
nest_material_grades.json               machine material name -> grade
pages/
  1_login.py                            empty, not yet implemented
  2_Dashboard.py                        empty on main - see the claude-dashboard branch
  3_Report_Material_Usage.py            takes sheets off live stock
  4_Report_Material_Delivery.py         puts sheets back on
  5_Perform_Stock_Take.py               count every line, overwrites live stock
  6_View_Stock_Takes.py                 latest count, history per material, past takes
```

## Notes

- Graph and Statii calls are wrapped in `st.cache_data` with a 15–25 minute TTL to keep the app responsive against slow upstream APIs.
- `data.py` reads Statii secrets at import time, so the app will fail on startup rather than at first use if they're missing.
- The database reads (`live_stock_read_from_db`, `flat_stock_take_read_from_db`) pass `ttl=0` rather than caching. Stock on hand is the one number that has to be current, and both are cheap queries.
