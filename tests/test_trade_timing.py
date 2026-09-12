import unittest

import numpy as np
import pandas as pd

from trade_timing import Position, _batch_input, entry_timing, position_timing
from timing_runner import build_message, parse_position_rows, parse_recommendation_rows


def _prices(rows: int = 70) -> pd.DataFrame:
    dates = pd.date_range("2026-01-02", periods=rows, freq="B")
    close = np.linspace(100.0, 169.0, rows)
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": close - 0.5,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": np.full(rows, 1_000_000.0),
        }
    )


class TradeTimingTest(unittest.TestCase):
    def test_entry_timing_marks_confirmed_breakout_buyable(self):
        frame = _prices()
        frame.loc[frame.index[-1], "Volume"] = 1_500_000.0
        result = entry_timing(frame)
        self.assertEqual(result["action"], "BUY_NOW")
        self.assertLess(result["initial_stop"], result["close"])

    def test_entry_timing_requires_history(self):
        result = entry_timing(_prices(30))
        self.assertEqual(result["action"], "INSUFFICIENT_DATA")

    def test_position_timing_uses_open_price_for_gap_stop(self):
        frame = _prices(75)
        entry_index = 60
        entry_date = frame.loc[entry_index, "Date"].strftime("%Y-%m-%d")
        entry_price = float(frame.loc[entry_index, "Close"])
        # After the high water mark has raised the ATR trail, the next session
        # gaps under that prior stop.  The model must exit at the opening price.
        frame.loc[74, ["Open", "High", "Low", "Close"]] = [150.0, 152.0, 149.0, 151.0]
        result = position_timing(frame, Position(entry_date, entry_price))
        self.assertEqual(result["action"], "EXITED")
        self.assertEqual(result["reason"], "갭하락으로 시가 청산")
        self.assertEqual(result["exit_price"], 150.0)

    def test_positions_csv_accepts_recommendation_sheet_headers(self):
        with self.subTest("korean headers"):
            path = self._write_csv("일자,티커,종목명,종가\n2026-09-01,5930,삼성전자,70000\n")
            rows = _batch_input(str(path), positions=True)
            self.assertEqual(rows, [{"ticker": "005930", "name": "삼성전자", "entry_date": "2026-09-01", "entry_price": 70000.0}])

    def test_recommendation_rows_keep_latest_per_ticker_inside_window(self):
        rows = [
            ["일자", "티커", "종목명", "종가", "전략"],
            ["2026-08-01", "5930", "삼성전자", "70000", "old"],
            ["2026-09-01", "5930", "삼성전자", "71000", "new"],
            ["2026-09-03", "660", "SK하이닉스", "200000", "factor"],
        ]
        result = parse_recommendation_rows(rows, "2026-09-10", 20)
        self.assertEqual([row["ticker"] for row in result], ["000660", "005930"])
        self.assertEqual(next(row for row in result if row["ticker"] == "005930")["strategy"], "new")

    def test_position_rows_accepts_manual_input_template(self):
        rows = [
            ["진입일", "티커", "종목명(선택)", "진입가(원)", "메모(선택)"],
            ["2026-09-01", "5930", "삼성전자", "71,000", "테스트"],
        ]
        self.assertEqual(
            parse_position_rows(rows),
            [{"ticker": "005930", "name": "삼성전자", "entry_date": "2026-09-01", "entry_price": 71000.0, "note": "테스트"}],
        )

    def test_message_is_distinct_and_only_lists_actionable_signals(self):
        message = build_message(
            [{"ticker": "005930", "name": "삼성전자", "action": "BUY_NOW", "initial_stop": 65000}],
            [{"ticker": "000660", "name": "SK하이닉스", "action": "EXITED", "exit_date": "2026-09-10", "exit_price": 190000}],
            "2026-09-10",
        )
        self.assertIn("독립 매수·매도 타이밍", message)
        self.assertIn("BUY_NOW", message)
        self.assertIn("모의 청산 조건", message)

    def test_batch_position_result_keeps_its_input_identity(self):
        from unittest.mock import patch
        from trade_timing import batch_timing

        with patch("trade_timing._fetch_ohlcv", return_value=_prices(75)):
            result = batch_timing(
                [{"ticker": "005930", "name": "삼성전자", "entry_date": "2026-03-27", "entry_price": 160.0}],
                "2026-04-15",
                positions=True,
            )
        self.assertEqual(result[0]["entry_date"], "2026-03-27")
        self.assertEqual(result[0]["entry_price"], 160.0)

    def _write_csv(self, content):
        import tempfile
        from pathlib import Path

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "positions.csv"
        path.write_text(content, encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
