import unittest
import threading
from unittest.mock import patch

from models.settings import BotSettings
from models.trade import TradeResult
from robot.executor import TradeExecutor
from robot.engine import RobotEngine
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

    def test_entry_after_open_grace_waits_next_candle(self) -> None:
        executor = object.__new__(TradeExecutor)
        executor.logger = DummyLogger()
        executor.current_trade = ""
        executor._operation_lock = threading.Lock()

        with patch("robot.executor.time.time", return_value=70), patch.object(executor, "sleep_until") as sleep_until:
            allowed = executor.ensure_candle_open_entry(BotSettings(timeframe="M1"))

        self.assertTrue(allowed)
        sleep_until.assert_called_once_with(119.65, "Preparando entrada no proximo candle")
        self.assertIn("Preparando entrada no proximo candle", executor.current_trade)

    def test_entry_inside_open_grace_is_allowed(self) -> None:
        executor = object.__new__(TradeExecutor)
        executor.logger = DummyLogger()
        executor.current_trade = ""

        with patch("robot.executor.time.time", return_value=62):
            allowed = executor.ensure_candle_open_entry(BotSettings(timeframe="M1"))

        self.assertTrue(allowed)

    def test_failed_buy_keeps_reason_visible(self) -> None:
        executor = object.__new__(TradeExecutor)
        executor.logger = DummyLogger()
        executor.current_trade = ""
        executor._operation_lock = threading.Lock()

        executor.resolve_robot_order_result = lambda result, profit, order_id: (result, profit)
        executor.wait_entry_time = lambda signal, settings: True
        executor.apply_martingale = lambda settings, step: settings.entry_value
        executor.client = type(
            "Client",
            (),
            {
                "get_balance": lambda _self: 100.0,
                "get_balance_mode": lambda _self: "DEMO",
                "buy": lambda _self, asset, direction, value, duration: (False, "mercado fechado"),
            },
        )()
        executor.risk = type(
            "Risk",
            (),
            {"can_trade": lambda _self, **kwargs: (True, ""), "add_profit": lambda _self, profit: None},
        )()

        signal = type(
            "Signal",
            (),
            {
                "asset": "EURUSD",
                "direction": "CALL",
                "pattern": "Estrategia 05",
                "timestamp": __import__("datetime").datetime.now(),
                "payout": 90,
                "max_entries": 1,
            },
        )()

        self.assertIsNone(executor.execute_cycle(signal, BotSettings(), "DEMO"))
        self.assertIn("Falha ao abrir ordem", executor.current_trade)

    def test_strategy_cooldown_blocks_same_family_until_signal_changes(self) -> None:
        engine = object.__new__(RobotEngine)
        engine.asset_strategy_cooldowns = {}
        asset = type("Asset", (), {"name": "EURUSD"})()
        signal = type("Signal", (), {"asset": "EURUSD", "pattern": "comprar no segundo 33 com entrada e G1"})()
        other_signal = type("Signal", (), {"asset": "EURUSD", "pattern": "Estrategia 05"})()

        engine.mark_strategy_cooldown(signal)

        self.assertTrue(engine.is_strategy_in_cooldown(asset, signal))
        engine.release_inactive_strategy_cooldowns(asset, signal)
        self.assertTrue(engine.is_strategy_in_cooldown(asset, signal))
        engine.release_inactive_strategy_cooldowns(asset, other_signal)
        self.assertFalse(engine.is_strategy_in_cooldown(asset, signal))


if __name__ == "__main__":
    unittest.main()
