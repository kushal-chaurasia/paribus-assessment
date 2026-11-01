from app.services.csv_service import HospitalRow, parse_hospital_csv


def parse(text: str, max_rows: int = 20, encoding: str = "utf-8"):
    return parse_hospital_csv(text.encode(encoding), max_rows)


def test_parses_valid_rows_with_optional_phone():
    result = parse("name,address,phone\nGeneral,1 Main St,555-1234\nCity,2 Oak Ave,\n")
    assert result.valid
    assert result.rows == [
        HospitalRow(row=1, name="General", address="1 Main St", phone="555-1234"),
        HospitalRow(row=2, name="City", address="2 Oak Ave", phone=None),
    ]


def test_phone_column_may_be_omitted_and_headers_are_case_insensitive():
    result = parse(" Name , ADDRESS \nGeneral,1 Main St\n")
    assert result.valid
    assert result.rows[0].phone is None


def test_handles_bom_quoted_commas_and_blank_lines():
    result = parse('﻿name,address\n\n"General","1 Main St, Suite 2"\n\n', encoding="utf-8")
    assert result.valid
    assert result.rows[0].address == "1 Main St, Suite 2"
    assert result.total_rows == 1


def test_reports_missing_required_column():
    result = parse("name,phone\nGeneral,555\n")
    assert not result.valid
    assert result.errors[0].row == 0
    assert "address" in result.errors[0].message


def test_reports_missing_required_values_per_row():
    result = parse("name,address\nGeneral,\n,2 Oak Ave\nCity,3 Elm St\n")
    assert [(e.row, e.field) for e in result.errors] == [(1, "address"), (2, "name")]
    assert [r.row for r in result.rows] == [3]


def test_rejects_unquoted_extra_values():
    result = parse("name,address,phone\nGeneral,1 Main St, Suite 2,555\n")
    assert not result.valid
    assert result.errors[0].row == 1


def test_enforces_row_limit():
    body = "name,address\n" + "".join(f"H{i},Addr {i}\n" for i in range(21))
    result = parse(body)
    assert not result.valid
    assert "maximum is 20" in result.errors[-1].message


def test_rejects_empty_and_header_only_files():
    assert parse("").errors[0].message == "CSV file is empty"
    assert parse("name,address\n").errors[0].message == "CSV contains no hospital rows"


def test_rejects_non_utf8():
    result = parse_hospital_csv("name,address\nCafé,1 St\n".encode("utf-16"), 20)
    assert result.errors[0].message == "File must be UTF-8 encoded"
