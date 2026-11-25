import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from sheet_logger import log_observation, log_selection


class SheetLoggerTest(unittest.TestCase):
    def test_recent_ticker_is_not_appended_again(self):
        worksheet = Mock()
        worksheet.get_all_values.return_value = [
            ["일자", "티커", "종목명", "종가", "전략"],
            ["2026-11-20", "005930", "삼성전자", "70000", "daily_multifactor"],
        ]
        with patch("sheet_logger._get_or_create_worksheet", return_value=worksheet):
            logged = log_selection(
                "005930", 71000, "daily_multifactor", datetime(2026, 11, 25), name="삼성전자"
            )
        self.assertFalse(logged)
        worksheet.append_row.assert_not_called()

    def test_expired_cooldown_allows_a_new_recommendation(self):
        worksheet = Mock()
        worksheet.get_all_values.return_value = [
            ["일자", "티커", "종목명", "종가", "전략"],
            ["2026-11-01", "005930", "삼성전자", "70000", "daily_multifactor"],
        ]
        with patch("sheet_logger._get_or_create_worksheet", return_value=worksheet):
            logged = log_selection(
                "005930", 71000, "daily_multifactor", datetime(2026, 11, 25), name="삼성전자"
            )
        self.assertTrue(logged)
        worksheet.append_row.assert_called_once()

    def test_observation_updates_existing_ticker_row(self):
        worksheet = Mock()
        worksheet.get_all_values.return_value = [
            ["최초관찰일", "최근관찰일", "티커", "종목명", "종가", "상태", "전략"],
            ["2026-11-20", "2026-11-20", "005930", "삼성전자", "70000", "ENTRY_WATCH", "old"],
        ]
        with patch("sheet_logger._get_or_create_worksheet", return_value=worksheet):
            log_observation(
                "005930", 71000, "OFF_WATCH", "new", datetime(2026, 11, 25), name="삼성전자"
            )
        worksheet.update.assert_called_once()
        args, kwargs = worksheet.update.call_args
        self.assertEqual(kwargs["range_name"], "A2:G2")
        self.assertEqual(kwargs["values"][0][0], "2026-11-20")
        self.assertEqual(kwargs["values"][0][5], "OFF_WATCH")


if __name__ == "__main__":
    unittest.main()
