from bullex.client import BullExClient


def load_open_assets(client: BullExClient, payout_min: int, limit: int):
    return client.get_assets(payout_min=payout_min, limit=limit)
