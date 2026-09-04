# live stock dashboard - the one page that puts the three sources against each other
#   what is on the shelf now       live stock, set by the last stock take and moved by the forms
#   what upcoming bundles will eat provisional nests, dated by the bundle's process date
#   what is still to be delivered  statii po lines, dated by the promised date
# the projection, kpis and charts are all specific to this page, so they live here rather than in
# data.py - that stays data access and parsing only

import pandas as pd
import plotly.express as px
import streamlit as st

from data import (
    flat_stock_take_read_from_db,
    get_flat_bundles,
    get_po_lines,
    get_standard_descriptions,
    live_stock_read_from_db,
    statii_report,
)

st.set_page_config(page_title="Live Stock Dashboard", layout="wide")

TODAY = pd.Timestamp.today().normalize()

# chart colours, stepped for whichever theme the viewer is on rather than flipped automatically
SERIES = "#3987e5" if getattr(st.context.theme, "type", "light") == "dark" else "#2a78d6"
CRITICAL = "#d03b3b"

# status carries an icon and words, so the colour is never the only thing saying it
STATUS_SHORT = "🔴 Short"
STATUS_TIGHT = "🟠 Tight"
STATUS_OK = "🟢 OK"
STATUS_UNKNOWN = "⚪ Not counted"

# pulling the sources onto one timeline
def latest_purchase_prices() -> pd.Series:
    # last price paid per standard description, in pounds per sheet
    # this reads the whole po history rather than get_po_lines, which only keeps future arrivals
    # the request is the same cached call get_po_lines makes, so it costs nothing extra
    # only material that has actually been bought under a standard description gets a price
    df = statii_report("purchase_order_lines")

    df = df[df["description"].isin(get_standard_descriptions())].copy()
    df["date_created"] = pd.to_datetime(df["date_created"], errors="coerce")
    df["unit_price"] = pd.to_numeric(df["unit_price"], errors="coerce")

    df = df.dropna(subset=["unit_price"]).sort_values("date_created")
    return df.groupby("description")["unit_price"].last()

DEMAND_COLUMNS = ["bundle", "nest_ref", "date", "description", "quantity", "customer", "machine"]

def build_demand() -> pd.DataFrame:
    # one row per bundle per material - what the incomplete bundles will consume, and when
    # bundles with no nest yet drop out: we know they will need material, not which or how much
    bundles = get_flat_bundles()
    demand = bundles[bundles["description"].notna()].copy()
    if demand.empty:
        return pd.DataFrame(columns=DEMAND_COLUMNS)

    demand = demand.rename(columns={
        "Bundle/Job": "bundle",
        "Earliest Process Date": "date",
        "Customer": "customer",
        "Machine": "machine",
    })
    demand["quantity"] = pd.to_numeric(demand["quantity"], errors="coerce").fillna(0)
    # a bundle with no process date still eats material, so date it now rather than lose the demand
    demand["date"] = demand["date"].fillna(TODAY)
    return demand[DEMAND_COLUMNS]

INCOMING_COLUMNS = ["po_no", "supplier", "date", "description", "quantity"]

def build_incoming() -> pd.DataFrame:
    # one row per po line still to be delivered
    # get_po_lines has already cut this to future, non cancelled, flat stock lines
    po = get_po_lines()
    if po.empty:
        return pd.DataFrame(columns=INCOMING_COLUMNS)

    po = po.rename(columns={"date_promised": "date"})
    # ordered is what was asked for, outstanding is what is actually still to turn up
    po["quantity"] = pd.to_numeric(po["quantity_outstanding"], errors="coerce").fillna(0)
    po = po[po["quantity"] > 0]
    return po[INCOMING_COLUMNS]

TIMELINE_COLUMNS = ["description", "date", "kind", "reference", "change", "balance"]

def build_timeline(stock: pd.DataFrame, demand: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    # every movement we know is coming, per material, in date order with the balance left after it
    # each material opens with what is on the shelf now, so one with nothing due still plots flat
    moves = []
    # consumption is ordered ahead of delivery on a shared date, so a delivery promised the same
    # day as a bundle cannot paper over the shortage that bundle causes
    for source, kind, order, sign in ((demand, "bundle", 1, -1), (incoming, "delivery", 2, 1)):
        if source.empty:
            continue
        moves.append(pd.DataFrame({
            "description": source["description"],
            "date": pd.to_datetime(source["date"]).dt.normalize(),
            "kind": kind,
            "reference": source["bundle" if kind == "bundle" else "po_no"],
            "change": sign * source["quantity"],
            "order": order,
        }))

    moves = pd.concat(moves, ignore_index=True) if moves else pd.DataFrame(
        columns=["description", "date", "kind", "reference", "change", "order"]
    )

    # overdue bundles are still real demand, so the opening balance sits behind them rather than
    # their date being dragged forward - the projection then starts at the oldest thing outstanding
    opening_date = min(TODAY, moves["date"].min()) if not moves.empty else TODAY

    opening = pd.DataFrame({
        "description": stock["material"],
        "date": opening_date,
        "kind": "opening",
        "reference": "stock on hand",
        # a material nobody has counted reads as nothing on the shelf - flagged separately below
        "change": pd.to_numeric(stock["quantity"], errors="coerce").fillna(0),
        "order": 0,
    })

    timeline = pd.concat([opening, moves], ignore_index=True)
    timeline = timeline.sort_values(["description", "date", "order"], kind="stable")
    timeline["balance"] = timeline.groupby("description")["change"].cumsum()
    return timeline[TIMELINE_COLUMNS].reset_index(drop=True)

def build_summary(stock: pd.DataFrame, timeline: pd.DataFrame) -> pd.DataFrame:
    # one row per standard description - the whole picture for a material on a single line
    df = pd.DataFrame({"material": get_standard_descriptions()})
    df = df.merge(stock, on="material", how="left")

    # a null quantity is a material nobody has counted, which is not the same as none in stock
    df["counted"] = df["quantity"].notna()
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0)
    df["location"] = df["location"].fillna("").replace("", "(not recorded)")
    # the grade and finish is everything after the size - "3000x1500x5 s275" -> "s275"
    df["grade"] = df["material"].str.split(" ", n=1).str[1].fillna("other")

    moves = timeline[timeline["kind"] != "opening"]
    demand = -moves[moves["kind"] == "bundle"].groupby("description")["change"].sum()
    incoming = moves[moves["kind"] == "delivery"].groupby("description")["change"].sum()
    df["committed_demand"] = df["material"].map(demand).fillna(0)
    df["on_order"] = df["material"].map(incoming).fillna(0)

    df["net_after_committed"] = df["quantity"] - df["committed_demand"]
    df["net_after_orders"] = df["net_after_committed"] + df["on_order"]

    # the day the running balance first goes under - what is already on order is in that balance,
    # so a material only lands here if the orders placed do not arrive in time to cover it
    short = timeline[timeline["balance"] < 0]
    df["runs_out_on"] = df["material"].map(short.groupby("description")["date"].first())
    df["days_cover"] = (df["runs_out_on"] - TODAY).dt.days

    # net after orders is the position once everything has been and gone, which can be healthy
    # even where the material is short in the middle - the shortfall is how deep that dip gets,
    # so it is what actually has to be bought to keep every bundle cuttable on its date
    df["worst_balance"] = df["material"].map(timeline.groupby("description")["balance"].min())
    df["shortfall"] = df["worst_balance"].fillna(0).clip(upper=0).abs()

    df["price_per_sheet"] = df["material"].map(latest_purchase_prices())
    df["stock_value"] = df["quantity"] * df["price_per_sheet"]

    df["status"] = STATUS_OK
    # a line nobody has counted is not a healthy one, it is one we know nothing about
    df.loc[~df["counted"], "status"] = STATUS_UNKNOWN
    # landing exactly on zero only means anything where something is actually due to be cut,
    # otherwise every empty line in the list would read as being on the knife edge
    df.loc[(df["committed_demand"] > 0) & (df["net_after_orders"] == 0), "status"] = STATUS_TIGHT
    df.loc[df["runs_out_on"].notna(), "status"] = STATUS_SHORT
    return df

# page
st.title("Live Stock Dashboard")

stock = live_stock_read_from_db()
bundles = get_flat_bundles()
demand = build_demand()
incoming = build_incoming()
timeline = build_timeline(stock, demand, incoming)
summary = build_summary(stock, timeline)

short = summary[summary["status"] == STATUS_SHORT]
never_counted = summary[~summary["counted"]]
bundle_moves = timeline[timeline["kind"] == "bundle"]
bundles_at_risk = bundle_moves.loc[bundle_moves["balance"] < 0, "reference"].nunique()

# kpis
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("Material lines", len(summary))
c2.metric("Lines running short", len(short))
c3.metric(
    "Sheets short",
    f"{summary['shortfall'].sum():,.1f}",
    help="How far the projected balance dips below zero across every material — the sheets "
         "that have to be bought to keep every nested bundle cuttable on its process date.",
)
c4.metric("Bundles at risk", bundles_at_risk)
c5.metric("On order (sheets)", f"{incoming['quantity'].sum():,.1f}")
c6.metric(
    "Stock value",
    f"£{summary['stock_value'].sum():,.0f}",
    help="Valued at the last price paid per sheet. Lines never bought under a standard "
         "description have no price and count as nothing.",
)

# how much of the picture is actually there - a shortage this page cannot see is worse than one
# it reports, so the gaps in the three sources are stated rather than left to be assumed away
counted_lines = int(summary["counted"].sum())
priced_lines = int(summary["price_per_sheet"].notna().sum())
bundles_total = bundles["nest_ref"].nunique()
bundles_nested = bundles.loc[bundles["material_name"].notna(), "nest_ref"].nunique()
st.caption(
    f"{counted_lines} of {len(summary)} lines have ever been counted · "
    f"{priced_lines} have a purchase price · "
    f"{bundles_nested} of {bundles_total} incomplete flat bundles have a provisional nest"
)
if bundles_nested < bundles_total:
    st.warning(
        f"No provisional nest in SharePoint for {bundles_total - bundles_nested} of the "
        f"{bundles_total} incomplete flat bundles — the material they need is missing from "
        "every demand figure below."
    )

tab_overview, tab_projection, tab_bundles, tab_locations, tab_stock_take = st.tabs(
    ["📊 Overview", "📉 Projection", "🛠️ Bundle Demand", "📍 Locations", "🧮 Stock Take Check"]
)

# overview - what needs doing, worst first
with tab_overview:
    st.markdown("### Lines running short")
    st.caption(
        "Stock on hand, less what the nested bundles need, plus what is already on order — "
        "a line only appears here if the material on order does not arrive in time."
    )

    if short.empty:
        st.success("Nothing runs short against the bundles currently nested.")
    else:
        st.dataframe(
            short.sort_values(["days_cover", "shortfall"], ascending=[True, False])[[
                "status", "material", "location", "quantity", "committed_demand",
                "net_after_committed", "on_order", "net_after_orders", "runs_out_on", "days_cover",
                "shortfall",
            ]].round(2).rename(columns={
                "status": "Status",
                "material": "Material",
                "location": "Location",
                "quantity": "Stock",
                "committed_demand": "Committed Demand",
                "net_after_committed": "Net",
                "on_order": "On Order",
                "net_after_orders": "Net After Orders",
                "runs_out_on": "Runs Out On",
                "days_cover": "Days Cover",
                "shortfall": "Sheets Short",
            }),
            hide_index=True,
        )

        fig = px.bar(
            short.nlargest(12, "shortfall").sort_values("shortfall"),
            x="shortfall",
            y="material",
            orientation="h",
            text="shortfall",
            title="Sheets short at the worst point",
            labels={"shortfall": "Sheets short", "material": ""},
        )
        fig.update_traces(marker_color=CRITICAL, texttemplate="%{text:.1f}", textposition="outside")
        fig.update_layout(showlegend=False)
        st.plotly_chart(fig)

    st.markdown("### Stock on hand by grade")
    by_grade = (
        summary.groupby("grade")
        .agg(sheets=("quantity", "sum"), lines=("material", "count"))
        .reset_index()
        .sort_values("sheets", ascending=False)
    )
    fig = px.bar(
        by_grade,
        x="grade",
        y="sheets",
        labels={"grade": "Grade / finish", "sheets": "Sheets in stock"},
    )
    fig.update_traces(marker_color=SERIES)
    st.plotly_chart(fig)

# projection - the running balance, the spreadsheet's job done properly
with tab_projection:
    st.markdown("### Projected balance for one material")
    st.caption(
        "Stock today, then every nested bundle taking material out and every purchase order "
        "putting it back, in date order. Below zero means the bundle cannot be cut."
    )

    # the materials with something happening are the only ones worth plotting, so offer those
    # first and fall back to the full list
    moving = sorted(timeline.loc[timeline["kind"] != "opening", "description"].unique())
    options = moving + [m for m in get_standard_descriptions() if m not in moving]
    material = st.selectbox(
        "Material",
        options=options,
        help=f"{len(moving)} materials have a bundle or a delivery due; the rest are flat.",
    )

    material_timeline = timeline[timeline["description"] == material]
    fig = px.line(
        material_timeline,
        x="date",
        y="balance",
        markers=True,
        line_shape="hv",
        labels={"date": "", "balance": "Sheets"},
        hover_data={"kind": True, "reference": True, "change": ":.2f"},
    )
    fig.update_traces(line_color=SERIES, line_width=2, marker_size=8)
    fig.add_hline(y=0, line_color=CRITICAL, line_width=2, line_dash="dot")
    st.plotly_chart(fig)

    st.dataframe(
        material_timeline[["date", "kind", "reference", "change", "balance"]].round(2).rename(
            columns={
                "date": "Date",
                "kind": "Movement",
                "reference": "Bundle / PO",
                "change": "Sheets",
                "balance": "Balance After",
            }
        ),
        hide_index=True,
    )

    st.markdown("### Every line")
    hide_flat = st.checkbox("Hide lines with no stock and nothing due", value=True)
    table = summary
    if hide_flat:
        table = table[(table["quantity"] != 0) | (table["committed_demand"] != 0) | (table["on_order"] != 0)]

    st.dataframe(
        table.sort_values(["days_cover", "shortfall"], ascending=[True, False], na_position="last")[[
            "status", "material", "location", "quantity", "committed_demand",
            "net_after_committed", "on_order", "net_after_orders", "runs_out_on", "days_cover",
            "shortfall", "stock_value",
        ]].round(2).rename(columns={
            "status": "Status",
            "material": "Material",
            "location": "Location",
            "quantity": "Stock",
            "committed_demand": "Committed Demand",
            "net_after_committed": "Net",
            "on_order": "On Order",
            "net_after_orders": "Net After Orders",
            "runs_out_on": "Runs Out On",
            "days_cover": "Days Cover",
            "shortfall": "Sheets Short",
            "stock_value": "Value (£)",
        }),
        hide_index=True,
    )

# bundle demand - the same numbers read the other way round, per bundle rather than per material
with tab_bundles:
    st.markdown("### What the nested bundles need")
    if demand.empty:
        st.info("No incomplete flat bundle currently has a provisional nest to read material from.")
    else:
        only_risk = st.checkbox("Show only bundles missing material", value=False)

        view = bundle_moves.copy()
        view["sheets_needed"] = -view["change"]
        view["status"] = view["balance"].map(lambda b: STATUS_SHORT if b < 0 else STATUS_OK)
        # one row per bundle per material, so pick the bundle details up by name
        details = demand.drop_duplicates("bundle").set_index("bundle")
        view["customer"] = view["reference"].map(details["customer"])
        view["machine"] = view["reference"].map(details["machine"])
        if only_risk:
            view = view[view["status"] == STATUS_SHORT]

        st.dataframe(
            view.sort_values("date")[[
                "status", "date", "reference", "customer", "machine", "description",
                "sheets_needed", "balance",
            ]].round(2).rename(columns={
                "status": "Status",
                "date": "Process Date",
                "reference": "Bundle",
                "customer": "Customer",
                "machine": "Machine",
                "description": "Material",
                "sheets_needed": "Sheets Needed",
                "balance": "Left After This Bundle",
            }),
            hide_index=True,
        )

    st.markdown("### Deliveries due")
    if incoming.empty:
        st.info("No flat stock is currently on order for a future date.")
    else:
        st.dataframe(
            incoming.sort_values("date").round(2).rename(columns={
                "po_no": "PO",
                "supplier": "Supplier",
                "date": "Promised",
                "description": "Material",
                "quantity": "Sheets Outstanding",
            }),
            hide_index=True,
        )

# locations - where it is, as recorded on the last stock take
with tab_locations:
    st.markdown("### Where the stock is")
    st.caption("Locations come from the stock take, so they are as good as the last count.")

    held = summary[summary["quantity"] != 0]
    by_location = (
        held.groupby("location")
        .agg(lines=("material", "count"), sheets=("quantity", "sum"), value=("stock_value", "sum"))
        .reset_index()
        .sort_values("sheets", ascending=False)
    )

    left, right = st.columns([1, 2])
    with left:
        st.dataframe(
            by_location.round(2).rename(columns={
                "location": "Location",
                "lines": "Lines",
                "sheets": "Sheets",
                "value": "Value (£)",
            }),
            hide_index=True,
        )
    with right:
        if by_location.empty:
            st.info("Nothing is recorded as in stock yet.")
        else:
            location = st.selectbox("Location", options=by_location["location"])
            st.dataframe(
                held[held["location"] == location].sort_values("material")[[
                    "status", "material", "quantity", "committed_demand", "net_after_orders",
                ]].round(2).rename(columns={
                    "status": "Status",
                    "material": "Material",
                    "quantity": "Stock",
                    "committed_demand": "Committed Demand",
                    "net_after_orders": "Net After Orders",
                }),
                hide_index=True,
            )

    if not never_counted.empty:
        with st.expander(f"{len(never_counted)} lines have never been counted"):
            st.caption(
                "These have no quantity and no location against them, so the dashboard treats "
                "them as empty. Anything actually on the shelf here is invisible to it."
            )
            st.dataframe(
                never_counted[["material"]].rename(columns={"material": "Material"}),
                hide_index=True,
            )

# stock take check - live stock against the count it was set from
with tab_stock_take:
    st.markdown("### Movement since the last stock take")
    st.caption(
        "A stock take overwrites live stock, so everything below is what the usage and delivery "
        "forms have recorded since — and a check on whether they are being used at all."
    )

    takes = flat_stock_take_read_from_db()
    if takes.empty:
        st.info("No stock take has been submitted yet.")
    else:
        latest = takes.sort_values("created_at", ascending=False).iloc[0]
        st.write(
            f"Last counted by **{latest['created_by']}** on "
            f"**{latest['created_at'].strftime('%B %d, %Y at %I:%M %p')}**"
        )

        counted = pd.DataFrame(latest["data"])[["Material", "Quantity"]].rename(
            columns={"Material": "material", "Quantity": "counted_quantity"}
        )
        counted["counted_quantity"] = pd.to_numeric(counted["counted_quantity"], errors="coerce")

        variance = stock[["material", "quantity"]].merge(counted, on="material", how="outer")
        variance["movement"] = variance["quantity"].fillna(0) - variance["counted_quantity"].fillna(0)
        moved = variance[variance["movement"] != 0]

        m1, m2, m3 = st.columns(3)
        m1.metric("Lines moved since", len(moved))
        m2.metric("Sheets used", f"{abs(moved['movement'].clip(upper=0).sum()):,.1f}")
        m3.metric("Sheets delivered", f"{moved['movement'].clip(lower=0).sum():,.1f}")

        if moved.empty:
            st.info("Nothing has been reported used or delivered since that count.")
        else:
            st.dataframe(
                moved.sort_values("movement")[[
                    "material", "counted_quantity", "quantity", "movement",
                ]].round(2).rename(columns={
                    "material": "Material",
                    "counted_quantity": "Counted",
                    "quantity": "Now",
                    "movement": "Movement",
                }),
                hide_index=True,
            )
