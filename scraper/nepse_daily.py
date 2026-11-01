"""
Daily scrape from nepalstock.com (open-source NepseUnofficialApi library).
This source includes the exact trade time. The site only serves the LATEST trading day.

Exit codes: 0 = saved / already saved and it is today's data,
            2 = no data for today (nepalstock empty, or its latest day is older than today):
                the workflow then falls back to Merolagani, which also detects holidays,
            1 = unexpected error.
"""
import sys
import time
from datetime import date, datetime

from nepse import Nepse

import common


def fetch(n, tries=3):
    last = None
    for i in range(1, tries + 1):
        try:
            return n.getFloorSheet()
        except TypeError as e:
            if "list indices" in str(e):
                # nepalstock answered with an empty list = nothing published (market closed / no data yet)
                print("nepalstock returned an empty reply (no floorsheet published right now).")
                return []
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        print(f"attempt {i}/{tries} failed: {last!r}")
        time.sleep(15 * i)
    raise last


def main():
    common.migrate_flat_files()
    n = Nepse()
    n.setTLSVerification(False)  # nepalstock's certificate often fails verification
    data = fetch(n)
    if not data:
        print("No floorsheet data; nothing saved.")
        return 2
    recs = [common.normalize(r) for r in data]
    d = date.fromisoformat(recs[0]["businessDate"][:10])
    existing = common.find_existing(d)
    if existing and common.file_has_time(existing):
        print(f"{existing} already exists with trade times; nothing to save.")
    else:
        common.learn_lookups(recs)
        recs.sort(key=lambda x: (len(x["contractId"]), x["contractId"]))
        p = common.save_csv(d, recs)
        common.nt_remove(d)
        print(f"Saved {len(recs)} rows -> {p}")
    today = datetime.now(common.NPT).date()
    if d < today:
        print(f"Latest nepalstock day is {d}, not {today}: asking Merolagani whether {today} was a holiday.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
