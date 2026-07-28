from bullex.client import BullExClient
from models.settings import BotSettings
from robot.risk import RiskManager


def test_account_mode_aliases_are_normalized():
    assert BullExClient._normalize_account_mode("PRACTICE") == "DEMO"
    assert BullExClient._normalize_account_mode("4") == "DEMO"
    assert BullExClient._normalize_account_mode("REAL") == "REAL"
    assert BullExClient._normalize_account_mode(1) == "REAL"


def test_real_trade_accepts_normalized_real_mode():
    risk = RiskManager()
    risk.confirm_real("CONFIRMO REAL")
    allowed, reason = risk.can_trade(
        settings=BotSettings(),
        account_mode="REAL",
        detected_mode="real",
        balance=100.0,
        value=2.0,
    )
    assert allowed is True
    assert reason == "ok"


def test_real_trade_stays_blocked_without_confirmation():
    risk = RiskManager()
    allowed, reason = risk.can_trade(
        settings=BotSettings(),
        account_mode="REAL",
        detected_mode="REAL",
        balance=100.0,
        value=2.0,
    )
    assert allowed is False
    assert "CONFIRMO REAL" in reason
