"""Shared helpers: paths, project rules, lookup tables, CSV saving, non-trading-day list, progress files."""
import csv
import json
from datetime import date, timedelta, timezone
from pathlib import Path

NPT = timezone(timedelta(hours=5, minutes=45))   # Nepal time

OUT_DIR = Path("data")                           # data/<YEAR>/<YYYY-MM-DD>_Floorsheet.csv
LOOKUP_DIR = OUT_DIR / "lookups"
REPORT_DIR = Path("reports")
STATE_DIR = Path("state")
FILENAME_FORMAT = "%Y-%m-%d_Floorsheet.csv"      # e.g. 2026-10-30_Floorsheet.csv

# ---- project rules --------------------------------------------------------
FIRST_DATE = date(2014, 5, 5)         # very first date of the project (done by hand with the manual workflow)
HISTORY_START = date(2014, 5, 6)      # the automatic backfill starts here
HISTORY_END = date(2026, 10, 30)      # last date of the project
WEEKEND_RULE_FROM = date(2026, 10, 30)  # from this date Saturday AND Sunday are not trading days
MAX_UNCERTAIN_TRIES = 5               # a date whose page keeps failing is given up (and reported) after this many runs


def is_non_trading_weekday(d):
    """Before 2026-10-30 only Saturday is skipped (Friday and Sunday were searched).
    From 2026-10-30 on, Saturday and Sunday are skipped."""
    if d >= WEEKEND_RULE_FROM:
        return d.weekday() in (5, 6)
    return d.weekday() == 5


COLUMNS = [
    "contractId", "stockSymbol", "buyerMemberId", "sellerMemberId",
    "contractQuantity", "contractRate", "contractAmount", "businessDate",
    "tradeBookId", "stockId", "buyerBrokerName", "sellerBrokerName",
    "tradeTime", "securityName",
    # extra columns split out for analysis
    "tradeDate", "tradeClock",
]


# ---------------------------------------------------------------- small json helpers
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1))


# ---------------------------------------------------------------- lookups
def _load(name, key):
    p = LOOKUP_DIR / name
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {r[key]: r for r in csv.DictReader(f)}


BROKERS = _load("brokers.csv", "memberId")
STOCKS = _load("stocks.csv", "stockSymbol")


def learn_lookups(records):
    """Add any new broker / stock names found in full-detail records to the lookup files."""
    new_b = {r["buyerMemberId"]: r["buyerBrokerName"] for r in records if r.get("buyerBrokerName")}
    new_b.update({r["sellerMemberId"]: r["sellerBrokerName"] for r in records if r.get("sellerBrokerName")})
    new_s = {r["stockSymbol"]: (r["stockId"], r["securityName"]) for r in records if r.get("securityName")}
    changed = False
    for k, v in new_b.items():
        if k not in BROKERS:
            BROKERS[k] = {"memberId": k, "brokerName": v}
            changed = True
    for k, (i, n) in new_s.items():
        if k not in STOCKS:
            STOCKS[k] = {"stockSymbol": k, "stockId": i, "securityName": n}
            changed = True
    if changed:
        LOOKUP_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOOKUP_DIR / "brokers.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["memberId", "brokerName"])
            for k in sorted(BROKERS, key=lambda x: int(x) if str(x).isdigit() else 0):
                w.writerow([k, BROKERS[k]["brokerName"]])
        with open(LOOKUP_DIR / "stocks.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["stockSymbol", "stockId", "securityName"])
            for k in sorted(STOCKS):
                w.writerow([k, STOCKS[k]["stockId"], STOCKS[k]["securityName"]])


def split_time(trade_time):
    """'2026-10-28T14:59:59.999526' -> ('2026-10-28', '14:59:59.999526')"""
    if not trade_time:
        return "", ""
    s = str(trade_time).replace(" ", "T")
    d, _, t = s.partition("T")
    return d, t


# ---------------------------------------------------------------- year-wise files
def csv_path(d):
    """data/2026/2026-10-30_Floorsheet.csv"""
    return OUT_DIR / str(d.year) / d.strftime(FILENAME_FORMAT)


def find_existing(d):
    """Path of the saved file for date d (year folder, or an old flat file), else None."""
    p = csv_path(d)
    if p.exists():
        return p
    legacy = OUT_DIR / d.strftime(FILENAME_FORMAT)
    return legacy if legacy.exists() else None


def existing_dates():
    """Set of ISO dates that have a saved file."""
    out = set()
    if OUT_DIR.exists():
        for p in list(OUT_DIR.glob("*/*_Floorsheet.csv")) + list(OUT_DIR.glob("*_Floorsheet.csv")):
            out.add(p.name[:10])
    return out


def file_has_time(path):
    """True if an existing CSV already has trade times (so it must not be overwritten by a time-less one)."""
    try:
        with open(path, encoding="utf-8") as f:
            rows = csv.DictReader(f)
            first = next(rows, None)
            return bool(first and first.get("tradeTime"))
    except OSError:
        return False


def migrate_flat_files():
    """Move old flat files data/YYYY-MM-DD_Floorsheet.csv into data/YYYY/. Safe to run every time."""
    moved = 0
    if not OUT_DIR.exists():
        return 0
    for p in sorted(OUT_DIR.glob("*_Floorsheet.csv")):
        if not p.name[:4].isdigit():
            continue
        target = OUT_DIR / p.name[:4] / p.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            new_t, old_t = file_has_time(p), file_has_time(target)
            keep_new = (new_t and not old_t) or (new_t == old_t and p.stat().st_size > target.stat().st_size)
            if keep_new:
                p.replace(target)
            else:
                p.unlink()
        else:
            p.replace(target)
        moved += 1
    return moved


def save_csv(d, records):
    path = csv_path(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    tmp.replace(path)
    legacy = OUT_DIR / d.strftime(FILENAME_FORMAT)
    if legacy.exists():
        legacy.unlink()
    return path


def normalize(r):
    """Turn one nepalstock record (dict) into a row with our 16 columns."""
    row = {c: r.get(c, "") for c in COLUMNS}
    row["contractId"] = str(r.get("contractId", "")).split(".")[0]
    for k in ("buyerMemberId", "sellerMemberId", "stockId", "tradeBookId"):
        v = r.get(k, "")
        row[k] = str(v).split(".")[0] if v != "" and v is not None else ""
    row["businessDate"] = str(r.get("businessDate", ""))[:10]
    row["tradeDate"], row["tradeClock"] = split_time(row["tradeTime"])
    return row


# ---------------------------------------------------------------- non-trading days (besides Saturdays)
NT_FILE = REPORT_DIR / "non_trading_days.csv"
NT_FIELDS = ["date", "weekday", "source", "checked_on"]
LEGACY_NT = STATE_DIR / "non_trading_days.txt"
# source: merolagani = found empty (not yet re-checked) | verified = re-checked strictly at the end
#         nepalstock = no trades on nepalstock between two trading days | daily = found empty by the daily run


def nt_save(table):
    NT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(NT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NT_FIELDS, extrasaction="ignore")
        w.writeheader()
        for k in sorted(table):
            w.writerow(table[k])


def nt_load():
    """{iso_date: row}. Also imports the old state/non_trading_days.txt once."""
    table = {}
    if NT_FILE.exists():
        with open(NT_FILE, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("date"):
                    table[r["date"]] = r
    if LEGACY_NT.exists():
        for line in LEGACY_NT.read_text().splitlines():
            parts = line.split()
            if not parts or parts[0] in table:
                continue
            try:
                dd = date.fromisoformat(parts[0])
            except ValueError:
                continue
            table[parts[0]] = {"date": parts[0], "weekday": dd.strftime("%A"),
                               "source": "merolagani", "checked_on": ""}
        nt_save(table)
        LEGACY_NT.unlink()
    return table


def nt_add(d, source):
    from datetime import datetime
    table = nt_load()
    table[d.isoformat()] = {"date": d.isoformat(), "weekday": d.strftime("%A"), "source": source,
                            "checked_on": datetime.now(NPT).strftime("%Y-%m-%d")}
    nt_save(table)


def nt_remove(d):
    table = nt_load()
    if d.isoformat() in table:
        del table[d.isoformat()]
        nt_save(table)


# ---------------------------------------------------------------- dates that failed (site errors)
UNC_FILE = STATE_DIR / "uncertain_dates.json"
LEGACY_UNC = STATE_DIR / "uncertain_dates.txt"


def save_uncertain(data):
    save_json(UNC_FILE, dict(sorted(data.items())))


def load_uncertain():
    """{iso_date: number_of_failed_retries}. Also imports the old uncertain_dates.txt once."""
    data = load_json(UNC_FILE, {})
    if LEGACY_UNC.exists():
        for x in LEGACY_UNC.read_text().split():
            try:
                date.fromisoformat(x)
            except ValueError:
                continue
            data.setdefault(x, 0)
        save_uncertain(data)
        LEGACY_UNC.unlink()
    return data


# ---------------------------------------------------------------- dates where the site shows fewer rows than it announces
MISMATCH_FILE = REPORT_DIR / "row_count_mismatch.csv"
MISMATCH_FIELDS = ["date", "site_announced", "saved", "missing", "note", "checked_on"]


def mismatch_add(d, announced, saved, note):
    from datetime import datetime
    rows = {}
    if MISMATCH_FILE.exists():
        with open(MISMATCH_FILE, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rows[r["date"]] = r
    rows[d.isoformat()] = {"date": d.isoformat(), "site_announced": announced, "saved": saved,
                           "missing": announced - saved, "note": note,
                           "checked_on": datetime.now(NPT).strftime("%Y-%m-%d")}
    MISMATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MISMATCH_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MISMATCH_FIELDS)
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])


# ---------------------------------------------------------------- data checking
def find_missing(end=None):
    """Trading-weekday dates (FIRST_DATE..end) that have no file and are not in the non-trading list."""
    end = min(end or HISTORY_END, HISTORY_END)
    have = existing_dates()
    nt = set(nt_load())
    out, d = [], FIRST_DATE
    while d <= end:
        iso = d.isoformat()
        if not is_non_trading_weekday(d) and iso not in have and iso not in nt:
            out.append(d)
        d += timedelta(days=1)
    return out
