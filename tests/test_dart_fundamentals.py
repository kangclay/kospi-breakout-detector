import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

from dart_fundamentals import OpenDARTClient, _metric_name, load_dart_fundamentals, report_period_for


class DartFundamentalsTest(unittest.TestCase):
    def test_client_retries_a_connect_timeout(self):
        response = Mock(status_code=200)
        session = Mock()
        session.get.side_effect = [requests.ConnectTimeout(), response]
        client = OpenDARTClient(
            "test-key",
            session=session,
            request_retries=2,
            retry_backoff=0.01,
        )

        with patch("dart_fundamentals.time.sleep") as sleep:
            result = client._get("corpCode.xml", {})

        self.assertIs(result, response)
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_report_period_uses_latest_conservatively_available_filing(self):
        half_year = report_period_for("2026-10-21")
        self.assertEqual((half_year.business_year, half_year.report_code), (2026, "11012"))

        previous_q3 = report_period_for("2026-03-01")
        self.assertEqual((previous_q3.business_year, previous_q3.report_code), (2025, "11014"))

    def test_metric_names_are_normalized(self):
        self.assertEqual(_metric_name("자기자본이익률(ROE)"), "roe")
        self.assertEqual(_metric_name("영업이익율"), "operating_margin")
        self.assertEqual(_metric_name("부채비율"), "debt_ratio")
        self.assertEqual(_metric_name("매출액 증가율"), "sales_yoy")
        self.assertEqual(_metric_name("영업이익 증가율"), "op_profit_yoy")

    @patch("dart_fundamentals.OpenDARTClient.financial_indices")
    @patch("dart_fundamentals.OpenDARTClient.corporation_codes")
    def test_snapshot_maps_financial_indices_to_listed_ticker(self, corp_codes, indicators):
        corp_codes.return_value = pd.DataFrame(
            [
                {"ticker": "005930", "corp_code": "00126380"},
                {"ticker": "000660", "corp_code": "00164779"},
            ]
        )
        indicators.return_value = [
            {"corp_code": "00126380", "stock_code": "005930", "idx_nm": "자기자본이익률", "idx_val": "12.5"},
            {"corp_code": "00126380", "stock_code": "005930", "idx_nm": "부채비율", "idx_val": "35.2"},
            {"corp_code": "00126380", "stock_code": "005930", "idx_nm": "매출액증가율", "idx_val": "7.8%"},
        ]

        frame = load_dart_fundamentals("test-key", "2026-10-21", tickers=["005930"])

        self.assertEqual(frame.iloc[0]["ticker"], "005930")
        self.assertEqual(frame.iloc[0]["available_date"], "2026-08-15")
        self.assertEqual(float(frame.iloc[0]["roe"]), 12.5)
        self.assertEqual(float(frame.iloc[0]["debt_ratio"]), 35.2)
        self.assertEqual(float(frame.iloc[0]["sales_yoy"]), 7.8)
        requested_codes = indicators.call_args.args[0]
        self.assertEqual(requested_codes, ["00126380"])


if __name__ == "__main__":
    unittest.main()
