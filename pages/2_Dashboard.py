import streamlit as st
import pandas as pd
import plotly.express as px
from data import *
from ui import page_setup

page_setup("Live Stock Dashboard")


# bundles & projection
@st.cache_data(ttl=900)
def on_order_by_material() -> pd.Series:
    try:
        po = get_po_lines()
    except Exception as error:
        print(f"purchase orders unavailable: {error}")
        return pd.Series(dtype="float64")
    if po.empty:
        return pd.Series(dtype="float64")
    return po.groupby("description")["quantity_outstanding"].sum()


@st.cache_data(ttl=900)
def weekly_projection() -> pd.DataFrame:
    # sheets left per material after each week's bundles are cut, and which bundles they were
    demand = get_flat_bundles()
    demand = demand[demand["description"].notna()].copy()
    demand["week"] = pd.to_datetime(demand["Earliest Process Date"]).apply(
        pd.offsets.Week(weekday=3).rollforward
    )
    # bundle level first, so a bundle split over several nests only counts once
    used = (
        demand.groupby(["description", "week", "nest_ref"], as_index=False)["quantity"].sum()
        .groupby(["description", "week"])
        .agg(needed=("quantity", "sum"), bundles=("nest_ref", ", ".join))
    )
    stock = live_stock_read_from_db().set_index("material")["quantity"]
    materials = stock.index.union(used.index.get_level_values("description").unique())
    weeks = used.index.get_level_values("week").unique().sort_values()
    weekly = used.reindex(pd.MultiIndex.from_product([materials, weeks],
                                                     names=["description", "week"]))
    weekly["needed"] = weekly["needed"].fillna(0.0)
    weekly["bundles"] = weekly["bundles"].fillna("")

    weekly = weekly.reset_index()
    weekly["stock"] = weekly["description"].map(stock).fillna(0.0)
    weekly["remaining"] = weekly["stock"] - weekly.groupby("description")["needed"].cumsum()
    weekly["on_order"] = weekly["description"].map(on_order_by_material()).fillna(0.0)
    weekly["net"] = weekly["remaining"] + weekly["on_order"]

    return weekly

st.markdown("## Weekly Projection")
weekly = weekly_projection()

grid = weekly.pivot(index="description", columns="week", values="net")
grid.columns = [f"w/e {c:%d %b}" for c in grid.columns]
week_cols = list(grid.columns)
grid.insert(0, "On order", weekly.groupby("description")["on_order"].first())
grid = grid.loc[grid[week_cols].min(axis=1).sort_values().index]  # worst net first


def shade(value):
    if value < 0:
        return "background-color: #fee4e2"
    return "background-color: #fef0c7" if value <= 0.5 else ""


st.dataframe(
    grid.style
    .map(shade, subset=week_cols)
    .format("{:+.1f}", subset=week_cols, na_rep="—")
    .format("{:.0f}", subset=["On order"]),
    width="stretch",
)

material = st.selectbox("Material", grid.index)
st.dataframe(
    weekly[weekly["description"] == material][["week", "needed", "remaining", "on_order", "net", "bundles"]],
    hide_index=True,
    width="stretch",
    column_config={
        "week": st.column_config.DateColumn("Week ending", format="DD MMM"),
        "needed": st.column_config.NumberColumn("Sheets needed", format="%.2f"),
        "remaining": st.column_config.NumberColumn("Stock only", format="%+.1f"),
        "on_order": st.column_config.NumberColumn("On order", format="%.0f"),
        "net": st.column_config.NumberColumn("Net after orders", format="%+.1f"),
        "bundles": "Bundles",
    },
)

@st.cache_data(ttl=900)
def bundle_ledger() -> pd.DataFrame:
    demand = get_flat_bundles()
    demand = demand[demand["description"].notna()].copy()
    demand["date"] = pd.to_datetime(demand["Earliest Process Date"])
    ledger = (
        demand.groupby(["description", "date", "nest_ref", "Bundle/Job"], as_index=False)["quantity"].sum()
        .sort_values(["description", "date", "nest_ref"])
    )
    stock = live_stock_read_from_db().set_index("material")["quantity"]
    ledger["physical"] = ledger["description"].map(stock).fillna(0) - ledger.groupby(
        "description"
    )["quantity"].cumsum()
    ledger["on_order"] = ledger["description"].map(on_order_by_material()).fillna(0)
    ledger["remaining"] = ledger["physical"] + ledger["on_order"]
    return ledger


st.markdown("## Bundles Waiting On Material")
ledger = bundle_ledger()

by_bundle = ledger.groupby(["nest_ref", "date"], as_index=False).agg(
    job=("Bundle/Job", "first"),
    materials=("description", "nunique"),
    Sheets=("quantity", "sum"),
    short=("remaining", lambda r: int((r < 0).sum())),
    Worst=("remaining", "min"),
).sort_values("date")

by_bundle.drop(columns="job", inplace=True)
by_bundle.rename(columns={"nest_ref": "Bundle", "date": "Earliest Process Date", "materials": "Different Material Types", "short": "Material Types Short"}, inplace=True)
st.dataframe(
    by_bundle,
    hide_index=True,
    width="stretch",
    column_config={
        "Earliest Process Date": st.column_config.DateColumn(format="DD MMM YYYY"),
        "Worst": st.column_config.NumberColumn(format="%+.1f"),
        "Sheets": st.column_config.NumberColumn(format="%.1f"),
    },
)

bundle = st.selectbox("Bundle", by_bundle["Bundle"])
st.dataframe(
    ledger[ledger["nest_ref"] == bundle][["description", "quantity", "physical", "on_order", "remaining"]],
    hide_index=True,
    width="stretch",
    column_config={
        "description": "Material",
        "quantity": st.column_config.NumberColumn("Sheets needed", format="%.2f"),
        "physical": st.column_config.NumberColumn("Stock only", format="%+.1f"),
        "on_order": st.column_config.NumberColumn("On order", format="%.0f"),
        "remaining": st.column_config.NumberColumn("Net after orders", format="%+.1f"),
    },
)


# stock by location
st.markdown("## Stock By Storage Location")
stock_df = live_stock_read_from_db()
count_by_location = stock_df.groupby("location").sum("quantity")
count_by_location.rename(columns={"quantity": "No of Sheets"}, inplace=True)
count_by_location.rename_axis("Location", inplace=True)
st.dataframe(count_by_location)

location_options = stock_df["location"].dropna().unique().tolist()
location_choice = st.selectbox(options=location_options,label="View stock at specific location")
stock_at_location = stock_df[stock_df["location"] == location_choice]
st.dataframe(stock_at_location)

# since last stock take
st.markdown("## Change Since Last Full Stock Take")
stc1,stc2=st.columns(2)

all_sts = flat_stock_take_read_from_db()
if all_sts.empty:
    st.info("No stock takes recorded yet, so there is nothing to compare live stock against.")
    st.stop()
all_sts = all_sts.sort_values("created_at", ascending=False).reset_index(drop=True)
all_sts["formatted_date"] = all_sts["created_at"].dt.strftime("%B %d, %Y at %I:%M %p")
latest_st_row = all_sts.iloc[0]
# time since taken
today = pd.Timestamp.now(tz="UTC").normalize()
days_since = (today - latest_st_row["created_at"].normalize()).days
stc1.write(f"{days_since} Day(s) since last Stock Take")
stc1.write(f"Taken By {latest_st_row['created_by']} on {latest_st_row['formatted_date']}")
latest_st = pd.DataFrame(latest_st_row["data"])[["Material", "Quantity", "Location"]]

# total movement of material
movement = latest_st.rename(columns={"Quantity": "At Last Stock Take"}).merge(
    stock_df[["material", "quantity"]].rename(
        columns={"material": "Material", "quantity": "Live Stock"}
    ),
    on="Material",
    how="outer",
)
movement["Change"] = movement["Live Stock"] - movement["At Last Stock Take"]

stc2.metric(label="Total Material +/- Since Last Take", value=movement["Change"].sum())

# table per material type
st.dataframe(
    movement[movement["Change"] != 0], 
    hide_index=True,
    column_config={"Change": st.column_config.NumberColumn("Change", format="%+.1f")}
)

# po lines
st.markdown("## Incoming Orders")
try:
    po_lines = get_po_lines()
except Exception as error:
    print(f"purchase orders unavailable: {error}")
    st.warning("Could not reach Statii, so incoming orders are unavailable just now.")
    po_lines = None

if po_lines is not None and po_lines.empty:
    st.info("Nothing is currently on order.")
elif po_lines is not None:
    st.dataframe(
        po_lines[["number", "supplier", "description", "quantity_outstanding", "date_promised"]].sort_values("date_promised"),
        hide_index=True,
        width="stretch",
        column_config={
            "number": "Order Number",
            "supplier": "Supplier",
            "description": "Material",
            "quantity_outstanding": st.column_config.NumberColumn("Sheets Outstanding", format="%.1f"),
            "date_promised": st.column_config.DateColumn("Date Promised", format="DD MMM YYYY"),
        },
    )
