"""
Historical floorsheet WITH trade time from nepalstock.com, one company at a time
(the library's getFloorSheetOf accepts a past business date).

Walks BACKWARD from the last project date. Stops by itself when ~60 trading days in a row have no data
(= the start of what nepalstock holds) or the time budget is used up, and resumes next run.
Progress is saved in state/nepal_history.json.

Days with no trades that lie BETWEEN two days with data are holidays: they are written to
reports/non_trading_days.csv (source = nepalstock).

  python scraper/nepse_history.py --budget-minutes 150
"""
import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from nepse import Nepse

import common

STATE_FILE = Path("state/nepal_history.json")
MIN_DATE = common.HISTORY_START
EMPTY_STREAK_STOP = 60          # this many empty non-weekend days in a row = no older data
DELAY = 0.15                    # polite pause between requests (seconds)
PROBE = ["NABIL", "NICA", "NLIC", "HRL", "UPPER", "SHIVM", "NTC", "CIT", "SBI", "EBL",
         "GBIME", "NIFRA", "HDL", "API", "CHCL"]
NPT = common.NPT


def log(msg):
    print(f"[{datetime.now(NPT):%Y-%m-%d %H:%M:%S} NPT] {msg}", flush=True)


def load_state():
    st = common.load_json(STATE_FILE, None)
    if not st:
        st = {"next_date": common.HISTORY_END.isoformat(), "earliest_data": None,
              "empty_streak": 0, "done": False, "note": "", "fail_runs": 0}
    st.setdefault("pending_empty", [])
    st.setdefault("fail_runs", 0)
    return st


def save_state(st):
    common.save_json(STATE_FILE, st)


def fetch_symbol(n, sym, d, tries=3):
    last = None
    for i in range(1, tries + 1):
        try:
            return n.getFloorSheetOf(sym, d.isoformat()) or []
        except TypeError as e:
            if "list indices" in str(e):
                return []
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(5 * i)
    raise RuntimeError(f"{sym} {d}: {last!r}")


def scan_date(n, d, symbols):
    """All trades of date d, or [] if nothing traded / date unavailable."""
    probe = [s for s in PROBE if s in symbols]
    rows, found = [], False
    for s in probe:
        r = fetch_symbol(n, s, d)
        time.sleep(DELAY)
        if r:
            found = True
            rows += r
    if not found:
        return []
    for s in symbols:
        if s in probe:
            continue
        rows += fetch_symbol(n, s, d)
        time.sleep(DELAY)
    iso = d.isoformat()
    seen, out = set(), []
    for r in rows:
        cid = str(r.get("contractId", "")).split(".")[0]
        bd = str(r.get("businessDate", ""))[:10] or f"{cid[:4]}-{cid[4:6]}-{cid[6:8]}"
        if bd != iso or cid in seen:      # drop wrong-date rows and duplicates
            continue
        seen.add(cid)
        out.append(common.normalize(r))
    out.sort(key=lambda x: (len(x["contractId"]), x["contractId"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-minutes", type=float, default=150)
    args = ap.parse_args()
    started = time.time()
    common.migrate_flat_files()

    st = load_state()
    if st.get("done"):
        log(f"Nepalstock history already complete. {st.get('note', '')}")
        return 0
    try:
        n = Nepse()
        n.setTLSVerification(False)
        symbols = list(n.getSecurityIDKeyMap().keys())
    except Exception as e:  # noqa: BLE001
        st["fail_runs"] = st.get("fail_runs", 0) + 1
        log(f"nepalstock not reachable ({str(e)[:150]}); failed run {st['fail_runs']}/3")
        if st["fail_runs"] >= 3:
            st["done"] = True
            st["note"] = "nepalstock was not reachable 3 runs in a row; skipped."
        save_state(st)
        return 0
    st["fail_runs"] = 0
    log(f"{len(symbols)} securities listed.")

    d = date.fromisoformat(st["next_date"])
    while d >= MIN_DATE:
        if (time.time() - started) / 60 > args.budget_minutes:
            log("Time budget used up; will continue next run.")
            break
        if common.is_non_trading_weekday(d):
            d -= timedelta(days=1)
            continue
        existing = common.find_existing(d)
        if existing and common.file_has_time(existing):
            found = True
            log(f"{d}: already have a file with trade times")
        else:
            try:
                recs = scan_date(n, d, symbols)
            except RuntimeError as e:
                log(f"{d}: failed after retries ({e}); stopping this run, will retry next run.")
                break
            found = bool(recs)
            if found:
                common.learn_lookups(recs)
                common.save_csv(d, recs)
                common.nt_remove(d)
                log(f"{d}: saved {len(recs)} rows (with trade time)")
            else:
                log(f"{d}: no data")
        if found:
            st["earliest_data"] = d.isoformat()
            st["empty_streak"] = 0
            for x in st["pending_empty"]:          # empty days newer than this data day = holidays
                common.nt_add(date.fromisoformat(x), "nepalstock")
            st["pending_empty"] = []
        else:
            st["empty_streak"] += 1
            st["pending_empty"].append(d.isoformat())
        d -= timedelta(days=1)
        st["next_date"] = d.isoformat()
        if st["empty_streak"] >= EMPTY_STREAK_STOP:
            st["done"] = True
            st["pending_empty"] = []                # these were "before nepalstock's history", not holidays
            st["note"] = (f"No older data after {st['earliest_data']}." if st["earliest_data"]
                          else "nepalstock returned no historical data at all.")
            log("DONE. " + st["note"])
            save_state(st)
            return 0
        save_state(st)
    else:
        st["done"] = True
        st["pending_empty"] = []
        st["note"] = "Reached the first date."
    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
