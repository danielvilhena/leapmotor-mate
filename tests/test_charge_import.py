"""Bulk charge-import CSV parsing + STRICT validation (#111). One typo must never block the whole file
nor land dirty data in the DB: good lines import, bad lines come back with a reason. Pure function, no DB."""
from datetime import date

import charge_import as ci

TODAY = date(2026, 7, 3)


def _parse(text):
    return ci.parse_charge_csv(text, today=TODAY)


def test_valid_rows_with_header_comments_and_blanks():
    rows, errors = _parse(
        "# instructions line, ignored\n"
        "date,energy_kwh,cost,type\n"
        "2025-11-03 21:30,42.5,8.10,AC\n"
        "\n"                                          # blank line ignored
        "2026-01-15,18,9.5,DC\n"
    )
    assert errors == []
    assert len(rows) == 2
    assert rows[0] == {"started_at": "2025-11-03T21:30:00", "ended_at": None, "energy_kwh": 42.5,
                       "cost": 8.1, "charge_type": "AC", "start_soc": None, "end_soc": None}
    # no time given → noon default (no day-shift), DC preserved
    assert rows[1] == {"started_at": "2026-01-15T12:00:00", "ended_at": None, "energy_kwh": 18.0,
                       "cost": 9.5, "charge_type": "DC", "start_soc": None, "end_soc": None}


def test_optional_fields_blank():
    rows, errors = _parse("2025-05-01,30\n")           # no cost, no type
    assert errors == []
    assert rows[0]["cost"] is None
    assert rows[0]["charge_type"] == "AC"              # blank type → AC


def test_european_semicolon_csv_with_comma_decimals():
    # European Excel (IT/FR/DE): ';' separator + ',' decimal. Must parse as one number, not two cells.
    rows, errors = _parse(
        "date;energy_kwh;cost;type\n"
        "2025-05-01 08:00;30,5;8,10;AC\n"
        "2025-06-02;12;;DC\n"
    )
    assert errors == []
    assert rows[0] == {"started_at": "2025-05-01T08:00:00", "ended_at": None, "energy_kwh": 30.5,
                       "cost": 8.1, "charge_type": "AC", "start_soc": None, "end_soc": None}
    assert rows[1]["energy_kwh"] == 12.0 and rows[1]["cost"] is None and rows[1]["charge_type"] == "DC"


def test_optional_end_time_gives_duration():
    rows, errors = _parse(
        "date,energy_kwh,cost,type,start_soc,end_soc,end\n"
        "2025-11-03 23:35,42.5,8.10,AC,23,60,2025-11-04 03:42\n"   # crosses midnight
        "2025-06-02 10:00,20,,DC,,,\n"                             # no end → None
    )
    assert errors == []
    assert rows[0]["started_at"] == "2025-11-03T23:35:00" and rows[0]["ended_at"] == "2025-11-04T03:42:00"
    assert rows[1]["ended_at"] is None


def test_end_before_start_and_bad_end_rejected():
    rows, errors = _parse(
        "2025-11-03 20:00,30,,AC,,,2025-11-03 18:00\n"   # end before start
        "2025-11-04 10:00,30,,AC,,,notadate\n"           # bad end
    )
    assert rows == []
    assert len(errors) == 2
    assert "before the start" in errors[0] and "end" in errors[1]


def test_optional_soc_columns():
    rows, errors = _parse(
        "date,energy_kwh,cost,type,start_soc,end_soc\n"
        "2025-05-01,30,5,AC,23,80\n"       # both SoC
        "2025-05-02,20,,DC,,\n"            # blank SoC → None
    )
    assert errors == []
    assert rows[0]["start_soc"] == 23.0 and rows[0]["end_soc"] == 80.0
    assert rows[1]["start_soc"] is None and rows[1]["end_soc"] is None


def test_soc_out_of_range_and_nonnumeric_rejected():
    rows, errors = _parse(
        "2025-05-01,30,,AC,120,80\n"       # start_soc > 100
        "2025-05-02,30,,AC,10,pieno\n"     # end_soc not a number
    )
    assert rows == []
    assert len(errors) == 2
    assert "start_soc" in errors[0] and "end_soc" in errors[1]


def test_comma_csv_keeps_dot_decimals():
    rows, errors = _parse("2025-05-01 08:00,30.5,8.10,AC\n")   # US/UK style: ',' sep + '.' decimal
    assert errors == []
    assert rows[0]["energy_kwh"] == 30.5 and rows[0]["cost"] == 8.1


def test_bad_date_rejected():
    rows, errors = _parse("03/11/2025,42\n")
    assert rows == []
    assert len(errors) == 1 and "bad date" in errors[0]


def test_future_date_rejected():
    rows, errors = _parse("2026-07-04,42\n")           # tomorrow relative to TODAY
    assert rows == []
    assert "future" in errors[0]


def test_non_positive_and_absurd_energy_rejected():
    rows, errors = _parse("2025-05-01,0\n2025-05-02,-3\n2025-05-03,999\n")
    assert rows == []
    assert len(errors) == 3
    assert all("energy_kwh" in e for e in errors)


def test_non_numeric_energy_rejected():
    rows, errors = _parse("2025-05-01,lots\n")
    assert rows == [] and "not a number" in errors[0]


def test_negative_and_bad_cost_rejected():
    rows, errors = _parse("2025-05-01,20,-4\n2025-05-02,20,free\n")
    assert rows == []
    assert len(errors) == 2


def test_bad_type_rejected():
    rows, errors = _parse("2025-05-01,20,5,PLUG\n")
    assert rows == [] and "must be AC or DC" in errors[0]


def test_fast_hpc_map_to_dc():
    rows, _ = _parse("2025-05-01,20,5,FAST\n2025-05-02,20,5,HPC\n")
    assert [r["charge_type"] for r in rows] == ["DC", "DC"]


def test_good_and_bad_mixed_partial_import():
    rows, errors = _parse(
        "date,energy_kwh,cost,type\n"
        "2025-05-01,20,5,AC\n"          # good
        "2025-05-02,oops,5,AC\n"        # bad energy
        "2025-05-03,25,,DC\n"           # good, no cost
    )
    assert len(rows) == 2                              # the two good ones imported
    assert len(errors) == 1 and "line 3" in errors[0]  # 1-based incl. header → the bad row is line 3


def test_empty_file_and_template_only():
    assert _parse("") == ([], [])
    # feeding our own blank template back in must import nothing and error on nothing
    rows, errors = _parse(ci.TEMPLATE)
    assert rows == [] and errors == []


def test_line_numbers_are_one_based_including_header():
    _, errors = _parse("date,energy_kwh\n2025-05-01,20\nbad-date,5\n")
    assert "line 3" in errors[0]                       # header=1, good=2, bad=3
