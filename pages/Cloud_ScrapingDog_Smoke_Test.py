from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from tracker_core import (  # noqa: E402
    CATEGORY_COLUMN,
    Product,
    estimate_scrapingdog_credits,
    load_seed_csv,
    scrape_amazon_scrapingdog,
    scrape_walmart_scrapingdog,
)


TEST_PATH = APP_DIR / "data" / "cloud_scrapingdog_smoke_test.csv"


def get_secret(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""
    return str(value or os.getenv(name, ""))


def products_from_table(df: pd.DataFrame) -> list[Product]:
    products: list[Product] = []
    for _, row in df.iterrows():
        products.append(
            Product(
                retailer=str(row.get("retailer", "")),
                brand=str(row.get("brand", "")),
                bras_bottoms=str(row.get(CATEGORY_COLUMN, "")),
                color=str(row.get("color", "")),
                size=str(row.get("size", "")),
                title=str(row.get("title", "")),
                url=str(row.get("link", "")),
            )
        )
    return products


def result_rows(results) -> pd.DataFrame:
    rows = []
    for result in results:
        rows.append(
            {
                "retailer": result.product.retailer,
                "brand": result.product.brand,
                "bras_bottoms": result.product.bras_bottoms,
                "color": result.product.color,
                "input_size": result.product.size,
                "scraped_size": result.detected_size,
                "title": result.product.title,
                "pdp_title": result.title,
                "input_url": result.product.url,
                "accepted_price": result.price,
                "discount_reported": result.discount_reported,
                "status": result.status,
                "source": result.source,
                "error": result.error,
            }
        )
    return pd.DataFrame(rows)


def run_scrapingdog_smoke(products: list[Product], api_key: str, delay_sec: float):
    results = []
    progress = st.progress(0)
    status_slot = st.empty()
    total = len(products)

    for idx, product in enumerate(products, start=1):
        status_slot.write(f"{idx}/{total} {product.retailer}: requesting")
        if idx > 1 and delay_sec > 0:
            time.sleep(delay_sec)

        retailer = product.retailer.strip().lower()
        try:
            if "walmart" in retailer:
                result = scrape_walmart_scrapingdog(product, api_key)
            elif "amazon" in retailer:
                result = scrape_amazon_scrapingdog(product, api_key)
            else:
                result = None
        except Exception as exc:
            from tracker_core import ScrapeResult

            result = ScrapeResult(
                product=product,
                status="error",
                source="smoke_exception",
                error=f"{type(exc).__name__}: {exc}",
            )

        if result is None:
            from tracker_core import ScrapeResult

            result = ScrapeResult(
                product=product,
                status="unsupported",
                error=f"Not a ScrapingDog smoke-test retailer: {product.retailer}",
            )

        results.append(result)
        progress.progress(min(idx / max(total, 1), 1.0))
        status_slot.write(f"{idx}/{total} {product.retailer}: {result.status}")

    return results


st.set_page_config(page_title="Cloud ScrapingDog Smoke Test", layout="wide")
st.title("Cloud ScrapingDog Smoke Test")

st.caption(
    "Runs a small Walmart Product API / Amazon Product API ScrapingDog batch in the current Streamlit deployment. "
    "No Playwright browser is used, and this does not save to GitHub or update the master table."
)

if not TEST_PATH.exists():
    st.error(f"Missing test file: {TEST_PATH}")
    st.stop()

test_table = load_seed_csv(TEST_PATH)
test_products = products_from_table(test_table)
counts = test_table.groupby("retailer", dropna=False).size().reset_index(name="rows")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Test rows", len(test_table))
c2.metric("Retailers", test_table["retailer"].nunique())
c3.metric("Full batch credits", estimate_scrapingdog_credits(test_products))
c4.metric("Last loaded", datetime.now().strftime("%H:%M:%S"))

st.dataframe(counts, hide_index=True, use_container_width=True)

with st.expander("Rows To Test", expanded=True):
    st.dataframe(
        test_table[["retailer", "brand", CATEGORY_COLUMN, "color", "size", "title", "link"]],
        hide_index=True,
        use_container_width=True,
    )

scrapingdog_key = st.text_input(
    "ScrapingDog API key",
    value=get_secret("SCRAPINGDOG_API_KEY"),
    type="password",
)
delay_sec = st.number_input("Delay seconds between requests", min_value=0.0, value=1.0, step=0.5)
max_test_rows = st.number_input(
    "Max rows to test",
    min_value=1,
    max_value=max(len(test_products), 1),
    value=min(2, max(len(test_products), 1)),
    step=1,
)
products_to_run = test_products[: int(max_test_rows)]
estimated_credits = estimate_scrapingdog_credits(products_to_run)
st.caption(
    f"Selected smoke run: {len(products_to_run)} of {len(test_products)} rows, "
    f"estimated {estimated_credits} credits."
)
confirm_paid_run = st.checkbox(
    f"Confirm ScrapingDog paid smoke test run, estimated {estimated_credits} credits",
    value=False,
)

run_clicked = st.button("Run ScrapingDog Smoke Test", type="primary")

if run_clicked:
    if not scrapingdog_key:
        st.error("ScrapingDog API key is required.")
        st.stop()
    if not confirm_paid_run:
        st.error("Check the confirmation box before spending ScrapingDog credits.")
        st.stop()

    products = products_to_run
    with st.spinner("Running ScrapingDog smoke test"):
        results = run_scrapingdog_smoke(products, scrapingdog_key, float(delay_sec))

    results_df = result_rows(results)
    captured = int(results_df["accepted_price"].notna().sum())
    failures = len(results_df) - captured
    scraped_sizes = int(results_df["scraped_size"].fillna("").astype(str).str.strip().ne("").sum())
    discounts = int(results_df["discount_reported"].fillna("").astype(str).str.strip().ne("").sum())

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Prices captured", f"{captured}/{len(results_df)}")
    m2.metric("Failures", failures)
    m3.metric("Sizes scraped", scraped_sizes)
    m4.metric("Discounts reported", discounts)
    m5.metric("Estimated credits used", estimated_credits)

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
        "Download ScrapingDog Smoke Test CSV",
        data=results_df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"cloud_scrapingdog_smoke_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )
