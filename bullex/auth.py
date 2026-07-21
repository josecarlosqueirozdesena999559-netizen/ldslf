from bullex.client import BullExClient


def connect(email: str, password: str, account_mode: str):
    client = BullExClient()
    ok, error = client.connect(email, password, account_mode)
    return client, ok, error
