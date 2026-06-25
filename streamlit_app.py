from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from tracker_core import (
    SCRAPINGDOG_RETAILERS,
    df_to_csv_bytes,
    df_to_xlsx_bytes,
    filter_products,
    load_seed_csv,
    merge_results_into_master,
    normalize_table,
    price_columns,
    scrape_products,
)


APP_DIR = Path(__file__).resolve().parent
IMPORT_PATH = APP_DIR / "data" / "retail_wip_links_import.csv"
DEMO_PATH = APP_DIR / "data" / "demo_price_master.csv"


def get_secret(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""
    return str(value or os.getenv(name, ""))


def default_seed_path() -> Path:
    return IMPORT_PATH if IMPORT_PATH.exists() else DEMO_PATH


def reset_to_seed(path: Path | None = None) -> None:
    st.session_state.price_table = load_seed_csv(path or default_seed_path())
    st.session_state.last_results = pd.DataFrame()
    st.session_state.upload_id = None


def result_rows(results) -> pd.DataFrame:
    rows = []
    for result in results:
        rows.append(
            {
                "retailer": result.product.retailer,
                "brand": result.product.brand,
                "color": result.product.color,
                "size": result.product.size,
                "link": result.product.url,
                "price": result.price,
                "status": result.status,
                "source": result.source,
                "error": result.error,
                "raw_price_text": result.raw_price_text,
            }
        )
    return pd.DataFrame(rows)


st.set_page_config(page_title="Hanes Price Tracker", page_icon="H", layout="wide")

st.markdown(
    """
    <style>
      [data-testid="stMetric"] {
        border: 1px solid #e5e7eb;
        border-radius: 8px;
        padding: 0.8rem 0.9rem;
        background: #ffffff;
      }
      div[data-testid="stDataFrame"] {
        border: 1px solid #e5e7eb;
        border-radius: 8px;
      }
      .block-container {
        padding-top: 1.5rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

if "price_table" not in st.session_state:
    reset_to_seed()
if "last_results" not in st.session_state:
    st.session_state.last_results = pd.DataFrame()
if "upload_id" not in st.session_state:
    st.session_state.upload_id = None

with st.sidebar:
    st.header("Price Tracker")

    uploaded = st.file_uploader("Reupload master table", type=["csv", "xlsx", "xls"])
    if uploaded is not None:
        upload_id = (uploaded.name, uploaded.size)
        if st.session_state.upload_id != upload_id:
            try:
                if uploaded.name.lower().endswith((".xlsx", ".xls")):
                    st.session_state.price_table = normalize_table(pd.read_excel(uploaded))
                else:
                    st.session_state.price_table = normalize_table(pd.read_csv(uploaded))
                st.session_state.upload_id = upload_id
                st.session_state.last_results = pd.DataFrame()
                st.success(f"Loaded {uploaded.name}")
            except Exception as exc:
                st.error(f"Could not load upload: {exc}")

    if st.button("Load workbook import", use_container_width=True):
        reset_to_seed(IMPORT_PATH)
        st.rerun()

    if st.button("Load small demo", use_container_width=True):
        reset_to_seed(DEMO_PATH)
        st.rerun()

    st.divider()

    run_date = st.date_input("Price column date", value=datetime.now().date())
    price_col = f"{run_date.isoformat()}_price"
    scrapingdog_key = st.text_input(
        "ScrapingDog API key",
        value=get_secret("SCRAPINGDOG_API_KEY"),
        type="password",
    )
    headless = st.checkbox("Headless browser", value=True)

    delay_min = st.number_input("Delay min seconds", min_value=0.0, value=3.0, step=0.5)
    delay_max = st.number_input("Delay max seconds", min_value=0.0, value=6.0, step=0.5)
    if delay_max < delay_min:
        st.warning("Max delay must be at least min delay.")

    table_for_filters = normalize_table(st.session_state.price_table)
    retailer_options = sorted(
        [r for r in table_for_filters["retailer"].dropna().astype(str).unique() if r.strip()]
    )
    selected_retailers = st.multiselect(
        "Retailers",
        retailer_options,
        default=retailer_options,
    )
    only_missing = st.checkbox("Only rows missing this price column", value=False)

    st.divider()

    c1, c2 = st.columns(2)
    c1.download_button(
        "Download CSV",
        data=df_to_csv_bytes(st.session_state.price_table),
        file_name=f"hanes_price_master_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    c2.download_button(
        "Download Excel",
        data=df_to_xlsx_bytes(st.session_state.price_table),
        file_name=f"hanes_price_master_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

st.title("Hanes Price Tracker")

table = normalize_table(st.session_state.price_table)
latest_price = price_columns(table)[-1] if price_columns(table) else "none"

m1, m2, m3, m4 = st.columns(4)
m1.metric("Rows", f"{len(table):,}")
m2.metric("Retailers", f"{table['retailer'].nunique():,}")
m3.metric("Price columns", f"{len(price_columns(table)):,}")
m4.metric("Latest price column", latest_price)

price_config = {
    col: st.column_config.NumberColumn(col, format="$%.2f")
    for col in price_columns(table)
}
column_config = {
    "retailer": st.column_config.TextColumn("retailer", width="small"),
    "brand": st.column_config.TextColumn("brand", width="small"),
    "color": st.column_config.TextColumn("color", width="small"),
    "size": st.column_config.TextColumn("size", width="small"),
    "link": st.column_config.LinkColumn("link", width="large"),
    "title": st.column_config.TextColumn("title", width="large"),
    "last_seen": st.column_config.TextColumn("last_seen", width="medium"),
    "scrape_status": st.column_config.TextColumn("scrape_status", width="small"),
    "scrape_error": st.column_config.TextColumn("scrape_error", width="large"),
    "price_source": st.column_config.TextColumn("price_source", width="small"),
    "raw_price_text": st.column_config.TextColumn("raw_price_text", width="large"),
    **price_config,
}

edited = st.data_editor(
    table,
    hide_index=True,
    num_rows="dynamic",
    use_container_width=True,
    height=520,
    column_config=column_config,
    key="price_master_editor",
)
st.session_state.price_table = normalize_table(edited)

run_col, result_col = st.columns([0.22, 0.78], vertical_alignment="center")
run_clicked = run_col.button("Run price scrape", type="primary", use_container_width=True)
result_slot = result_col.empty()

if run_clicked:
    products = filter_products(
        st.session_state.price_table,
        retailers=selected_retailers,
        only_missing_price_col=price_col if only_missing else None,
    )
    needs_scrapingdog = any(
        str(product.retailer).strip().lower() in SCRAPINGDOG_RETAILERS for product in products
    )

    if delay_max < delay_min:
        result_slot.error("Delay max must be at least delay min.")
    elif not products:
        result_slot.warning("No rows matched the selected filters.")
    elif needs_scrapingdog and not scrapingdog_key:
        result_slot.error("ScrapingDog key is required for Walmart and Amazon rows.")
    else:
        progress = st.progress(0)
        status = st.empty()

        def on_progress(current, total, product, result):
            progress.progress(min(current / max(total, 1), 1.0))
            if result is None:
                status.write(f"{current}/{total} {product.retailer}")
            else:
                status.write(f"{current}/{total} {product.retailer}: {result.status}")

        with st.spinner("Running scrape"):
            results = scrape_products(
                products,
                scrapingdog_api_key=scrapingdog_key,
                headless=headless,
                delay_min_sec=float(delay_min),
                delay_max_sec=float(delay_max),
                progress_callback=on_progress,
            )

        st.session_state.price_table = merge_results_into_master(
            st.session_state.price_table,
            results,
            price_col=price_col,
            now=datetime.now().astimezone(),
        )
        st.session_state.last_results = result_rows(results)
        progress.empty()
        status.empty()
        result_slot.success(f"Finished {len(results)} rows into {price_col}.")
        st.rerun()

if not st.session_state.last_results.empty:
    st.subheader("Last Run")
    st.dataframe(
        st.session_state.last_results,
        hide_index=True,
        use_container_width=True,
        height=260,
    )
