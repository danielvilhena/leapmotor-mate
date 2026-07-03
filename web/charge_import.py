"""Bulk charge import from a user-filled CSV (idea from #111). Pure — no DB, no HTTP — so the parsing
and the STRICT validation are fully unit-testable. `parse_charge_csv` returns (rows, errors): `rows` are
clean dicts ready for db_reader.add_manual_charge, `errors` are human-readable per-line problems. The
import endpoint inserts ONLY `rows`, so a single bad line never blocks the whole file AND never lands
dirty data in the DB — every rejected line is reported back with the reason. Excel round-trips CSV
natively, so the same file works whether the user edits it in Excel, Numbers or a text editor."""
from datetime import date as _date
from datetime import datetime
import csv
import io

# The template columns, in order. `date` + `energy_kwh` are required; the rest are optional. New optional
# columns go at the END so existing files/positions never shift.
COLUMNS = ("date", "energy_kwh", "cost", "type", "start_soc", "end_soc", "end")
_HEADER_ALIASES = {"date", "data", "datum", "date_time", "datetime"}   # first-cell values that mean "header row"
MAX_KWH = 250.0            # a single session above this is almost certainly a typo (biggest pack here ~100 kWh)

# Empty template we hand the user — self-documenting, with commented examples they delete. The importer
# skips every line starting with '#', so the instructions and the sample rows are never imported.
TEMPLATE = (
    "# LeapMotor Mate - charge import template\n"
    "# One charge per row. Lines starting with '#' are IGNORED (delete the two example rows below).\n"
    "#\n"
    "# date       (required) : YYYY-MM-DD  or  YYYY-MM-DD HH:MM   - not in the future\n"
    "# energy_kwh (required) : kWh added, e.g. 42.5              - 0 to 250, dot or comma decimal\n"
    "# cost       (optional) : amount paid, e.g. 8.10            - leave blank if unknown\n"
    "# type       (optional) : AC or DC                          - leave blank to default to AC\n"
    "# start_soc  (optional) : battery % at start, e.g. 23       - 0-100, blank if unknown\n"
    "# end_soc    (optional) : battery % at end, e.g. 80         - 0-100, blank if unknown\n"
    "# end        (optional) : end date/time, YYYY-MM-DD HH:MM   - for the duration; blank = no duration\n"
    "#\n"
    "# Example (delete these two lines before importing):\n"
    "# 2025-11-03 21:30,42.5,8.10,AC,23,80,2025-11-04 01:37\n"
    "# 2026-01-15,18,9.5,DC,,,\n"
    "date,energy_kwh,cost,type,start_soc,end_soc,end\n"
)

_DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def _parse_dt(s: str):
    s = s.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _num(s: str) -> float:
    # accept both "42.5" and the European "42,5" (in a ';'-delimited file the comma is the decimal)
    return float(str(s).strip().replace(",", "."))


def _opt_soc(s: str):
    """Parse an optional SoC %: blank → None, else a 0-100 number (raises ValueError otherwise)."""
    s = (s or "").strip()
    if not s:
        return None
    v = _num(s)                              # raises ValueError if not a number
    if not (0.0 <= v <= 100.0):
        raise ValueError("SoC out of 0-100")
    return v


def _sniff_delimiter(text: str) -> str:
    """European Excel (IT/FR/DE locales) saves CSV with ';' as the field separator and ',' as the
    decimal — US/UK Excel uses ',' and '.'. Pick the delimiter from the first real line so both a
    `date,energy` and a `date;energy` file import correctly (and `30,5` stays one number, not two)."""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return ";" if s.count(";") > s.count(",") else ","
    return ","


def parse_charge_csv(text: str, *, today=None):
    """Parse + validate the CSV text. Returns (rows, errors).

    rows  : list of {started_at, energy_kwh, cost, charge_type} dicts (feed each to add_manual_charge).
    errors: list of "line N: <reason>" strings for every rejected row (header/blank/# lines are silent).
    Strict on purpose — a charge that can't be trusted (unparseable date, future date, non-positive or
    absurd energy, negative cost, unknown type) is rejected, not guessed."""
    today = today or _date.today()
    rows: list[dict] = []
    errors: list[str] = []
    seen_header = False
    delim = _sniff_delimiter(text)
    for i, raw in enumerate(csv.reader(io.StringIO(text), delimiter=delim), start=1):
        if not raw or all(not c.strip() for c in raw):
            continue                                             # blank line
        if raw[0].lstrip().startswith("#"):
            continue                                             # comment / instructions
        cells = [c.strip() for c in raw]
        if not seen_header and cells[0].lower() in _HEADER_ALIASES:
            seen_header = True
            continue                                             # the column-name header row
        if len(cells) < 2 or not cells[0] or not cells[1]:
            errors.append(f"line {i}: needs at least a date and energy_kwh")
            continue

        dt = _parse_dt(cells[0])
        if dt is None:
            errors.append(f"line {i}: bad date '{cells[0]}' — use YYYY-MM-DD or YYYY-MM-DD HH:MM")
            continue
        if dt.date() > today:
            errors.append(f"line {i}: date '{cells[0]}' is in the future")
            continue

        try:
            energy = _num(cells[1])
        except ValueError:
            errors.append(f"line {i}: energy_kwh '{cells[1]}' is not a number")
            continue
        if not (0 < energy <= MAX_KWH):
            errors.append(f"line {i}: energy_kwh must be greater than 0 and at most {MAX_KWH:.0f}")
            continue

        cost = None
        cost_s = cells[2] if len(cells) > 2 else ""
        if cost_s:
            try:
                cost = _num(cost_s)
            except ValueError:
                errors.append(f"line {i}: cost '{cost_s}' is not a number")
                continue
            if cost < 0:
                errors.append(f"line {i}: cost cannot be negative")
                continue

        type_s = (cells[3] if len(cells) > 3 else "").upper()
        ctype = "DC" if type_s in ("DC", "FAST", "HPC") else ("AC" if type_s in ("", "AC") else None)
        if ctype is None:
            errors.append(f"line {i}: type '{cells[3]}' must be AC or DC")
            continue

        # Optional start/end SoC % (0-100) → the card's SoC-gain tile (#67 @rossiadobe). Imported charges
        # have no power curve, so they stay excluded from the SoH estimate whether or not SoC is given.
        try:
            start_soc = _opt_soc(cells[4] if len(cells) > 4 else "")
        except ValueError:
            errors.append(f"line {i}: start_soc '{cells[4]}' must be a number 0-100")
            continue
        try:
            end_soc = _opt_soc(cells[5] if len(cells) > 5 else "")
        except ValueError:
            errors.append(f"line {i}: end_soc '{cells[5]}' must be a number 0-100")
            continue

        # Noon default when no time given → the charge never day-shifts on display across time zones,
        # matching the manual-entry form's own 12:00 default.
        start_final = dt if (dt.hour or dt.minute) else dt.replace(hour=12)

        # Optional end date/time → the charge's duration (#67 @rossiadobe). Full 'YYYY-MM-DD HH:MM' (or
        # date); must not be before the start. Blank → None (add_manual_charge then makes end == start).
        ended_at = None
        end_s = cells[6] if len(cells) > 6 else ""
        if end_s:
            end_dt = _parse_dt(end_s)
            if end_dt is None:
                errors.append(f"line {i}: end '{end_s}' — use YYYY-MM-DD HH:MM")
                continue
            if end_dt < start_final:
                errors.append(f"line {i}: end '{end_s}' is before the start")
                continue
            ended_at = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

        rows.append({
            "started_at": start_final.strftime("%Y-%m-%dT%H:%M:%S"),
            "ended_at": ended_at,
            "energy_kwh": round(energy, 3),
            "cost": round(cost, 2) if cost is not None else None,
            "charge_type": ctype,
            "start_soc": round(start_soc, 1) if start_soc is not None else None,
            "end_soc": round(end_soc, 1) if end_soc is not None else None,
        })
    return rows, errors
