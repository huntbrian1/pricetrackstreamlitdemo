# Hanes Power Automate Packages

These packages are optional local runners that complement the Streamlit dashboard.

## Packages

- `hanes_power_automate_all_retailers_package.zip`: runs Walmart and Amazon through ScrapingDog APIs and runs Target, Dollar General, TJ Maxx, and JCPenney locally through Playwright.
- `hanes_local_runner_power_automate_package.zip`: runs the local browser lane for Target, Dollar General, TJ Maxx, and JCPenney. This is the free/local lane intended to export results for Streamlit upload.
- `hanes_power_automate_demo_all_retailers_50row.zip`: smaller demo package for walkthroughs.

The unzipped folders are included so the team can inspect or edit the files directly from GitHub.

## API key note

The zip files and folders intentionally do not include a real ScrapingDog API key. Put the key in `config/scrapingdog_api_key.txt` after downloading, or set `SCRAPINGDOG_API_KEY` as a Windows environment variable.