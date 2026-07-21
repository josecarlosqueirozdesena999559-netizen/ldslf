import unittest

from models.asset import Asset
from models.candle import Candle
from robot.strategy import generate_signal


def candle(color: str, timestamp: int, wickless: bool = False, update_timestamp: int | None = None) -> Candle:
    open_price = 1.0
    close_price = 2.0 if color == "GREEN" else 0.5
    return Candle(
        open=open_price,
        close=close_price,
        high=max(open_price, close_price) if wickless else max(open_price, close_price) + 0.1,
        low=min(open_price, close_price) if wickless else min(open_price, close_price) - 0.1,
        timestamp=timestamp,
        update_timestamp=update_timestamp,
    )


class EightCandleReversalStrategyTests(unittest.TestCase):
    def make_asset(self, colors: list[str]) -> Asset:
        return Asset(
            name="EURUSD",
            active_id=1,
            payout=90,
            candles=[candle(color, index) for index, color in enumerate(colors)],
        )

    def test_eight_red_then_two_green_signals_put(self) -> None:
        signal = generate_signal(self.make_asset(["RED"] * 8 + ["GREEN"] * 2))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertIn("velas 3, 4 e 5", signal.pattern)

    def test_eight_green_then_two_red_signals_call(self) -> None:
        signal = generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"] * 2))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")

    def test_requires_at_least_eight_before_reversal(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 7 + ["RED"] * 2)))

    def test_requires_second_reversal_candle(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"])))

    def test_does_not_signal_after_reversal_window_moves_past_second_candle(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 8 + ["RED"] * 3)))

    def test_eight_green_plus_three_green_signals_put_for_candles_four_five_six(self) -> None:
        signal = generate_signal(self.make_asset(["GREEN"] * 11))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertEqual(signal.strategy_window_seconds, 600)
        self.assertEqual(signal.max_entries, 3)
        self.assertIn("velas 4, 5 e 6", signal.pattern)

    def test_eight_red_plus_three_red_signals_call_for_candles_four_five_six(self) -> None:
        signal = generate_signal(self.make_asset(["RED"] * 11))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertEqual(signal.strategy_window_seconds, 600)
        self.assertEqual(signal.max_entries, 3)

    def test_continuation_requires_exactly_third_equal_after_eight(self) -> None:
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 10)))
        self.assertIsNone(generate_signal(self.make_asset(["GREEN"] * 12)))

    def test_red_wickless_below_ma21_then_four_green_signals_put_for_five_six_seven(self) -> None:
        candles = [candle("GREEN", index) for index in range(20)]
        candles.append(candle("RED", 20, wickless=True, update_timestamp=20))
        candles.extend(candle("GREEN", index) for index in range(21, 25))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        signal = generate_signal(asset)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertEqual(signal.max_entries, 3)
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

    def test_green_above_ma21_after_33_without_three_previous_green_signals_call_at_33(self) -> None:
        candles = [candle("RED", index) for index in range(20)]
        candles.append(candle("GREEN", 20, update_timestamp=34))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        signal = generate_signal(asset)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertEqual(signal.max_entries, 2)
        self.assertEqual(signal.entry_second, 33)

    def test_green_above_ma21_strategy_rejects_three_previous_green_candles(self) -> None:
        candles = [candle("RED", index) for index in range(17)]
        candles.extend(candle("GREEN", index) for index in range(17, 20))
        candles.append(candle("GREEN", 20, update_timestamp=34))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))

    def test_green_above_ma21_strategy_requires_close_after_33_seconds(self) -> None:
        candles = [candle("RED", index) for index in range(20)]
        candles.append(candle("GREEN", 20, update_timestamp=33))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))


if __name__ == "__main__":
    unittest.main()
