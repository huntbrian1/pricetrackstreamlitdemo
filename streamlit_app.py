from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from tracker_core import (
    ROW_ID_COLUMN,
    SCRAPINGDOG_RETAILERS,
    canonical_retailer,
    df_to_csv_bytes,
    df_to_xlsx_bytes,
    filter_products,
    load_table_from_github,
    load_seed_csv,
    merge_results_into_master,
    normalize_table,
    price_columns,
    save_table_to_github,
    scrape_products,
)


APP_DIR = Path(__file__).resolve().parent
IMPORT_PATH = APP_DIR / "data" / "retail_wip_links_import.csv"
DEMO_PATH = APP_DIR / "data" / "demo_price_master.csv"
DEFAULT_GITHUB_REPO = "huntbrian1/pricetrackstreamlitdemo"
DEFAULT_GITHUB_BRANCH = "main"
DEFAULT_GITHUB_DATA_PATH = "data/retail_wip_links_import.csv"


def get_secret(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""
    return str(value or os.getenv(name, ""))


def default_seed_path() -> Path:
    return IMPORT_PATH if IMPORT_PATH.exists() else DEMO_PATH


def github_config() -> dict[str, str]:
    return {
        "token": get_secret("GITHUB_TOKEN"),
        "repo": get_secret("GITHUB_REPO") or DEFAULT_GITHUB_REPO,
        "branch": get_secret("GITHUB_BRANCH") or DEFAULT_GITHUB_BRANCH,
        "path": get_secret("GITHUB_DATA_PATH") or DEFAULT_GITHUB_DATA_PATH,
    }


def github_ready(config: dict[str, str]) -> bool:
    return bool(config["token"] and config["repo"] and config["branch"] and config["path"])


def load_default_table() -> pd.DataFrame:
    config = github_config()
    if github_ready(config):
        try:
            table = load_table_from_github(**config)
            st.session_state.github_status = (
                f"Loaded GitHub master from {config['repo']}/{config['path']}"
            )
            return table
        except Exception as exc:
            st.session_state.github_status = (
                f"GitHub load failed, using bundled CSV: {exc}"
            )

    return load_seed_csv(default_seed_path())


def reset_to_seed(path: Path | None = None) -> None:
    st.session_state.price_table = load_seed_csv(path or default_seed_path())
    st.session_state.last_results = pd.DataFrame()
    st.session_state.upload_id = None


def reset_to_github() -> None:
    config = github_config()
    if not github_ready(config):
        st.session_state.github_status = "GitHub save/load is not configured."
        return

    st.session_state.price_table = load_table_from_github(**config)
    st.session_state.last_results = pd.DataFrame()
    st.session_state.upload_id = None
    st.session_state.github_status = f"Loaded GitHub master from {config['repo']}/{config['path']}"


def save_current_table_to_github(message: str) -> str:
    config = github_config()
    if not github_ready(config):
        raise RuntimeError("GitHub save is not configured.")

    response = save_table_to_github(
        st.session_state.price_table,
        **config,
        message=message,
    )
    commit = response.get("commit") or {}
    html_url = commit.get("html_url") or ""
    st.session_state.github_status = (
        f"Saved GitHub master to {config['repo']}/{config['path']}"
    )
    return str(html_url)


def result_rows(results) -> pd.DataFrame:
    rows = []
    for result in results:
        rows.append(
            {
                "row": result.product.row_id,
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


def batched(items: list, batch_size: int) -> list[list]:
    size = max(int(batch_size or 1), 1)
    return [items[start : start + size] for start in range(0, len(items), size)]


def estimate_scrapingdog_credits(products) -> int:
    credits = 0
    for product in products:
        retailer = canonical_retailer(product.retailer)
        if "walmart" in retailer:
            credits += 5
        elif "amazon" in retailer:
            credits += 10
    return credits


def filter_options(df: pd.DataFrame, column: str) -> list[str]:
    return sorted(
        [
            value
            for value in df[column].fillna("").astype(str).str.strip().unique()
            if value
        ],
        key=str.lower,
    )


def contains_filter(df: pd.DataFrame, column: str, needle: str) -> pd.Series:
    if not needle.strip():
        return pd.Series(True, index=df.index)
    return df[column].fillna("").astype(str).str.contains(needle.strip(), case=False, na=False)


def add_row_ids(df: pd.DataFrame) -> pd.DataFrame:
    table = normalize_table(df).reset_index(drop=True)
    table.insert(0, ROW_ID_COLUMN, range(1, len(table) + 1))
    return table


def table_without_row_ids(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_table(df.drop(columns=[ROW_ID_COLUMN], errors="ignore"))


def apply_column_filters(df: pd.DataFrame) -> pd.DataFrame:
    view = add_row_ids(df)

    retailer_filter = st.session_state.get("filter_retailer", [])
    brand_filter = st.session_state.get("filter_brand", [])
    color_filter = st.session_state.get("filter_color", [])
    size_filter = st.session_state.get("filter_size", [])
    title_filter = st.session_state.get("filter_title", "")
    link_filter = st.session_state.get("filter_link", "")
    price_filter_col = st.session_state.get("filter_price_col", "")
    price_filter_mode = st.session_state.get("filter_price_mode", "Any")

    if retailer_filter:
        view = view[view["retailer"].isin(retailer_filter)]
    if brand_filter:
        view = view[view["brand"].isin(brand_filter)]
    if color_filter:
        view = view[view["color"].isin(color_filter)]
    if size_filter:
        view = view[view["size"].isin(size_filter)]

    view = view[contains_filter(view, "title", str(title_filter))]
    view = view[contains_filter(view, "link", str(link_filter))]

    if price_filter_col in view.columns and price_filter_mode != "Any":
        filled = view[price_filter_col].notna() & (
            view[price_filter_col].astype(str).str.strip() != ""
        )
        view = view[filled] if price_filter_mode == "Filled" else view[~filled]

    return view


def merge_editor_view_into_master(master_df: pd.DataFrame, edited_view: pd.DataFrame) -> pd.DataFrame:
    master = add_row_ids(master_df)
    edited = edited_view.copy()

    if ROW_ID_COLUMN not in edited.columns:
        return table_without_row_ids(edited)

    data_columns = [col for col in edited.columns if col != ROW_ID_COLUMN]
    existing_ids = set(master[ROW_ID_COLUMN].astype(int).tolist())
    next_id = int(master[ROW_ID_COLUMN].max()) + 1 if len(master) else 1

    for _, row in edited.iterrows():
        raw_id = row.get(ROW_ID_COLUMN, "")
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError):
            row_id = 0

        if row_id in existing_ids:
            target_index = master.index[master[ROW_ID_COLUMN] == row_id][0]
        else:
            target_index = len(master)
            row_id = next_id
            next_id += 1
            existing_ids.add(row_id)
            master.loc[target_index, ROW_ID_COLUMN] = row_id

        for column in data_columns:
            if column not in master.columns:
                master[column] = ""
            master.at[target_index, column] = row.get(column, "")

    return table_without_row_ids(master)


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
    st.session_state.price_table = load_default_table()
if "last_results" not in st.session_state:
    st.session_state.last_results = pd.DataFrame()
if "upload_id" not in st.session_state:
    st.session_state.upload_id = None
if "github_status" not in st.session_state:
    st.session_state.github_status = ""
if "last_save_url" not in st.session_state:
    st.session_state.last_save_url = ""

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

    config = github_config()
    can_sync_github = github_ready(config)

    if st.button("Load saved GitHub master", use_container_width=True, disabled=not can_sync_github):
        try:
            reset_to_github()
            st.rerun()
        except Exception as exc:
            st.error(f"Could not load GitHub master: {exc}")

    if DEMO_PATH.exists():
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
    run_visible_only = st.checkbox("Run visible filtered rows only", value=True)
    only_missing = st.checkbox("Only rows missing this price column", value=True)
    checkpoint_rows = st.number_input(
        "Checkpoint save every rows",
        min_value=1,
        max_value=500,
        value=75,
        step=5,
    )
    max_rows_this_run = st.number_input(
        "Max rows this run (0 = all)",
        min_value=0,
        max_value=5000,
        value=75,
        step=25,
    )
    confirm_paid_run = st.checkbox("Confirm paid ScrapingDog run")

    st.divider()

    auto_save_github = st.checkbox(
        "Auto-save GitHub master after scrape",
        value=can_sync_github,
        disabled=not can_sync_github,
    )
    if can_sync_github:
        st.caption(f"GitHub file: `{config['repo']}/{config['path']}`")
    else:
        st.caption("GitHub auto-save needs `GITHUB_TOKEN` in Streamlit Secrets.")

    if st.session_state.github_status:
        st.caption(st.session_state.github_status)
    if st.session_state.last_save_url:
        st.caption(f"[Last GitHub save]({st.session_state.last_save_url})")

st.title("Hanes Price Tracker")

table = normalize_table(st.session_state.price_table)
latest_price = price_columns(table)[-1] if price_columns(table) else "none"

m1, m2, m3, m4 = st.columns(4)
m1.metric("Rows", f"{len(table):,}")
m2.metric("Retailers", f"{table['retailer'].nunique():,}")
m3.metric("Price columns", f"{len(price_columns(table)):,}")
m4.metric("Latest price column", latest_price)

with st.expander("Column Filters", expanded=True):
    f1, f2, f3, f4 = st.columns(4)
    f1.multiselect(
        "retailer",
        filter_options(table, "retailer"),
        key="filter_retailer",
    )
    f2.multiselect(
        "brand",
        filter_options(table, "brand"),
        key="filter_brand",
    )
    f3.multiselect(
        "color",
        filter_options(table, "color"),
        key="filter_color",
    )
    f4.multiselect(
        "size",
        filter_options(table, "size"),
        key="filter_size",
    )

    f5, f6, f7, f8 = st.columns([0.28, 0.28, 0.24, 0.20])
    f5.text_input("title contains", key="filter_title")
    f6.text_input("link contains", key="filter_link")
    price_filter_options = [""] + price_columns(table)
    f7.selectbox(
        "price column",
        price_filter_options,
        key="filter_price_col",
    )
    f8.selectbox(
        "price filter",
        ["Any", "Filled", "Blank"],
        key="filter_price_mode",
    )

filtered_view = apply_column_filters(table)
filtered_export = table_without_row_ids(filtered_view)
st.caption(f"Showing {len(filtered_export):,} of {len(table):,} rows.")

price_config = {
    col: st.column_config.NumberColumn(col, format="$%.2f")
    for col in price_columns(table)
}
column_config = {
    ROW_ID_COLUMN: st.column_config.NumberColumn("row", width="small", disabled=True),
    "retailer": st.column_config.TextColumn("retailer", width="small"),
    "brand": st.column_config.TextColumn("brand", width="small"),
    "color": st.column_config.TextColumn("color", width="small"),
    "size": st.column_config.TextColumn("size", width="small"),
    "title": st.column_config.TextColumn("title", width="large"),
    "link": st.column_config.LinkColumn("link", width="large"),
    "last_run": st.column_config.TextColumn("last_run", width="medium"),
    **price_config,
}

edited = st.data_editor(
    filtered_view,
    hide_index=True,
    num_rows="dynamic",
    use_container_width=True,
    height=520,
    column_config=column_config,
    disabled=[ROW_ID_COLUMN],
    key="price_master_editor",
)
st.session_state.price_table = merge_editor_view_into_master(table, edited)

current_table = normalize_table(st.session_state.price_table)
current_filtered_export = table_without_row_ids(apply_column_filters(current_table))
e1, e2, e3, e4, e5 = st.columns([0.17, 0.17, 0.17, 0.17, 0.32], vertical_alignment="center")
e1.download_button(
    "Export Filtered CSV",
    data=df_to_csv_bytes(current_filtered_export),
    file_name=f"hanes_price_master_filtered_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
    mime="text/csv",
    use_container_width=True,
)
e2.download_button(
    "Export Filtered Excel",
    data=df_to_xlsx_bytes(current_filtered_export),
    file_name=f"hanes_price_master_filtered_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)
e3.download_button(
    "Export Full CSV",
    data=df_to_csv_bytes(current_table),
    file_name=f"hanes_price_master_full_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
    mime="text/csv",
    use_container_width=True,
)
e4.download_button(
    "Export Full Excel",
    data=df_to_xlsx_bytes(current_table),
    file_name=f"hanes_price_master_full_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True,
)
e5.caption(
    f"Filtered export: {len(current_filtered_export):,} rows. Full export: {len(current_table):,} rows."
)

run_source_table = apply_column_filters(current_table) if run_visible_only else add_row_ids(current_table)
candidate_products = filter_products(
    run_source_table,
    retailers=selected_retailers,
    only_missing_price_col=price_col if only_missing else None,
)
total_candidate_rows = len(candidate_products)
if max_rows_this_run:
    products_for_run = candidate_products[: int(max_rows_this_run)]
else:
    products_for_run = candidate_products
estimated_credits = estimate_scrapingdog_credits(products_for_run)
limit_note = ""
if max_rows_this_run and total_candidate_rows > len(products_for_run):
    limit_note = f" Limited to first {len(products_for_run):,} by Max rows."
st.caption(
    f"Ready to run {len(products_for_run):,} of {total_candidate_rows:,} matched rows. "
    f"Estimated ScrapingDog credits: {estimated_credits:,}.{limit_note}"
)

save_col, run_col, result_col = st.columns([0.18, 0.22, 0.60], vertical_alignment="center")
save_clicked = save_col.button(
    "Save to GitHub",
    use_container_width=True,
    disabled=not can_sync_github,
)
run_clicked = run_col.button("Run price scrape", type="primary", use_container_width=True)
result_slot = result_col.empty()
checkpoint_slot = st.empty()

if save_clicked:
    try:
        st.session_state.last_save_url = save_current_table_to_github(
            f"Manual price master save {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        result_slot.success("Saved the current table to GitHub.")
    except Exception as exc:
        result_slot.error(f"GitHub save failed: {exc}")

if run_clicked:
    products = products_for_run
    needs_scrapingdog = any(
        canonical_retailer(product.retailer) in SCRAPINGDOG_RETAILERS for product in products
    )

    if delay_max < delay_min:
        result_slot.error("Delay max must be at least delay min.")
    elif not products:
        result_slot.warning("No rows matched the selected filters.")
    elif needs_scrapingdog and not scrapingdog_key:
        result_slot.error("ScrapingDog key is required for Walmart and Amazon rows.")
    elif needs_scrapingdog and not confirm_paid_run:
        result_slot.error("Check Confirm paid ScrapingDog run before running Walmart or Amazon rows.")
    elif needs_scrapingdog and (not auto_save_github or not can_sync_github):
        result_slot.error("Paid ScrapingDog runs require GitHub auto-save so checkpoint results persist.")
    else:
        progress = st.progress(0)
        status = st.empty()
        all_results = []
        completed_rows = 0
        save_message = ""
        save_failed = ""

        batches = batched(products, int(checkpoint_rows))

        with st.spinner("Running scrape"):
            for batch_number, batch in enumerate(batches, start=1):
                batch_start = completed_rows + 1
                batch_end = completed_rows + len(batch)

                def on_progress(current, total, product, result):
                    absolute_current = completed_rows + current
                    progress.progress(min(absolute_current / max(len(products), 1), 1.0))
                    if result is None:
                        status.write(
                            f"{absolute_current}/{len(products)} {product.retailer} "
                            f"(batch {batch_number}/{len(batches)})"
                        )
                    else:
                        status.write(
                            f"{absolute_current}/{len(products)} {product.retailer}: {result.status} "
                            f"(batch {batch_number}/{len(batches)})"
                        )

                batch_results = scrape_products(
                    batch,
                    scrapingdog_api_key=scrapingdog_key,
                    headless=headless,
                    delay_min_sec=float(delay_min),
                    delay_max_sec=float(delay_max),
                    progress_callback=on_progress,
                )

                all_results.extend(batch_results)
                st.session_state.price_table = merge_results_into_master(
                    st.session_state.price_table,
                    batch_results,
                    price_col=price_col,
                    now=datetime.now().astimezone(),
                )
                st.session_state.last_results = result_rows(all_results)
                checkpoint_slot.dataframe(
                    st.session_state.last_results,
                    hide_index=True,
                    use_container_width=True,
                    height=260,
                )
                completed_rows += len(batch)

                if auto_save_github and can_sync_github:
                    try:
                        st.session_state.last_save_url = save_current_table_to_github(
                            f"Checkpoint {price_col} rows {batch_start}-{batch_end}"
                        )
                        save_message = f" Last checkpoint saved rows {batch_start}-{batch_end}."
                        status.write(
                            f"Checkpoint saved rows {batch_start}-{batch_end} of {len(products)}."
                        )
                    except Exception as exc:
                        save_failed = f"GitHub checkpoint save failed after rows {batch_start}-{batch_end}: {exc}"
                        status.error(save_failed)
                        break

        progress.empty()
        if save_failed:
            result_slot.error(
                f"Stopped after {completed_rows} rows into {price_col}. {save_failed}"
            )
        else:
            result_slot.success(
                f"Finished {completed_rows} rows into {price_col}.{save_message}"
            )

if not st.session_state.last_results.empty:
    st.subheader("Last Run")
    st.dataframe(
        st.session_state.last_results,
        hide_index=True,
        use_container_width=True,
        height=260,
    )
