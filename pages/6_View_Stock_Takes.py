# full overview of most recent stock takes
# view history of previous stock takes

import streamlit as st
from data import flat_stock_take_read_from_db, get_standard_descriptions
import pandas as pd
import plotly.express as px
from ui import page_setup

page_setup("Stock Take History")
st.markdown("# Latest Stock Take")
all_sts = flat_stock_take_read_from_db()
all_sts = all_sts.sort_values("created_at", ascending=False).reset_index(drop=True)
all_sts["formatted_date"] = all_sts["created_at"].dt.strftime("%B %d, %Y at %I:%M %p")
latest_st_row = all_sts.iloc[0]
st.write(f"Taken By {latest_st_row['created_by']} on {latest_st_row['formatted_date']}")
st.dataframe(pd.DataFrame(latest_st_row["data"])[["Material", "Quantity", "Location"]])

st.markdown("# See History per Material Type")
material = st.selectbox("Material", options=get_standard_descriptions())
rows=[]
for _, take in all_sts.iterrows():
        entry = next((e for e in take["data"] if e.get("Material") == material), None)
        rows.append({
            "Date": take["created_at"],
            "Taken By": take["created_by"],
            "Quantity": entry.get("Quantity") if entry else None,
            "Location": entry.get("Location") if entry else None,
        })
material_history = pd.DataFrame(rows)
st.dataframe(material_history)

fig = px.line(material_history, x="Date", y="Quantity", markers=True, title=f"Quantity Over Time - {material}")
st.plotly_chart(fig, use_container_width=True)

st.markdown("# View Specific Stock Take")

@st.dialog("Stock Take Details")
def show_stock_take_details(row):
    st.write(f"**Taken By:** {row['created_by']}")
    st.write(f"**Date:** {row['formatted_date']}")
    st.dataframe(pd.DataFrame(row["data"]))

cards_per_row = 3
for start in range(0, len(all_sts), cards_per_row):
    cols = st.columns(cards_per_row)
    for col, (i, row) in zip(cols, all_sts.iloc[start:start + cards_per_row].iterrows()):
        with col:
            with st.container(border=True, height=180):
                st.write(f"**{row['formatted_date']}**")
                st.write(f"Taken by {row['created_by']}")
                if st.button("View Details", key=f"view_st_{i}"):
                    show_stock_take_details(row)