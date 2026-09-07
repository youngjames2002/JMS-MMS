import streamlit as st
from data import flat_stock_take_generate_empty, flat_stock_take_save_to_db
from ui import page_setup, current_user

page_setup("Perform a Full Stock Take")
st.markdown("Enter Qty and Location for each material (leave blank if none in stock)")

stock_take_empty = flat_stock_take_generate_empty()
stock_take_complete = st.data_editor(stock_take_empty)
st.button(
    "submit stock take",
    on_click=flat_stock_take_save_to_db,
    args=(stock_take_complete, current_user())
)