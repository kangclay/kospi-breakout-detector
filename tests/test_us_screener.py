import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from alpaca_market_data import AlpacaMarketDataClient, AlpacaMarketDataError
from us_screener import USScreenConfig, _assign_actions, _market_regime, _score_rows, build_message


class AlpacaMarketDataTest(unittest.TestCase):
    def test_credentials_are_required(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(AlpacaMarketDataError, "ALPACA_API_KEY"):
                AlpacaMarketDataClient()

    def test_daily_bars_are_normalized_and_paginated(self):
        first = Mock()
        first.raise_for_status.return_value = None
        first.json.return_value = {
            "bars": {"AAPL": [{"t": "2026-11-20T05:00:00Z", "o": 100, "h": 105, "l": 99, "c": 104, "v": 1000}]},
            "next_page_token": "next",
        }
        second = Mock()
        second.raise_for_status.return_value = None
        second.json.return_value = {
            "bars": {"AAPL": [{"t": "2026-11-23T05:00:00Z", "o": 104, "h": 108, "l": 103, "c": 107, "v": 1200}]},
            "next_page_token": None,
        }
        session = Mock()
        session.get.side_effect = [first, second]

        client = AlpacaMarketDataClient("key", "secret", session=session, retry_backoff=0)
        result = client.daily_bars(["AAPL"], "2026-01-01", "2026-11-24")

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(result["AAPL"]), 2)
        self.assertEqual(float(result["AAPL"].iloc[-1].TradingValue), 107 * 1200)
        self.assertEqual(session.get.call_args_list[0].kwargs["params"]["feed"], "iex")
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["page_token"], "next")


class USScreenTest(unittest.TestCase):
    def test_market_regime_uses_qqq_60_and_200_day_averages(self):
        frame = pd.DataFrame({"Close": np.linspace(100, 220, 260)})
        self.assertEqual(_market_regime(frame)["state"], "ON")

    def test_scores_are_cross_sectional_and_bounded(self):
        frame = pd.DataFrame(
            {
                "ret_20": [0.05, 0.10, 0.20],
                "ret_60": [0.08, 0.15, 0.30],
                "ret_120": [0.10, 0.20, 0.40],
                "ret_200": [0.12, 0.25, 0.50],
                "close_vs_ma20": [0.01, 0.03, 0.05],
                "close_vs_ma60": [0.02, 0.05, 0.10],
                "ma20_vs_ma60": [0.01, 0.02, 0.04],
                "dist_high120": [-0.10, -0.05, -0.01],
                "volatility20": [0.30, 0.20, 0.25],
                "drawdown60": [-0.15, -0.08, -0.03],
                "avg_trading_value20": [30e6, 50e6, 100e6],
            }
        )
        scored = _score_rows(frame)
        self.assertTrue(scored["score"].between(0, 100).all())
        self.assertGreater(float(scored.iloc[2].score), float(scored.iloc[0].score))

    def test_actions_keep_off_regime_as_watch_only(self):
        base = pd.DataFrame(
            {
                "trend_ok": [True],
                "not_chasing": [True],
                "score": [95.0],
                "close": [100.0],
                "avg_trading_value20": [100e6],
                "breakout20": [True],
                "volume_vs20": [2.0],
            }
        )
        self.assertEqual(_assign_actions(base, USScreenConfig(), "ON").iloc[0].action, "US_BUY_CANDIDATE")
        self.assertEqual(_assign_actions(base, USScreenConfig(), "OFF").iloc[0].action, "US_OFF_WATCH")

    def test_message_explicitly_reports_empty_sections(self):
        message = build_message(
            pd.DataFrame(),
            {"asof_date": "2026-11-24", "regime": {"state": "ON"}},
        )
        self.assertIn("오늘의 US_BUY_CANDIDATE가 없습니다.", message)
        self.assertIn("자동 주문 없음", message)


if __name__ == "__main__":
    unittest.main()
