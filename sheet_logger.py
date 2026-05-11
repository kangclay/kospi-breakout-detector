# sheet_logger.py
from datetime import datetime
import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_ID = "1T_Yj8wSx2V0XoTmwTtqetZGXi495sc6Saj0bpRE85Rg"          # /d/와 /edit 사이
_KEY_FILE = "gsheet_key.json"          # 경로·파일명 맞추기
_RECOMMENDATION_SHEET_TITLE = "recommendations"
_RECOMMENDATION_HEADER = ["일자", "티커", "종목명", "종가", "전략"]

def _get_sheet():
    creds = Credentials.from_service_account_file(_KEY_FILE, scopes=_SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(_SHEET_ID)
    try:
        worksheet = spreadsheet.worksheet(_RECOMMENDATION_SHEET_TITLE)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=_RECOMMENDATION_SHEET_TITLE,
            rows=1000,
            cols=len(_RECOMMENDATION_HEADER),
        )

    if worksheet.row_values(1) != _RECOMMENDATION_HEADER:
        worksheet.update("A1:E1", [_RECOMMENDATION_HEADER])
    return worksheet

def log_selection(ticker: str,
                  close_price: float,
                  method: str,
                  when: datetime,
                  name: str = ""):
    row = [
        when.strftime("%Y-%m-%d"),
        ticker,
        name or ticker,
        f"{close_price:.2f}",
        method,
    ]
    _get_sheet().append_row(row, value_input_option="USER_ENTERED", table_range="A:E")
