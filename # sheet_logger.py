# sheet_logger.py
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_SHEET_ID = "1T_Yj8wSx2V0XoTmwTtqetZGXi495sc6Saj0bpRE85Rg"          # /d/와 /edit 사이
_KEY_FILE = "gsheet_key.json"          # 경로·파일명 맞추기

def _get_sheet():
    creds = Credentials.from_service_account_file(_KEY_FILE, scopes=_SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(_SHEET_ID).sheet1          # 첫 번째 탭

def log_selection(ticker: str,
                  close_price: float,
                  method: str,
                  when: datetime):
    row = [
        when.strftime("%Y-%m-%d"),
        ticker,
        method,
        f"{close_price:.2f}",
    ]
    _get_sheet().append_row(row, value_input_option="USER_ENTERED")