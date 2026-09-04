import pandas as pd
import streamlit as st
from data import *

st.write("debug data collation page")

po_lines = get_po_lines()
st.write("po lines")
st.dataframe(po_lines)

st.write("stock take")
stock_take_empty = flat_stock_take_generate_empty()
stock_take_complete = st.data_editor(stock_take_empty)
st.button(
    "submit stock take",
    on_click=flat_stock_take_save_to_db,
    args=(stock_take_complete, "username here")
)

st.write("stock take read from db")
st.dataframe(flat_stock_take_read_from_db())

st.write("read bundles staging sheet")
st.dataframe(load_data_sp())

st.write("read material data for each bundle from nest")
nests = read_material_from_nest()
st.write(f"{len(nests)} nests read from sharepoint")
nest_materials = get_flat_bundle_materials()
st.dataframe(nest_materials)

# anything without a description is either cut from a remnant or missing from the mapping / standard list
st.write("nest materials that did not match a standard description")
st.dataframe(nest_materials[nest_materials["description"].isna()])

st.write("raw nest data")
st.json(nests, expanded=True)

st.write("flat bundles joined to their nest materials")
flat_bundles = get_flat_bundles()
bundles_with_material = flat_bundles[flat_bundles["material_name"].notna()]
st.write(
    f"{flat_bundles['nest_ref'].nunique()} incomplete flat bundles, "
    f"{bundles_with_material['nest_ref'].nunique()} of them with a nest"
)
st.dataframe(flat_bundles)

# nests that never found a bundle - the bundle is either finished or not on the staging sheet
orphan_nests = sorted(set(nest_materials["nest_ref"]) - set(flat_bundles["nest_ref"]))
st.write(f"nests with no incomplete bundle to join to: {orphan_nests}")

# live stock table
live_stock = live_stock_read_from_db()
st.write("live stock table")
st.dataframe(live_stock)