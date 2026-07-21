import unittest

from models.trade import TradeResult
from robot.executor import TradeExecutor
from web_main import WebBot


class DummyLogger:
    def info(self, *args, **kwargs) -> None:
        return None


def trade(result: str, profit: float = 0.0) -> TradeResult:
    return TradeResult(
        timestamp="2026-07-21 12:00:00",
        asset="EURUSD-OTC",
        direction="CALL",
        payout=87,
        value=10.0,
        attempt="normal",
        result=result,
        profit=profit,
        balance_before=100.0,
        balance_after=100.0 + profit,
        account_mode="DEMO",
    )


class ResultAccountingTests(unittest.TestCase):
    def test_loose_result_counts_as_loss_with_negative_profit(self) -> None:
        executor = object.__new__(TradeExecutor)
        executor.logger = DummyLogger()

        result, profit = executor.resolve_robot_order_result("loose", 8.7, 123)

        self.assertEqual(result, "LOSS")
        self.assertEqual(profit, -8.7)

    def test_cycle_without_win_counts_loss(self) -> None:
        bot = object.__new__(WebBot)
        bot.session_wins = 0
        bot.session_losses = 0
        bot.session_profit = 0.0
        bot.session_results = []
        bot.save_session_score = lambda: None

        bot.add_session_cycle([trade("DOJI"), trade("LOSS", -10.0)])

        self.assertEqual(bot.session_wins, 0)
        self.assertEqual(bot.session_losses, 1)
        self.assertEqual(bot.session_results[0]["result"], "LOSS")


if __name__ == "__main__":
    unittest.main()
