import streamlit as st
from data import get_standard_descriptions, record_material_usage
from ui import page_setup

page_setup("Report Material Usage")

with st.form("material_usage_form", clear_on_submit=True):
    material = st.selectbox("Material", options=get_standard_descriptions())
    quantity = st.number_input("Sheets used", min_value=0.0, step=0.5)
    submitted = st.form_submit_button("Submit")

if submitted:
    if quantity <= 0:
        st.error("Enter a quantity greater than zero.")
    else:
        try:
            level = record_material_usage(material, -abs(quantity))
        except LookupError as error:
            st.error(str(error))
        else:
            st.success(
                f"Recorded {quantity:g} sheet(s) of {material} used. "
                f"Now {level:g} sheet(s) in stock."
            )