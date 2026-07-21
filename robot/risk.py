from models.settings import BotSettings


class RiskManager:
    def __init__(self) -> None:
        self.real_confirmed = False
        self.daily_profit = 0.0

    def confirm_real(self, text: str) -> bool:
        self.real_confirmed = text == "CONFIRMO REAL"
        return self.real_confirmed

    def check_real_confirmation(self, account_mode: str) -> bool:
        return account_mode.upper() != "REAL" or self.real_confirmed

    def check_stop_win(self, settings: BotSettings) -> bool:
        return self.daily_profit >= settings.stop_win

    def check_stop_loss(self, settings: BotSettings) -> bool:
        return self.daily_profit <= -abs(settings.stop_loss)

    @staticmethod
    def check_balance(balance: float, value: float) -> bool:
        return balance >= value

    def can_trade(
        self,
        settings: BotSettings,
        account_mode: str,
        detected_mode: str,
        balance: float,
        value: float,
    ) -> tuple[bool, str]:
        if account_mode != detected_mode:
            return False, "conta detectada diferente da selecionada"
        if not self.check_real_confirmation(account_mode):
            return False, "REAL bloqueado sem CONFIRMO REAL"
        if self.check_stop_win(settings):
            return False, "stop win atingido"
        if self.check_stop_loss(settings):
            return False, "stop loss atingido"
        if not self.check_balance(balance, value):
            return False, "saldo insuficiente"
        return True, "ok"

    def add_profit(self, profit: float) -> None:
        self.daily_profit = round(self.daily_profit + profit, 2)
