from bullex.client import BullExClient


def account_snapshot(client: BullExClient) -> dict[str, str | float]:
    return {
        "connected": client.connected,
        "mode": client.get_balance_mode(),
        "balance": client.get_balance(),
        "currency": client.get_currency(),
    }
