"""
Merolagani Floorsheet scraper.

Modes
  python scraper/scrape.py daily                   -> today's floorsheet (Nepal date)
  python scraper/scrape.py daily --date 2026-10-29 -> one specific date
  python scraper/scrape.py backfill --budget-minutes 150
        -> walks forward from the last completed date (start: 2014-05-06) and stops cleanly when the
           time budget is used up. When the forward walk is finished it also
             1. fills any date that has no file and is not a known non-trading day,
             2. re-checks every "no data" date once more, strictly (the final data check).

Files are saved as data/<YEAR>/<YYYY-MM-DD>_Floorsheet.csv
Non-trading days (besides Saturday, and Sunday from 2026-10-30) are listed in reports/non_trading_days.csv

IMPORTANT behaviour (the reason for the rewrite): Merolagani sometimes shows FEWER rows than the
"of N records" figure it announces (some rows are simply not displayed by the site, the same rows
every time). The old script treated that as an error and retried forever. Now the rows that exist are
saved, and the difference is written to reports/row_count_mismatch.csv so nothing is hidden.
"""
import argparse
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

import common

# ----------------------------- SETTINGS ---------------------------------
URL = "https://merolagani.com/Floorsheet.aspx"
STATE_FILE = Path("state/backfill_state.json")
TIMING_FILE = Path("state/timing.json")
TIMING = {"data": [], "empty": []}              # learned search durations (seconds)
NPT = common.NPT
DELAY_RANGE = (1.0, 2.5)                       # polite pause between pages (seconds)
MAX_ATTEMPTS = 3                               # fresh-browser attempts when the site errors
PAGE_SIZE = 500
MIN_WAIT = 3                                   # seconds after pressing Search before a "page reloaded" counts
RUN = {"started": time.time(), "budget": 150.0}
# ------------------------------------------------------------------------

# every table row with at least 3 cells; rows with 8+ cells are trades, shorter ones are only reported
ROWS_JS = """
() => [...document.querySelectorAll('table tr')]
  .map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim()))
  .filter(c => c.length >= 3)
"""

FIRST_TXN_CHANGED_JS = """
prev => {
  const r = [...document.querySelectorAll('table tr')]
    .find(tr => tr.querySelectorAll('td').length >= 8);
  return r && r.querySelectorAll('td')[1].innerText.trim() !== prev;
}
"""


def log(msg):
    print(f"[{datetime.now(NPT):%Y-%m-%d %H:%M:%S} NPT] {msg}", flush=True)


def out_of_time():
    return (time.time() - RUN["started"]) / 60 > RUN["budget"]


def num(text):
    return text.replace(",", "").strip()


def fmt_int(text):
    t = num(text)
    try:
        return str(int(float(t)))
    except ValueError:
        return t


def fmt_float(text):
    """nepalstock style: 412.5 / 41250.0 (no thousands commas, no trailing zeros except one decimal)."""
    t = num(text)
    try:
        return repr(float(t))
    except ValueError:
        return t


def to_seq(text):
    try:
        return int(text.replace(",", "").strip())
    except ValueError:
        return None


def to_record(cells, d):
    # cells: [#, Transact No, Symbol, Buyer, Seller, Quantity, Rate, Amount]
    cid = cells[1].split(".")[0]
    sym, buy, sell = cells[2], cells[3], cells[4]
    stock = common.STOCKS.get(sym, {})
    return {
        "contractId": cid,
        "stockSymbol": sym,
        "buyerMemberId": buy,
        "sellerMemberId": sell,
        "contractQuantity": fmt_int(cells[5]),
        "contractRate": fmt_float(cells[6]),
        "contractAmount": fmt_float(cells[7]),
        "businessDate": d.isoformat(),
        "tradeBookId": "",                       # not on Merolagani
        "stockId": stock.get("stockId", ""),
        "buyerBrokerName": common.BROKERS.get(buy, {}).get("brokerName", ""),
        "sellerBrokerName": common.BROKERS.get(sell, {}).get("brokerName", ""),
        "tradeTime": "",                         # Merolagani has no clock time
        "securityName": stock.get("securityName", ""),
        # the Transact. No. starts with the date YYYYMMDD
        "tradeDate": f"{cid[:4]}-{cid[4:6]}-{cid[6:8]}" if len(cid) >= 8 else d.isoformat(),
        "tradeClock": "",
    }


SEARCH_CANDIDATES = [
    'a[id*="SearchFloorsheet" i]:visible',
    'input[id*="SearchFloorsheet" i]:visible',
    'a[id*="Search" i][id*="Floor" i]:visible',
    'input[type="submit"][value*="Search" i]:visible',
    'input[type="button"][value*="Search" i]:visible',
    'button:has-text("Search"):visible',
    'a.btn:has-text("Search"):visible',
    'a:has-text("Search"):visible',
]


def dump_debug(page, why):
    """Save a screenshot and a list of 'search' elements so the problem can be diagnosed."""
    Path("debug").mkdir(exist_ok=True)
    try:
        page.screenshot(path="debug/page.png", full_page=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        info = page.evaluate("""() => [...document.querySelectorAll('a,button,input')]
          .filter(e => /search/i.test((e.id||'') + (e.value||'') + (e.innerText||'')))
          .map(e => `${e.tagName} id=${e.id} type=${e.type||''} text=${(e.innerText||e.value||'').trim().slice(0,30)} visible=${!!(e.offsetWidth||e.offsetHeight)}`)""")
    except Exception as e:  # noqa: BLE001
        info = [f"could not list elements: {e}"]
    print(f"DEBUG ({why}) - page title: {page.title()!r}, url: {page.url}")
    for line in info:
        print("  candidate:", line)


NAV_HINTS = ("context was destroyed", "navigation", "navigated", "Target closed")


def click_search(page, date_box):
    """Click the visible Search button. The click may start a slow page reload: that is fine."""
    for sel in SEARCH_CANDIDATES:
        try:
            if page.locator(sel).count() == 0:
                continue
            page.locator(sel).first.click(timeout=20000, no_wait_after=True)
            return
        except PWError as e:
            if any(h in str(e) for h in NAV_HINTS):
                return                                # the click did start a page reload
            print(f"  search selector failed: {sel} ({str(e)[:80]})")
    print("  no Search button worked; pressing Enter in the date box")
    try:
        date_box.press("Enter")
    except PWError:
        dump_debug(page, "search button not found")
        raise RuntimeError("Could not find a clickable Search button")


# ----------------------------------------------------------------- learned timing
def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def load_timing():
    t = common.load_json(TIMING_FILE, {})
    TIMING["data"], TIMING["empty"] = t.get("data", []), t.get("empty", [])


def save_timing():
    common.save_json(TIMING_FILE, {"data": TIMING["data"][-300:], "empty": TIMING["empty"][-300:]})


def timing_report():
    md, me = median(TIMING["data"]), median(TIMING["empty"])
    log("Learned search times: trading days median "
        + (f"{md:.0f}s ({len(TIMING['data'])} days)" if md else "n/a")
        + ", no-data days median "
        + (f"{me:.0f}s ({len(TIMING['empty'])} days)" if me else "n/a"))


def fast_cutoff():
    """A 'no data' answer that comes back much faster than trading days usually take is suspicious."""
    if len(TIMING["data"]) >= 5:
        return 0.4 * median(TIMING["data"])
    return 0.0


# ----------------------------------------------------------------- page handling
DATE_BOX = 'input[id*="FloorsheetDate"]:visible'


def settle(page, seconds=2.0):
    """Let the page finish any loading or auto-reload it started, then pause briefly."""
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWError:
        pass
    time.sleep(seconds)


def wait_for_results(page, t0, timeout=240):
    """Wait (patiently, the site is slow) until results appear, or the page reloaded and stayed empty.
    A 'reload' seen in the first MIN_WAIT seconds is ignored: that was the cause of the bogus 0-second answers."""
    deadline = time.time() + timeout
    reloaded_at = None
    while time.time() < deadline:
        try:
            body = page.inner_text("body")
            if re.search(r"of\s+[\d,]+\s+records", body):
                return body, time.time() - t0
            reloaded = not page.evaluate("window.__before_search === true")
            done = page.evaluate("document.readyState") == "complete"
        except PWError:                               # page is changing right now: just wait
            time.sleep(1)
            continue
        if reloaded and done and time.time() - t0 >= MIN_WAIT:
            reloaded_at = reloaded_at or time.time()
            if time.time() - reloaded_at > 8:         # reload finished and still no table
                return body, reloaded_at - t0
        else:
            reloaded_at = None
        time.sleep(1)
    raise RuntimeError("Search did not finish in time (site slow, or the Search click did nothing)")


def search_once(page, d):
    """Load the page, enter the date, search. Returns (page_text, search_really_ran_for_this_date, seconds)."""
    want = d.strftime("%m/%d/%Y")
    page.goto(URL, wait_until="domcontentloaded", timeout=90000)
    settle(page)
    box = page.locator(DATE_BOX).first
    for _ in range(3):
        box.fill(want)
        box.press("Tab")                              # commit the value (may auto-reload the page)
        settle(page)
        box = page.locator(DATE_BOX).first
        if box.input_value() == want:
            break
    else:
        raise RuntimeError("the date box would not keep the date")
    page.evaluate("window.__before_search = true")   # marker: disappears when the page reloads
    t0 = time.time()
    click_search(page, box)
    body, elapsed = wait_for_results(page, t0)
    settle(page, 1.0)
    try:
        ran = page.locator(DATE_BOX).first.input_value() == want
    except PWError:
        ran = False
    return body, ran, elapsed


def page_rows(page):
    """(trade_rows, other_rows) of the page currently shown."""
    data, odd = [], []
    for c in page.evaluate(ROWS_JS):
        (data if len(c) >= 8 else odd).append(c)
    return data, odd


def scrape_date(page, d, empty_need=2.0):
    """One complete pass over date d.
    Returns (records, announced_total, info). ([], 0, {}) only when repeated, complete searches found
    nothing (empty_need = how many full-weight 'no data' answers are required; a hurried answer counts half
    and at least one full-weight answer is always required)."""
    score, full_seen, empty_times = 0.0, False, []
    body = total = None
    for attempt in range(1, 6):
        body, ran, elapsed = search_once(page, d)
        as_of = re.search(r"As of\s+(\d{4})/(\d{2})/(\d{2})", body)
        total = re.search(r"of\s+([\d,]+)\s+records", body)
        if as_of and total and date(int(as_of[1]), int(as_of[2]), int(as_of[3])) == d \
                and int(total[1].replace(",", "")) > 0:
            TIMING["data"].append(round(elapsed, 1))
            break                                     # real data for the right date
        print(f"  {d}: attempt {attempt}: no results (search ran: {ran}, took {elapsed:.0f}s). "
              f"Page text starts: {' '.join(body.split())[:160]!r}")
        if ran:
            empty_times.append(round(elapsed, 1))
            if elapsed >= fast_cutoff():
                score += 1.0
                full_seen = True
            else:
                score += 0.5
            if score >= empty_need and full_seen:
                TIMING["empty"].extend(empty_times)
                return [], 0, {}                      # confirmed: genuinely no data
        time.sleep(5)
    else:
        raise RuntimeError(f"{d}: could not get a clear answer from the site")

    announced = int(total[1].replace(",", ""))
    pages_m = re.search(r"Total pages:\s*(\d+)", body)
    total_pages = int(pages_m[1]) if pages_m else 1

    cur, odd = page_rows(page)
    if not cur:
        raise RuntimeError(f"{d}: first page showed no trade rows")
    raw, odd_all = list(cur), list(odd)
    for n in range(2, total_pages + 1):
        time.sleep(random.uniform(*DELAY_RANGE))
        prev = cur[0][1]
        page.get_by_role("link", name="Next", exact=True).first.click(timeout=60000, no_wait_after=True)
        page.wait_for_function(FIRST_TXN_CHANGED_JS, arg=prev, timeout=120000)
        page.wait_for_load_state("networkidle", timeout=120000)
        cur, odd = page_rows(page)
        if not cur:
            raise RuntimeError(f"{d}: page {n} showed no trade rows")
        raw += cur
        odd_all += odd
        log(f"  {d} page {n}/{total_pages} ({len(raw)}/{announced})")

    seen, dups, seqs = {}, 0, set()
    for c in raw:
        rec = to_record(c, d)
        if rec["contractId"] in seen:
            dups += 1
            continue
        seen[rec["contractId"]] = rec
        s = to_seq(c[0])
        if s is not None:
            seqs.add(s)

    # The first column "#" is a running row number. If it runs up to the announced total and has holes,
    # the site itself does not show those rows (nothing is lost by our parser).
    missing_seq = []
    if seqs and max(seqs) > PAGE_SIZE:
        missing_seq = sorted(set(range(1, announced + 1)) - seqs)
    deficit = announced - len(seen)
    explained = bool(missing_seq) and len(missing_seq) == deficit and dups == 0 and not odd_all
    # Transact. No. = YYYYMMDD + 2-digit group + running number (1..N inside each group, no holes on a full day).
    # If the holes in those running numbers add up exactly to the missing rows, the site itself did not show them.
    groups, gap_by_group = {}, {}
    for cid in seen:
        if len(cid) == 16 and cid.isdigit():
            groups.setdefault(cid[8:10], set()).add(int(cid[10:]))
    for g, nums in groups.items():
        if max(nums) - len(nums) > 0:
            gap_by_group[g] = max(nums) - len(nums)
    if deficit > 0 and dups == 0 and groups and sum(gap_by_group.values()) == deficit:
        explained = True
    info = {"dups": dups, "odd_rows": len(odd_all), "missing_seq": missing_seq[:20],
            "explained": explained, "gap_by_group": gap_by_group}
    return list(seen.values()), announced, info


def scrape_in_new_context(browser, d, empty_need):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    try:
        return scrape_date(ctx.new_page(), d, empty_need)
    finally:
        ctx.close()


def scrape_with_retry(browser, d, empty_need=2.0):
    """Returns (records, announced_total, info). ([], 0, {}) = confirmed no data.
    Site errors are retried in a fresh browser. A shortfall in rows is NOT an error: it is checked once more
    (unless the site's own row numbers already show the rows are not displayed) and the rows found are kept."""
    union, expected, info = {}, 0, {}
    ok_runs, fails, prev_size = 0, 0, -1
    while True:
        try:
            recs, total, run_info = scrape_in_new_context(browser, d, empty_need)
            if total == 0 and union:
                raise RuntimeError("the site now answers 'no data' for a date that had data")
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"  attempt {fails}/{MAX_ATTEMPTS} failed for {d}: {str(e)[:200]}")
            if fails >= MAX_ATTEMPTS:
                raise
            time.sleep(10 * fails)
            continue
        if total == 0:
            return [], 0, {}
        ok_runs += 1
        expected, info = total, run_info
        for r in recs:
            union.setdefault(r["contractId"], r)
        if len(union) >= expected:
            break
        if info.get("explained"):
            break
        if ok_runs >= 2 and len(union) == prev_size:
            break
        if ok_runs >= 3:
            break
        prev_size = len(union)
        log(f"  {d}: site announced {expected} rows but {len(union)} were shown; checking once more")
    recs = sorted(union.values(), key=lambda r: (len(r["contractId"]), r["contractId"]))
    if len(recs) < 0.8 * expected and not info.get("explained"):
        raise RuntimeError(f"{d}: only {len(recs)} of {expected} rows could be read")
    return recs, expected, info


def process_date(browser, d, skip_existing=False, empty_need=2.0, source="merolagani"):
    """Returns 'weekend' | 'exists' | 'saved' | 'empty'."""
    if common.is_non_trading_weekday(d):
        log(f"{d}: {d.strftime('%A')} is not a trading day, skipped")
        return "weekend"
    ex = common.find_existing(d)
    if ex and (skip_existing or common.file_has_time(ex)):
        log(f"{d}: file already exists, skipped")
        return "exists"
    t0 = time.time()
    recs, expected, info = scrape_with_retry(browser, d, empty_need)
    if not recs:
        common.nt_add(d, source)
        log(f"{d}: ({d.strftime('%A')}) no data after repeated checks = not a trading day "
            f"({time.time() - t0:.0f}s) -> reports/non_trading_days.csv")
        return "empty"
    common.nt_remove(d)
    path = common.save_csv(d, recs)
    msg = f"{d}: ({d.strftime('%A')}) saved {len(recs)} rows -> {path} ({time.time() - t0:.0f}s)"
    if len(recs) != expected:
        note = ("rows not shown by the site (numbering has holes" + (f", gaps per group {info['gap_by_group']}" if info.get("gap_by_group") else "") + ")"
                if info.get("explained") else "shortfall repeated on re-check")
        common.mismatch_add(d, expected, len(recs), note)
        msg += f"  [site announced {expected}, shows {len(recs)}: noted in reports/row_count_mismatch.csv]"
    log(msg)
    return "saved"


# ----------------------------------------------------------------- progress helpers
def load_state():
    st = common.load_json(STATE_FILE, None)
    if st:
        return date.fromisoformat(st["last_completed"])
    return common.HISTORY_START - timedelta(days=1)


def save_state(d):
    common.save_json(STATE_FILE, {"last_completed": d.isoformat()})


def nepal_info():
    """(earliest date nepalstock history has reached or None, nepalstock history finished?)"""
    st = common.load_json("state/nepal_history.json", {})
    try:
        e = date.fromisoformat(st["earliest_data"]) if st.get("earliest_data") else None
    except ValueError:
        e = None
    return e, bool(st.get("done"))


def retry_uncertain(browser):
    """Dates that failed on an earlier run get another chance first (max common.MAX_UNCERTAIN_TRIES runs).
    Returns False when the site seems down (3 failures in a row)."""
    unc = common.load_uncertain()
    streak = 0
    for s_ in sorted(unc):
        if unc[s_] >= common.MAX_UNCERTAIN_TRIES:
            continue
        if out_of_time():
            return True
        try:
            process_date(browser, date.fromisoformat(s_), skip_existing=True)
            unc.pop(s_)
            streak = 0
        except Exception as e:  # noqa: BLE001
            unc[s_] += 1
            streak += 1
            log(f"{s_}: still uncertain, try {unc[s_]}/{common.MAX_UNCERTAIN_TRIES} ({str(e)[:100]})")
        common.save_uncertain(unc)
        if streak >= 3:
            log("3 retries in a row failed: site down or page changed; stopping this run, progress is saved")
            return False
    return True


def fill_gaps(browser):
    """Dates with no file that are not weekends and not in the non-trading list."""
    missing = common.find_missing()
    if not missing:
        return
    log(f"Gap check: {len(missing)} dates have neither a file nor a non-trading entry; fetching them now.")
    unc = common.load_uncertain()
    for d in missing:
        if out_of_time():
            return
        if unc.get(d.isoformat(), 0) >= common.MAX_UNCERTAIN_TRIES:
            continue
        try:
            process_date(browser, d, skip_existing=True)
            unc.pop(d.isoformat(), None)
        except Exception as e:  # noqa: BLE001
            unc[d.isoformat()] = unc.get(d.isoformat(), 0) + 1
            log(f"{d}: gap fill failed ({str(e)[:100]})")
        common.save_uncertain(unc)


def verify_non_trading(browser):
    """Final data check: every date listed as 'no data' by this scraper is searched again, strictly
    (three full-length empty answers needed). If data turns up it is saved and the date leaves the list."""
    table = common.nt_load()
    todo = sorted(k for k, v in table.items() if v.get("source") == "merolagani")
    if not todo:
        log("Final check of non-trading days: nothing left to verify.")
        return
    log(f"Final check of non-trading days: {len(todo)} dates still to verify.")
    streak = 0
    for s_ in todo:
        if out_of_time():
            return
        d = date.fromisoformat(s_)
        if common.is_non_trading_weekday(d):
            common.nt_remove(d)
            continue
        try:
            res = process_date(browser, d, skip_existing=True, empty_need=3.0, source="verified")
            if res == "exists":
                common.nt_remove(d)
            streak = 0
        except Exception as e:  # noqa: BLE001
            streak += 1
            log(f"{d}: verification failed ({str(e)[:100]}); will try again next run")
            if streak >= 3:
                log("3 failures in a row: stopping this run")
                return


def daily_reverify(browser, today):
    """Dates the daily run found empty are checked strictly once more on a later day."""
    for s_, row in sorted(common.nt_load().items()):
        d = date.fromisoformat(s_)
        if row.get("source") == "daily" and d < today:
            try:
                process_date(browser, d, skip_existing=True, empty_need=3.0, source="verified")
            except Exception as e:  # noqa: BLE001
                log(f"{d}: re-check failed ({str(e)[:100]})")


# ----------------------------------------------------------------- backfill
def backfill(browser):
    if not retry_uncertain(browser):
        return
    d = load_state() + timedelta(days=1)
    nepal, nepal_done = nepal_info()
    failures = 0
    forward_done = False
    while True:
        if d > common.HISTORY_END:
            forward_done = True
            break
        if nepal and d >= nepal:
            log(f"Reached {nepal}: later dates come from nepalstock (with trade time).")
            forward_done = nepal_done
            break
        if out_of_time():
            log("Time budget used up; will continue next run.")
            return
        try:
            process_date(browser, d, skip_existing=True)
            failures = 0
        except Exception as e:  # noqa: BLE001
            failures += 1
            log(f"{d}: UNCERTAIN, could not be scraped ({str(e)[:150]}); will be retried on later runs")
            unc = common.load_uncertain()
            unc.setdefault(d.isoformat(), 0)
            common.save_uncertain(unc)
            if failures >= 3:
                log("3 dates in a row failed: site down or page changed; stopping this run, progress is saved")
                save_state(d)
                save_timing()
                return
        save_state(d)
        save_timing()
        d += timedelta(days=1)
    if forward_done:
        log("Forward walk finished.")
        fill_gaps(browser)
        if not out_of_time():
            verify_non_trading(browser)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["daily", "backfill"])
    ap.add_argument("--date", help="YYYY-MM-DD (daily mode only)")
    ap.add_argument("--budget-minutes", type=float, default=150)
    args = ap.parse_args()

    RUN["started"], RUN["budget"] = time.time(), args.budget_minutes
    today = datetime.now(NPT).date()
    n = common.migrate_flat_files()
    if n:
        log(f"Moved {n} old files into year folders.")
    load_timing()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            if args.mode == "daily":
                d = date.fromisoformat(args.date) if args.date else today
                daily_reverify(browser, d)
                process_date(browser, d, source="daily")
            else:
                backfill(browser)
        finally:
            save_timing()
            timing_report()
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
