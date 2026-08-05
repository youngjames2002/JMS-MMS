import pandas as pd
import streamlit as st
import json
import re
from pathlib import Path
import requests
from sqlalchemy import text
import msal
import pdfplumber
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

def download_item_from_sharepoint(site_name: str, item_id: str) -> BytesIO:
    # download a file by drive item id, as listed by list_files_in_sharepoint_folder
    response = graph_get(site_name, f"drive/items/{item_id}/content")
    return BytesIO(response.content) if response is not None else None

@st.cache_data(ttl=900, show_spinner=True)
def list_files_in_sharepoint_folder(site_name: str, folder_path: str, extension: str = "") -> list[dict]:
    # list the files in a folder as [{"name": ..., "id": ...}, ...]
    files = []
    endpoint = f"drive/root:/{folder_path}:/children?$select=id,name,file&$top=200"

    while endpoint:
        response = graph_get(site_name, endpoint)
        if response is None:
            return files
        payload = response.json()

        for item in payload.get("value", []):
            # sub folders have no file facet so are skipped
            if "file" in item and item["name"].lower().endswith(extension.lower()):
                files.append({"name": item["name"], "id": item["id"]})

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

@st.cache_data(ttl=1500)
def get_po_lines() -> pd.DataFrame:
    token = statii_get_session_token()
    df = statii_get_request(token, "purchase_order_lines")

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
    # filter to only arriving today onwards
    # this data is only used for projecting future stock, as current stock should be up to date through stock take + forms
    df = df[df["date_promised"] > pd.Timestamp.today()]
    # filter to just flat stock
    # matches PO description to standardised list in config file
    STANDARD_DESCRIPTIONS = get_standard_descriptions()
    df = df[df["description"].isin(STANDARD_DESCRIPTIONS)]

    # filter out cancelled orders
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
    payload = df.to_json(orient="records")
    conn = st.connection("sql")
    with conn.session as session:
        session.execute(
            text('INSERT INTO stock_takes (created_by, data) VALUES (:user, :data)'),
            {"user": user, "data": payload}
        )
        session.commit()

def flat_stock_take_read_from_db() -> pd.DataFrame:
    conn = st.connection("sql")
    with conn.session as session:
        df = conn.query("SELECT * FROM stock_takes")
    df["data"] = df["data"].apply(json.dumps)
    return df
    
# bundles data
@st.cache_data(show_spinner=True)
def load_data_sp() -> pd.DataFrame:
    bytes_io = download_file_from_sharepoint(
        site_name="JMSEngineeringTeam",
        file_path="JMS Engineering Team SharePoint/JMS Master Schedule/testAutomation/bundleStagingSheet.xlsx"
    )
    if bytes_io is None:
        return pd.DataFrame()  # return empty DataFrame if download failed

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

def parse_used_material_from_pdf(file: BytesIO) -> list[dict]:
    # pull the "Used Material Info" page out of a nest pdf
    # that page holds one row per sheet of raw material the schedule consumes
    materials = []

    with pdfplumber.open(file) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                table = [[clean_cell(c) for c in row] for row in table]

                # find the used material table by its header row
                if not table or "Sheet Code" not in table[0] or "Material Name" not in table[0]:
                    continue

                columns = {name: i for i, name in enumerate(table[0])}
                for row in table[1:]:
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

    return materials

@st.cache_data(ttl=900, show_spinner=True)
def read_material_from_nest() -> dict:
    # read every nest pdf in the sharepoint provisional nests folder
    # return the raw material each nest consumes, keyed by bundle ref
    files = list_files_in_sharepoint_folder(
        site_name=NEST_SITE_NAME,
        folder_path=NEST_FOLDER_PATH,
        extension=".pdf"
    )

    nests = {}
    for file in files:
        bytes_io = download_item_from_sharepoint(NEST_SITE_NAME, file["id"])
        if bytes_io is None:
            continue

        materials = parse_used_material_from_pdf(bytes_io)
        if not materials:
            # no used material page - not a nest pdf
            print(f"no used material info found in {file['name']}")
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