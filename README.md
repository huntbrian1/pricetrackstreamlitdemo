# Hanes Price Tracker

Streamlit app for maintaining a growing price master table. The team can edit rows in the browser, upload an updated CSV/XLSX, run the scraper, and save the refreshed master table back to GitHub so it is still there the next day.

When GitHub sync is configured, the app loads `data/retail_wip_links_import.csv` from the GitHub repo at startup. If GitHub sync is not configured, it falls back to the bundled copy of that file. The original workbook is not modified.

## What It Tracks

The public table columns are:

- `retailer`
- `brand`
- `color`
- `size`
- `title`
- `link`
- dated price columns such as `2026-06-23_price`
- `last_run`, a down-to-the-second timestamp from the most recent scrape run

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
- Walmart: 375 rows from `Walmart Online bottoms` and `paste`.
- Amazon: 254 rows total.
  - 3 original Amazon 1P web-search rows.
  - 251 rows merged from `hanes_fotl_amazon_innerwear_links_expanded.xlsx` and `hanes_amazon_womens_underwear_links.xlsx`.
- Dollar General: 50 rows, including size-level rows from `hanes_womens_underwear_dg_tjmaxx_jcpenney_size_links.xlsx`.
- TJ Maxx: 4 proven Playwright rows.
- JCPenney: 84 rows, including size-list candidate rows from `hanes_womens_underwear_dg_tjmaxx_jcpenney_size_links.xlsx`.

Target URLs keep their `preselect=` query values so size-specific links still point to the intended variant.

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

For GitHub persistence, set these locally if you want to test auto-save:

```powershell
$env:GITHUB_TOKEN = "your_github_token_here"
$env:GITHUB_REPO = "huntbrian1/pricetrackstreamlitdemo"
$env:GITHUB_BRANCH = "main"
$env:GITHUB_DATA_PATH = "data/retail_wip_links_import.csv"
```

## Streamlit Cloud From GitHub

1. Put this `hanes_streamlit_tracker` folder in a GitHub repo.
2. In Streamlit Cloud, create an app from that repo.
3. Set the main file path to `streamlit_app.py` if this folder is the repo root.
4. If this folder is inside a larger repo, set the main file path to `hanes_streamlit_tracker/streamlit_app.py` and copy `requirements.txt` and `packages.txt` to the repo root if Streamlit does not install them automatically.
5. Add these secrets in Streamlit Cloud app settings:

```toml
SCRAPINGDOG_API_KEY = "your_key_here"
GITHUB_TOKEN = "your_github_token_here"
GITHUB_REPO = "huntbrian1/pricetrackstreamlitdemo"
GITHUB_BRANCH = "main"
GITHUB_DATA_PATH = "data/retail_wip_links_import.csv"
```

The GitHub token needs permission to read and write repository contents. For a fine-grained token, give this repo `Contents: Read and write`.

## Team Workflow

1. Open the Streamlit app.
2. Edit rows directly in the table, or reupload the latest CSV/XLSX.
3. Leave `Only rows missing this price column` checked for normal daily runs.
4. Leave `Auto-save GitHub master after scrape` checked.
5. Keep `Checkpoint save every rows` at `75` unless there is a reason to change it.
6. Use `Max rows this run` for test runs; set it to `0` only when intentionally running all matched rows.
7. Check `Confirm paid ScrapingDog run` before running Walmart or Amazon rows.
8. Run the price scrape.
9. The app checkpoint-saves the updated CSV back to `data/retail_wip_links_import.csv` every 75 rows.
10. Download CSV or Excel whenever someone wants a local copy.

The `Save to GitHub` button also saves the current edited table without running a scrape, which is useful after manually adding links.

Column filters above the table narrow the visible rows by retailer, brand, color, size, title, link, and price-column blank/filled status. Use `Export Filtered CSV/Excel` for the visible filtered rows, or `Export Full CSV/Excel` for the complete master table.

If a run is interrupted, use the same price column date and leave `Only rows missing this price column` checked. The next run will skip rows already saved in that date column and continue from the blanks.

Scrape results are attached back to the exact table row they came from, so editing `size`, `color`, `title`, or other descriptive cells does not move price history to a different row.
