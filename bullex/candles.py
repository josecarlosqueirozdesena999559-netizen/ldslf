from bullex.client import BullExClient


def load_candles(client: BullExClient, active_name: str, timeframe: str, count: int = 12):
    return client.get_candles(active_name, timeframe, count)
