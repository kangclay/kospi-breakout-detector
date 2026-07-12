import unittest

import numpy as np
import pandas as pd

from daily_screener import _add_cross_sectional_scores, _fundamental_scores, build_price_features


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
        self.assertGreaterEqual(float(scored.iloc[0]["score"]), float(scored.iloc[1]["score"]))

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


if __name__ == "__main__":
    unittest.main()
