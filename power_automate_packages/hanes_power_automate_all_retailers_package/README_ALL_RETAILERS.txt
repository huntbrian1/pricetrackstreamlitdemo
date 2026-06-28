Hanes All-Retailer Power Automate Runner
========================================

Use this version if Hanes wants one local computer to run everything instead of using Streamlit Cloud for Walmart/Amazon.

What This Runs
--------------

Free local Playwright lane:

- Target
- Dollar General
- TJ Maxx
- JCPenney

Paid ScrapingDog API lane:

- Walmart
- Amazon

Important Cost Note
-------------------

Walmart and Amazon use ScrapingDog credits in this version.

Based on the current estimate:

- Walmart: about 5 credits per link
- Amazon: about 10 credits per link

Local Playwright retailers do not use ScrapingDog credits.

First-Time Setup
----------------

1. Put this folder somewhere simple and permanent, such as:
   C:\Hanes\Hanes_All_Retailers_Price_Runner

2. Make sure Python 3.10+ or Anaconda is installed on the computer.

3. Add the ScrapingDog API key using one of these methods:

   Option A:
   Create this file:
   config\scrapingdog_api_key.txt

   Paste the real key on the first line.

   Option B:
   Set a Windows environment variable named:
   SCRAPINGDOG_API_KEY

Power Automate Setup
--------------------

Use Power Automate Desktop action:

Run DOS command

DOS command:

cmd.exe /c ""C:\Hanes\Hanes_All_Retailers_Price_Runner\PowerAutomate_Run_Hanes_All_Retailers.bat""

Working folder:

C:\Hanes\Hanes_All_Retailers_Price_Runner

If the folder is placed somewhere else, update both paths to match the real folder.

Visible Demo Version
--------------------

For a video or live demo, use:

Run_Hanes_All_Retailers_Visible.bat

That version shows the terminal progress while prices are pulled.

Outputs
-------

This version has two kinds of output.

1. Living master file

The runner keeps updating these files:

master\hanes_all_retailers_price_master.xlsx
master\hanes_all_retailers_price_master.csv

This is the main Excel-style master. Each new run adds the new daily price/discount columns into this same file.

2. Daily run files

This version also writes timestamped daily files into:

all_retailer_outputs

Use the newest:

*_full_master.csv
or
*_full_master.xlsx

The daily run folder also contains a *_run_results.csv file with row-by-row status, price captured/missing, source method, and error notes.

Checkpoint Saves
----------------

The runner saves every 25 completed rows.

If the run is interrupted, use the newest checkpoint full-master file from all_retailer_outputs.

The fixed master file in master is also updated every 25 completed rows, so it should usually reflect the latest completed checkpoint.

Pacing And Block Handling
-------------------------

- Target waits a random 8 to 20 seconds between Target rows.
- Target is capped at 50 attempted rows per run by default.
- Target restarts the browser every 25 attempted Target rows, then rests 3 to 6 minutes before continuing.
- Target takes a longer 10 to 20 minute rest every 75 attempted Target rows.
- If 6 Target rows in a row load but do not expose a price, the runner saves a checkpoint, restarts the browser, rests 5 to 10 minutes, and continues cautiously.
- If Target keeps missing prices after that restart, the runner saves a checkpoint, puts Target into a 60 to 90 minute cooldown, defers the missed Target streak plus remaining Target rows, continues later retailers, then waits out any remaining cooldown time and tries those Target rows once.
- If Target shows a true block page, CAPTCHA, access denied, or bot-check page, the runner saves immediately, puts Target into a 24 hour cooldown, and skips the rest of Target for the current run. If that happens again within the repeat window, Target cooldown becomes 48 hours.
- Dollar General, TJ Maxx, and JCPenney wait 5 to 9 seconds between rows, restart/rest every 40 rows, and take a longer rest every 100 rows.
- Non-Target browser retailers are capped at 150 attempted rows per retailer per run by default.
- If a true block page is detected for Dollar General, TJ Maxx, or JCPenney, the runner saves immediately and skips the rest of that retailer for the current run.
- If the browser is manually closed, the runner saves immediately and stops cleanly so remaining rows are left untouched.
- Target cooldowns are saved in state\retailer_cooldowns.json so the next Power Automate run remembers whether Target should be skipped.

How This Differs From The Recommended Streamlit Workflow
--------------------------------------------------------

Recommended shared workflow:

1. Streamlit Cloud runs Walmart/Amazon.
2. Local Playwright runs Target/Dollar General/TJ Maxx/JCPenney.
3. Upload local output back into Streamlit.
4. Save to GitHub for shared visibility.

All-local alternative:

1. One local computer runs both the Playwright lane and the ScrapingDog lane.
2. The output still can be uploaded into Streamlit afterward for shared visibility.

This all-local option is useful if the team prefers one controlled-machine process over splitting collection between Streamlit Cloud and the local browser runner.
