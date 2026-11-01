import csv
import io
from dataclasses import dataclass, field

from app.schemas import CSVRowError, CSVValidationResponse, HospitalRowPreview

REQUIRED_COLUMNS = ("name", "address")


@dataclass(frozen=True, slots=True)
class HospitalRow:
    row: int
    name: str
    address: str
    phone: str | None


@dataclass
class ParsedCSV:
    rows: list[HospitalRow] = field(default_factory=list)
    errors: list[CSVRowError] = field(default_factory=list)
    total_rows: int = 0
    # True when row numbers came from a ``row`` column rather than file position.
    explicit_rows: bool = False

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_response(self) -> CSVValidationResponse:
        return CSVValidationResponse(
            valid=self.valid,
            total_hospitals=self.total_rows,
            errors=self.errors,
            hospitals=[
                HospitalRowPreview(row=r.row, name=r.name, address=r.address, phone=r.phone)
                for r in self.rows
            ],
        )


def _file_error(message: str) -> ParsedCSV:
    return ParsedCSV(errors=[CSVRowError(row=0, message=message)])


def parse_hospital_csv(
    content: bytes, max_rows: int, *, allow_row_column: bool = False
) -> ParsedCSV:
    """Parse ``name,address,phone`` CSV content.

    Rows are numbered from 1 starting at the first data row; blank lines are
    skipped. Errors on row 0 concern the file as a whole. With
    ``allow_row_column`` an optional ``row`` column overrides the numbering,
    which lets a correction file list only the rows it fixes.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _file_error("File must be UTF-8 encoded")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader, None)
        if not header or not any(cell.strip() for cell in header):
            return _file_error("CSV file is empty")

        columns = [cell.strip().lower() for cell in header]
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            return _file_error(f"Missing required column(s): {', '.join(missing)}")
        index = {name: columns.index(name) for name in ("name", "address", "phone") if name in columns}
        row_index = columns.index("row") if allow_row_column and "row" in columns else None

        result = ParsedCSV(explicit_rows=row_index is not None)
        seen_rows: set[int] = set()
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue
            result.total_rows += 1
            row_number = result.total_rows

            if row_index is not None:
                raw = record[row_index].strip() if row_index < len(record) else ""
                if not raw.isdigit() or int(raw) < 1:
                    result.errors.append(
                        CSVRowError(
                            row=row_number,
                            field="row",
                            message="'row' must be a positive integer",
                        )
                    )
                    continue
                row_number = int(raw)
                if row_number in seen_rows:
                    result.errors.append(
                        CSVRowError(
                            row=row_number, field="row", message="Row listed more than once"
                        )
                    )
                    continue
                seen_rows.add(row_number)

            if len(record) > len(columns):
                result.errors.append(
                    CSVRowError(
                        row=row_number,
                        message="Row has more values than header columns; quote values containing commas",
                    )
                )
                continue

            values = {name: record[i].strip() if i < len(record) else "" for name, i in index.items()}
            row_errors = [
                CSVRowError(row=row_number, field=name, message=f"'{name}' is required")
                for name in REQUIRED_COLUMNS
                if not values[name]
            ]
            if row_errors:
                result.errors.extend(row_errors)
                continue

            result.rows.append(
                HospitalRow(
                    row=row_number,
                    name=values["name"],
                    address=values["address"],
                    phone=values.get("phone") or None,
                )
            )
    except csv.Error as exc:
        return _file_error(f"Malformed CSV: {exc}")

    if result.total_rows == 0:
        result.errors.append(CSVRowError(row=0, message="CSV contains no hospital rows"))
    elif result.total_rows > max_rows:
        result.errors.append(
            CSVRowError(row=0, message=f"CSV contains {result.total_rows} hospitals; the maximum is {max_rows}")
        )
    return result
