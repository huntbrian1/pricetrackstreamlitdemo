Hanes Power Automate Demo Package
=================================

Purpose
-------
This folder is a smaller demo version of the all-retailer local runner.

It includes 50 total product rows:

- 10 Target
- 10 Walmart
- 10 Amazon
- 10 Dollar General
- 10 JCPenney
- 0 TJ Maxx

The demo input is ordered in retailer blocks:

10 Target, then 10 Walmart, then 10 Amazon, then 10 Dollar General, then 10 JCPenney.

That makes the Power Automate demo easier to explain one retailer section at a time:

- Browser lane: Target, Dollar General, JCPenney
- ScrapingDog API lane: Walmart, Amazon

Recommended Demo Folder
-----------------------
For the video, copy or unzip this folder to:

C:\Hanes\Hanes_All_Retailers_Demo

Power Automate Demo Flow
------------------------
Create a new Power Automate Desktop flow named:

Hanes All Retailers Demo - ScrapingDog + Browser

Add one action:

Run application

Use these settings:

Application path:

C:\Windows\System32\cmd.exe

Command line arguments:

/k ""C:\Hanes\Hanes_All_Retailers_Demo\Run_Hanes_All_Retailers_Visible.bat""

Working folder:

C:\Hanes\Hanes_All_Retailers_Demo

ScrapingDog API Key
-------------------
Before running the demo, put the ScrapingDog API key here:

C:\Hanes\Hanes_All_Retailers_Demo\config\scrapingdog_api_key.txt

Paste the key on the first line of that file.

Input File
----------
The demo already includes the smaller 50-row input file:

input\retail_wip_links_import.csv

Do not add a current_price_master file for the demo unless you intentionally want to override the demo list.

Outputs
-------
After the demo runs, outputs appear in:

all_retailer_outputs

The living master files appear in:

master\hanes_all_retailers_price_master.xlsx
master\hanes_all_retailers_price_master.csv

Video Notes
-----------
For the video, you do not need to let all 50 rows finish. Show:

1. The demo folder.
2. The config file where the ScrapingDog key goes.
3. The 50-row input file.
4. The Power Automate flow with the Run DOS command step.
5. The run completing and output files appearing.
6. The output folders where files will appear.

Target Behavior
---------------
Target is intentionally cautious. If Target has soft issues, the runner saves progress, continues other retailers, waits out any remaining soft cooldown, and tries deferred Target rows once.

If Target shows a hard block, CAPTCHA, or access denied page, the runner saves progress and leaves Target for a future run.
