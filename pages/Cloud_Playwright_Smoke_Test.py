from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from tracker_core import Product, df_to_csv_bytes, load_seed_csv, scrape_products  # noqa: E402


TEST_PATH = APP_DIR / "data" / "cloud_playwright_smoke_test.csv"


def running_on_streamlit_cloud() -> bool:
    return (
        Path("/mount/src").exists()
        or os.getenv("STREAMLIT_SHARING_MODE") == "streamlit-cloud"
        or os.getenv("HOME") == "/home/adminuser"
    )


def result_rows(results) -> pd.DataFrame:
    rows = []
    for result in results:
        rows.append(
            {
                "retailer": result.product.retailer,
                "brand": result.product.brand,
                "size": result.product.size,
                "input_url": result.product.url,
                "detected_title": result.title,
                "accepted_price": result.price,
                "status": result.status,
                "source": result.source,
                "raw_price_text": result.raw_price_text,
                "error": result.error,
            }
        )
    return pd.DataFrame(rows)


def products_from_table(df: pd.DataFrame) -> list[Product]:
    products: list[Product] = []
    for _, row in df.iterrows():
        products.append(
            Product(
                retailer=str(row.get("retailer", "")),
                brand=str(row.get("brand", "")),
                color=str(row.get("color", "")),
                size=str(row.get("size", "")),
                url=str(row.get("link", "")),
            )
        )
    return products


st.set_page_config(page_title="Cloud Playwright Smoke Test", layout="wide")
st.title("Cloud Playwright Smoke Test")

st.caption(
    "Runs a small non-Target Playwright-only batch in the current Streamlit deployment. "
    "This does not save to GitHub and does not update the master table."
)

if not TEST_PATH.exists():
    st.error(f"Missing test file: {TEST_PATH}")
    st.stop()

test_table = load_seed_csv(TEST_PATH)
counts = test_table.groupby("retailer", dropna=False).size().reset_index(name="rows")

c1, c2, c3 = st.columns(3)
c1.metric("Test rows", len(test_table))
c2.metric("Retailers", test_table["retailer"].nunique())
c3.metric("Last loaded", datetime.now().strftime("%H:%M:%S"))

st.dataframe(counts, hide_index=True, use_container_width=True)

with st.expander("Rows To Test", expanded=True):
    st.dataframe(
        test_table[["retailer", "brand", "size", "title", "link"]],
        hide_index=True,
        use_container_width=True,
    )

is_cloud = running_on_streamlit_cloud()
if is_cloud:
    st.info("Streamlit Cloud must run Playwright headless. Headed browser mode is disabled here.")
    headless = True
    st.checkbox("Headless browser", value=True, disabled=True)
else:
    headless = st.checkbox("Headless browser", value=True)
delay_min = st.number_input("Delay min seconds", min_value=0.0, value=3.0, step=0.5)
delay_max = st.number_input("Delay max seconds", min_value=0.0, value=6.0, step=0.5)

if delay_max < delay_min:
    st.warning("Delay max must be at least delay min.")

run_clicked = st.button("Run Cloud Playwright Smoke Test", type="primary")

if run_clicked:
    if delay_max < delay_min:
        st.error("Delay max must be at least delay min.")
        st.stop()

    products = products_from_table(test_table)
    progress = st.progress(0)
    status_slot = st.empty()

    def on_progress(current, total, product, result):
        progress.progress(min(current / max(total, 1), 1.0))
        if result is None:
            status_slot.write(f"{current}/{total} {product.retailer}: loading")
        else:
            status_slot.write(f"{current}/{total} {product.retailer}: {result.status}")

    try:
        with st.spinner("Running Playwright smoke test"):
            results = scrape_products(
                products,
                scrapingdog_api_key="",
                headless=headless,
                delay_min_sec=float(delay_min),
                delay_max_sec=float(delay_max),
                progress_callback=on_progress,
            )
    except Exception as exc:
        error_df = pd.DataFrame(
            [
                {
                    "retailer": "cloud_browser_launch",
                    "brand": "",
                    "size": "",
                    "input_url": "",
                    "detected_title": "",
                    "accepted_price": None,
                    "status": "error",
                    "source": "playwright_launch",
                    "raw_price_text": "",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ]
        )
        st.error("Playwright failed before row scraping started. Download the error CSV below.")
        st.dataframe(error_df, hide_index=True, use_container_width=True)
        st.download_button(
            "Download Smoke Test Error CSV",
            data=df_to_csv_bytes(error_df),
            file_name=f"cloud_playwright_smoke_test_error_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
        )
        st.stop()

    results_df = result_rows(results)
    captured = int(results_df["accepted_price"].notna().sum())
    failures = len(results_df) - captured

    m1, m2, m3 = st.columns(3)
    m1.metric("Prices captured", f"{captured}/{len(results_df)}")
    m2.metric("Failures", failures)
    m3.metric("Headless", "yes" if headless else "no")

    by_retailer = (
        results_df.assign(price_found=results_df["accepted_price"].notna())
        .groupby("retailer", dropna=False)
        .agg(rows=("retailer", "size"), prices_found=("price_found", "sum"))
        .reset_index()
    )
    by_retailer["prices_found"] = by_retailer["prices_found"].astype(int)

    st.subheader("Retailer Summary")
    st.dataframe(by_retailer, hide_index=True, use_container_width=True)

    st.subheader("Row Results")
    st.dataframe(results_df, hide_index=True, use_container_width=True)

    st.download_button(
        "Download Smoke Test CSV",
        data=df_to_csv_bytes(results_df),
        file_name=f"cloud_playwright_smoke_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )
