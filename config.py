from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
HISTORY_FILE = DATA_DIR / "history.json"
LOG_FILE = LOG_DIR / "bot.log"

DEFAULT_ASSET_LIMIT = 10
DEFAULT_PAYOUT = 80
DEFAULT_MARTINGALE_STEPS = 3
DEFAULT_MARTINGALE_MULTIPLIER = 2.0
DEFAULT_TIMEFRAME = "M1"

TIMEFRAMES = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
}

ASSET_PRIORITY = [
    "EURUSD-OTC",
    "GBPUSD-OTC",
    "EURJPY-OTC",
    "USDJPY-OTC",
    "AUDCAD-OTC",
    "EURGBP-OTC",
    "USDCHF-OTC",
    "GBPJPY-OTC",
    "AUDUSD-OTC",
    "USDCAD-OTC",
]
