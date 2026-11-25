"""Google Sheets logging for actionable recommendations and watch-only signals."""

from __future__ import annotations

from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound


_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_ID = "1T_Yj8wSx2V0XoTmwTtqetZGXi495sc6Saj0bpRE85Rg"
_KEY_FILE = "gsheet_key.json"

_RECOMMENDATION_SHEET_TITLE = "recommendations"
_RECOMMENDATION_HEADER = ["일자", "티커", "종목명", "종가", "전략"]
_OBSERVATION_SHEET_TITLE = "observations"
_OBSERVATION_HEADER = ["최초관찰일", "최근관찰일", "티커", "종목명", "종가", "상태", "전략"]
_RECOMMENDATION_COOLDOWN_DAYS = 14


def _get_spreadsheet():
    creds = Credentials.from_service_account_file(_KEY_FILE, scopes=_SCOPES)
    return gspread.authorize(creds).open_by_key(_SHEET_ID)


def _get_or_create_worksheet(title: str, header: list[str]):
    spreadsheet = _get_spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(title)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(header))

    existing_header = worksheet.row_values(1)
    if not existing_header:
        worksheet.update(range_name="A1", values=[header])
        worksheet.freeze(rows=1)
    elif existing_header[: len(header)] != header:
        # Preserve any legacy columns to the right while repairing the stable
        # columns used by this logger.
        end_column = gspread.utils.rowcol_to_a1(1, len(header)).rstrip("1")
        worksheet.update(range_name=f"A1:{end_column}1", values=[header])
    return worksheet


def _normalized_ticker(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _has_recent_recommendation(worksheet, ticker: str, when: datetime, cooldown_days: int) -> bool:
    values = worksheet.get_all_values()
    if len(values) <= 1:
        return False
    header = [str(value).strip() for value in values[0]]
    try:
        date_index = header.index("일자")
        ticker_index = header.index("티커")
    except ValueError:
        return False

    lower_bound = when.date() - timedelta(days=max(0, cooldown_days))
    for row in reversed(values[1:]):
        if ticker_index >= len(row) or _normalized_ticker(row[ticker_index]) != ticker:
            continue
        try:
            logged_date = datetime.strptime(row[date_index].strip(), "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue
        if lower_bound <= logged_date <= when.date():
            return True
    return False


def log_selection(
    ticker: str,
    close_price: float,
    method: str,
    when: datetime,
    name: str = "",
    cooldown_days: int = _RECOMMENDATION_COOLDOWN_DAYS,
) -> bool:
    """Append one actionable recommendation unless the ticker is cooling down."""
    normalized = _normalized_ticker(ticker)
    worksheet = _get_or_create_worksheet(_RECOMMENDATION_SHEET_TITLE, _RECOMMENDATION_HEADER)
    if _has_recent_recommendation(worksheet, normalized, when, cooldown_days):
        return False
    worksheet.append_row(
        [when.strftime("%Y-%m-%d"), normalized, name or normalized, f"{close_price:.2f}", method],
        value_input_option="USER_ENTERED",
        table_range="A:E",
    )
    return True


def log_observation(
    ticker: str,
    close_price: float,
    state: str,
    method: str,
    when: datetime,
    name: str = "",
) -> None:
    """Upsert one latest watch-only row per ticker instead of appending duplicates."""
    normalized = _normalized_ticker(ticker)
    worksheet = _get_or_create_worksheet(_OBSERVATION_SHEET_TITLE, _OBSERVATION_HEADER)
    values = worksheet.get_all_values()
    first_seen = when.strftime("%Y-%m-%d")
    target_row = None
    if values:
        header = [str(value).strip() for value in values[0]]
        try:
            ticker_index = header.index("티커")
            first_seen_index = header.index("최초관찰일")
        except ValueError:
            ticker_index = first_seen_index = -1
        if ticker_index >= 0:
            for row_number, row in enumerate(values[1:], start=2):
                if ticker_index < len(row) and _normalized_ticker(row[ticker_index]) == normalized:
                    target_row = row_number
                    if first_seen_index < len(row) and row[first_seen_index].strip():
                        first_seen = row[first_seen_index].strip()
                    break

    payload = [[
        first_seen,
        when.strftime("%Y-%m-%d"),
        normalized,
        name or normalized,
        f"{close_price:.2f}",
        state,
        method,
    ]]
    if target_row is None:
        worksheet.append_row(payload[0], value_input_option="USER_ENTERED", table_range="A:G")
    else:
        worksheet.update(
            range_name=f"A{target_row}:G{target_row}",
            values=payload,
            value_input_option="USER_ENTERED",
        )
