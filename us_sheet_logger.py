"""Google Sheets logging dedicated to US stock signals."""

from __future__ import annotations

from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound


_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_ID = "1T_Yj8wSx2V0XoTmwTtqetZGXi495sc6Saj0bpRE85Rg"
_KEY_FILE = "gsheet_key.json"
_RECOMMENDATION_TITLE = "us_recommendations"
_OBSERVATION_TITLE = "us_observations"
_RECOMMENDATION_HEADER = ["일자(미국)", "티커", "종목명", "종가(USD)", "점수", "상태", "전략"]
_OBSERVATION_HEADER = ["최초관찰일", "최근관찰일", "티커", "종목명", "종가(USD)", "점수", "상태", "전략"]
_COOLDOWN_DAYS = 14


def _spreadsheet():
    credentials = Credentials.from_service_account_file(_KEY_FILE, scopes=_SCOPES)
    return gspread.authorize(credentials).open_by_key(_SHEET_ID)


def _worksheet(title: str, header: list[str]):
    spreadsheet = _spreadsheet()
    try:
        worksheet = spreadsheet.worksheet(title)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(header))
    if worksheet.row_values(1)[: len(header)] != header:
        end = gspread.utils.rowcol_to_a1(1, len(header)).rstrip("1")
        worksheet.update(range_name=f"A1:{end}1", values=[header])
        worksheet.freeze(rows=1)
        worksheet.format(
            f"A1:{end}1",
            {
                "backgroundColor": {"red": 0.07, "green": 0.25, "blue": 0.46},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            },
        )
    return worksheet


def _normalize_ticker(value: object) -> str:
    return str(value or "").strip().upper()


def log_recommendation(
    ticker: str,
    name: str,
    close_price: float,
    score: float,
    state: str,
    method: str,
    when: datetime,
    cooldown_days: int = _COOLDOWN_DAYS,
) -> bool:
    worksheet = _worksheet(_RECOMMENDATION_TITLE, _RECOMMENDATION_HEADER)
    values = worksheet.get_all_values()
    lower_bound = when.date() - timedelta(days=max(0, cooldown_days))
    for row in reversed(values[1:]):
        if len(row) < 2 or _normalize_ticker(row[1]) != _normalize_ticker(ticker):
            continue
        try:
            logged = datetime.strptime(row[0].strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        if lower_bound <= logged <= when.date():
            return False
    worksheet.append_row(
        [when.strftime("%Y-%m-%d"), _normalize_ticker(ticker), name, f"{close_price:.2f}", f"{score:.1f}", state, method],
        value_input_option="USER_ENTERED",
        table_range="A:G",
    )
    return True


def log_observation(
    ticker: str,
    name: str,
    close_price: float,
    score: float,
    state: str,
    method: str,
    when: datetime,
) -> None:
    worksheet = _worksheet(_OBSERVATION_TITLE, _OBSERVATION_HEADER)
    values = worksheet.get_all_values()
    first_seen = when.strftime("%Y-%m-%d")
    target_row = None
    for row_number, row in enumerate(values[1:], start=2):
        if len(row) >= 3 and _normalize_ticker(row[2]) == _normalize_ticker(ticker):
            target_row = row_number
            if row[0].strip():
                first_seen = row[0].strip()
            break
    payload = [[first_seen, when.strftime("%Y-%m-%d"), _normalize_ticker(ticker), name, f"{close_price:.2f}", f"{score:.1f}", state, method]]
    if target_row is None:
        worksheet.append_row(payload[0], value_input_option="USER_ENTERED", table_range="A:H")
    else:
        worksheet.update(range_name=f"A{target_row}:H{target_row}", values=payload, value_input_option="USER_ENTERED")
