from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd

try:
    import requests
except Exception:  # pragma: no cover - handled only when API retailers are selected
    requests = None

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - handled at runtime for install diagnostics
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None


CATEGORY_COLUMN = "bras_bottoms"
PDP_TITLE_COLUMN = "pdp_title"
LAST_RUN_COLUMN = "last_run"
PRICE_COL_SUFFIX = "_price"
DISCOUNT_COL_SUFFIX = "_discount"

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
API_RETAILERS = {"walmart", "amazon"}
DEFAULT_LOCAL_RETAILERS = ["Target", "Dollar General", "TJ Maxx", "JCPenney"]
JCPENNEY_RETAILERS = {"jcpenney", "jcpenny", "jcp", "jc penney"}
NON_PRODUCT_PRICE_CONTEXT_RE = re.compile(
    r"shipping|orders?\s+over|gift\s+card|credit\s+card|financing|minimum|threshold|"
    r"pickup\s+fee|delivery\s+fee|reward|you\s+may\s+also\s+like|similar\s+items|"
    r"customers\s+also\s+viewed|sponsored|recently\s+viewed|more\s+like\s+this|"
    r"complete\s+the\s+look|shop\s+similar|people\s+also\s+viewed|recommended",
    re.I,
)
RECOMMENDATION_CUTOFF_TERMS = [
    "you may also like",
    "similar items",
    "customers also viewed",
    "sponsored",
    "recently viewed",
    "more like this",
    "complete the look",
    "shop similar",
    "people also viewed",
    "recommended",
]

WALMART_PRODUCT_API = "https://api.scrapingdog.com/walmart/product"
AMAZON_PRODUCT_API = "https://api.scrapingdog.com/amazon/product"


@dataclass
class Product:
    row_number: int
    retailer: str
    brand: str
    bras_bottoms: str
    color: str
    size: str
    title: str
    url: str


@dataclass
class ScrapeResult:
    product: Product
    price: float | None = None
    title: str = ""
    status: str = "not_started"
    source: str = ""
    error: str = ""
    raw_price_text: str = ""
    discount_reported: str = ""


def log(message: str = "") -> None:
    print(message, flush=True)


def normalize_text(value: Any) -> str:
    text = html.unescape(str(value or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def canonical_retailer(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def normalize_retailer(value: Any, link: str = "") -> str:
    raw = str(value or "").strip()
    if raw:
        return raw
    lower = link.lower()
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
    return ""


def infer_bras_bottoms(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values).lower()
    if re.search(r"\b(bra|bralette|wirefree|wireless|underwire|t-shirt bra|nursing)\b", text):
        return "Bras"
    if re.search(r"\b(underwear|panty|panties|brief|briefs|bikini|hi-cut|high cut|boyshort|boy short)\b", text):
        return "Bottoms"
    return ""


def price_col_for_date(run_date: str) -> str:
    return f"{run_date}{PRICE_COL_SUFFIX}"


def discount_col_for_price_col(price_col: str) -> str:
    if price_col.endswith(PRICE_COL_SUFFIX):
        return f"{price_col[:-len(PRICE_COL_SUFFIX)]}{DISCOUNT_COL_SUFFIX}"
    return f"{price_col}{DISCOUNT_COL_SUFFIX}"


def price_columns(df: pd.DataFrame) -> list[str]:
    return [col for col in df.columns if str(col).endswith(PRICE_COL_SUFFIX)]


def discount_columns(df: pd.DataFrame) -> list[str]:
    return [col for col in df.columns if str(col).endswith(DISCOUNT_COL_SUFFIX)]


def parse_price(value: Any) -> float | None:
    text = normalize_text(value)
    if not text:
        return None
    match = re.search(r"\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)", text)
    if not match:
        match = re.search(r"\b([0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2}))\b", text)
    if not match:
        return None
    try:
        return round(float(match.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def compact_for_match(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def title_words(value: Any) -> set[str]:
    ignored = {
        "the",
        "and",
        "for",
        "with",
        "pack",
        "multi",
        "regular",
        "underwear",
        "panty",
        "panties",
    }
    return {word for word in compact_for_match(value).split() if len(word) > 2 and word not in ignored}


def titles_match(left: str, right: str) -> bool:
    left_compact = compact_for_match(left)
    right_compact = compact_for_match(right)
    if not left_compact or not right_compact:
        return False
    if left_compact in right_compact or right_compact in left_compact:
        return True
    left_words = title_words(left_compact)
    right_words = title_words(right_compact)
    if not left_words or not right_words:
        return False
    overlap = left_words & right_words
    required = min(4, max(2, min(len(left_words), len(right_words)) // 2))
    return len(overlap) >= required


def load_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return normalize_table(pd.read_excel(path))
    return normalize_table(pd.read_csv(path))


def normalize_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    lower_columns = {str(col).strip().lower(): col for col in df.columns}
    if "url" in df.columns and "link" not in df.columns:
        df = df.rename(columns={"url": "link"})
    elif "url" in df.columns and "link" in df.columns:
        df["link"] = df["link"].fillna("")
        df.loc[df["link"].astype(str).str.strip() == "", "link"] = df["url"]
        df = df.drop(columns=["url"])

    if "last_seen" in df.columns and LAST_RUN_COLUMN not in df.columns:
        df = df.rename(columns={"last_seen": LAST_RUN_COLUMN})

    if CATEGORY_COLUMN not in df.columns:
        for alias in ("bra/bottoms", "bras/bottoms", "bra_or_bottoms", "category"):
            source = lower_columns.get(alias)
            if source is not None:
                df = df.rename(columns={source: CATEGORY_COLUMN})
                break

    if PDP_TITLE_COLUMN not in df.columns:
        for alias in ("product_title", "detected_title", "pdp title", "pdp_title"):
            source = lower_columns.get(alias)
            if source is not None and source != "title":
                df = df.rename(columns={source: PDP_TITLE_COLUMN})
                break

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
    for col in ("brand", CATEGORY_COLUMN, "color", "size", "title", PDP_TITLE_COLUMN):
        df[col] = df[col].fillna("").astype(str).str.strip()

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

    for col in price_columns(df) + discount_columns(df):
        df[col] = df[col].astype("object")

    ordered = BASE_COLUMNS + price_columns(df) + discount_columns(df) + [LAST_RUN_COLUMN]
    return df[ordered]


def products_from_table(
    df: pd.DataFrame,
    retailers: list[str],
    price_col: str,
    only_missing: bool,
    max_rows: int,
) -> list[Product]:
    normalized = normalize_table(df).reset_index(drop=True)
    wanted_order: list[str] = []
    for retailer in retailers:
        retailer_key = canonical_retailer(retailer)
        if retailer_key and retailer_key not in wanted_order:
            wanted_order.append(retailer_key)
    normalized["_retailer_key_for_run"] = normalized["retailer"].map(canonical_retailer)
    rows = normalized[normalized["_retailer_key_for_run"].isin(wanted_order)]
    if only_missing and price_col in rows.columns:
        rows = rows[
            rows[price_col].isna()
            | (rows[price_col].astype(str).str.strip() == "")
        ]
    products: list[Product] = []
    for retailer_key in wanted_order:
        retailer_rows = rows[rows["_retailer_key_for_run"] == retailer_key]
        for idx, row in retailer_rows.iterrows():
            link = str(row.get("link", "") or "").strip()
            if not link:
                continue
            products.append(
                Product(
                    row_number=int(idx) + 1,
                    retailer=str(row.get("retailer", "") or "").strip(),
                    brand=str(row.get("brand", "") or "").strip(),
                    bras_bottoms=str(row.get(CATEGORY_COLUMN, "") or "").strip(),
                    color=str(row.get("color", "") or "").strip(),
                    size=str(row.get("size", "") or "").strip(),
                    title=str(row.get("title", "") or "").strip(),
                    url=link,
                )
            )
    if max_rows and max_rows > 0:
        products = products[:max_rows]
    return products


def selected_tcin_from_url(url: str) -> str:
    return (parse_qs(urlparse(url).query).get("preselect") or [""])[0]


def parent_product_id_from_url(url: str) -> str:
    match = re.search(r"/A-(\d+)", url)
    return match.group(1) if match else ""


def detect_block(page: Any) -> bool:
    try:
        body = normalize_text(page.locator("body").inner_text(timeout=5000))
    except Exception:
        body = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    return bool(re.search(r"captcha|access denied|forbidden|verify you are human|blocked|robot check", f"{title} {body}", re.I))


def page_unavailable(page: Any) -> bool:
    try:
        body = normalize_text(page.locator("body").inner_text(timeout=5000))
    except Exception:
        body = ""
    return "this page is currently unavailable" in body.lower()


def title_from_page(page: Any) -> str:
    for selector in ("h1[data-test='product-title']", "h1", "[data-test='product-title']"):
        try:
            text = normalize_text(page.locator(selector).first.inner_text(timeout=2500))
            if text:
                return text
        except Exception:
            pass
    try:
        return normalize_text(page.title())
    except Exception:
        return ""


def dom_price_candidates(page: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    selectors = [
        '[class*="currentPriceFontSize"]',
        '[data-test="product-price"]',
        '[data-test*="price"]',
    ]
    seen = set()
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 30)
            for index in range(count):
                raw = normalize_text(locator.nth(index).inner_text(timeout=1500))
                price = parse_price(raw)
                key = (selector, raw, price)
                if key in seen:
                    continue
                seen.add(key)
                if raw or price is not None:
                    candidates.append(
                        {
                            "selector": selector,
                            "index": index,
                            "text": raw,
                            "price": price,
                        }
                    )
        except Exception:
            pass
    return candidates


def accepted_target_dom_price(candidates: list[dict[str, Any]]) -> tuple[float | None, str, str]:
    current_hits = [
        candidate
        for candidate in candidates
        if candidate.get("selector") == '[class*="currentPriceFontSize"]'
        and candidate.get("price") is not None
    ]
    if len(current_hits) == 1:
        return current_hits[0]["price"], "target_dom_currentPriceFontSize", "single_currentPriceFontSize_candidate"
    if len(current_hits) > 1:
        prices = {candidate["price"] for candidate in current_hits}
        if len(prices) == 1:
            return current_hits[0]["price"], "target_dom_currentPriceFontSize", "multiple_currentPriceFontSize_candidates_same_price"
        return None, "target_dom", "multiple_currentPriceFontSize_candidates_conflict"

    product_hits = [
        candidate
        for candidate in candidates
        if candidate.get("selector") == '[data-test="product-price"]'
        and candidate.get("price") is not None
    ]
    if len(product_hits) == 1:
        return product_hits[0]["price"], "target_dom_product_price", "single_product_price_candidate"
    if len(product_hits) > 1:
        prices = {candidate["price"] for candidate in product_hits}
        if len(prices) == 1:
            return product_hits[0]["price"], "target_dom_product_price", "multiple_product_price_candidates_same_price"
        return None, "target_dom", "multiple_product_price_candidates_conflict"

    return None, "target_dom", "no_visible_dom_price_candidate"


def scrape_target_dom_only(
    page: Any,
    product: Product,
    screenshot_dir: Path,
    html_dir: Path,
    wait_ms: int,
) -> ScrapeResult:
    try:
        page.goto(product.url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(wait_ms)
    except PlaywrightTimeoutError as exc:
        return ScrapeResult(product=product, status="error", error=f"navigation_timeout: {exc}", source="target_dom")
    except Exception as exc:
        return ScrapeResult(product=product, status="error", error=f"navigation_error:{type(exc).__name__}: {exc}", source="target_dom")

    block_detected = detect_block(page)
    unavailable = page_unavailable(page)
    title = title_from_page(page)
    candidates = dom_price_candidates(page)
    price, source, reason = accepted_target_dom_price(candidates)

    raw_file_stem = f"row_{product.row_number}_{canonical_retailer(product.retailer).replace(' ', '_')}"
    try:
        screenshot_path = screenshot_dir / f"{raw_file_stem}.png"
        html_path = html_dir / f"{raw_file_stem}.html"
        page.screenshot(path=str(screenshot_path), full_page=True)
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass

    if block_detected:
        status = "blocked"
        error = "block_detected"
    elif unavailable:
        status = "page_unavailable"
        error = "page_currently_unavailable"
    elif price is not None:
        status = "price_captured"
        error = ""
    else:
        status = "loaded_no_price"
        error = reason

    raw = {
        "reason": reason,
        "selected_tcin": selected_tcin_from_url(product.url),
        "parent_product_id": parent_product_id_from_url(product.url),
        "candidates": candidates,
    }
    return ScrapeResult(
        product=product,
        price=price,
        title=title,
        status=status,
        source=source,
        error=error,
        raw_price_text=json.dumps(raw, ensure_ascii=True)[:500],
    )


def extract_first_product_price(text: str) -> tuple[float | None, str]:
    normalized = normalize_text(text)
    if not normalized:
        return None, ""
    for match in re.finditer(r"\$\s*[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?", normalized):
        context = normalized[max(0, match.start() - 100): match.end() + 140]
        if NON_PRODUCT_PRICE_CONTEXT_RE.search(context):
            continue
        price = parse_price(match.group(0))
        if price is not None and 0 < price < 500:
            return price, context
    return None, ""


def iter_json_nodes(value: Any) -> Any:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_nodes(child)


def jsonld_product_price(page_html: str, expected_titles: list[str]) -> tuple[float | None, str, str]:
    scripts = re.findall(
        r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>([\s\S]*?)</script>",
        page_html,
        flags=re.I,
    )
    product_hits: list[tuple[float, str, str]] = []
    for script_index, script in enumerate(scripts):
        try:
            data = json.loads(html.unescape(script).strip())
        except Exception:
            continue
        for node_index, node in enumerate(iter_json_nodes(data)):
            node_type = node.get("@type") or node.get("type") or ""
            node_types = node_type if isinstance(node_type, list) else [node_type]
            if not any(str(item).lower() == "product" for item in node_types):
                continue
            node_name = normalize_text(node.get("name") or node.get("title") or "")
            if expected_titles and node_name and not any(titles_match(node_name, title) for title in expected_titles if title):
                continue
            offers = node.get("offers") or node.get("offer") or {}
            offer_items = offers if isinstance(offers, list) else [offers]
            for offer_index, offer in enumerate(offer_items):
                if not isinstance(offer, dict):
                    continue
                for key in ("price", "lowPrice", "highPrice", "salePrice"):
                    price = parse_price(offer.get(key))
                    if price is not None:
                        path = f"json_ld[{script_index}].product[{node_index}].offers[{offer_index}].{key}"
                        product_hits.append((price, path, node_name))
                spec = offer.get("priceSpecification")
                spec_items = spec if isinstance(spec, list) else [spec]
                for spec_index, spec_item in enumerate(spec_items):
                    if not isinstance(spec_item, dict):
                        continue
                    price = parse_price(spec_item.get("price"))
                    if price is not None:
                        path = f"json_ld[{script_index}].product[{node_index}].offers[{offer_index}].priceSpecification[{spec_index}].price"
                        product_hits.append((price, path, node_name))
    if len(product_hits) == 1:
        price, path, name = product_hits[0]
        return price, path, name
    if len(product_hits) > 1:
        prices = {item[0] for item in product_hits}
        if len(prices) == 1:
            price, path, name = product_hits[0]
            return price, f"{path}; multiple matching JSON-LD prices agree", name
    return None, "", ""


def primary_h1_region_price(page: Any) -> tuple[float | None, str, str]:
    try:
        region = page.evaluate(
            """() => {
                const h1 = document.querySelector('h1');
                if (!h1) return null;
                let node = h1;
                const cutoffTerms = [
                    'you may also like',
                    'similar items',
                    'customers also viewed',
                    'sponsored',
                    'recently viewed',
                    'more like this',
                    'complete the look',
                    'shop similar',
                    'people also viewed',
                    'recommended',
                ];
                for (let depth = 0; depth < 6 && node; depth += 1, node = node.parentElement) {
                    const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                    let scopedText = text;
                    const lower = text.toLowerCase();
                    const cutIndexes = cutoffTerms
                        .map(term => lower.indexOf(term))
                        .filter(index => index > 100);
                    if (cutIndexes.length) {
                        scopedText = text.slice(0, Math.min(...cutIndexes));
                    }
                    if (scopedText.includes('$') && scopedText.length < 3500) {
                        return { depth, text: scopedText };
                    }
                }
                return null;
            }"""
        )
    except Exception:
        region = None
    if not isinstance(region, dict):
        return None, "", ""
    text = normalize_text(region.get("text") or "")
    price, context = extract_first_product_price(text)
    if price is not None:
        return price, f"h1_ancestor_depth_{region.get('depth')}", context
    return None, "", ""


def title_scoped_body_price(page: Any, title: str) -> tuple[float | None, str, str]:
    if not title:
        return None, "", ""
    try:
        body = normalize_text(page.locator("body").inner_text(timeout=5000))
    except Exception:
        return None, "", ""
    body_compact = compact_for_match(body)
    title_compact = compact_for_match(title)
    start = body_compact.find(title_compact)
    if start < 0:
        return None, "", ""

    # Map the compact hit back approximately by using a direct normalized text search first.
    direct_start = body.lower().find(title.lower())
    text_start = direct_start if direct_start >= 0 else max(0, start - 300)
    segment = body[text_start:text_start + 6000]
    lower_segment = segment.lower()
    cut_positions = [lower_segment.find(term) for term in RECOMMENDATION_CUTOFF_TERMS if lower_segment.find(term) > 100]
    if cut_positions:
        segment = segment[:min(cut_positions)]
    price, context = extract_first_product_price(segment)
    if price is not None:
        return price, "title_scoped_body_text", context
    return None, "", ""


def save_generic_artifacts(page: Any, product: Product, screenshot_dir: Path, html_dir: Path, suffix: str) -> None:
    raw_file_stem = f"row_{product.row_number}_{canonical_retailer(product.retailer).replace(' ', '_')}_{suffix}"
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        html_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_dir / f"{raw_file_stem}.png"), full_page=True)
        (html_dir / f"{raw_file_stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def scrape_jcpenney(page: Any, product: Product, screenshot_dir: Path, html_dir: Path, wait_ms: int) -> ScrapeResult:
    try:
        page.goto(product.url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(wait_ms)
    except PlaywrightTimeoutError as exc:
        return ScrapeResult(product=product, status="error", error=f"navigation_timeout: {exc}", source="jcpenney_primary_product")
    except Exception as exc:
        return ScrapeResult(product=product, status="error", error=f"navigation_error:{type(exc).__name__}: {exc}", source="jcpenney_primary_product")

    title = title_from_page(page)
    expected_titles = [title, product.title]
    try:
        page_html = page.content()
    except Exception:
        page_html = ""

    price, source, raw = jsonld_product_price(page_html, expected_titles)
    if price is None:
        price, source, raw = title_scoped_body_price(page, title)
    if price is None:
        price, source, raw = primary_h1_region_price(page)

    if price is None:
        save_generic_artifacts(page, product, screenshot_dir, html_dir, "jcpenney_no_price")
        return ScrapeResult(
            product=product,
            title=title,
            status="loaded_no_price",
            source="jcpenney_primary_product",
            error="No primary-product price found; skipped broad page-price fallback",
            raw_price_text="",
        )

    return ScrapeResult(
        product=product,
        price=price,
        title=title,
        status="price_captured",
        source=f"jcpenney_{source}",
        error="",
        raw_price_text=raw,
    )


def generic_visible_price_from_page(page: Any) -> tuple[float | None, str, str]:
    selectors = [
        '[data-test="product-price"]',
        '[data-test="product-price-container"]',
        '[data-testid*="price" i]',
        '[class*="price" i]',
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
    ]
    for selector in selectors:
        try:
            if selector.startswith("meta"):
                raw = page.locator(selector).first.get_attribute("content", timeout=1000) or ""
            else:
                raw = page.locator(selector).first.inner_text(timeout=2000)
            price = parse_price(raw)
            if price is not None:
                return price, selector, normalize_text(raw)
        except Exception:
            continue
    try:
        body = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body = ""
    match = re.search(r"\$[0-9]{1,4}(?:,[0-9]{3})*(?:\.[0-9]{2})?", body)
    if match:
        return parse_price(match.group(0)), "body_text", match.group(0)
    return None, "", ""


def scrape_playwright_generic(page: Any, product: Product, wait_ms: int) -> ScrapeResult:
    try:
        page.goto(product.url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(wait_ms)
    except PlaywrightTimeoutError as exc:
        return ScrapeResult(product=product, status="error", error=f"navigation_timeout: {exc}", source="playwright_generic")
    except Exception as exc:
        return ScrapeResult(product=product, status="error", error=f"navigation_error:{type(exc).__name__}: {exc}", source="playwright_generic")

    title = title_from_page(page)
    price, source, raw = generic_visible_price_from_page(page)
    return ScrapeResult(
        product=product,
        price=price,
        title=title,
        status="price_captured" if price is not None else "loaded_no_price",
        source=f"playwright_{source}" if source else "playwright_generic",
        error="" if price is not None else "Price not found",
        raw_price_text=raw,
    )


def extract_discount_from_json(data: dict[str, Any], price: float | None) -> str:
    for key in ("discount_percentage", "discount_percent", "percent_off", "savings", "discount"):
        value = data.get(key)
        if value:
            text = normalize_text(value)
            percent = re.search(r"([0-9]{1,3}(?:\.[0-9]+)?)\s*%?", text)
            if percent:
                return f"{percent.group(1).rstrip('0').rstrip('.')}%"
            return text[:60]
    for old_key in ("regular_price", "list_price", "was_price", "strike_price"):
        old_price = parse_price(data.get(old_key))
        if price is not None and old_price and old_price > price:
            return f"{((old_price - price) / old_price * 100):.1f}%".replace(".0%", "%")
    return ""


def scrape_walmart_api(product: Product, api_key: str) -> ScrapeResult:
    if requests is None:
        return ScrapeResult(product=product, status="error", source="scrapingdog_walmart", error="Missing requests package; run Install_Local_Runner_Dependencies.bat")
    if not api_key:
        return ScrapeResult(product=product, status="error", source="scrapingdog_walmart", error="Missing ScrapingDog API key")
    response = requests.get(WALMART_PRODUCT_API, params={"api_key": api_key, "url": product.url}, timeout=90)
    if response.status_code != 200:
        return ScrapeResult(product=product, status="error", source="scrapingdog_walmart", error=f"HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    product_data = data.get("product_results") or {}
    title = normalize_text(product_data.get("title") or "")
    raw = ""
    price = None
    price_map = product_data.get("price_map")
    if isinstance(price_map, list) and price_map:
        raw = str(price_map[0])
        price = parse_price(raw)
    if price is None and isinstance(product_data.get("offers"), list) and product_data["offers"]:
        raw = str((product_data["offers"][0] or {}).get("price") or "")
        price = parse_price(raw)
    if price is None:
        raw = str(product_data.get("price") or "")
        price = parse_price(raw)
    return ScrapeResult(
        product=product,
        price=price,
        title=title,
        status="price_captured" if price is not None else "loaded_no_price",
        source="scrapingdog_walmart",
        error="" if price is not None else "Price not found in Walmart API response",
        raw_price_text=raw,
        discount_reported=extract_discount_from_json(product_data, price),
    )


def scrape_amazon_api(product: Product, api_key: str) -> ScrapeResult:
    if requests is None:
        return ScrapeResult(product=product, status="error", source="scrapingdog_amazon_product", error="Missing requests package; run Install_Local_Runner_Dependencies.bat")
    if not api_key:
        return ScrapeResult(product=product, status="error", source="scrapingdog_amazon_product", error="Missing ScrapingDog API key")
    response = requests.get(AMAZON_PRODUCT_API, params={"api_key": api_key, "url": product.url, "country": "us"}, timeout=120)
    if response.status_code != 200:
        return ScrapeResult(product=product, status="error", source="scrapingdog_amazon_product", error=f"HTTP {response.status_code}: {response.text[:200]}")
    data = response.json()
    title = normalize_text(data.get("title") or "")
    price = None
    raw = ""
    for key in ("price", "current_price", "buybox_price", "sale_price", "deal_price", "our_price"):
        raw = str(data.get(key) or "")
        price = parse_price(raw)
        if price is not None:
            break
    return ScrapeResult(
        product=product,
        price=price,
        title=title,
        status="price_captured" if price is not None else "loaded_no_price",
        source="scrapingdog_amazon_product",
        error="" if price is not None else "Price not found in Amazon API response",
        raw_price_text=raw,
        discount_reported=extract_discount_from_json(data, price),
    )


def launch_browser(playwright: Any, headless: bool) -> Any:
    try:
        return playwright.chromium.launch(channel="chrome", headless=headless)
    except Exception as exc:
        log(f"Chrome launch failed ({type(exc).__name__}); falling back to bundled Chromium.")
        return playwright.chromium.launch(headless=headless)


def merge_result(table: pd.DataFrame, result: ScrapeResult, price_col: str, run_timestamp: str) -> pd.DataFrame:
    table = normalize_table(table)
    discount_col = discount_col_for_price_col(price_col)
    if price_col not in table.columns:
        table[price_col] = ""
    if discount_col not in table.columns:
        table[discount_col] = ""
    table[price_col] = table[price_col].astype("object")
    table[discount_col] = table[discount_col].astype("object")

    idx = result.product.row_number - 1
    if not (0 <= idx < len(table)):
        idx = len(table)
        table.loc[idx, BASE_COLUMNS] = [
            result.product.retailer,
            result.product.brand,
            result.product.bras_bottoms,
            result.product.color,
            result.product.size,
            result.product.title,
            "",
            result.product.url,
        ]

    if result.product.title and not str(table.at[idx, "title"] or "").strip():
        table.at[idx, "title"] = result.product.title
    if result.title:
        table.at[idx, PDP_TITLE_COLUMN] = result.title
    table.at[idx, price_col] = "" if result.price is None else result.price
    table.at[idx, discount_col] = result.discount_reported or ""
    table.at[idx, LAST_RUN_COLUMN] = run_timestamp
    return normalize_table(table)


def write_outputs(table: pd.DataFrame, results: list[ScrapeResult], output_dir: Path, stem: str) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    full_csv = output_dir / f"{stem}_full_master.csv"
    full_xlsx = output_dir / f"{stem}_full_master.xlsx"
    results_csv = output_dir / f"{stem}_run_results.csv"

    normalize_table(table).to_csv(full_csv, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(full_xlsx, engine="openpyxl") as writer:
        normalize_table(table).to_excel(writer, index=False, sheet_name="Price Master")

    fields = [
        "row_number",
        "retailer",
        "brand",
        "bras_bottoms",
        "color",
        "size",
        "title",
        "pdp_title",
        "link",
        "price",
        "discount_reported",
        "status",
        "source",
        "error",
        "raw_price_text",
    ]
    with results_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "row_number": result.product.row_number,
                    "retailer": result.product.retailer,
                    "brand": result.product.brand,
                    "bras_bottoms": result.product.bras_bottoms,
                    "color": result.product.color,
                    "size": result.product.size,
                    "title": result.product.title,
                    "pdp_title": result.title,
                    "link": result.product.url,
                    "price": result.price if result.price is not None else "",
                    "discount_reported": result.discount_reported,
                    "status": result.status,
                    "source": result.source,
                    "error": result.error,
                    "raw_price_text": result.raw_price_text,
                }
            )
    return full_csv, full_xlsx, results_csv


def write_persistent_master(table: pd.DataFrame, master_dir: Path, master_name: str) -> tuple[Path, Path]:
    master_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", master_name).strip("._") or "hanes_price_master"
    if safe_name.lower().endswith(".xlsx") or safe_name.lower().endswith(".csv"):
        safe_name = Path(safe_name).stem
    master_csv = master_dir / f"{safe_name}.csv"
    master_xlsx = master_dir / f"{safe_name}.xlsx"

    normalized = normalize_table(table)
    normalized.to_csv(master_csv, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(master_xlsx, engine="openpyxl") as writer:
        normalized.to_excel(writer, index=False, sheet_name="Price Master")
    return master_csv, master_xlsx


def browser_closed_error(result: ScrapeResult) -> bool:
    text = f"{result.status} {result.source} {result.error}".lower()
    return (
        "targetclosederror" in text
        or "page, context or browser has been closed" in text
        or "target page, context or browser has been closed" in text
    )


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone()


def load_cooldown_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_cooldown_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def active_cooldown(state: dict[str, Any], retailer_key: str, now: datetime) -> tuple[str, datetime] | None:
    record = state.get(retailer_key)
    if not isinstance(record, dict):
        return None
    active: list[tuple[str, datetime]] = []
    for reason, field in (("hard block", "hard_block_until"), ("soft cooldown", "soft_cooldown_until")):
        until = parse_iso_datetime(record.get(field))
        if until and until > now:
            active.append((reason, until))
    if not active:
        return None
    return max(active, key=lambda item: item[1])


def mark_target_hard_block(
    state: dict[str, Any],
    now: datetime,
    first_cooldown_hours: float,
    repeat_cooldown_hours: float,
    repeat_window_hours: float,
) -> datetime:
    record = state.setdefault("target", {})
    if not isinstance(record, dict):
        record = {}
        state["target"] = record
    last_block = parse_iso_datetime(record.get("last_hard_block"))
    previous_count = int(record.get("hard_block_count") or 0)
    if last_block and now - last_block <= timedelta(hours=repeat_window_hours):
        hard_block_count = previous_count + 1
    else:
        hard_block_count = 1
    cooldown_hours = repeat_cooldown_hours if hard_block_count >= 2 else first_cooldown_hours
    until = now + timedelta(hours=cooldown_hours)
    record["last_hard_block"] = now.isoformat(timespec="seconds")
    record["hard_block_count"] = hard_block_count
    record["hard_block_until"] = until.isoformat(timespec="seconds")
    return until


def mark_target_soft_cooldown(
    state: dict[str, Any],
    now: datetime,
    min_minutes: float,
    max_minutes: float,
) -> datetime:
    record = state.setdefault("target", {})
    if not isinstance(record, dict):
        record = {}
        state["target"] = record
    cooldown_minutes = random.uniform(min_minutes, max_minutes)
    until = now + timedelta(minutes=cooldown_minutes)
    record["last_soft_cooldown"] = now.isoformat(timespec="seconds")
    record["soft_cooldown_until"] = until.isoformat(timespec="seconds")
    return until


def parse_retailers(raw: str, include_api: bool) -> list[str]:
    if raw.strip():
        retailers = [item.strip() for item in re.split(r"[,;]", raw) if item.strip()]
    else:
        retailers = list(DEFAULT_LOCAL_RETAILERS)
    if include_api:
        retailers.extend(["Walmart", "Amazon"])
    return retailers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hanes local price runner for Streamlit upload.")
    parser.add_argument("--input", default="input/retail_wip_links_import.csv", help="CSV/XLSX master table to run from.")
    parser.add_argument("--output-dir", default="local_outputs", help="Folder for generated CSV/XLSX outputs.")
    parser.add_argument("--retailers", default="", help="Comma-separated retailers. Default: Target, Dollar General, TJ Maxx, JCPenney.")
    parser.add_argument("--include-api-retailers", action="store_true", help="Also run Walmart/Amazon locally through ScrapingDog APIs.")
    parser.add_argument("--scrapingdog-api-key", default=os.getenv("SCRAPINGDOG_API_KEY", ""), help="ScrapingDog key for Walmart/Amazon if enabled.")
    parser.add_argument("--run-date", default=datetime.now().date().isoformat(), help="Date for the output price column, YYYY-MM-DD.")
    parser.add_argument("--max-rows", type=int, default=0, help="Max rows to run; 0 means all matching rows.")
    parser.add_argument("--only-missing", action=argparse.BooleanOptionalAction, default=True, help="Only run rows blank in today's price column.")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False, help="Run browser headless. Default is headed for local reliability.")
    parser.add_argument("--delay-min", type=float, default=5.0, help="Minimum random delay between browser rows.")
    parser.add_argument("--delay-max", type=float, default=9.0, help="Maximum random delay between browser rows.")
    parser.add_argument("--target-wait-ms", type=int, default=7000, help="Extra Target DOM wait after page load.")
    parser.add_argument("--generic-wait-ms", type=int, default=3500, help="Extra generic retailer DOM wait after page load.")
    parser.add_argument("--save-every", type=int, default=25, help="Checkpoint every N completed rows.")
    parser.add_argument("--persistent-master-dir", default="", help="Optional folder for fixed master CSV/XLSX updated during each run.")
    parser.add_argument("--persistent-master-name", default="hanes_price_master", help="Base filename for the fixed master CSV/XLSX.")
    parser.add_argument("--browser-restart-every", type=int, default=40, help="Restart browser after N Playwright rows; 0 disables.")
    parser.add_argument("--browser-rest-min", type=float, default=120.0, help="Minimum rest seconds during scheduled browser restarts.")
    parser.add_argument("--browser-rest-max", type=float, default=240.0, help="Maximum rest seconds during scheduled browser restarts.")
    parser.add_argument("--long-rest-every", type=int, default=100, help="Take a longer rest after N Playwright rows; 0 disables.")
    parser.add_argument("--long-rest-min", type=float, default=480.0, help="Minimum seconds for long rest.")
    parser.add_argument("--long-rest-max", type=float, default=720.0, help="Maximum seconds for long rest.")
    parser.add_argument("--consecutive-miss-restart", type=int, default=8, help="Restart/rest after N consecutive loaded_no_price/error browser rows; 0 disables.")
    parser.add_argument("--stop-retailer-on-block", action=argparse.BooleanOptionalAction, default=True, help="If a browser retailer shows a block page, save and skip the rest of that retailer for this run.")
    parser.add_argument("--target-delay-min", type=float, default=8.0, help="Minimum random delay before Target rows.")
    parser.add_argument("--target-delay-max", type=float, default=20.0, help="Maximum random delay before Target rows.")
    parser.add_argument("--target-restart-every", type=int, default=25, help="Restart/rest browser after N Target rows; 0 disables.")
    parser.add_argument("--target-rest-min", type=float, default=180.0, help="Minimum rest seconds during Target scheduled restarts.")
    parser.add_argument("--target-rest-max", type=float, default=360.0, help="Maximum rest seconds during Target scheduled restarts.")
    parser.add_argument("--target-long-rest-every", type=int, default=75, help="Take a longer rest after N Target rows; 0 disables.")
    parser.add_argument("--target-long-rest-min", type=float, default=600.0, help="Minimum seconds for Target long rest.")
    parser.add_argument("--target-long-rest-max", type=float, default=1200.0, help="Maximum seconds for Target long rest.")
    parser.add_argument("--target-consecutive-miss-restart", type=int, default=6, help="Restart/rest after N consecutive Target loaded_no_price/error rows; 0 disables.")
    parser.add_argument("--target-soft-rest-min", type=float, default=300.0, help="Minimum rest seconds for first Target soft-miss restart.")
    parser.add_argument("--target-soft-rest-max", type=float, default=600.0, help="Maximum rest seconds for first Target soft-miss restart.")
    parser.add_argument("--target-soft-cooldown-minutes", type=float, default=60.0, help="Minimum Target cooldown minutes after repeated soft failures.")
    parser.add_argument("--target-soft-cooldown-max-minutes", type=float, default=90.0, help="Maximum Target cooldown minutes after repeated soft failures.")
    parser.add_argument("--target-hard-cooldown-hours", type=float, default=24.0, help="Target cooldown hours after a hard block.")
    parser.add_argument("--target-repeat-hard-cooldown-hours", type=float, default=48.0, help="Target cooldown hours after repeated hard blocks.")
    parser.add_argument("--target-repeat-hard-window-hours", type=float, default=72.0, help="Window for treating Target hard blocks as repeated.")
    parser.add_argument("--wait-for-target-soft-cooldown", action=argparse.BooleanOptionalAction, default=True, help="After other retailers finish, wait for a Target soft cooldown and try deferred Target rows once.")
    parser.add_argument("--target-max-rows-per-run", type=int, default=50, help="Max Target rows to attempt in one run; 0 disables.")
    parser.add_argument("--other-browser-max-rows-per-retailer", type=int, default=150, help="Max non-Target browser rows per retailer in one run; 0 disables.")
    parser.add_argument("--cooldown-state-file", default="state/retailer_cooldowns.json", help="JSON file used to remember Target cooldowns across runs.")
    parser.add_argument("--dry-run", action="store_true", help="Validate input and print selected rows without scraping.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_dir = Path(__file__).resolve().parent
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = base_dir / input_path
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = base_dir / output_dir
    persistent_master_dir = Path(args.persistent_master_dir) if args.persistent_master_dir else None
    if persistent_master_dir and not persistent_master_dir.is_absolute():
        persistent_master_dir = base_dir / persistent_master_dir
    cooldown_state_path = Path(args.cooldown_state_file)
    if not cooldown_state_path.is_absolute():
        cooldown_state_path = base_dir / cooldown_state_path

    run_timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    price_col = price_col_for_date(args.run_date)
    retailers = parse_retailers(args.retailers, args.include_api_retailers)

    log("=" * 78)
    log("Hanes Local Price Runner")
    log("=" * 78)
    log(f"Input:       {input_path}")
    log(f"Output dir:  {output_dir}")
    log(f"Run date:    {args.run_date} -> {price_col}")
    log(f"Retailers:   {', '.join(retailers)}")
    log(f"Browser:     {'headless' if args.headless else 'headed'} Playwright")
    log(f"Only missing:{args.only_missing}")

    table = load_table(input_path)
    products = products_from_table(table, retailers, price_col, args.only_missing, args.max_rows)
    log(f"Loaded rows: {len(table):,}")
    log(f"Run rows:    {len(products):,}")

    if args.dry_run:
        log("")
        log("DRY RUN - no websites opened.")
        for product in products[:25]:
            log(f"  row {product.row_number}: {product.retailer} | {product.brand} | {product.size} | {product.url}")
        if len(products) > 25:
            log(f"  ... {len(products) - 25:,} more selected rows")
        return 0

    if not products:
        log("No rows matched; nothing to run.")
        return 0

    if args.delay_max < args.delay_min:
        raise ValueError("--delay-max must be at least --delay-min")
    if args.browser_rest_max < args.browser_rest_min:
        raise ValueError("--browser-rest-max must be at least --browser-rest-min")
    if args.long_rest_max < args.long_rest_min:
        raise ValueError("--long-rest-max must be at least --long-rest-min")
    if args.target_delay_max < args.target_delay_min:
        raise ValueError("--target-delay-max must be at least --target-delay-min")
    if args.target_rest_max < args.target_rest_min:
        raise ValueError("--target-rest-max must be at least --target-rest-min")
    if args.target_long_rest_max < args.target_long_rest_min:
        raise ValueError("--target-long-rest-max must be at least --target-long-rest-min")
    if args.target_soft_rest_max < args.target_soft_rest_min:
        raise ValueError("--target-soft-rest-max must be at least --target-soft-rest-min")
    if args.target_soft_cooldown_max_minutes < args.target_soft_cooldown_minutes:
        raise ValueError("--target-soft-cooldown-max-minutes must be at least --target-soft-cooldown-minutes")

    needs_playwright = any(canonical_retailer(p.retailer) in PLAYWRIGHT_RETAILERS for p in products)
    needs_api = any(canonical_retailer(p.retailer) in API_RETAILERS for p in products)
    if needs_playwright and sync_playwright is None:
        raise RuntimeError("Playwright is not installed. Run Install_Local_Runner_Dependencies.bat first.")
    if needs_api and requests is None:
        raise RuntimeError("Requests is not installed. Run Install_Local_Runner_Dependencies.bat first.")
    if needs_api and not args.scrapingdog_api_key:
        raise RuntimeError("Walmart/Amazon selected but no ScrapingDog API key was provided.")

    artifacts_dir = output_dir / f"artifacts_{run_stamp}"
    screenshot_dir = artifacts_dir / "screenshots"
    html_dir = artifacts_dir / "raw_html"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    html_dir.mkdir(parents=True, exist_ok=True)

    results: list[ScrapeResult] = []
    completed = 0
    browser = None
    context = None
    page = None

    if needs_playwright:
        playwright = sync_playwright().start()
    else:
        playwright = None

    def close_browser_session() -> None:
        nonlocal browser, context, page
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        context = None
        browser = None
        page = None

    def open_browser_session() -> None:
        nonlocal browser, context, page
        if playwright is None:
            return
        browser = launch_browser(playwright, headless=args.headless)
        context = browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US", timezone_id="America/New_York")
        page = context.new_page()

    def rest_and_restart_browser(reason: str, min_seconds: float, max_seconds: float) -> None:
        if not needs_playwright:
            return
        log(f"  Browser rest/restart: {reason}")
        close_browser_session()
        rest_seconds = random.uniform(min_seconds, max_seconds)
        log(f"  Resting {rest_seconds:.1f}s before reopening browser")
        time.sleep(rest_seconds)
        open_browser_session()

    if needs_playwright:
        open_browser_session()

    cooldown_state = load_cooldown_state(cooldown_state_path)
    browser_rows_total = 0
    retailer_rows_attempted: dict[str, int] = {}
    retailer_rows_since_restart: dict[str, int] = {}
    retailer_rows_since_long_rest: dict[str, int] = {}
    consecutive_soft_misses: dict[str, int] = {}
    consecutive_soft_miss_products: dict[str, list[Product]] = {}
    target_soft_restarts = 0
    target_soft_pause_until: datetime | None = None
    deferred_target_products: list[Product] = []
    deferred_target_row_numbers: set[int] = set()
    paused_retailers: dict[str, str] = {}

    def defer_target_product(product: Product) -> None:
        if product.row_number not in deferred_target_row_numbers:
            deferred_target_products.append(product)
            deferred_target_row_numbers.add(product.row_number)

    try:
        for run_index, product in enumerate(products, start=1):
            retailer_key = canonical_retailer(product.retailer)
            uses_browser = retailer_key in PLAYWRIGHT_RETAILERS
            log("")
            log(f"[{run_index}/{len(products)}] row {product.row_number} | {product.retailer} | {product.brand} | size={product.size or '-'}")
            log(f"  {product.url}")

            if uses_browser and retailer_key in paused_retailers:
                if retailer_key == "target" and target_soft_pause_until:
                    defer_target_product(product)
                    log(f"  DEFER | {paused_retailers[retailer_key]}; will try Target again after other retailers.")
                else:
                    log(f"  SKIP | {paused_retailers[retailer_key]}; leaving this row untouched.")
                continue

            if retailer_key == "target":
                cooldown = active_cooldown(cooldown_state, "target", datetime.now().astimezone())
                if cooldown:
                    reason, until = cooldown
                    paused_retailers[retailer_key] = f"Target {reason} active until {until.isoformat(timespec='seconds')}"
                    if reason == "soft cooldown":
                        target_soft_pause_until = until
                        defer_target_product(product)
                        log(f"  DEFER | {paused_retailers[retailer_key]}; will try Target again after other retailers.")
                    else:
                        log(f"  SKIP | {paused_retailers[retailer_key]}; leaving this row untouched.")
                    continue

            if uses_browser:
                max_rows = args.target_max_rows_per_run if retailer_key == "target" else args.other_browser_max_rows_per_retailer
                if max_rows > 0 and retailer_rows_attempted.get(retailer_key, 0) >= max_rows:
                    paused_retailers[retailer_key] = f"{product.retailer} per-run cap reached ({max_rows} rows)"
                    log(f"  SKIP | {paused_retailers[retailer_key]}; leaving this row untouched.")
                    continue

            restart_every = args.target_restart_every if retailer_key == "target" else args.browser_restart_every
            rest_min = args.target_rest_min if retailer_key == "target" else args.browser_rest_min
            rest_max = args.target_rest_max if retailer_key == "target" else args.browser_rest_max
            long_rest_every = args.target_long_rest_every if retailer_key == "target" else args.long_rest_every
            long_rest_min = args.target_long_rest_min if retailer_key == "target" else args.long_rest_min
            long_rest_max = args.target_long_rest_max if retailer_key == "target" else args.long_rest_max
            rows_since_restart = retailer_rows_since_restart.get(retailer_key, 0)
            rows_since_long_rest = retailer_rows_since_long_rest.get(retailer_key, 0)

            if uses_browser and long_rest_every > 0 and rows_since_long_rest >= long_rest_every:
                rest_and_restart_browser(f"{rows_since_long_rest} {product.retailer} rows since long rest", long_rest_min, long_rest_max)
                retailer_rows_since_restart[retailer_key] = 0
                retailer_rows_since_long_rest[retailer_key] = 0
                consecutive_soft_misses[retailer_key] = 0
            elif uses_browser and restart_every > 0 and rows_since_restart >= restart_every:
                rest_and_restart_browser(f"{rows_since_restart} {product.retailer} rows since browser restart", rest_min, rest_max)
                retailer_rows_since_restart[retailer_key] = 0
                consecutive_soft_misses[retailer_key] = 0

            if browser_rows_total > 0 and uses_browser:
                delay_min = args.target_delay_min if retailer_key == "target" else args.delay_min
                delay_max = args.target_delay_max if retailer_key == "target" else args.delay_max
                delay = random.uniform(delay_min, delay_max)
                log(f"  Delay {delay:.1f}s")
                time.sleep(delay)

            if uses_browser and page is None:
                open_browser_session()

            start = time.time()
            try:
                if retailer_key == "target":
                    result = scrape_target_dom_only(page, product, screenshot_dir, html_dir, args.target_wait_ms)
                elif retailer_key in JCPENNEY_RETAILERS:
                    result = scrape_jcpenney(page, product, screenshot_dir, html_dir, args.generic_wait_ms)
                elif retailer_key in PLAYWRIGHT_RETAILERS:
                    result = scrape_playwright_generic(page, product, args.generic_wait_ms)
                elif "walmart" in retailer_key:
                    result = scrape_walmart_api(product, args.scrapingdog_api_key)
                elif "amazon" in retailer_key:
                    result = scrape_amazon_api(product, args.scrapingdog_api_key)
                else:
                    result = ScrapeResult(product=product, status="unsupported", error=f"Unsupported retailer: {product.retailer}")
            except Exception as exc:
                result = ScrapeResult(product=product, status="error", source="runner_exception", error=f"{type(exc).__name__}: {exc}")

            elapsed = time.time() - start
            results.append(result)
            table = merge_result(table, result, price_col, run_timestamp)
            completed += 1
            if uses_browser:
                browser_rows_total += 1
                retailer_rows_attempted[retailer_key] = retailer_rows_attempted.get(retailer_key, 0) + 1
                retailer_rows_since_restart[retailer_key] = retailer_rows_since_restart.get(retailer_key, 0) + 1
                retailer_rows_since_long_rest[retailer_key] = retailer_rows_since_long_rest.get(retailer_key, 0) + 1

            if result.price is not None:
                log(f"  OK ${result.price:.2f} | {result.status} | {result.source} | {elapsed:.1f}s")
            else:
                log(f"  MISS | {result.status} | {result.source} | {result.error} | {elapsed:.1f}s")
            if result.title:
                log(f"  PDP title: {result.title[:100]}")

            if uses_browser and result.status == "blocked" and args.stop_retailer_on_block:
                checkpoint_stem = f"checkpoint_{run_stamp}_after_{completed:04d}_{retailer_key.replace(' ', '_')}_blocked"
                full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, checkpoint_stem)
                log(f"  Block detected. Saved immediate checkpoint: {full_csv.name}, {full_xlsx.name}, {results_csv.name}")
                if persistent_master_dir:
                    master_csv, master_xlsx = write_persistent_master(table, persistent_master_dir, args.persistent_master_name)
                    log(f"  Persistent master updated: {master_csv.name}, {master_xlsx.name}")
                if retailer_key == "target":
                    until = mark_target_hard_block(
                        cooldown_state,
                        datetime.now().astimezone(),
                        args.target_hard_cooldown_hours,
                        args.target_repeat_hard_cooldown_hours,
                        args.target_repeat_hard_window_hours,
                    )
                    save_cooldown_state(cooldown_state_path, cooldown_state)
                    paused_retailers[retailer_key] = f"Target hard block cooldown active until {until.isoformat(timespec='seconds')}"
                    log(f"  Target cooldown saved: {paused_retailers[retailer_key]}")
                else:
                    paused_retailers[retailer_key] = f"{product.retailer} was blocked earlier in this run"
                log(f"  Skipping remaining {product.retailer} rows for this run instead of retrying into the block.")
                close_browser_session()
                retailer_rows_since_restart[retailer_key] = 0
                consecutive_soft_misses[retailer_key] = 0
                continue

            if uses_browser and result.status in {"loaded_no_price", "error"}:
                consecutive_soft_misses[retailer_key] = consecutive_soft_misses.get(retailer_key, 0) + 1
                consecutive_soft_miss_products.setdefault(retailer_key, []).append(product)
            elif uses_browser:
                consecutive_soft_misses[retailer_key] = 0
                consecutive_soft_miss_products[retailer_key] = []

            if args.save_every > 0 and completed % args.save_every == 0:
                checkpoint_stem = f"checkpoint_{run_stamp}_after_{completed:04d}"
                full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, checkpoint_stem)
                log(f"  Checkpoint saved: {full_csv.name}, {full_xlsx.name}, {results_csv.name}")
                if persistent_master_dir:
                    master_csv, master_xlsx = write_persistent_master(table, persistent_master_dir, args.persistent_master_name)
                    log(f"  Persistent master updated: {master_csv.name}, {master_xlsx.name}")

            if uses_browser and browser_closed_error(result):
                checkpoint_stem = f"checkpoint_{run_stamp}_after_{completed:04d}_browser_closed"
                full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, checkpoint_stem)
                log(f"  Browser was closed. Saved immediate checkpoint: {full_csv.name}, {full_xlsx.name}, {results_csv.name}")
                log("  Stopping run now so remaining rows are not marked as failed.")
                break

            if (
                uses_browser
                and (args.target_consecutive_miss_restart if retailer_key == "target" else args.consecutive_miss_restart) > 0
                and consecutive_soft_misses.get(retailer_key, 0)
                >= (args.target_consecutive_miss_restart if retailer_key == "target" else args.consecutive_miss_restart)
            ):
                checkpoint_stem = f"checkpoint_{run_stamp}_after_{completed:04d}_soft_miss_rest"
                full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, checkpoint_stem)
                miss_count = consecutive_soft_misses.get(retailer_key, 0)
                log(f"  Consecutive misses reached {miss_count}. Saved checkpoint: {full_csv.name}, {full_xlsx.name}, {results_csv.name}")
                if retailer_key == "target" and target_soft_restarts >= 1:
                    for missed_product in consecutive_soft_miss_products.get(retailer_key, []):
                        defer_target_product(missed_product)
                    until = mark_target_soft_cooldown(
                        cooldown_state,
                        datetime.now().astimezone(),
                        args.target_soft_cooldown_minutes,
                        args.target_soft_cooldown_max_minutes,
                    )
                    target_soft_pause_until = until
                    save_cooldown_state(cooldown_state_path, cooldown_state)
                    paused_retailers[retailer_key] = f"Target soft cooldown active until {until.isoformat(timespec='seconds')}"
                    log(f"  Target soft failures continued after restart. {paused_retailers[retailer_key]}.")
                    close_browser_session()
                elif retailer_key == "target":
                    target_soft_restarts += 1
                    rest_and_restart_browser(f"{miss_count} consecutive Target loaded_no_price/error rows", args.target_soft_rest_min, args.target_soft_rest_max)
                else:
                    rest_and_restart_browser(f"{miss_count} consecutive loaded_no_price/error rows", args.browser_rest_min, args.browser_rest_max)
                retailer_rows_since_restart[retailer_key] = 0
                consecutive_soft_misses[retailer_key] = 0
                consecutive_soft_miss_products[retailer_key] = []

        if deferred_target_products and target_soft_pause_until:
            if not args.wait_for_target_soft_cooldown:
                log("")
                log(f"Target soft cooldown is active; {len(deferred_target_products):,} Target rows were left untouched for a later run.")
            else:
                wait_seconds = max(0.0, (target_soft_pause_until - datetime.now().astimezone()).total_seconds())
                if wait_seconds > 0:
                    log("")
                    log(f"Target soft cooldown still has {wait_seconds / 60:.1f} minutes left. Waiting, then trying deferred Target rows once.")
                    time.sleep(wait_seconds)
                else:
                    log("")
                    log("Target soft cooldown elapsed while other retailers were running. Trying deferred Target rows once.")

                target_record = cooldown_state.get("target")
                if isinstance(target_record, dict):
                    target_record.pop("soft_cooldown_until", None)
                    save_cooldown_state(cooldown_state_path, cooldown_state)
                paused_retailers.pop("target", None)
                target_soft_pause_until = None
                target_soft_restarts = 0
                consecutive_soft_misses["target"] = 0

                for retry_index, product in enumerate(deferred_target_products, start=1):
                    retailer_key = "target"
                    log("")
                    log(f"[Target retry {retry_index}/{len(deferred_target_products)}] row {product.row_number} | {product.retailer} | {product.brand} | size={product.size or '-'}")
                    log(f"  {product.url}")

                    cooldown = active_cooldown(cooldown_state, "target", datetime.now().astimezone())
                    if cooldown:
                        reason, until = cooldown
                        log(f"  SKIP | Target {reason} active until {until.isoformat(timespec='seconds')}; leaving remaining Target rows untouched.")
                        break

                    if args.target_max_rows_per_run > 0 and retailer_rows_attempted.get(retailer_key, 0) >= args.target_max_rows_per_run:
                        log(f"  SKIP | Target per-run cap reached ({args.target_max_rows_per_run} rows); leaving remaining Target rows untouched.")
                        break

                    rows_since_restart = retailer_rows_since_restart.get(retailer_key, 0)
                    rows_since_long_rest = retailer_rows_since_long_rest.get(retailer_key, 0)
                    if args.target_long_rest_every > 0 and rows_since_long_rest >= args.target_long_rest_every:
                        rest_and_restart_browser(f"{rows_since_long_rest} Target rows since long rest", args.target_long_rest_min, args.target_long_rest_max)
                        retailer_rows_since_restart[retailer_key] = 0
                        retailer_rows_since_long_rest[retailer_key] = 0
                        consecutive_soft_misses[retailer_key] = 0
                    elif args.target_restart_every > 0 and rows_since_restart >= args.target_restart_every:
                        rest_and_restart_browser(f"{rows_since_restart} Target rows since browser restart", args.target_rest_min, args.target_rest_max)
                        retailer_rows_since_restart[retailer_key] = 0
                        consecutive_soft_misses[retailer_key] = 0

                    if browser_rows_total > 0:
                        delay = random.uniform(args.target_delay_min, args.target_delay_max)
                        log(f"  Delay {delay:.1f}s")
                        time.sleep(delay)

                    if page is None:
                        open_browser_session()

                    start = time.time()
                    try:
                        result = scrape_target_dom_only(page, product, screenshot_dir, html_dir, args.target_wait_ms)
                    except Exception as exc:
                        result = ScrapeResult(product=product, status="error", source="runner_exception", error=f"{type(exc).__name__}: {exc}")

                    elapsed = time.time() - start
                    results.append(result)
                    table = merge_result(table, result, price_col, run_timestamp)
                    completed += 1
                    browser_rows_total += 1
                    retailer_rows_attempted[retailer_key] = retailer_rows_attempted.get(retailer_key, 0) + 1
                    retailer_rows_since_restart[retailer_key] = retailer_rows_since_restart.get(retailer_key, 0) + 1
                    retailer_rows_since_long_rest[retailer_key] = retailer_rows_since_long_rest.get(retailer_key, 0) + 1

                    if result.price is not None:
                        log(f"  OK ${result.price:.2f} | {result.status} | {result.source} | {elapsed:.1f}s")
                    else:
                        log(f"  MISS | {result.status} | {result.source} | {result.error} | {elapsed:.1f}s")
                    if result.title:
                        log(f"  PDP title: {result.title[:100]}")

                    if result.status == "blocked" and args.stop_retailer_on_block:
                        checkpoint_stem = f"checkpoint_{run_stamp}_after_{completed:04d}_target_blocked"
                        full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, checkpoint_stem)
                        log(f"  Block detected. Saved immediate checkpoint: {full_csv.name}, {full_xlsx.name}, {results_csv.name}")
                        if persistent_master_dir:
                            master_csv, master_xlsx = write_persistent_master(table, persistent_master_dir, args.persistent_master_name)
                            log(f"  Persistent master updated: {master_csv.name}, {master_xlsx.name}")
                        until = mark_target_hard_block(
                            cooldown_state,
                            datetime.now().astimezone(),
                            args.target_hard_cooldown_hours,
                            args.target_repeat_hard_cooldown_hours,
                            args.target_repeat_hard_window_hours,
                        )
                        save_cooldown_state(cooldown_state_path, cooldown_state)
                        log(f"  Target cooldown saved: Target hard block cooldown active until {until.isoformat(timespec='seconds')}")
                        close_browser_session()
                        break

                    if result.status in {"loaded_no_price", "error"}:
                        consecutive_soft_misses[retailer_key] = consecutive_soft_misses.get(retailer_key, 0) + 1
                        consecutive_soft_miss_products.setdefault(retailer_key, []).append(product)
                    else:
                        consecutive_soft_misses[retailer_key] = 0
                        consecutive_soft_miss_products[retailer_key] = []

                    if args.save_every > 0 and completed % args.save_every == 0:
                        checkpoint_stem = f"checkpoint_{run_stamp}_after_{completed:04d}"
                        full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, checkpoint_stem)
                        log(f"  Checkpoint saved: {full_csv.name}, {full_xlsx.name}, {results_csv.name}")
                        if persistent_master_dir:
                            master_csv, master_xlsx = write_persistent_master(table, persistent_master_dir, args.persistent_master_name)
                            log(f"  Persistent master updated: {master_csv.name}, {master_xlsx.name}")

                    if browser_closed_error(result):
                        checkpoint_stem = f"checkpoint_{run_stamp}_after_{completed:04d}_browser_closed"
                        full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, checkpoint_stem)
                        log(f"  Browser was closed. Saved immediate checkpoint: {full_csv.name}, {full_xlsx.name}, {results_csv.name}")
                        log("  Stopping run now so remaining rows are not marked as failed.")
                        break

                    if (
                        args.target_consecutive_miss_restart > 0
                        and consecutive_soft_misses.get(retailer_key, 0) >= args.target_consecutive_miss_restart
                    ):
                        checkpoint_stem = f"checkpoint_{run_stamp}_after_{completed:04d}_target_soft_miss_rest"
                        full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, checkpoint_stem)
                        miss_count = consecutive_soft_misses.get(retailer_key, 0)
                        log(f"  Consecutive Target misses reached {miss_count}. Saved checkpoint: {full_csv.name}, {full_xlsx.name}, {results_csv.name}")
                        if target_soft_restarts >= 1:
                            for missed_product in consecutive_soft_miss_products.get(retailer_key, []):
                                defer_target_product(missed_product)
                            until = mark_target_soft_cooldown(
                                cooldown_state,
                                datetime.now().astimezone(),
                                args.target_soft_cooldown_minutes,
                                args.target_soft_cooldown_max_minutes,
                            )
                            save_cooldown_state(cooldown_state_path, cooldown_state)
                            log(f"  Target soft failures continued on retry. Target soft cooldown active until {until.isoformat(timespec='seconds')}.")
                            close_browser_session()
                            break
                        target_soft_restarts += 1
                        rest_and_restart_browser(f"{miss_count} consecutive Target loaded_no_price/error rows", args.target_soft_rest_min, args.target_soft_rest_max)
                        retailer_rows_since_restart[retailer_key] = 0
                        consecutive_soft_misses[retailer_key] = 0
                        consecutive_soft_miss_products[retailer_key] = []
    finally:
        try:
            close_browser_session()
            if playwright:
                playwright.stop()
        except Exception:
            pass

    final_stem = f"local_price_results_{run_stamp}"
    full_csv, full_xlsx, results_csv = write_outputs(table, results, output_dir, final_stem)
    persistent_paths: tuple[Path, Path] | None = None
    if persistent_master_dir:
        persistent_paths = write_persistent_master(table, persistent_master_dir, args.persistent_master_name)
    captured = sum(1 for result in results if result.price is not None)
    misses = len(results) - captured

    log("")
    log("=" * 78)
    log("Run complete")
    log(f"Rows attempted: {len(results):,}")
    log(f"Prices captured: {captured:,}")
    log(f"Missing/error: {misses:,}")
    log(f"Upload this full CSV or Excel into Streamlit:")
    log(f"  {full_csv}")
    log(f"  {full_xlsx}")
    log(f"Detailed run log:")
    log(f"  {results_csv}")
    if persistent_paths:
        log(f"Persistent master updated for next run:")
        log(f"  {persistent_paths[0]}")
        log(f"  {persistent_paths[1]}")
    log("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
