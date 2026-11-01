"""
Data checking system.

  python scraper/check_data.py          -> coverage report (fast): for every date 2014-05-05 .. 2026-10-30
                                           says: traded (file exists) / weekend / holiday (non-trading list) / MISSING
  python scraper/check_data.py --deep   -> also opens every CSV and checks header, row count, duplicate
                                           transaction numbers, tradeDate = file date, trade-time presence

Writes reports/coverage_report.txt (and reports/data_summary.csv with --deep).
The list of non-trading days besides Saturday is reports/non_trading_days.csv (kept sorted by this script).
"""
import argparse
import csv
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import common

MAX_LISTED = 60


def deep_check(files):
    """Yield one summary dict per file."""
    for iso, path in sorted(files.items()):
        problems, rows, dup, wrong_date, with_time = [], 0, 0, 0, 0
        ids, groups = set(), {}
        try:
            with open(path, encoding="utf-8", newline="") as f:
                rd = csv.reader(f)
                header = next(rd, None)
                if header != common.COLUMNS:
                    problems.append("header differs")
                else:
                    ci, ti, di = (header.index("contractId"), header.index("tradeTime"),
                                  header.index("tradeDate"))
                    for r in rd:
                        if len(r) != len(header):
                            problems.append("row with wrong column count")
                            break
                        rows += 1
                        if r[ci] in ids:
                            dup += 1
                        ids.add(r[ci])
                        if len(r[ci]) == 16 and r[ci].isdigit():
                            groups.setdefault(r[ci][8:10], set()).add(int(r[ci][10:]))
                        if r[di] != iso:
                            wrong_date += 1
                        if r[ti]:
                            with_time += 1
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            problems.append(f"unreadable ({e})")
        if rows == 0 and not problems:
            problems.append("no data rows")
        if dup:
            problems.append(f"{dup} duplicate transaction numbers")
        if wrong_date:
            problems.append(f"{wrong_date} rows whose Transact. No. date differs from the file date")
        gaps = {g: max(n) - len(n) for g, n in sorted(groups.items()) if max(n) - len(n) > 0}
        yield {"date": iso, "rows": rows, "rows_with_trade_time": with_time,
               "sequence_gaps": ", ".join(f"{g}:{n}" for g, n in gaps.items()), "problems": "; ".join(problems)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true")
    args = ap.parse_args()

    common.migrate_flat_files()
    files = {p.name[:10]: p for p in sorted(common.OUT_DIR.glob("*/*_Floorsheet.csv"))}
    nt = common.nt_load()
    common.nt_save(nt)                       # keeps the file sorted and tidy
    unc = common.load_uncertain()

    per_year = defaultdict(Counter)
    missing, conflict = [], []
    d = common.FIRST_DATE
    while d <= common.HISTORY_END:
        iso, y = d.isoformat(), d.year
        if common.is_non_trading_weekday(d):
            per_year[y]["weekend"] += 1
        elif iso in files:
            per_year[y]["trading day (file)"] += 1
            if iso in nt:
                conflict.append(iso)
        elif iso in nt:
            per_year[y]["holiday (listed)"] += 1
        else:
            per_year[y]["MISSING"] += 1
            missing.append(iso)
        d += timedelta(days=1)

    out = [f"DATA CHECK  {datetime.now(common.NPT):%Y-%m-%d %H:%M} NPT",
           f"Range {common.FIRST_DATE} .. {common.HISTORY_END}; files found: {len(files)}; "
           f"non-trading days listed (besides weekends): {len(nt)}", "",
           f"{'Year':<6}{'trading(file)':>15}{'holiday':>9}{'weekend':>9}{'MISSING':>9}"]
    for y in sorted(per_year):
        c = per_year[y]
        out.append(f"{y:<6}{c['trading day (file)']:>15}{c['holiday (listed)']:>9}{c['weekend']:>9}{c['MISSING']:>9}")
    out.append("")
    out.append(f"Dates with neither file nor non-trading entry: {len(missing)}"
               + (" -> " + ", ".join(missing[:MAX_LISTED]) + (" ..." if len(missing) > MAX_LISTED else "")
                  if missing else ""))
    if conflict:
        out.append(f"Dates that have a file AND are listed as non-trading (remove from list): {', '.join(conflict[:MAX_LISTED])}")
    pending = [k for k, v in unc.items() if v < common.MAX_UNCERTAIN_TRIES]
    given_up = [k for k, v in unc.items() if v >= common.MAX_UNCERTAIN_TRIES]
    out.append(f"Dates being retried after site errors: {len(pending)}; given up (check by hand): {len(given_up)}"
               + (" -> " + ", ".join(sorted(given_up)) if given_up else ""))
    if common.MISMATCH_FILE.exists():
        with open(common.MISMATCH_FILE, encoding="utf-8") as f:
            n_mm = max(0, sum(1 for _ in f) - 1)
        out.append(f"Dates where the site shows fewer rows than it announces: {n_mm} (reports/row_count_mismatch.csv)")

    if args.deep:
        summary, bad = [], []
        for s in deep_check(files):
            summary.append(s)
            if s["problems"]:
                bad.append(s)
        with open(common.REPORT_DIR / "data_summary.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["date", "rows", "rows_with_trade_time", "sequence_gaps", "problems"])
            w.writeheader()
            w.writerows(summary)
        total_rows = sum(s["rows"] for s in summary)
        out.append("")
        out.append(f"Deep check: {len(summary)} files, {total_rows} trade rows, files with problems: {len(bad)}")
        for s in bad[:MAX_LISTED]:
            out.append(f"  {s['date']}: {s['problems']}")
        if len(bad) > MAX_LISTED:
            out.append("  ... (see reports/data_summary.csv)")

    common.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    text = "\n".join(out) + "\n"
    (common.REPORT_DIR / "coverage_report.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
