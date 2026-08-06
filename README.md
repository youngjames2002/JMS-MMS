# JMS Material Management System

A Streamlit app for tracking flat (sheet) stock at JMS Metaltec — what's on the shelf, what's committed to upcoming jobs, and what's on order.

It pulls from three systems and reconciles them against a single canonical list of stock descriptions:

| Source | Via | What it provides |
|---|---|---|
| SharePoint | Microsoft Graph API | Bundle staging sheet, provisional nest PDFs |
| Statii (ERP) | REST API | Purchase order lines — incoming material |
| PostgreSQL | SQLAlchemy | Stock take submissions |

## Status

**Early development.** The data layer in [`data.py`](data.py) is working and covers SharePoint, Statii, nest parsing and stock takes. The UI is not built yet — everything under [`pages/`](pages/) is an empty stub. [`debug.py`](debug.py) is the working harness that exercises each function end to end, and is currently the only runnable entry point.

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

The database needs a `stock_takes` table with `created_by` and `data` columns; `data` holds the stock take as JSON.

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
pages/                                  UI stubs, not yet implemented
  1_login.py
  2_Report_Material_Usage.py
  3_Report_Material_Delivery.py
  4_Perform_Stock_Take.py
```

## Notes

- Graph and Statii calls are wrapped in `st.cache_data` with a 15–25 minute TTL to keep the app responsive against slow upstream APIs.
- `data.py` reads Statii secrets at import time, so the app will fail on startup rather than at first use if they're missing.
