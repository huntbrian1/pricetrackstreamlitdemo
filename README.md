# Hanes Price Tracker

Streamlit app for maintaining a growing price master table. The team can edit rows in the browser, upload an updated CSV/XLSX, run the scraper, then download the refreshed master table.

By default, the app loads `data/retail_wip_links_import.csv`, a link-only import from `Retail Price Changes Womens (updatewip) (4) (1).xlsx`. The original workbook is not modified. A smaller demo file remains available from the sidebar.

## What It Tracks

The public table columns are:

- `retailer`
- `brand`
- `color`
- `size`
- `link`
- `title`
- `last_seen`
- dated price columns such as `2026-06-23_price`
- scrape status columns for troubleshooting

Old tracker files that use `url` instead of `link` still upload cleanly. The app normalizes them into the `link` column.

## Retailer Routing

- Target: Playwright, randomized delay.
- Dollar General: Playwright, randomized delay.
- TJ Maxx: Playwright, randomized delay.
- JCPenney: Playwright, randomized delay.
- Walmart: ScrapingDog Walmart Product API.
- Amazon: ScrapingDog generic scrape endpoint.
- Macy's and Kohl's: disabled for now because the tests in this thread did not reliably pull prices through Playwright or ScrapingDog.

## Included Link Import

`data/retail_wip_links_import.csv` includes:

- Target ALL: 410 rows.
- Walmart Online bottoms: 206 rows.
- paste: 169 rows.
- Amazon: 254 rows total.
  - 3 original Amazon 1P web-search rows.
  - 251 rows merged from `hanes_fotl_amazon_innerwear_links_expanded.xlsx` and `hanes_amazon_womens_underwear_links.xlsx`.

Target URLs keep their `preselect=` query values so size-specific links still point to the intended variant.

Amazon rows include `amazon_status`, `amazon_ship_seller`, and `amazon_notes` columns so verified, unverified, and review-needed rows stay visible to the team instead of getting blended together.

The implementation is based on the older tracker shape from `target_walmart_price.py` / `retail_price_tracker__automation.py` for the wide price master and `price_tracker2.py` for the delay and Target/variant-era structure. Those source scripts were left untouched.

## Local Run

```powershell
cd C:\Users\J1999\Documents\Codex\2026-06-06\files-mentioned-by-the-user-ar7a6\hanes_streamlit_tracker
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

For Walmart and Amazon rows, add the ScrapingDog key in the sidebar or set:

```powershell
$env:SCRAPINGDOG_API_KEY = "your_key_here"
```

## Streamlit Cloud From GitHub

1. Put this `hanes_streamlit_tracker` folder in a GitHub repo.
2. In Streamlit Cloud, create an app from that repo.
3. Set the main file path to `streamlit_app.py` if this folder is the repo root.
4. If this folder is inside a larger repo, set the main file path to `hanes_streamlit_tracker/streamlit_app.py` and copy `requirements.txt` and `packages.txt` to the repo root if Streamlit does not install them automatically.
5. Add this secret in Streamlit Cloud app settings:

```toml
SCRAPINGDOG_API_KEY = "your_key_here"
```

## Team Workflow

1. Open the Streamlit app.
2. Edit rows directly in the table, or reupload the latest CSV/XLSX.
3. Run the price scrape.
4. Download the updated CSV or Excel file.
5. Use that downloaded file as the next upload when adding more links or doing the next scrape.
