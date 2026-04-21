import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from strategy_engine import (
    StrategyConfig,
    build_quant_surge_strategy,
    load_strategy_config,
    preset_requires_golden_cross,
    summarize_evaluations,
)


class StrategyEngineTest(unittest.TestCase):
    def test_preset_requires_golden_cross_only_for_gc_based_presets(self):
        self.assertTrue(preset_requires_golden_cross("macd_gc"))
        self.assertTrue(preset_requires_golden_cross("strict_union"))
        self.assertFalse(preset_requires_golden_cross("trend_ma_union"))
        self.assertFalse(preset_requires_golden_cross("ma2060_atr"))
        self.assertFalse(preset_requires_golden_cross("quant_surge_common"))

    def test_build_quant_surge_strategy_defaults(self):
        strategy = build_quant_surge_strategy()
        self.assertEqual(strategy.entry_set, "quant_surge_common")
        self.assertEqual(strategy.vol_mult, 1.2)
        self.assertEqual(strategy.stop_pct, 0.1)
        self.assertEqual(strategy.max_hold, 15)

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
