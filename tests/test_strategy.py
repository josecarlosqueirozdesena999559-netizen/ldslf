import unittest

from models.asset import Asset
from models.candle import Candle
from robot.executor import TradeExecutor
from robot.strategy import generate_signal


def candle(
    color: str,
    timestamp: int,
    wickless: bool = False,
    update_timestamp: int | None = None,
    negative_at_33: bool = False,
    positive_at_33: bool = False,
) -> Candle:
    open_price = 1.0
    close_price = 2.0 if color == "GREEN" else 0.5
    return Candle(
        open=open_price,
        close=close_price,
        high=max(open_price, close_price) if wickless else max(open_price, close_price) + 0.1,
        low=min(open_price, close_price) if wickless else min(open_price, close_price) - 0.1,
        timestamp=timestamp,
        update_timestamp=update_timestamp,
        negative_at_33=negative_at_33,
        positive_at_33=positive_at_33,
    )


class EightCandleReversalStrategyTests(unittest.TestCase):
    def make_asset(self, colors: list[str]) -> Asset:
        return Asset(
            name="EURUSD",
            active_id=1,
            payout=90,
            candles=[candle(color, index) for index, color in enumerate(colors)],
        )

    def make_ma21_break_asset(self, previous_green_count: int, update_timestamp: int = 34) -> Asset:
        candles = [
            Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=index)
            for index in range(20 - previous_green_count)
        ]
        start = len(candles)
        for index in range(start, 20):
            candles.append(Candle(open=1.0, close=1.05, high=1.06, low=0.99, timestamp=index))
        candles.append(Candle(open=1.0, close=1.3, high=1.31, low=0.98, timestamp=20, update_timestamp=update_timestamp))
        return Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

    def make_ma21_red_break_asset(self, previous_red_count: int, update_timestamp: int = 34) -> Asset:
        candles = [
            Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=index)
            for index in range(20 - previous_red_count)
        ]
        start = len(candles)
        for index in range(start, 20):
            candles.append(Candle(open=1.0, close=0.95, high=1.01, low=0.94, timestamp=index))
        candles.append(Candle(open=1.0, close=0.7, high=1.02, low=0.69, timestamp=20, update_timestamp=update_timestamp))
        return Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

    def test_eight_green_in_sequence_waits_reversal(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 8)))

    def test_eight_red_in_sequence_does_not_trigger_strategy_01(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["RED"] * 8)))

    def test_eight_red_then_two_green_does_not_trigger_strategy_01(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["RED"] * 8 + ["GREEN"] * 2)))

    def test_eight_green_then_two_red_signals_put_with_g2(self) -> None:
        signal = generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"] * 2))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertEqual(signal.max_entries, 3)
        self.assertIn("Estrategia 01", signal.pattern)

    def test_requires_at_least_eight_before_reversal(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 7 + ["RED"] * 2)))

    def test_requires_second_reversal_candle(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"])))

    def test_third_red_after_strategy_01_becomes_strategy_03(self) -> None:
        signal = generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"] * 3))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertIn("Estrategia 03", signal.pattern)

    def test_strategy_03_eight_green_then_three_red_signals_call_with_g1(self) -> None:
        signal = generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"] * 3))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertEqual(signal.strategy_window_seconds, 600)
        self.assertEqual(signal.max_entries, 2)
        self.assertIn("Estrategia 03", signal.pattern)

    def test_strategy_03_does_not_use_eight_red_then_three_red(self) -> None:
        signal = generate_signal(self.make_asset(["RED"] * 11))

        self.assertIsNone(signal)

    def test_strategy_03_requires_exactly_three_red_after_eight_green(self) -> None:
        self.assertEqual(generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"] * 2)).direction, "PUT")
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"] * 4)))

    def test_strategy_04_red_green_red_green_sells_then_g1_buys(self) -> None:
        signal = generate_signal(self.make_asset(["RED", "GREEN", "RED", "GREEN"]))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertEqual(signal.max_entries, 2)
        self.assertIn("Estrategia 04", signal.pattern)
        self.assertEqual(TradeExecutor.direction_for_step(signal, 0), "PUT")
        self.assertEqual(TradeExecutor.direction_for_step(signal, 1), "CALL")

    def test_strategy_04_requires_exact_color_order(self) -> None:
        signal = generate_signal(self.make_asset(["GREEN", "RED", "GREEN", "RED"]))

        self.assertIsNotNone(signal)
        self.assertIn("Estrategia 05", signal.pattern)

    def test_strategy_05_green_red_green_red_buys_then_g1_sells(self) -> None:
        signal = generate_signal(self.make_asset(["GREEN", "RED", "GREEN", "RED"]))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertEqual(signal.max_entries, 2)
        self.assertIn("Estrategia 05", signal.pattern)
        self.assertEqual(TradeExecutor.direction_for_step(signal, 0), "CALL")
        self.assertEqual(TradeExecutor.direction_for_step(signal, 1), "PUT")

    def test_red_wickless_below_ma21_then_four_green_signals_put_for_five_six_seven(self) -> None:
        candles = [candle("GREEN", index) for index in range(20)]
        candles.append(candle("RED", 20, wickless=True, update_timestamp=20))
        candles.extend(candle("GREEN", index) for index in range(21, 25))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        signal = generate_signal(asset)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertEqual(signal.max_entries, 2)
        self.assertIn("velas 5, 6 e 7", signal.pattern)

    def test_red_wickless_ma21_strategy_requires_close_before_33_seconds(self) -> None:
        candles = [candle("GREEN", index) for index in range(20)]
        candles.append(candle("RED", 20, wickless=True, update_timestamp=34))
        candles.extend(candle("GREEN", index) for index in range(21, 25))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))

    def test_red_wickless_ma21_strategy_requires_wickless_anchor(self) -> None:
        candles = [candle("GREEN", index) for index in range(20)]
        candles.append(candle("RED", 20, wickless=False, update_timestamp=20))
        candles.extend(candle("GREEN", index) for index in range(21, 25))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))

    def test_green_above_ma21_after_33_signals_call_at_33(self) -> None:
        asset = self.make_ma21_break_asset(previous_green_count=1)

        signal = generate_signal(asset)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertEqual(signal.max_entries, 2)
        self.assertEqual(signal.entry_second, 33)

    def test_green_ma21_break_strategy_allows_two_previous_green_candles(self) -> None:
        asset = self.make_ma21_break_asset(previous_green_count=2)

        self.assertIsNotNone(generate_signal(asset))

    def test_green_ma21_break_strategy_blocks_three_previous_green_candles(self) -> None:
        asset = self.make_ma21_break_asset(previous_green_count=3)

        self.assertIsNone(generate_signal(asset))

    def test_green_ma21_break_strategy_requires_previous_green_candle(self) -> None:
        asset = self.make_ma21_break_asset(previous_green_count=0)

        self.assertIsNone(generate_signal(asset))

    def test_green_above_ma21_strategy_requires_close_after_33_seconds(self) -> None:
        asset = self.make_ma21_break_asset(previous_green_count=1, update_timestamp=33)

        self.assertIsNone(generate_signal(asset))

    def test_red_below_ma21_after_33_signals_put_at_33(self) -> None:
        asset = self.make_ma21_red_break_asset(previous_red_count=1)

        signal = generate_signal(asset)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertEqual(signal.max_entries, 2)
        self.assertEqual(signal.entry_second, 33)
        self.assertIn("Vermelho rompeu a MA21", signal.pattern)

    def test_red_ma21_break_strategy_allows_two_previous_red_candles(self) -> None:
        asset = self.make_ma21_red_break_asset(previous_red_count=2)

        self.assertIsNotNone(generate_signal(asset))

    def test_red_ma21_break_strategy_blocks_three_previous_red_candles(self) -> None:
        asset = self.make_ma21_red_break_asset(previous_red_count=3)

        self.assertIsNone(generate_signal(asset))

    def test_red_below_ma21_strategy_requires_close_after_33_seconds(self) -> None:
        asset = self.make_ma21_red_break_asset(previous_red_count=1, update_timestamp=33)

        self.assertIsNone(generate_signal(asset))

    def test_green_breaks_ma21_then_next_negative_at_33_green_close_signals_call(self) -> None:
        candles = [
            Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=index)
            for index in range(20)
        ]
        candles.append(Candle(open=0.9, close=1.1, high=1.12, low=0.88, timestamp=20))
        candles.append(Candle(open=1.0, close=1.2, high=1.21, low=0.8, timestamp=21, negative_at_33=True))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        signal = generate_signal(asset)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertEqual(signal.max_entries, 2)
        self.assertIsNone(signal.entry_second)
        self.assertIn("rompeu a MA21", signal.pattern)
        self.assertIn("negativo aos 33s", signal.pattern)

    def test_negative_at_33_strategy_requires_previous_green_ma21_break(self) -> None:
        candles = [
            Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=index)
            for index in range(20)
        ]
        candles.append(Candle(open=1.05, close=1.1, high=1.12, low=1.0, timestamp=20))
        candles.append(Candle(open=1.0, close=1.2, high=1.21, low=0.8, timestamp=21, negative_at_33=True))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))

    def test_negative_at_33_strategy_requires_negative_marker(self) -> None:
        candles = [
            Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=index)
            for index in range(20)
        ]
        candles.append(Candle(open=0.9, close=1.1, high=1.12, low=0.88, timestamp=20))
        candles.append(Candle(open=1.0, close=1.2, high=1.21, low=0.8, timestamp=21))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))

    def test_red_breaks_ma21_then_next_positive_at_33_red_close_signals_put(self) -> None:
        candles = [
            Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=index)
            for index in range(20)
        ]
        candles.append(Candle(open=1.1, close=0.9, high=1.12, low=0.88, timestamp=20))
        candles.append(Candle(open=1.0, close=0.8, high=1.1, low=0.79, timestamp=21, positive_at_33=True))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        signal = generate_signal(asset)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertEqual(signal.max_entries, 2)
        self.assertIsNone(signal.entry_second)
        self.assertIn("rompeu a MA21", signal.pattern)
        self.assertIn("verde aos 33s", signal.pattern)

    def test_positive_at_33_strategy_requires_previous_red_ma21_break(self) -> None:
        candles = [
            Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=index)
            for index in range(20)
        ]
        candles.append(Candle(open=0.95, close=0.9, high=1.0, low=0.88, timestamp=20))
        candles.append(Candle(open=1.0, close=0.8, high=1.1, low=0.79, timestamp=21, positive_at_33=True))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))

    def test_positive_at_33_strategy_requires_positive_marker(self) -> None:
        candles = [
            Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=index)
            for index in range(20)
        ]
        candles.append(Candle(open=1.1, close=0.9, high=1.12, low=0.88, timestamp=20))
        candles.append(Candle(open=1.0, close=0.8, high=1.1, low=0.79, timestamp=21))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))


if __name__ == "__main__":
    unittest.main()
