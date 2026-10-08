import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from daily_screener import ScreenConfig, _add_cross_sectional_scores, _fundamental_scores, _log_sheet, _send_telegram, build_price_features
from krx_open_api import KRXOpenAPIClient


def _make_ohlcv(rows=260):
    dates = pd.date_range("2025-01-01", periods=rows, freq="B")
    close = pd.Series(np.linspace(100, 180, rows), index=dates)
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": close.values - 1,
            "High": close.values + 2,
            "Low": close.values - 2,
            "Close": close.values,
            "Volume": np.full(rows, 1_000_000),
        }
    )


class DailyScreenerTest(unittest.TestCase):
    def test_krx_open_api_normalizes_daily_market_snapshot(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "OutBlock_1": [
                {
                    "BAS_DD": "20261008",
                    "ISU_CD": "KR7005930003",
                    "ISU_NM": "삼성전자",
                    "TDD_OPNPRC": "70,000",
                    "TDD_HGPRC": "71,000",
                    "TDD_LWPRC": "69,500",
                    "TDD_CLSPRC": "70,500",
                    "ACC_TRDVOL": "1,000,000",
                    "ACC_TRDVAL": "70,500,000,000",
                    "MKTCAP": "420,000,000,000,000",
                }
            ]
        }
        session = Mock()
        session.get.return_value = response
        frame = KRXOpenAPIClient("test-key", request_sleep=0, session=session).market_daily("KOSPI", "20261008")

        self.assertEqual(frame.iloc[0].ticker, "005930")
        self.assertEqual(frame.iloc[0]["name"], "삼성전자")
        self.assertEqual(float(frame.iloc[0].Close), 70_500.0)
        self.assertEqual(float(frame.iloc[0].market_cap), 420_000_000_000_000.0)

    def test_krx_open_api_history_filters_each_daily_snapshot(self):
        client = KRXOpenAPIClient("test-key", request_sleep=0, max_workers=2)
        calls = []

        def fake_daily(market, date):
            calls.append((market, date))
            return pd.DataFrame(
                {
                    "ticker": ["005930", "000660"],
                    "Date": [pd.Timestamp(date), pd.Timestamp(date)],
                    "Close": [70_000, 100_000],
                }
            )

        client.market_daily = fake_daily
        history = client.market_history("KOSPI", "20261001", "20261003", tickers=["005930"])

        self.assertEqual(len(calls), 3)
        self.assertEqual(history["ticker"].unique().tolist(), ["005930"])
        self.assertEqual(len(history), 3)

    def test_build_price_features_has_trend_and_atr(self):
        features = build_price_features(_make_ohlcv())
        self.assertTrue(features["trend_ok"])
        self.assertTrue(features["not_chasing"])
        self.assertGreater(features["atr_pct"], 0)
        self.assertGreater(features["ret_200"], 0)

    def test_scores_are_ranked_and_missing_fundamentals_are_allowed(self):
        frame = pd.DataFrame(
            {
                "ret_20": [0.1, 0.2],
                "ret_60": [0.2, 0.1],
                "ret_120": [0.3, 0.2],
                "ret_200": [0.4, 0.1],
                "dist_high120": [-0.01, -0.10],
                "ma20_vs_ma60": [0.04, 0.01],
                "per_positive": [10.0, 20.0],
                "pbr_positive": [1.0, 2.0],
                "dividend_yield": [1.0, 0.5],
                "volatility20": [0.2, 0.4],
                "drawdown60": [-0.05, -0.20],
                "quality_score": [np.nan, np.nan],
                "catalyst_score": [np.nan, np.nan],
            }
        )
        scored = _add_cross_sectional_scores(frame)
        self.assertEqual(len(scored), 2)
        self.assertTrue(scored["score"].notna().all())
        self.assertTrue(np.allclose(scored["factor_coverage"], 0.7))
        self.assertGreaterEqual(float(scored.iloc[0]["score"]), float(scored.iloc[1]["score"]))

    def test_default_entry_threshold_is_calibrated_to_82(self):
        config = ScreenConfig()
        self.assertEqual(config.min_score, 82.0)
        self.assertEqual(config.watch_score, 75.0)
        self.assertEqual(config.min_factor_coverage, 0.70)

    def test_fundamental_scores_are_computed(self):
        frame = pd.DataFrame(
            {
                "roe": [10.0, 5.0],
                "roic": [8.0, 2.0],
                "operating_margin": [12.0, 4.0],
                "debt_ratio": [30.0, 80.0],
                "sales_yoy": [5.0, -2.0],
                "op_profit_yoy": [10.0, -10.0],
            }
        )
        scored = _fundamental_scores(frame)
        self.assertTrue((scored["quality_score"] > 0).all())
        self.assertTrue((scored["catalyst_score"] > 0).all())

    def test_telegram_http_error_does_not_fail_screening(self):
        import os
        import requests

        response = Mock(status_code=400)
        response.json.return_value = {"description": "Bad Request: chat not found"}
        error = requests.HTTPError(response=response)
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123"}, clear=False), patch("requests.post", side_effect=error):
            self.assertFalse(_send_telegram("test"))

    def test_sheet_log_skips_empty_result_without_action_column(self):
        # This is the exact shape returned when neither market yields rows.
        _log_sheet(pd.DataFrame())


if __name__ == "__main__":
    unittest.main()
