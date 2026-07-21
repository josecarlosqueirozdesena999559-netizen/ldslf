from bullex.client import BullExClient


def buy(client: BullExClient, active_name: str, direction: str, value: float, duration: int):
    return client.buy(active_name, direction, value, duration)
