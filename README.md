# NEPSE Floorsheet Scraper

One CSV per trading day, saved **year by year**:
`data/2026/2026-10-30_Floorsheet.csv`

Columns (16): `contractId, stockSymbol, buyerMemberId, sellerMemberId, contractQuantity, contractRate,
contractAmount, businessDate, tradeBookId, stockId, buyerBrokerName, sellerBrokerName, tradeTime,
securityName, tradeDate, tradeClock`

* `tradeDate` = the date, `tradeClock` = the time of day, split out of `tradeTime`.
* nepalstock rows have the real trade time. Merolagani rows have no clock time, so `tradeClock` is blank there;
  `tradeDate` is always filled (it is the first 8 digits, YYYYMMDD, of the Transact. No.).

## Folders

| Path | What |
|---|---|
| `data/<YEAR>/` | the daily CSV files (old flat files are moved here automatically) |
| `reports/non_trading_days.csv` | **dates with no trading besides Saturday** (and Sunday from 2026-10-30): date, weekday, source, checked_on |
| `reports/coverage_report.txt` | data check: per year, trading days / holidays / weekends / MISSING |
| `reports/row_count_mismatch.csv` | dates where the website shows fewer rows than it announces (what was announced, what was saved) |
| `reports/data_summary.csv` | per-file check (rows, rows with trade time, problems), written by the final deep check |
| `state/` | progress files (do not delete) |

`source` in the non-trading list: `merolagani` = found empty, will be re-checked strictly before the project finishes;
`verified` = re-checked, confirmed; `nepalstock` = no trades between two trading days on nepalstock; `daily` = found by the daily run.

## What runs (Nepal time)

| Workflow | When | What it does |
|---|---|---|
| **Historical Backfill** | nightly, 7:05 PM and 12:45 AM shifts (about 7 PM to 6:30 AM) | Fills 2014-05-06 to 2026-10-30, fills gaps, re-checks every "no data" date, writes the reports, then **switches itself off** when everything is complete. |
| **Daily Floorsheet** | 3:30 PM, Monday to Friday (after the 3:00 PM close); also **Run workflow** at any time (optional box: one date YYYY-MM-DD, scraped from Merolagani) | Saves the day from nepalstock.com (with trade time). If nepalstock has no data for today it asks Merolagani, which also records holidays. |

## Trading-day rules

* Up to 2026-10-29 only **Saturday** is skipped. Friday and Sunday are searched.
* From 2026-10-30 **Saturday and Sunday** are skipped.
* A date is listed as non-trading only when repeated, complete searches find no data.
  A hurried empty answer counts only half, and at least one full-length empty answer is always required.
* At the end, every `merolagani` entry is searched again (3 full-length empty answers needed).
  If data appears, it is saved and the date leaves the list.

## Row-count differences (the problem fixed in this version)

For some dates Merolagani announces "of N records" but displays fewer rows, the same rows missing every time.
Before, that was treated as an error and retried forever. Now the rows shown are saved, the difference is recorded
in `reports/row_count_mismatch.csv`, and the run goes on. A real site error is retried in a fresh browser and,
if it keeps failing, in later runs (up to 5); dates given up are listed in `reports/coverage_report.txt`.

## How the history works

1. `nepse_history.py` (nepalstock, WITH trade time) walks backward from 2026-10-30 and stops by itself
   where nepalstock's history ends.
2. `scrape.py backfill` (Merolagani, no trade time) walks forward from 2014-05-06 and stops where
   nepalstock's history begins; then it fills gaps and re-checks the non-trading list.
3. `check_done.py` decides when all of that is finished; the workflow then disables itself.

## First-time / fresh upload steps

1. Unzip the file on your computer. You get a folder with `.github`, `scraper`, `requirements.txt`, `README.md`.
2. GitHub repository > **Add file > Upload files**. Drag in the CONTENTS of the folder
   (the `.github` and `scraper` folders and the two files). Click **Commit changes**.
   If your computer hides the `.github` folder, use the "git" method in the chat instructions.
3. **Settings > Actions > General > Workflow permissions**: *Read and write permissions* > **Save**.
4. **Actions** tab > **Historical Backfill** > **Run workflow** (green button) > **Run workflow**.
   Do the same for **Daily Floorsheet** once to test it. If a workflow shows as disabled, press **Enable workflow**.
5. When a run finishes, check the **data** folder (year folders) and the **reports** folder.
   The backfill needs many nightly runs; it stops by itself when everything is complete.

Free GitHub minutes are unlimited for a **public** repository.
If a run shows a red cross, open the failed step, copy the red text and send it to Claude.
