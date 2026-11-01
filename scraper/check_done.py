"""Exit code 0 when the whole historical backfill is finished, 1 otherwise. Prints why.
Uses only the standard library (no Playwright needed), so it can run before anything is installed."""
import sys
from datetime import date, timedelta

import common

bf = common.load_json("state/backfill_state.json", None)
nh = common.load_json("state/nepal_history.json", None)
last = date.fromisoformat(bf["last_completed"]) if bf else None
nepal_done = bool(nh and nh.get("done"))
earliest = date.fromisoformat(nh["earliest_data"]) if nh and nh.get("earliest_data") else None

unc = common.load_uncertain()
pending = sorted(k for k, v in unc.items() if v < common.MAX_UNCERTAIN_TRIES)
given_up = sorted(k for k, v in unc.items() if v >= common.MAX_UNCERTAIN_TRIES)

forward_done = bool(last and (last >= common.HISTORY_END or
                              (nepal_done and earliest and last >= earliest - timedelta(days=1))))

# only dates that are not already being retried count as "missing"
missing = [d for d in common.find_missing() if d.isoformat() not in unc] if forward_done else []
unverified = [k for k, v in common.nt_load().items() if v.get("source") == "merolagani"] if forward_done else []

print(f"Merolagani forward walk done: {forward_done} (last date {last}); nepalstock history done: {nepal_done}"
      f" ({nh.get('note') if nh else 'not started'})")
print(f"Dates still being retried: {len(pending)}; given up after {common.MAX_UNCERTAIN_TRIES} tries: {len(given_up)}"
      f"; dates without file or non-trading entry: {len(missing)}; no-data dates not yet re-checked: {len(unverified)}")
if given_up:
    print("Given up (please look at them by hand): " + ", ".join(given_up))

finished = forward_done and nepal_done and not pending and not missing and not unverified
print("FINISHED" if finished else "NOT FINISHED YET")
sys.exit(0 if finished else 1)
