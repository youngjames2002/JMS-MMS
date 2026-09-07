import streamlit as st
import pandas as pd
import plotly.express as px
from data import *
from ui import page_setup

page_setup("Live Stock Dashboard")


# bundles & projection
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
    # every material against every week, so a quiet week carries the level forward rather
    # than leaving a hole, and a material nothing is nested against still shows its stock
    materials = stock.index.union(used.index.get_level_values("description").unique())
    weeks = used.index.get_level_values("week").unique().sort_values()
    weekly = used.reindex(pd.MultiIndex.from_product([materials, weeks],
                                                     names=["description", "week"]))
    weekly["needed"] = weekly["needed"].fillna(0.0)
    weekly["bundles"] = weekly["bundles"].fillna("")

    weekly = weekly.reset_index()
    weekly["stock"] = weekly["description"].map(stock).fillna(0.0)
    # cumulative usage, so weeks with no usage repeat the previous week's number
    weekly["remaining"] = weekly["stock"] - weekly.groupby("description")["needed"].cumsum()

    return weekly

st.markdown("## Weekly Projection")
weekly = weekly_projection()

grid = weekly.pivot(index="description", columns="week", values="remaining")
grid.columns = [f"w/e {c:%d %b}" for c in grid.columns]
grid = grid.loc[grid.min(axis=1).sort_values().index]  # worst first


def shade(value):
    if value < 0:
        return "background-color: #fee4e2"
    return "background-color: #fef0c7" if value <= 0.5 else ""


st.dataframe(grid.style.map(shade).format("{:+.1f}", na_rep="—"), width="stretch")

material = st.selectbox("Material", grid.index)
st.dataframe(
    weekly[weekly["description"] == material][["week", "needed", "remaining", "bundles"]],
    hide_index=True,
    width="stretch",
    column_config={
        "week": st.column_config.DateColumn("Week ending", format="DD MMM"),
        "needed": st.column_config.NumberColumn("Sheets needed", format="%.2f"),
        "remaining": st.column_config.NumberColumn("Left after", format="%+.1f"),
        "bundles": "Bundles",
    },
)

@st.cache_data(ttl=900)
def bundle_ledger() -> pd.DataFrame:
    # one row per bundle per material, with what is left of that material once this
    # bundle has been cut - the same running balance, kept at bundle grain
    demand = get_flat_bundles()
    demand = demand[demand["description"].notna()].copy()
    demand["date"] = pd.to_datetime(demand["Earliest Process Date"])
    ledger = (
        demand.groupby(["description", "date", "nest_ref", "Bundle/Job"], as_index=False)["quantity"].sum()
        .sort_values(["description", "date", "nest_ref"])
    )
    stock = live_stock_read_from_db().set_index("material")["quantity"]
    ledger["remaining"] = ledger["description"].map(stock).fillna(0) - ledger.groupby(
        "description"
    )["quantity"].cumsum()
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
    ledger[ledger["nest_ref"] == bundle][["description", "quantity", "remaining"]],
    hide_index=True,
    width="stretch",
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