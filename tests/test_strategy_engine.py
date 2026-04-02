import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from strategy_engine import StrategyConfig, load_strategy_config, summarize_evaluations


class StrategyEngineTest(unittest.TestCase):
    def test_load_strategy_config_reads_json(self):
        payload = {
            "entry_set": "strict_union",
            "stop_pct": 0.1,
            "max_hold": 15,
            "vol_mult": 1.8,
            "entry": "next_open",
            "macd_zero_filter": True,
            "market": "KOSPI",
            "days": 365,
            "fee_bp": 5.0,
            "slip_bp": 5.0,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "best_strategy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = load_strategy_config(path)
        self.assertEqual(config.entry_set, "strict_union")
        self.assertEqual(config.max_hold, 15)
        self.assertTrue(config.macd_zero_filter)

    def test_summarize_evaluations_aggregates_metrics(self):
        rows = [
            {
                "trade_returns": [0.05, -0.02],
                "trailing_stop_count": 1,
                "dead_cross_count": 0,
                "max_hold_count": 1,
            },
            {
                "trade_returns": [0.03],
                "trailing_stop_count": 0,
                "dead_cross_count": 1,
                "max_hold_count": 0,
            },
        ]
        summary = summarize_evaluations(rows, StrategyConfig())
        self.assertIsNotNone(summary)
        self.assertEqual(summary["trade_count"], 3)
        self.assertEqual(summary["active_tickers"], 2)
        self.assertEqual(summary["trailing_stop_count"], 1)
        self.assertEqual(summary["dead_cross_count"], 1)
        self.assertEqual(summary["max_hold_count"], 1)
        self.assertEqual(summary["profit_factor"], 4.0)


if __name__ == "__main__":
    unittest.main()
