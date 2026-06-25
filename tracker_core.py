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


BASE_COLUMNS = ["retailer", "brand", "color", "size", "title", "link"]
LAST_RUN_COLUMN = "last_run"
ROW_ID_COLUMN = "_row_id"
PRICE_COL_SUFFIX = "_price"
WALMART_PRODUCT_API = "https://api.scrapingdog.com/walmart/product"
SCRAPINGDOG_GENERIC_API = "https://api.scrapingdog.com/scrape"
GITHUB_CONTENTS_API = "https://api.github.com/repos/{repo}/contents/{path}"


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


class GitHubStorageError(RuntimeError):
    pass


def today_price_col(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.date().isoformat()}{PRICE_COL_SUFFIX}"


def price_columns(df: pd.DataFrame) -> list[str]:
    return [col for col in df.columns if str(col).endswith(PRICE_COL_SUFFIX)]


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
    if "last_seen" in df.columns and LAST_RUN_COLUMN not in df.columns:
        df = df.rename(columns={"last_seen": LAST_RUN_COLUMN})

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
        df[col] = df[col].fillna("").astype(str)
        df[col] = df[col].replace({"nan": "", "NaN": "", "None": ""})

    df["link"] = df["link"].fillna("").astype(str).str.strip()
    df["retailer"] = [
        normalize_retailer(retailer, link)
        for retailer, link in zip(df["retailer"], df["link"], strict=False)
    ]
    df["brand"] = df["brand"].fillna("").astype(str).str.strip()
    df["color"] = df["color"].fillna("").astype(str).str.strip()
    df["size"] = df["size"].fillna("").astype(str).str.strip()

    ordered = BASE_COLUMNS + price_columns(df) + [LAST_RUN_COLUMN]
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
                color=str(row.get("color", "") or "").strip(),
                size=str(row.get("size", "") or "").strip(),
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


def merge_results_into_master(
    df: pd.DataFrame,
    results: list[ScrapeResult],
    price_col: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    now = now or datetime.now(timezone.utc)
    master = normalize_table(df)
    if price_col not in master.columns:
        master[price_col] = ""

    key_cols = ["retailer", "brand", "color", "size", "link"]
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

        key = (product.retailer, product.brand, product.color, product.size, product.url)
        if row_index is not None:
            idx = row_index
        elif key in existing:
            idx = existing[key]
        else:
            idx = len(master)
            master.loc[idx, key_cols] = list(key)
            existing[key] = idx

        if result.title:
            master.at[idx, "title"] = result.title
        master.at[idx, LAST_RUN_COLUMN] = now.isoformat(timespec="seconds")
        master.at[idx, price_col] = "" if result.price is None else result.price

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

    return ScrapeResult(
        product=product,
        title=title,
        price=price,
        status="price_captured" if price is not None else "loaded_no_price",
        error="" if price is not None else "Price not found in ScrapingDog Walmart response",
        source="scrapingdog_walmart",
        raw_price_text=raw,
    )


def scrape_amazon_scrapingdog(product: Product, api_key: str) -> ScrapeResult:
    if not api_key:
        return ScrapeResult(product=product, status="error", error="Missing ScrapingDog API key")

    try:
        response = requests.get(
            SCRAPINGDOG_GENERIC_API,
            params={
                "api_key": api_key,
                "url": product.url,
                "dynamic": "true",
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
            error=f"ScrapingDog generic API HTTP {response.status_code}: {body[:250]}",
            source="scrapingdog_generic",
        )

    price, raw, json_title = parse_jsonld_for_price(body)
    title = normalize_text(json_title)
    if not title:
        title_match = re.search(r"<title[^>]*>([\s\S]*?)</title>", body, flags=re.I)
        title = normalize_text(title_match.group(1)) if title_match else ""

    if price is None:
        text = re.sub(r"<[^>]+>", " ", body)
        price, raw = price_from_text(text)

    return ScrapeResult(
        product=product,
        title=title,
        price=price,
        status="price_captured" if price is not None else "loaded_no_price",
        error="" if price is not None else "Price not found in ScrapingDog Amazon response",
        source="scrapingdog_generic",
        raw_price_text=raw,
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
            if "walmart" in retailer:
                result = scrape_walmart_scrapingdog(product, scrapingdog_api_key)
            else:
                result = scrape_amazon_scrapingdog(product, scrapingdog_api_key)
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

        with sync_playwright() as playwright:
            browser = launch_browser(playwright, headless=headless)
            page = browser.new_page(viewport={"width": 1366, "height": 900})
            try:
                for idx, product in playwright_products:
                    delay = random.uniform(delay_min_sec, delay_max_sec)
                    time.sleep(delay)
                    result = scrape_playwright_generic(page, product)
                    results.append(result)
                    if progress_callback:
                        progress_callback(idx, len(products), product, result)
            finally:
                browser.close()

    return results
