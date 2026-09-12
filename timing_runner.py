"""Run the independent timing engine with Google Sheets and Telegram outputs.

The daily multi-factor screener remains the source of *what to research*.
This runner only reads its ``recommendations`` tab, then writes separate,
clearly-labelled timing tabs.  Actual holdings are read exclusively from the
``타이밍엔진_보유입력`` tab and are never inferred from a recommendation.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from daily_screener import _resolve_asof_date
from trade_timing import TimingConfig, _as_float, _json_safe, _ticker, batch_timing


DEFAULT_SHEET_ID = "1T_Yj8wSx2V0XoTmwTtqetZGXi495sc6Saj0bpRE85Rg"
SOURCE_TAB = "recommendations"
POSITIONS_INPUT_TAB = "타이밍엔진_보유입력"
ENTRY_OUTPUT_TAB = "타이밍엔진_매수신호"
POSITION_OUTPUT_TAB = "타이밍엔진_보유신호"
GENERATED_MARKER = "⚙️ 독립 타이밍 엔진 자동 생성 탭 — 직접 수정하지 마세요"

POSITIONS_HEADER = ["진입일", "티커", "종목명(선택)", "진입가(원)", "메모(선택)"]
ENTRY_HEADER = [
    "기준일", "추천일", "티커", "종목명", "원본 전략", "매수 신호", "판단 근거",
    "종가", "ATR14", "MA20", "MA60", "최초 스탑가", "1주당 리스크",
]
POSITION_HEADER = [
    "기준일", "진입일", "티커", "종목명", "진입가", "보유 신호", "판단 근거",
    "평가 종가/청산가", "최초 스탑가", "현재 스탑가", "최고가", "평가수익률", "청산일", "메모",
]


def _column_index(header: Iterable[object], choices: Iterable[str]) -> Optional[int]:
    normalized = {str(value).strip().lower(): index for index, value in enumerate(header)}
    for choice in choices:
        if choice.lower() in normalized:
            return normalized[choice.lower()]
    return None


def _value(row: list[str], index: Optional[int]) -> str:
    return row[index].strip() if index is not None and index < len(row) else ""


def _format_number(value: object, digits: int = 0) -> str:
    number = _as_float(value)
    return "" if not np.isfinite(number) else f"{number:,.{digits}f}"


def _format_percent(value: object) -> str:
    number = _as_float(value)
    return "" if not np.isfinite(number) else f"{number * 100:.1f}%"


def parse_recommendation_rows(
    values: list[list[str]],
    as_of_date: str,
    lookback_days: int,
    strategy_prefix: str = "",
) -> list[dict]:
    """Return one latest recommendation per ticker inside the lookback window."""
    if not values:
        return []
    header = values[0]
    date_index = _column_index(header, ["추천일", "일자", "date"])
    ticker_index = _column_index(header, ["티커", "ticker"])
    name_index = _column_index(header, ["종목명", "name"])
    close_index = _column_index(header, ["종가", "close", "추천가"])
    strategy_index = _column_index(header, ["전략", "strategy"])
    if date_index is None or ticker_index is None:
        raise ValueError(f"{SOURCE_TAB} 탭에 일자와 티커 열이 필요합니다.")

    cutoff = pd.Timestamp(as_of_date) - pd.Timedelta(days=lookback_days)
    latest: dict[str, dict] = {}
    for row in values[1:]:
        ticker = _ticker(_value(row, ticker_index))
        when = pd.to_datetime(_value(row, date_index), errors="coerce")
        if not ticker or pd.isna(when) or when > pd.Timestamp(as_of_date) or when < cutoff:
            continue
        strategy = _value(row, strategy_index)
        if strategy_prefix and not strategy.startswith(strategy_prefix):
            continue
        candidate = {
            "ticker": ticker,
            "name": _value(row, name_index) or ticker,
            "recommendation_date": when.strftime("%Y-%m-%d"),
            "recommendation_close": _as_float(_value(row, close_index)),
            "strategy": strategy,
        }
        previous = latest.get(ticker)
        if previous is None or candidate["recommendation_date"] >= previous["recommendation_date"]:
            latest[ticker] = candidate
    return sorted(latest.values(), key=lambda item: (item["recommendation_date"], item["ticker"]), reverse=True)


def parse_position_rows(values: list[list[str]]) -> list[dict]:
    """Parse the user-maintained holdings tab without changing its contents."""
    if not values:
        return []
    header = values[0]
    date_index = _column_index(header, ["진입일", "entry_date"])
    ticker_index = _column_index(header, ["티커", "ticker"])
    name_index = _column_index(header, ["종목명(선택)", "종목명", "name"])
    price_index = _column_index(header, ["진입가(원)", "진입가", "entry_price"])
    note_index = _column_index(header, ["메모(선택)", "메모", "note"])
    if date_index is None or ticker_index is None or price_index is None:
        raise ValueError(f"{POSITIONS_INPUT_TAB} 탭에 진입일, 티커, 진입가 열이 필요합니다.")

    result = []
    for row in values[1:]:
        ticker = _ticker(_value(row, ticker_index))
        entry_date = pd.to_datetime(_value(row, date_index), errors="coerce")
        entry_price = _as_float(_value(row, price_index).replace(",", ""))
        if not ticker or pd.isna(entry_date) or not np.isfinite(entry_price) or entry_price <= 0:
            continue
        result.append({
            "ticker": ticker,
            "name": _value(row, name_index) or ticker,
            "entry_date": entry_date.strftime("%Y-%m-%d"),
            "entry_price": entry_price,
            "note": _value(row, note_index),
        })
    return result


def _entry_output_rows(results: list[dict], recommendations: list[dict], as_of_date: str) -> list[list[str]]:
    source = {row["ticker"]: row for row in recommendations}
    rows = []
    for result in results:
        recommendation = source.get(result["ticker"], {})
        rows.append([
            as_of_date, recommendation.get("recommendation_date", ""), result["ticker"], result.get("name", ""),
            recommendation.get("strategy", ""), result.get("action", ""), result.get("reason", ""),
            _format_number(result.get("close")), _format_number(result.get("atr14"), 1),
            _format_number(result.get("ma20")), _format_number(result.get("ma60")),
            _format_number(result.get("initial_stop")), _format_number(result.get("risk_per_share")),
        ])
    return rows


def _position_output_rows(results: list[dict], positions: list[dict], as_of_date: str) -> list[list[str]]:
    source = {(row["ticker"], row["entry_date"], row["entry_price"]): row for row in positions}
    rows = []
    for result in results:
        key = (result["ticker"], result.get("entry_date"), result.get("entry_price"))
        position = source.get(key)
        if position is None:
            position = next((row for row in positions if row["ticker"] == result["ticker"]), {})
        market_price = result.get("exit_price") if result.get("action") == "EXITED" else result.get("close")
        rows.append([
            as_of_date, position.get("entry_date", result.get("entry_date", "")), result["ticker"], result.get("name", ""),
            _format_number(position.get("entry_price", result.get("entry_price"))), result.get("action", ""), result.get("reason", ""),
            _format_number(market_price), _format_number(result.get("initial_stop")), _format_number(result.get("stop_price")),
            _format_number(result.get("highest_high")), _format_percent(result.get("unrealized_return")),
            result.get("exit_date", ""), position.get("note", ""),
        ])
    return rows


def build_message(entry_results: list[dict], position_results: list[dict], as_of_date: str) -> str:
    """Make a deliberately separate, concise Telegram message."""
    lines = [f"⏱️ 독립 매수·매도 타이밍 | 기준일 {as_of_date}"]
    buyable = [row for row in entry_results if row.get("action") in {"BUY_NOW", "BUY_PULLBACK"}]
    lines.append(f"매수 실행 신호: {len(buyable)}건 (후보 분석: {len(entry_results)}건)")
    if buyable:
        for row in buyable[:8]:
            lines.append(f"- {row.get('name') or row['ticker']} ({row['ticker']}): {row['action']} · 스탑 {_format_number(row.get('initial_stop'))}원")
    else:
        lines.append("- 오늘은 신규 매수 실행 신호 없음")

    exits = [row for row in position_results if row.get("action") == "EXITED"]
    trailing = [row for row in position_results if row.get("action") == "TRAIL_ACTIVE"]
    lines.append(f"보유 관리: 청산 조건 {len(exits)}건 · 트레일 적용 {len(trailing)}건")
    for row in exits[:8]:
        lines.append(f"- {row.get('name') or row['ticker']} ({row['ticker']}): 모의 청산 조건 · {row.get('exit_date', '')} {_format_number(row.get('exit_price'))}원")
    for row in trailing[:5]:
        lines.append(f"- {row.get('name') or row['ticker']} ({row['ticker']}): TRAIL_ACTIVE · 현재 스탑 {_format_number(row.get('stop_price'))}원")
    lines.append("※ 주문은 자동 실행되지 않으며, 독립 타이밍 엔진의 일봉 기준 신호입니다.")
    return "\n".join(lines)


def _get_spreadsheet(sheet_id: str, key_file: str):
    import gspread
    from google.oauth2.service_account import Credentials

    credentials = Credentials.from_service_account_file(
        key_file, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return gspread.authorize(credentials).open_by_key(sheet_id)


def _worksheet(spreadsheet, title: str, rows: int, cols: int):
    from gspread.exceptions import WorksheetNotFound

    try:
        return spreadsheet.worksheet(title)
    except WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def _ensure_positions_input(spreadsheet) -> list[list[str]]:
    worksheet = _worksheet(spreadsheet, POSITIONS_INPUT_TAB, rows=200, cols=len(POSITIONS_HEADER))
    values = worksheet.get_all_values()
    if not values:
        worksheet.update(range_name="A1:E1", values=[POSITIONS_HEADER])
        worksheet.format("A1:E1", {"backgroundColor": {"red": 0.12, "green": 0.26, "blue": 0.42}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
        worksheet.freeze(rows=1)
        return [POSITIONS_HEADER]
    if values[0] != POSITIONS_HEADER:
        raise ValueError(f"{POSITIONS_INPUT_TAB} 탭 헤더가 예상 형식과 다릅니다. 수동 입력값을 보호하기 위해 중단합니다.")
    return values


def _write_generated_tab(spreadsheet, title: str, header: list[str], rows: list[list[str]]) -> None:
    worksheet = _worksheet(spreadsheet, title, rows=max(200, len(rows) + 5), cols=len(header))
    existing = worksheet.get_all_values()
    if existing and existing[0] and existing[0][0] != GENERATED_MARKER:
        raise ValueError(f"{title} 탭이 타이밍 엔진 탭으로 확인되지 않아 덮어쓰지 않습니다.")
    worksheet.clear()
    worksheet.update(range_name="A1", values=[[GENERATED_MARKER]])
    worksheet.update(range_name=f"A2:{chr(64 + len(header))}2", values=[header])
    if rows:
        worksheet.update(range_name=f"A3:{chr(64 + len(header))}{len(rows) + 2}", values=rows)
    worksheet.format("A1:Z1", {"backgroundColor": {"red": 0.18, "green": 0.18, "blue": 0.18}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
    worksheet.format(f"A2:{chr(64 + len(header))}2", {"backgroundColor": {"red": 0.11, "green": 0.45, "blue": 0.33}, "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
    worksheet.freeze(rows=2)


def _send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] TELEGRAM_TOKEN 또는 TELEGRAM_CHAT_ID가 없어 알림을 건너뜁니다.")
        return
    import requests

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message[:3900]},
        timeout=15,
    )
    response.raise_for_status()


def _write_reports(payload: dict, entry_rows: list[list[str]], position_rows: list[list[str]], output_dir: str) -> None:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "timing_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_safe) + "\n", encoding="utf-8"
    )
    pd.DataFrame(entry_rows, columns=ENTRY_HEADER).to_csv(directory / "entry_signals.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(position_rows, columns=POSITION_HEADER).to_csv(directory / "position_signals.csv", index=False, encoding="utf-8-sig")


def run(
    sheet_id: str,
    key_file: str,
    as_of_date: str,
    lookback_days: int,
    strategy_prefix: str,
    write_sheet: bool,
    notify: bool,
    output_dir: str,
) -> dict:
    spreadsheet = _get_spreadsheet(sheet_id, key_file)
    recommendations = parse_recommendation_rows(
        spreadsheet.worksheet(SOURCE_TAB).get_all_values(), as_of_date, lookback_days, strategy_prefix
    )
    positions_values = _ensure_positions_input(spreadsheet)
    positions = parse_position_rows(positions_values)

    config = TimingConfig()
    entry_results = batch_timing(recommendations, as_of_date, positions=False, config=config)
    position_results = batch_timing(positions, as_of_date, positions=True, config=config)
    entry_rows = _entry_output_rows(entry_results, recommendations, as_of_date)
    position_rows = _position_output_rows(position_results, positions, as_of_date)
    message = build_message(entry_results, position_results, as_of_date)
    payload = {
        "as_of_date": as_of_date,
        "source_lookback_days": lookback_days,
        "source_strategy_prefix": strategy_prefix,
        "config": asdict(config),
        "recommendation_count": len(recommendations),
        "position_count": len(positions),
        "entry_results": entry_results,
        "position_results": position_results,
    }
    _write_reports(payload, entry_rows, position_rows, output_dir)
    if write_sheet:
        _write_generated_tab(spreadsheet, ENTRY_OUTPUT_TAB, ENTRY_HEADER, entry_rows)
        _write_generated_tab(spreadsheet, POSITION_OUTPUT_TAB, POSITION_HEADER, position_rows)
    if notify:
        _send_telegram(message)
    return {"message": message, **payload}


def main() -> None:
    parser = argparse.ArgumentParser(description="독립 매수·매도 타이밍을 Google Sheets·Telegram에 연동합니다.")
    parser.add_argument("--as-of-date", default="", help="기준일 YYYYMMDD. 비우면 최근 거래일")
    parser.add_argument("--source-lookback-days", type=int, default=20, help="추천 원본을 읽을 최근 일수")
    parser.add_argument("--strategy-prefix", default="daily_multifactor:", help="기존 recommendations 탭에서 읽을 전략 접두사")
    parser.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    parser.add_argument("--key-file", default="gsheet_key.json")
    parser.add_argument("--output-dir", default="reports/timing")
    parser.add_argument("--write-sheet", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    resolved_as_of = _resolve_asof_date(args.as_of_date.strip() or None, ("KOSPI",))
    as_of_date = pd.Timestamp(resolved_as_of).strftime("%Y-%m-%d")
    result = run(
        sheet_id=args.sheet_id,
        key_file=args.key_file,
        as_of_date=as_of_date,
        lookback_days=args.source_lookback_days,
        strategy_prefix=args.strategy_prefix,
        write_sheet=args.write_sheet,
        notify=args.notify,
        output_dir=args.output_dir,
    )
    print(result["message"])
    print(f"Saved {args.output_dir}")


if __name__ == "__main__":
    main()
