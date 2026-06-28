from __future__ import annotations

import base64
import csv
import html
import io
import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
except Exception:
    PlaywrightTimeoutError = TimeoutError


CATEGORY_COLUMN = "bras_bottoms"
PDP_TITLE_COLUMN = "pdp_title"
BASE_COLUMNS = [
    "retailer",
    "brand",
    CATEGORY_COLUMN,
    "color",
    "size",
    "title",
    PDP_TITLE_COLUMN,
    "link",
]
LAST_RUN_COLUMN = "last_run"
ROW_ID_COLUMN = "_row_id"
PRICE_COL_SUFFIX = "_price"
DISCOUNT_COL_SUFFIX = "_discount"
WALMART_PRODUCT_API = "https://api.scrapingdog.com/walmart/product"
AMAZON_PRODUCT_API = "https://api.scrapingdog.com/amazon/product"
SCRAPINGDOG_GENERIC_API = "https://api.scrapingdog.com/scrape"
GITHUB_CONTENTS_API = "https://api.github.com/repos/{repo}/contents/{path}"
CATEGORY_ALIASES = {
    "bra/bottoms",
    "bras/bottoms",
    "bra_or_bottom",
    "bra_or_bottoms",
    "category",
}
PDP_TITLE_ALIASES = {"product_title", "detected_title", "pdp title", "pdp_title"}


PLAYWRIGHT_RETAILERS = {
    "target",
    "dollar general",
    "dollar_general",
    "tj maxx",
    "tjmaxx",
    "jcpenney",
    "jcpenny",
    "jcp",
    "jc penney",
}

SCRAPINGDOG_RETAILERS = {"walmart", "amazon"}
UNSUPPORTED_RETAILERS = {"macy's", "macys", "macy", "kohl's", "kohls", "kohl"}


@dataclass
class Product:
    retailer: str
    brand: str
    color: str
    size: str
    url: str
    title: str = ""
    bras_bottoms: str = ""
    row_id: int | None = None


@dataclass
class ScrapeResult:
    product: Product
    title: str = ""
    price: float | None = None
    status: str = "not_started"
    error: str = ""
    source: str = ""
    raw_price_text: str = ""
    detected_size: str = ""
    discount_reported: str = ""


class GitHubStorageError(RuntimeError):
    pass


def today_price_col(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.date().isoformat()}{PRICE_COL_SUFFIX}"


def price_columns(df: pd.DataFrame) -> list[str]:
    return [col for col in df.columns if str(col).endswith(PRICE_COL_SUFFIX)]

def is_missing_cell(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() in {"", "nan", "NaN", "None", "<NA>"}


def normalize_text_cell(value: Any) -> str:
    return "" if is_missing_cell(value) else str(value).strip()


def normalize_price_cell(value: Any) -> Any:
    if is_missing_cell(value):
        return ""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    numeric = pd.to_numeric(text.replace("$", "").replace(",", ""), errors="coerce")
    if not pd.isna(numeric):
        return float(numeric)
    return text


def discount_columns(df: pd.DataFrame) -> list[str]:
    return [col for col in df.columns if str(col).endswith(DISCOUNT_COL_SUFFIX)]


def discount_col_for_price_col(price_col: str) -> str:
    if price_col.endswith(PRICE_COL_SUFFIX):
        return f"{price_col[: -len(PRICE_COL_SUFFIX)]}{DISCOUNT_COL_SUFFIX}"
    return f"{price_col}{DISCOUNT_COL_SUFFIX}"


def infer_bras_bottoms(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values).lower()
    if re.search(r"\b(bra|bralette|wirefree|wireless|underwire|t-shirt bra|nursing)\b", text):
        return "Bras"
    if re.search(
        r"\b(underwear|panty|panties|brief|briefs|bikini|hi-cut|high cut|boyshort|boy short)\b",
        text,
    ):
        return "Bottoms"
    return ""


def normalize_retailer(value: Any, url: str = "") -> str:
    raw = str(value or "").strip()
    if raw:
        return raw

    lower = url.lower()
    if "target.com" in lower:
        return "Target"
    if "walmart.com" in lower:
        return "Walmart"
    if "amazon." in lower:
        return "Amazon"
    if "dollargeneral.com" in lower:
        return "Dollar General"
    if "tjmaxx.tjx.com" in lower:
        return "TJ Maxx"
    if "jcpenney.com" in lower:
        return "JCPenney"
    if "macys.com" in lower:
        return "Macy's"
    if "kohls.com" in lower:
        return "Kohl's"
    return ""


def canonical_retailer(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(col).strip() for col in df.columns]
    df = df.astype("object")
    if "last_seen" in df.columns and LAST_RUN_COLUMN not in df.columns:
        df = df.rename(columns={"last_seen": LAST_RUN_COLUMN})

    lower_columns = {str(col).strip().lower(): col for col in df.columns}
    if CATEGORY_COLUMN not in df.columns:
        for alias in CATEGORY_ALIASES:
            source_col = lower_columns.get(alias)
            if source_col is not None:
                df = df.rename(columns={source_col: CATEGORY_COLUMN})
                break

    if PDP_TITLE_COLUMN not in df.columns:
        for alias in PDP_TITLE_ALIASES:
            source_col = lower_columns.get(alias)
            if source_col is not None and source_col != "title":
                df = df.rename(columns={source_col: PDP_TITLE_COLUMN})
                break

    if "link" in df.columns and "url" not in df.columns:
        pass
    elif "url" in df.columns and "link" not in df.columns:
        df = df.rename(columns={"url": "link"})
    elif "url" in df.columns and "link" in df.columns:
        df["link"] = df["link"].fillna("")
        df.loc[df["link"].astype(str).str.strip() == "", "link"] = df["url"]
        df = df.drop(columns=["url"])

    for col in BASE_COLUMNS + [LAST_RUN_COLUMN]:
        if col not in df.columns:
            df[col] = ""

    for col in BASE_COLUMNS + [LAST_RUN_COLUMN]:
        df[col] = df[col].map(normalize_text_cell).astype("object")

    for col in price_columns(df):
        df[col] = df[col].map(normalize_price_cell).astype("object")

    df["link"] = df["link"].fillna("").astype(str).str.strip()
    df["retailer"] = [
        normalize_retailer(retailer, link)
        for retailer, link in zip(df["retailer"], df["link"], strict=False)
    ]
    df["brand"] = df["brand"].fillna("").astype(str).str.strip()
    df[CATEGORY_COLUMN] = df[CATEGORY_COLUMN].fillna("").astype(str).str.strip()
    df["color"] = df["color"].fillna("").astype(str).str.strip()
    df["size"] = df["size"].fillna("").astype(str).str.strip()
    df["title"] = df["title"].fillna("").astype(str).str.strip()
    df[PDP_TITLE_COLUMN] = df[PDP_TITLE_COLUMN].fillna("").astype(str).str.strip()
    for col in price_columns(df) + discount_columns(df):
        df[col] = df[col].astype("object")

    missing_category = df[CATEGORY_COLUMN].astype(str).str.strip() == ""
    if missing_category.any():
        df.loc[missing_category, CATEGORY_COLUMN] = [
            infer_bras_bottoms(title, pdp_title, link)
            for title, pdp_title, link in zip(
                df.loc[missing_category, "title"],
                df.loc[missing_category, PDP_TITLE_COLUMN],
                df.loc[missing_category, "link"],
                strict=False,
            )
        ]

    ordered = BASE_COLUMNS + price_columns(df) + discount_columns(df) + [LAST_RUN_COLUMN]
    return df[ordered]


def dataframe_from_upload(uploaded_file: Any) -> pd.DataFrame:
    name = uploaded_file.name.lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(uploaded_file)
    else:
        df = pd.read_csv(uploaded_file)
    return normalize_table(df)


def load_seed_csv(path: Path) -> pd.DataFrame:
    return normalize_table(pd.read_csv(path))


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return normalize_table(df).to_csv(index=False).encode("utf-8-sig")


def df_to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        normalize_table(df).to_excel(writer, index=False, sheet_name="Price Master")
    output.seek(0)
    return output.read()


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def github_contents_url(repo: str, path: str) -> str:
    return GITHUB_CONTENTS_API.format(
        repo=repo.strip(),
        path=path.strip().lstrip("/"),
    )


def load_table_from_github(
    *,
    token: str,
    repo: str,
    path: str,
    branch: str = "main",
) -> pd.DataFrame:
    response = requests.get(
        github_contents_url(repo, path),
        headers=github_headers(token),
        params={"ref": branch},
        timeout=30,
    )
    if response.status_code != 200:
        raise GitHubStorageError(
            f"GitHub load failed HTTP {response.status_code}: {response.text[:300]}"
        )

    payload = response.json()
    encoded = str(payload.get("content") or "")
    if payload.get("encoding") != "base64" or not encoded:
        raise GitHubStorageError("GitHub file response did not include base64 CSV content.")

    csv_bytes = base64.b64decode(encoded)
    return normalize_table(pd.read_csv(io.BytesIO(csv_bytes)))


def save_table_to_github(
    df: pd.DataFrame,
    *,
    token: str,
    repo: str,
    path: str,
    branch: str = "main",
    message: str = "Update price master table",
) -> dict[str, Any]:
    url = github_contents_url(repo, path)
    headers = github_headers(token)
    params = {"ref": branch}

    sha = ""
    get_response = requests.get(url, headers=headers, params=params, timeout=30)
    if get_response.status_code == 200:
        sha = str(get_response.json().get("sha") or "")
    elif get_response.status_code != 404:
        raise GitHubStorageError(
            f"GitHub pre-save check failed HTTP {get_response.status_code}: {get_response.text[:300]}"
        )

    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(df_to_csv_bytes(df)).decode("ascii"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha

    put_response = requests.put(url, headers=headers, json=body, timeout=45)
    if put_response.status_code not in {200, 201}:
        raise GitHubStorageError(
            f"GitHub save failed HTTP {put_response.status_code}: {put_response.text[:300]}"
        )

    return put_response.json()


def products_from_df(df: pd.DataFrame) -> list[Product]:
    rows: list[Product] = []
    source = df.copy()
    row_ids: dict[Any, int] = {}
    if ROW_ID_COLUMN in source.columns:
        for idx, value in source[ROW_ID_COLUMN].items():
            try:
                row_ids[idx] = int(value)
            except (TypeError, ValueError):
                pass

    normalized = normalize_table(df)
    for _, row in normalized.iterrows():
        link = str(row.get("link", "") or "").strip()
        if not link:
            continue
        rows.append(
            Product(
                retailer=str(row.get("retailer", "") or "").strip(),
                brand=str(row.get("brand", "") or "").strip(),
                bras_bottoms=str(row.get(CATEGORY_COLUMN, "") or "").strip(),
                color=str(row.get("color", "") or "").strip(),
                size=str(row.get("size", "") or "").strip(),
                title=str(row.get("title", "") or "").strip(),
                url=link,
                row_id=row_ids.get(row.name),
            )
        )
    return rows


def filter_products(
    df: pd.DataFrame,
    retailers: list[str] | None = None,
    only_missing_price_col: str | None = None,
) -> list[Product]:
    normalized = normalize_table(df)
    if ROW_ID_COLUMN in df.columns:
        normalized[ROW_ID_COLUMN] = df[ROW_ID_COLUMN]

    if retailers:
        wanted = {canonical_retailer(r) for r in retailers}
        normalized = normalized[
            normalized["retailer"].map(canonical_retailer).isin(wanted)
        ]

    if only_missing_price_col and only_missing_price_col in normalized.columns:
        normalized = normalized[
            normalized[only_missing_price_col].isna()
            | (normalized[only_missing_price_col].astype(str).str.strip() == "")
        ]

    return products_from_df(normalized)


def estimate_scrapingdog_credits(products: list[Product]) -> int:
    credits = 0
    for product in products:
        retailer = canonical_retailer(product.retailer)
        if "walmart" in retailer:
            credits += 5
        elif "amazon" in retailer:
            credits += 10
    return credits


def merge_results_into_master(
    df: pd.DataFrame,
    results: list[ScrapeResult],
    price_col: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    now = now or datetime.now(timezone.utc)
    master = normalize_table(df)
    discount_col = discount_col_for_price_col(price_col)
    if price_col not in master.columns:
        master[price_col] = ""
    if discount_col not in master.columns:
        master[discount_col] = ""
    master[price_col] = master[price_col].astype("object")
    master[discount_col] = master[discount_col].astype("object")

    key_cols = ["retailer", "brand", CATEGORY_COLUMN, "color", "size", "link"]
    existing = {
        tuple(str(row.get(col, "") or "").strip() for col in key_cols): idx
        for idx, row in master.iterrows()
    }

    for result in results:
        product = result.product
        row_index = None
        if product.row_id is not None:
            candidate_index = product.row_id - 1
            if 0 <= candidate_index < len(master):
                row_index = candidate_index

        key = (
            product.retailer,
            product.brand,
            product.bras_bottoms,
            product.color,
            product.size,
            product.url,
        )
        if row_index is not None:
            idx = row_index
        elif key in existing:
            idx = existing[key]
        else:
            idx = len(master)
            master.loc[idx, key_cols] = list(key)
            existing[key] = idx

        if product.title and not str(master.at[idx, "title"] or "").strip():
            master.at[idx, "title"] = product.title
        if result.title:
            master.at[idx, PDP_TITLE_COLUMN] = result.title
        master.at[idx, LAST_RUN_COLUMN] = now.isoformat(timespec="seconds")
        master.at[idx, price_col] = "" if result.price is None else result.price
        master.at[idx, discount_col] = result.discount_reported or ""

    return normalize_table(master)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def parse_price(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"\$?\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def price_from_text(text: str) -> tuple[float | None, str]:
    clean = normalize_text(text)
    patterns = [
        r"(?:Now|Sale|Current price|Price)\s*\$([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)",
        r"\$([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?)\s*(?:Sale|Now|current|price)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.I)
        if match:
            return parse_price(match.group(1)), match.group(0)
    return None, ""


def walk_json(value: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(value, dict):
        values.append(value)
        for item in value.values():
            values.extend(walk_json(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(walk_json(item))
    return values


def compact_discount(value: Any) -> str:
    text = normalize_text(str(value or ""))
    if not text:
        return ""
    percent = re.search(r"([0-9]{1,3}(?:\.[0-9]+)?)\s*(?:%|percent)", text, flags=re.I)
    if percent:
        number = percent.group(1).rstrip("0").rstrip(".")
        return f"{number}%"
    money = re.search(r"\$[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?", text)
    if money:
        return money.group(0)
    return text[:80]


def discount_from_prices(current: float | None, previous: Any) -> str:
    previous_price = parse_price(previous)
    if current is None or previous_price is None or previous_price <= current:
        return ""
    savings_pct = ((previous_price - current) / previous_price) * 100
    if savings_pct <= 0:
        return ""
    return f"{savings_pct:.1f}%".replace(".0%", "%")


def discount_from_json(data: dict[str, Any], current_price: float | None = None) -> str:
    preferred_keys = (
        "median_price_savings_percentage",
        "savings_percentage",
        "discount_percentage",
        "discount_percent",
        "percent_off",
        "savings",
        "discount",
        "you_save",
    )
    for key in preferred_keys:
        value = data.get(key)
        discount = compact_discount(value)
        if discount:
            return discount

    for previous_key in ("previous_price", "regular_price", "list_price", "was_price", "strike_price"):
        discount = discount_from_prices(current_price, data.get(previous_key))
        if discount:
            return discount

    for node in walk_json(data):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            key_text = str(key).lower()
            if any(token in key_text for token in ("saving", "discount", "percent_off")):
                discount = compact_discount(value)
                if discount:
                    return discount
    return ""


def clean_size_candidate(value: Any) -> str:
    text = normalize_text(str(value or ""))
    if not text or len(text) > 40:
        return ""
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:in|inch|inches|cm|mm)\b", text, flags=re.I):
        return ""
    if re.search(r"\d+\s*[xX]\s*\d+", text):
        return ""
    bad_terms = ("dimension", "department", "model", "weight", "package", "care", "origin")
    if any(term in text.lower() for term in bad_terms):
        return ""
    return text


def extract_reported_size_from_json(data: dict[str, Any]) -> str:
    direct_keys = (
        "size",
        "selected_size",
        "size_name",
        "clothing_size",
        "apparel_size",
        "variant_size",
    )
    for key in direct_keys:
        size = clean_size_candidate(data.get(key))
        if size:
            return size

    product_info = data.get("product_information")
    if isinstance(product_info, dict):
        for key, value in product_info.items():
            key_text = str(key).strip().lower()
            if key_text in {"size", "size name", "clothing size"}:
                size = clean_size_candidate(value)
                if size:
                    return size

    for node in walk_json(data):
        if not isinstance(node, dict):
            continue
        label = normalize_text(
            str(
                node.get("name")
                or node.get("label")
                or node.get("dimension")
                or node.get("variant")
                or ""
            )
        ).lower()
        if "size" not in label:
            continue

        for value_key in ("selected_value", "selectedValue", "current_value", "value", "display_value"):
            size = clean_size_candidate(node.get(value_key))
            if size:
                return size

        values = node.get("values")
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                selected = value.get("selected") or value.get("is_selected") or value.get("current")
                if selected:
                    size = clean_size_candidate(
                        value.get("name") or value.get("value") or value.get("display_value")
                    )
                    if size:
                        return size
    return ""


def parse_jsonld_for_price(markup: str) -> tuple[float | None, str, str]:
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>([\s\S]*?)</script>",
        markup or "",
        flags=re.I,
    )
    for script in scripts:
        try:
            payload = json.loads(script.strip())
        except Exception:
            continue
        for node in walk_json(payload):
            if not isinstance(node, dict):
                continue
            offers = node.get("offers")
            if not offers:
                continue
            offer_list = offers if isinstance(offers, list) else [offers]
            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                raw = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
                price = parse_price(raw)
                if price is not None:
                    return price, str(raw), str(node.get("name") or "")
    return None, "", ""


def common_chromium_executable() -> str | None:
    env_path = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    candidates = [
        env_path,
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def launch_browser(playwright: Any, headless: bool):
    executable = common_chromium_executable()
    kwargs: dict[str, Any] = {"headless": headless}
    if executable:
        kwargs["executable_path"] = executable
    return playwright.chromium.launch(**kwargs)


def scrape_playwright_generic(
    page: Any,
    product: Product,
    navigation_timeout_ms: int = 60_000,
) -> ScrapeResult:
    try:
        page.goto(product.url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
        page.wait_for_timeout(2_500)
    except PlaywrightTimeoutError:
        return ScrapeResult(product=product, status="error", error="Timeout loading page")
    except Exception as exc:
        return ScrapeResult(product=product, status="error", error=str(exc))

    try:
        snapshot = page.evaluate(
            """
            () => {
              const textOf = (selector) => {
                const el = document.querySelector(selector);
                return el ? (el.textContent || '').trim() : '';
              };
              const attr = (selector, name) => {
                const el = document.querySelector(selector);
                return el ? (el.getAttribute(name) || '') : '';
              };
              const title =
                textOf('[data-test="product-title"]') ||
                textOf('h1[data-test="product-title"]') ||
                textOf('h1') ||
                document.title ||
                '';
              const visiblePrice =
                textOf('[data-test="product-price"]') ||
                textOf('[data-test="product-price-container"]') ||
                textOf('[data-testid*="price" i]') ||
                textOf('[class*="price" i]') ||
                attr('meta[property="product:price:amount"]', 'content') ||
                attr('meta[property="og:price:amount"]', 'content') ||
                attr('meta[itemprop="price"]', 'content') ||
                '';
              const bodyText = document.body ? document.body.innerText : '';
              return {
                title,
                visiblePrice,
                bodyText,
                html: document.documentElement.outerHTML
              };
            }
            """
        )
    except Exception as exc:
        return ScrapeResult(product=product, status="error", error=f"Page evaluate failed: {exc}")

    title = normalize_text(snapshot.get("title", ""))
    price = parse_price(snapshot.get("visiblePrice", ""))
    raw = normalize_text(snapshot.get("visiblePrice", ""))
    source = "visible_selector" if price is not None else ""

    if price is None:
        json_price, json_raw, json_title = parse_jsonld_for_price(snapshot.get("html", ""))
        if json_price is not None:
            price = json_price
            raw = json_raw
            source = "json_ld"
            title = title or normalize_text(json_title)

    if price is None:
        text_price, text_raw = price_from_text(snapshot.get("bodyText", ""))
        if text_price is not None:
            price = text_price
            raw = text_raw
            source = "body_text"

    return ScrapeResult(
        product=product,
        title=title,
        price=price,
        status="price_captured" if price is not None else "loaded_no_price",
        error="" if price is not None else "Price not found",
        source=source,
        raw_price_text=raw,
    )


def scrape_walmart_scrapingdog(product: Product, api_key: str) -> ScrapeResult:
    if not api_key:
        return ScrapeResult(product=product, status="error", error="Missing ScrapingDog API key")
    try:
        response = requests.get(
            WALMART_PRODUCT_API,
            params={"api_key": api_key, "url": product.url},
            timeout=90,
        )
    except Exception as exc:
        return ScrapeResult(product=product, status="error", error=str(exc))

    if response.status_code != 200:
        return ScrapeResult(
            product=product,
            status="error",
            error=f"ScrapingDog Walmart API HTTP {response.status_code}: {response.text[:250]}",
            source="scrapingdog_walmart",
        )

    try:
        data = response.json()
    except ValueError:
        return ScrapeResult(product=product, status="error", error="ScrapingDog returned non-JSON")

    product_data = data.get("product_results") or {}
    if not isinstance(product_data, dict):
        product_data = {}
    title = normalize_text(product_data.get("title") or "")
    raw = ""
    price = None

    price_map = product_data.get("price_map")
    if isinstance(price_map, list) and price_map:
        raw = str(price_map[0])
        price = parse_price(raw)

    if price is None:
        offers = product_data.get("offers")
        if isinstance(offers, list) and offers and isinstance(offers[0], dict):
            raw = str(offers[0].get("price") or "")
            price = parse_price(raw)

    if price is None:
        raw = str(product_data.get("price") or "")
        price = parse_price(raw)

    detected_size = extract_reported_size_from_json(product_data)
    discount_reported = discount_from_json(product_data, price)

    return ScrapeResult(
        product=product,
        title=title,
        price=price,
        status="price_captured" if price is not None else "loaded_no_price",
        error="" if price is not None else "Price not found in ScrapingDog Walmart response",
        source="scrapingdog_walmart",
        raw_price_text=raw,
        detected_size=detected_size,
        discount_reported=discount_reported,
    )


def extract_amazon_product_api_price(data: dict[str, Any]) -> tuple[float | None, str, str]:
    price_keys = ("price", "current_price", "buybox_price", "sale_price", "deal_price", "our_price")

    def parse_candidate(path: str, value: Any) -> tuple[float | None, str, str]:
        if value in (None, ""):
            return None, "", ""
        price = parse_price(value)
        if price is not None:
            return price, str(value), path
        return None, "", ""

    for key in price_keys:
        price, raw, path = parse_candidate(key, data.get(key))
        if price is not None:
            return price, raw, path

    for container_key in ("buybox", "buying_options", "purchase_options"):
        container = data.get(container_key)
        if isinstance(container, dict):
            for key in price_keys:
                price, raw, path = parse_candidate(f"{container_key}.{key}", container.get(key))
                if price is not None:
                    return price, raw, path
        elif isinstance(container, list):
            for idx, item in enumerate(container):
                if not isinstance(item, dict):
                    continue
                for key in price_keys:
                    price, raw, path = parse_candidate(f"{container_key}[{idx}].{key}", item.get(key))
                    if price is not None:
                        return price, raw, path

    return None, "", ""


def scrape_amazon_scrapingdog(product: Product, api_key: str) -> ScrapeResult:
    if not api_key:
        return ScrapeResult(product=product, status="error", error="Missing ScrapingDog API key")

    try:
        response = requests.get(
            AMAZON_PRODUCT_API,
            params={
                "api_key": api_key,
                "url": product.url,
                "country": "us",
            },
            timeout=120,
        )
    except Exception as exc:
        return ScrapeResult(product=product, status="error", error=str(exc))

    body = response.text or ""
    if response.status_code != 200:
        return ScrapeResult(
            product=product,
            status="error",
            error=f"ScrapingDog Amazon Product API HTTP {response.status_code}: {body[:250]}",
            source="scrapingdog_amazon_product",
        )

    try:
        data = response.json()
    except ValueError:
        return ScrapeResult(
            product=product,
            status="error",
            error="ScrapingDog Amazon Product API returned non-JSON",
            source="scrapingdog_amazon_product",
        )

    title = normalize_text(data.get("title") or "")
    price, raw, path = extract_amazon_product_api_price(data)
    detected_size = extract_reported_size_from_json(data)
    discount_reported = discount_from_json(data, price)

    return ScrapeResult(
        product=product,
        title=title,
        price=price,
        status="price_captured" if price is not None else "loaded_no_price",
        error="" if price is not None else "Price not found in ScrapingDog Amazon Product API response",
        source="scrapingdog_amazon_product",
        raw_price_text=f"{path}: {raw}" if path and raw else raw,
        detected_size=detected_size,
        discount_reported=discount_reported,
    )


def scrape_products(
    products: list[Product],
    scrapingdog_api_key: str = "",
    headless: bool = True,
    delay_min_sec: float = 3.0,
    delay_max_sec: float = 6.0,
    progress_callback: Callable[[int, int, Product, ScrapeResult | None], None] | None = None,
) -> list[ScrapeResult]:
    results: list[ScrapeResult] = []
    playwright_products: list[tuple[int, Product]] = []

    for idx, product in enumerate(products, start=1):
        retailer = canonical_retailer(product.retailer)
        if retailer in UNSUPPORTED_RETAILERS:
            result = ScrapeResult(
                product=product,
                status="unsupported",
                error="Retailer disabled: Macy's/Kohl's did not work with Playwright or ScrapingDog in testing.",
            )
            results.append(result)
            if progress_callback:
                progress_callback(idx, len(products), product, result)
            continue

        if retailer in SCRAPINGDOG_RETAILERS:
            try:
                if "walmart" in retailer:
                    result = scrape_walmart_scrapingdog(product, scrapingdog_api_key)
                else:
                    result = scrape_amazon_scrapingdog(product, scrapingdog_api_key)
            except Exception as exc:
                result = ScrapeResult(
                    product=product,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}",
                    source="scrapingdog_exception",
                )
            results.append(result)
            if progress_callback:
                progress_callback(idx, len(products), product, result)
            continue

        if retailer in PLAYWRIGHT_RETAILERS:
            playwright_products.append((idx, product))
        else:
            result = ScrapeResult(product=product, status="unsupported", error=f"Unsupported retailer: {product.retailer}")
            results.append(result)
            if progress_callback:
                progress_callback(idx, len(products), product, result)

    if playwright_products:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            for idx, product in playwright_products:
                result = ScrapeResult(
                    product=product,
                    status="error",
                    error=f"Playwright is not available in this deployment: {exc}",
                )
                results.append(result)
                if progress_callback:
                    progress_callback(idx, len(products), product, result)
            return results

        browser = None
        try:
            playwright_context = sync_playwright().start()
            browser = launch_browser(playwright_context, headless=headless)
        except Exception as exc:
            for idx, product in playwright_products:
                result = ScrapeResult(
                    product=product,
                    status="error",
                    error=f"Playwright browser launch failed: {type(exc).__name__}: {exc}",
                    source="playwright_launch",
                )
                results.append(result)
                if progress_callback:
                    progress_callback(idx, len(products), product, result)
            if "playwright_context" in locals():
                try:
                    playwright_context.stop()
                except Exception:
                    pass
            return results

        try:
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            try:
                for idx, product in playwright_products:
                    delay = random.uniform(delay_min_sec, delay_max_sec)
                    time.sleep(delay)
                    try:
                        result = scrape_playwright_generic(page, product)
                    except Exception as exc:
                        result = ScrapeResult(
                            product=product,
                            status="error",
                            error=f"{type(exc).__name__}: {exc}",
                            source="playwright_exception",
                        )
                    results.append(result)
                    if progress_callback:
                        progress_callback(idx, len(products), product, result)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        finally:
            try:
                playwright_context.stop()
            except Exception:
                pass

    return results
