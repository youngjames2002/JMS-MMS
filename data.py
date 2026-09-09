import pandas as pd
import streamlit as st
import json
import re
from pathlib import Path
import requests
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
import msal
import pdfplumber
import threading
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO,StringIO

# shared helpers
# config files
@st.cache_data(ttl=1500)
def load_json_config(file_name: str) -> list | dict:
    # read a config file that sits alongside this module
    path = Path(__file__).parent / file_name
    with open(path) as f:
        config = json.load(f)
    return config

# text and number parsing
def clean_cell(cell: str | None) -> str:
    # normalise a pdf table cell - strips and collapses whitespace / line breaks
    return " ".join((cell or "").split())

def cell_to_number(cell: str) -> float | None:
    # pdf numbers come through as text - return None rather than nan so the dict stays json safe
    try:
        return float(cell)
    except (TypeError, ValueError):
        return None

def get_bundle_ref(text: str) -> str:
    # pulls the bundle out of a nest file name ("Provisional B385 (do not cut) 1.pdf")
    # and out of a staging sheet Bundle/Job ("B343 (355 & GALV)") so the two can be joined
    match = re.search(r"\bB\d+\b", str(text), flags=re.IGNORECASE)
    return match.group(0).upper() if match else str(text).strip().upper()

def parse_size(size: str) -> tuple | None:
    # sizes read as "3000.00 x 1500.00" in nests and "3000x1500x3 cr" in the standard descriptions
    # returns (longest side, shortest side), ignoring any thickness or grade on the end
    dimensions = [cell_to_number(part) for part in size.split("x")]
    dimensions = [d for d in dimensions if d is not None][:2]
    if len(dimensions) != 2:
        return None
    return tuple(sorted(dimensions, reverse=True))

# sharepoint over the graph api
@st.cache_data(ttl=1500)
def graph_get_headers() -> dict:
    # authenticate against the graph api and return the auth headers
    TENANT_ID = st.secrets["sharepoint"]["TENANT_ID"]
    CLIENT_ID = st.secrets["sharepoint"]["CLIENT_ID"]
    CLIENT_SECRET = st.secrets["sharepoint"]["CLIENT_SECRET"]

    AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
    SCOPE = ["https://graph.microsoft.com/.default"]

    app = msal.ConfidentialClientApplication(
        client_id=CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET
    )
    # debug st.json(token)  # should contain "access_token" if successful

    token = app.acquire_token_for_client(scopes=SCOPE)
    if "access_token" not in token:
        st.error("Authentication failed")
        return None

    return {"Authorization": f"Bearer {token['access_token']}"}

@st.cache_data(ttl=1500)
def graph_get_site_id(site_name: str) -> str:
    # look up a sharepoint site id by name
    headers = graph_get_headers()
    if headers is None:
        return None

    SHAREPOINT_SITE = st.secrets["sharepoint"]["SHAREPOINT_SITE"]

    site_url = f"https://graph.microsoft.com/v1.0/sites/{SHAREPOINT_SITE}:/sites/{site_name}:/"
    site_response = requests.get(site_url, headers=headers)
    # debug
    # st.write("Site lookup status:", site_response.status_code)
    # st.write(site_response.json())

    if site_response.status_code != 200:
        st.error("Site lookup failed")
        return None

    return site_response.json()["id"]

def graph_get(site_name: str, endpoint: str) -> requests.Response:
    # endpoint is either the part of the url after /sites/{site id}/ or a full url from @odata.nextLink
    headers = graph_get_headers()
    if headers is None:
        return None

    if endpoint.startswith("https://"):
        url = endpoint
    else:
        site_id = graph_get_site_id(site_name)
        if site_id is None:
            return None
        url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/{endpoint}"

    response = requests.get(url, headers=headers)
    if not response.ok:
        print(response.status_code, response.text)
        response.raise_for_status()
    return response

@st.cache_data(show_spinner=True)
def download_file_from_sharepoint(site_name: str, file_path: str) -> BytesIO:
    # download a file by its path in the document library and return a bytesIO object
    response = graph_get(site_name, f"drive/root:/{file_path}:/content")
    return BytesIO(response.content) if response is not None else None

def graph_download_item(headers: dict, site_id: str, item_id: str) -> BytesIO | None:
    # download a drive item from already resolved headers and site id
    # nothing in here touches streamlit, so this is the one that is safe to call off the main
    # thread - a failure returns None rather than raising so one bad file cannot sink a batch
    url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{item_id}/content"
    try:
        response = requests.get(url, headers=headers, timeout=120)
    except requests.RequestException as error:
        print(f"download of {item_id} failed: {error}")
        return None
    if not response.ok:
        print(response.status_code, response.text)
        return None
    return BytesIO(response.content)

def download_item_from_sharepoint(site_name: str, item_id: str) -> BytesIO:
    # download a file by drive item id, as listed by list_files_in_sharepoint_folder
    headers = graph_get_headers()
    site_id = graph_get_site_id(site_name)
    if headers is None or site_id is None:
        return None
    return graph_download_item(headers, site_id, item_id)

@st.cache_data(ttl=900, show_spinner=True)
def list_files_in_sharepoint_folder(site_name: str, folder_path: str, extension: str = "") -> list[dict]:
    # list the files in a folder as [{"name": ..., "id": ..., "eTag": ...}, ...]
    # eTag covers metadata and content, so it moves whenever a file does - it is what lets
    # callers tell an unchanged file from a changed one without downloading it
    files = []
    endpoint = (
        f"drive/root:/{folder_path}:/children"
        "?$select=id,name,file,eTag,lastModifiedDateTime,size&$top=200"
    )

    while endpoint:
        response = graph_get(site_name, endpoint)
        if response is None:
            return files
        payload = response.json()

        for item in payload.get("value", []):
            # sub folders have no file facet so are skipped
            if "file" in item and item["name"].lower().endswith(extension.lower()):
                files.append({
                    "name": item["name"],
                    "id": item["id"],
                    "eTag": item.get("eTag"),
                    "lastModifiedDateTime": item.get("lastModifiedDateTime"),
                    "size": item.get("size"),
                })

        # graph pages at 200 items
        endpoint = payload.get("@odata.nextLink")

    return files

# statii data - po lines
STATII_BASE_URL     = st.secrets["statii"]["BASE_URL"]
STATII_CLIENT_SECRET = st.secrets["statii"]["CLIENT_SECRET"]
STATII_CLIENT_ID     = st.secrets["statii"]["CLIENT_ID"]

@st.cache_data(ttl=1500)
def statii_get_session_token() -> str:
    response = requests.get(f"{STATII_BASE_URL}/auth", auth=(STATII_CLIENT_ID, STATII_CLIENT_SECRET))
    if not response.ok:
        print("Auth failed:", response.status_code, response.text)
        response.raise_for_status()
    return response.json()["ResponseBody"]["data"]["session"]

@st.cache_data(ttl=1500)
def statii_get_request(session_token: str, report: str, filters: dict | None = None) -> pd.DataFrame:
    response = requests.get(
        f"{STATII_BASE_URL}/report/{report}",
        headers={
            "Authorization": f"Bearer {session_token}",
            "Accept": "application/json",
        },
        params=filters,
    )
    if not response.ok:
        print(response.status_code, response.text)
        response.raise_for_status()
    data = response.json()["ResponseBody"]["data"]
    df = pd.DataFrame(data["rows"], columns=data["columns"])
    return df

def statii_report(report: str, filters: dict | None = None) -> pd.DataFrame:
    # statii keeps one live session per set of credentials, so anything else that authenticates
    # with them - a second deployment, someone running a script - silently kills the token we are
    # holding, and every request after that comes back 401 until our cached copy times out
    # so a 401 is not a failure, it is a session that has been taken away: get a new one and go
    # again. the retry is not itself retried, so bad credentials still fail rather than loop
    try:
        return statii_get_request(statii_get_session_token(), report, filters)
    except requests.HTTPError as error:
        if error.response is None or error.response.status_code != 401:
            raise
        statii_get_session_token.clear()
        return statii_get_request(statii_get_session_token(), report, filters)

@st.cache_data(ttl=1500)
def get_po_lines() -> pd.DataFrame:
    df = statii_report("purchase_order_lines")

    # clean up df
    DEAD = [
    "dateformat", "dateformat0", "company", "currency", "stock_batch",
    "project", "nominal_code", "ncr", "uom", "line_weight", "value_credited",
    "buy_to_stock_batch", "show_cancelledness", "unit_conversion",
    "line_price_payable",   # identical to line_price
    "row_id", "po_id", "late", "submitted_by", "works_order",
    ]

    DATES = ["date_created", "date_required", "date_promised", "date_submitted", "date_paid"]
    STAMPS = ["date_received", "first_received_date", "latest_received_date"]

    df = df.drop(columns=[c for c in DEAD if c in df])

    for c in DATES:
        df[c] = pd.to_datetime(df[c], format="%Y-%m-%d", errors="coerce")
    for c in STAMPS:
        df[c] = pd.to_datetime(df[c], format="%Y-%m-%d %H:%M:%S", errors="coerce")

    # filters
    df["quantity_outstanding"] = pd.to_numeric(df["quantity_outstanding"], errors="coerce")
    df = df[df["quantity_outstanding"] > 0]
    # filter to just flat stock
    # matches PO description to standardised list in config file
    STANDARD_DESCRIPTIONS = get_standard_descriptions()
    df = df[df["description"].isin(STANDARD_DESCRIPTIONS)]
    df = df[df["po_status"] != "Cancelled"]
    return df

def get_standard_descriptions() -> list:
    return load_json_config("flat_stock_standard_descriptions.json")

# stock take data
def flat_stock_take_generate_empty() -> pd.DataFrame:
    # create df for stock take user to save to
    df = pd.DataFrame(columns=["Material", "Quantity", "Location"])
    df["Material"] = get_standard_descriptions()
    return df

def flat_stock_take_save_to_db(df: pd.DataFrame, user: str):
    # save stock take to database
    df = df.copy()
    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0.0)
    df["Location"] = [
        (str(value).strip() or None) if pd.notna(value) else None
        for value in df["Location"]
    ]
    # history and live stock are written from the same cleaned frame so they cannot disagree
    payload = df.to_json(orient="records")
    conn = st.connection("sql")
    with conn.session as session:
        session.execute(
            text('INSERT INTO stock_takes (created_by, data) VALUES (:user, :data)'),
            {"user": user, "data": payload}
        )
        # overwrite live stock with latest count
        session.execute(
            text(
                """
                INSERT INTO live_stock (material, quantity, location)
                VALUES (:Material, :Quantity, :Location)
                ON CONFLICT (material) DO UPDATE
                SET quantity = EXCLUDED.quantity, location = EXCLUDED.location
                """
            ),
            df.to_dict(orient="records")
        )
        session.commit()

def flat_stock_take_read_from_db() -> pd.DataFrame:
    # created_at is stored as timestamptz (an absolute UTC instant). psycopg2 hands that to
    # pandas, which normalises every tz-aware column back to UTC, so the pages would render an
    # hour behind through the summer whatever the session timezone is. converting to a naive
    # Europe/London wall-clock here, in the database, is the fix pandas cannot undo - the app
    # then just displays what it is given, with no timezone maths of its own
    conn = st.connection("sql")
    try:
        df = conn.query(
            "SELECT id, created_by, data,"
            " (created_at AT TIME ZONE 'Europe/London') AS created_at"
            " FROM stock_takes",
            ttl=0,
        )
    except SQLAlchemyError as error:
        db_unavailable(error)
    df["data"] = df["data"].apply(lambda x: json.loads(x) if isinstance(x, str) else x)
    return df

# live stock data
def db_unavailable(error: Exception):
    print(f"database error: {error}")
    st.error("Could not reach the database just now. Refresh the page to try again.")
    st.stop()

def live_stock_read_from_db() -> pd.DataFrame:
    conn = st.connection("sql")
    try:
        return conn.query("SELECT * FROM live_stock", ttl=0)
    except SQLAlchemyError as error:
        db_unavailable(error)

# bundles data
@st.cache_data(show_spinner=True)
def load_data_sp() -> pd.DataFrame:
    bytes_io = download_file_from_sharepoint(
        site_name="JMSEngineeringTeam",
        file_path="JMS Engineering Team SharePoint/JMS Master Schedule/testAutomation/bundleStagingSheet.xlsx"
    )
    if bytes_io is None:
        raise RuntimeError("Could not download bundle sheet from SharePoint")

    df = pd.read_excel(bytes_io)

    df["Earliest Process Date"] = pd.to_datetime(
        df["Earliest Process Date"],
        format="mixed",
        dayfirst=True,
        errors="coerce"
    )
    return df

def get_flat_bundles() -> pd.DataFrame:
    # aggregate staging sheet and nest data
    # filter and process data
    # return dataframe
    staging_sheet = load_data_sp()

    df = staging_sheet.copy()
    # filter flat only
    df = df[df["Type"] == "FLAT"]
    # filter incomplete only
    # only checking laser complete here as those are what need material - cut but not folded means material is already there and used
    df = df[df['Completed?'] == 'No']

    # both sides go through the same ref extraction, so "B343 (355 & GALV)" still finds the B343 nest
    df["nest_ref"] = df["Bundle/Job"].apply(get_bundle_ref)

    # one row per material per bundle - bundles with no nest yet keep their row with no material against it
    df = df.merge(get_flat_bundle_materials(), on="nest_ref", how="left")
    return df

# nest data
NEST_SITE_NAME = "JMSEngineeringTeam"
NEST_FOLDER_PATH = "JMS Engineering Team SharePoint/Dev/provisional_nests"

def get_nest_material_grades() -> dict:
    # maps the machine material name to the grade used in the standard descriptions
    return load_json_config("nest_material_grades.json")

def standardise_material(material_name: str, material_size: str, thickness: str) -> tuple:
    # nest materials read as "SPH5.0" / "3000.00 x 1500.00" - standard descriptions read as "3000x1500x5 s275"
    # returns (standard description, share of a full sheet) - nests often run off remnants, so a
    # 1500x1500 piece of a 3000x1500 sheet is half a sheet of stock rather than a whole one
    # returns (None, None) when the material is not one of the standard flat stock sheets

    # matched on the full machine material name rather than a prefix, otherwise
    # SPH5.0-355-GRADE reads as a plain SPH and gets priced as s275
    grade = get_nest_material_grades().get(material_name.upper())
    if grade is None:
        return None, None

    size = parse_size(material_size)
    thickness = cell_to_number(thickness)
    if size is None or thickness is None:
        return None, None

    # standard descriptions drop trailing zeros off the thickness
    length, width = size
    grade_and_thickness = f"x{thickness:g} {grade}"

    # every standard sheet in this grade and thickness, smallest area first
    sheets = []
    for description in get_standard_descriptions():
        if not description.endswith(grade_and_thickness):
            continue
        sheet_size = parse_size(description)
        if sheet_size is None:
            continue
        sheet_length, sheet_width = sheet_size
        sheets.append((sheet_length * sheet_width, sheet_length, sheet_width, description))

    # the piece has to have come off a sheet at least as big as itself, so take the smallest that fits
    fits = [s for s in sorted(sheets) if s[1] >= length and s[2] >= width]
    if not fits:
        return None, None

    sheet_area, sheet_length, sheet_width, description = fits[0]
    return description, round(length * width / sheet_area, 4)

def split_merged_rows(row: list, count_column: int) -> list[list[str]]:
    # the nest pdf only rules a line between two rows when the material name changes, so
    # consecutive rows for the same material come back merged into one row with newlines
    # inside each cell ("SS400-8.0\nSS400-8.0") - split them back out so both sheets count
    parts = [str(cell or "").split("\n") for cell in row]
    count = len(parts[count_column])
    if count <= 1:
        return [[clean_cell(cell) for cell in row]]

    # a cell that did not split spans every row it was merged across, so reuse it
    # a cell that split a different number of times is a wrapped value, so keep it whole
    return [
        [clean_cell(part[i] if len(part) == count else " ".join(part)) for part in parts]
        for i in range(count)
    ]

def parse_used_material_from_pdf(file: BytesIO, max_pages: int = 10) -> list[dict]:
    # pull the "Used Material Info" page out of a nest pdf
    # that page holds one row per sheet of raw material the schedule consumes
    # it sits in the front matter, and the sheet layout pages behind it carry tens of thousands
    # of vector edges each that pdfminer needs seconds apiece to load - so stop at the first page
    # holding the table, and never walk past the front matter looking for it
    materials = []

    with pdfplumber.open(file) as pdf:
        for page in pdf.pages[:max_pages]:
            found = False
            for table in page.extract_tables():
                if not table:
                    continue

                # find the used material table by its header row
                header = [clean_cell(cell) for cell in table[0]]
                if "Sheet Code" not in header or "Material Name" not in header:
                    continue

                found = True
                columns = {name: i for i, name in enumerate(header)}
                # a quantity never wraps, so its line count is how many rows were merged together
                count_column = columns["Process Qty"]

                for merged_row in table[1:]:
                    for row in split_merged_rows(merged_row, count_column):
                        material_name = row[columns["Material Name"]]
                        if not material_name:
                            # blank spacer row
                            continue

                        # material size comes through as "3000.00 x 1500.00"
                        material_size = row[columns["Material Size"]]
                        description, sheet_share = standardise_material(
                            material_name, material_size, row[columns["Thickness"]]
                        )

                        # quantity is sheets of stock consumed, so a part sheet counts as a fraction
                        process_qty = cell_to_number(row[columns["Process Qty"]])
                        quantity = None
                        if process_qty is not None and sheet_share is not None:
                            quantity = round(process_qty * sheet_share, 4)

                        materials.append({
                            "material_name": material_name,
                            "material_size": material_size,
                            "quantity": quantity,
                            "description": description,
                        })

            # a nest layout holds enough objects that releasing them between pages matters
            page.flush_cache()
            page.get_textmap.cache_clear()
            if found:
                break

    return materials

NEST_DOWNLOAD_WORKERS = 8

@st.cache_resource
def nest_material_cache() -> dict:
    # {drive item id: (change key, materials)}
    # cache_resource hands back the one dict rather than a copy, so this survives reruns and is
    # shared across sessions - which is the point, a nest only gets downloaded and parsed once
    return {}

# cache_resource objects are shared across sessions, and every session runs its script on its own
# thread, so guard the read-modify-write
nest_cache_lock = threading.Lock()

def nest_change_key(file: dict) -> tuple:
    # what sharepoint says about the file, without downloading it
    # size and last modified are belt and braces in case eTag ever comes back empty
    return (file.get("eTag"), file.get("lastModifiedDateTime"), file.get("size"))

@st.cache_data(ttl=900, show_spinner=True)
def read_material_from_nest() -> dict:
    # read every nest pdf in the sharepoint provisional nests folder
    # return the raw material each nest consumes, keyed by bundle ref
    files = list_files_in_sharepoint_folder(
        site_name=NEST_SITE_NAME,
        folder_path=NEST_FOLDER_PATH,
        extension=".pdf"
    )

    cache = nest_material_cache()
    with nest_cache_lock:
        # anything sharepoint reports unchanged is already parsed, so it never gets downloaded
        stale = [f for f in files if cache.get(f["id"], (None, None))[0] != nest_change_key(f)]

    if stale:
        # resolve the auth up here: the workers must not call streamlit cached functions,
        # which is also why they use graph_download_item rather than graph_get
        headers = graph_get_headers()
        site_id = graph_get_site_id(NEST_SITE_NAME)
        if headers is None or site_id is None:
            return {}

        # downloading is the slow half and is pure network, so overlap the requests
        # parsing stays on this thread - it is cpu bound, so threads would not buy anything
        with ThreadPoolExecutor(max_workers=min(NEST_DOWNLOAD_WORKERS, len(stale))) as pool:
            downloads = pool.map(lambda f: graph_download_item(headers, site_id, f["id"]), stale)

            for file, bytes_io in zip(stale, downloads):
                if bytes_io is None:
                    continue

                try:
                    materials = parse_used_material_from_pdf(bytes_io)
                except Exception as error:
                    # anyone can drop a file in the folder - a corrupt or non pdf one should
                    # cost us that nest, not the whole read
                    print(f"could not read {file['name']}: {error}")
                    materials = []

                if not materials:
                    # no used material page - not a nest pdf
                    # still cached, so it is only scanned once rather than on every read
                    print(f"no used material info found in {file['name']}")

                with nest_cache_lock:
                    cache[file["id"]] = (nest_change_key(file), materials)

    with nest_cache_lock:
        # forget files that have left the folder, otherwise the cache only ever grows
        for item_id in set(cache) - {f["id"] for f in files}:
            del cache[item_id]

        nests = {}
        for file in files:
            _, materials = cache.get(file["id"], (None, None))
            if not materials:
                continue

            # a bundle can be split over more than one nest pdf, so collect rather than overwrite
            ref = get_bundle_ref(file["name"])
            nests.setdefault(ref, []).extend(materials)

    return nests

MATERIAL_COLUMNS = ["nest_ref", "material_name", "material_size", "quantity", "description"]

def get_flat_bundle_materials() -> pd.DataFrame:
    # flatten the nest materials out to one row per bundle per material, ready to join onto the bundles
    nests = read_material_from_nest()
    df = pd.DataFrame([
        {"nest_ref": ref, **material}
        for ref, materials in nests.items()
        for material in materials
    ])
    if df.empty:
        # keep the columns so the join gives the same shape before any nests have been dropped in
        df = pd.DataFrame(columns=MATERIAL_COLUMNS)
    return df

# material usage and arrival forms
def record_material_usage(material: str, quantity: float, user: str) -> float:
    # returns the new stock level, and raises rather than reporting success for a material
    # that is not in live_stock - a form that says "recorded" while writing nothing is worse
    # than one that says it could not
    # quantity is signed: negative off the usage form, positive off the delivery form
    conn = st.connection("sql")
    with conn.session as session:
        row = session.execute(
            text(
                "UPDATE live_stock SET quantity = quantity + :delta"
                " WHERE material = :material RETURNING quantity"
            ),
            {"delta": quantity, "material": material}
        ).one_or_none()

        if row is None:
            # nothing moved, so roll back rather than leave a movement attributed to stock
            # that was never touched
            session.rollback()
            raise LookupError(f"{material} has never been counted, so there is nothing to add to.")

        # log who moved it, in the same transaction as the stock change so the two cannot
        # disagree - a movement against whoever reported it, same as a stock take is
        session.execute(
            text(
                "INSERT INTO material_movements (created_by, material, quantity)"
                " VALUES (:user, :material, :quantity)"
            ),
            {"user": user, "material": material, "quantity": quantity}
        )
        session.commit()

    return float(row[0])