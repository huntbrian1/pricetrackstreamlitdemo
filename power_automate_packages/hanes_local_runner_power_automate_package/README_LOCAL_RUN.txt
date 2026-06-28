Hanes Local Browser Price Runner
================================

Purpose
-------
This folder runs the browser-rendered retailers locally:

- Target
- Dollar General
- TJ Maxx
- JCPenney

These retailers do not run reliably inside Streamlit Cloud, but they are still able to be run locally for free with Playwright. Walmart and Amazon should usually be run from Streamlit Cloud through ScrapingDog because that path is API-backed and shared.

What The Runner Produces
------------------------
The local runner creates files in:

local_outputs

Upload the newest file ending in either of these names into Streamlit:

- *_full_master.csv
- *_full_master.xlsx

Those files preserve the Streamlit table structure:

- retailer
- brand
- bras_bottoms
- color
- size
- title
- pdp_title
- link
- dated price columns
- dated discount columns
- last_run

The separate *_run_results.csv file is a run log. It is useful for QA, but it is not the main upload file.

First-Time Setup
----------------
1. Put this folder somewhere stable, such as:
   C:\Hanes\Hanes_Local_Price_Runner

2. Make sure Python 3.10+ or Anaconda is installed on the computer.

3. Optional first-time installer:
   Install_Local_Runner_Dependencies.bat

   The normal run files also check/install missing Python packages, so this installer is helpful but not mandatory.

Daily Local Browser Run
-----------------------
1. If Walmart/Amazon were already run in Streamlit, export the full Streamlit table and place it in the input folder as either:
   input\current_price_master.xlsx
   or
   input\current_price_master.csv

   If that file is not present, the runner falls back to the seeded link list:
   input\retail_wip_links_import.csv

2. Double-click:
   Run_Hanes_Local_Price_Tracker.bat

   This visible runner checks/install packages if needed, then shows row-by-row progress in the terminal.

3. A terminal window will show live progress:
   - row number
   - retailer
   - link
   - captured price or miss reason
   - PDP title when available

4. When finished, upload the newest *_full_master.csv or *_full_master.xlsx from local_outputs into Streamlit.

5. Review the table in Streamlit and click Save to GitHub.

Important Workflow Notes
------------------------
- The local browser run is free because it uses Playwright instead of ScrapingDog.
- The machine should stay awake and online during the local run.
- The user can walk away from the local run if the computer stays awake and unlocked enough for browser automation.
- Streamlit Cloud API runs should stay open and monitored, but the user can still do other computer work in other windows while the Streamlit run is active.
- Streamlit gives the team shared visibility after local uploads and GitHub save.

Pacing And Interruption Behavior
--------------------------------
- Target waits a random 8 to 20 seconds between Target rows.
- Target is capped at 50 attempted rows per run by default.
- Target restarts the browser every 25 attempted Target rows, then rests 3 to 6 minutes before continuing.
- Target takes a longer 10 to 20 minute rest every 75 attempted Target rows.
- If 6 Target rows in a row load but do not expose a price, the runner saves a checkpoint, restarts the browser, rests 5 to 10 minutes, and continues cautiously.
- If Target keeps missing prices after that restart, the runner saves a checkpoint, puts Target into a 60 to 90 minute cooldown, defers the missed Target streak plus remaining Target rows, continues later retailers, then waits out any remaining cooldown time and tries those Target rows once.
- If Target shows a true block page, CAPTCHA, access denied, or bot-check page, the runner saves immediately, puts Target into a 24 hour cooldown, and skips the rest of Target for the current run. If that happens again within the repeat window, Target cooldown becomes 48 hours.
- Dollar General, TJ Maxx, and JCPenney wait 5 to 9 seconds between rows, restart/rest every 40 rows, and take a longer rest every 100 rows.
- Non-Target browser retailers are capped at 150 attempted rows per retailer per run by default.
- If a true block page is detected for Dollar General, TJ Maxx, or JCPenney, the runner saves immediately and skips the rest of that retailer for the current run instead of retrying into the block.
- If the browser is manually closed during a run, the runner saves immediately and stops cleanly so the remaining rows are not marked as failed.
- Target cooldowns are saved in state\retailer_cooldowns.json so the next Power Automate run remembers whether Target should be skipped.

Target Method
-------------
Target uses the updated DOM-only method:

- Chrome first, Chromium fallback
- no direct Redsky endpoint calls
- no Redsky JSON acceptance
- visible Target price nodes only
- preselect/TCIN IDs are logged for QA context
- screenshots and HTML artifacts are saved for Target rows

Optional Local ScrapingDog Run
------------------------------
If the team wants one person/machine to run everything locally, the same script can include Walmart and Amazon too. Set SCRAPINGDOG_API_KEY as a Windows environment variable, then run:

local_price_runner.py --include-api-retailers --scrapingdog-api-key YOUR_KEY

There is also a ready-made all-retailer Power Automate entry point:

PowerAutomate_Run_Hanes_All_Retailers.bat

That version runs Target, Dollar General, TJ Maxx, JCPenney, Walmart, and Amazon from one local computer. Walmart and Amazon use paid ScrapingDog credits. See:

Power_Automate_All_Retailers_Setup.txt

This is optional. The recommended shared workflow is:

1. Streamlit Cloud: Walmart and Amazon through ScrapingDog.
2. Export the full Streamlit table and save it into this folder as input\current_price_master.csv or input\current_price_master.xlsx.
3. Local runner: Target, Dollar General, TJ Maxx, JCPenney through Playwright.
4. Upload local output to Streamlit.
5. Save the coalesced table to GitHub.
