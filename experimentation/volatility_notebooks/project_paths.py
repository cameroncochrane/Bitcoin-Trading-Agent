from pathlib import Path
import sys

PROJ_ROOT = Path(__file__).resolve().parents[2]
project_root = str(PROJ_ROOT)
sys.path.insert(0, project_root)

DATA_DIR = PROJ_ROOT / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
PROCESSED_DATA_DIR = DATA_DIR / "processed"

# Direction modelling:
DCN_DATA_DIR = PROCESSED_DATA_DIR / "direction"

RAW_DATA_PATH = RAW_DATA_DIR / "btcusd_data.pkl" # Raw BTC_USD data

MODEL_DIR = PROJ_ROOT / "models"
DCN_MODEL_DIR = MODEL_DIR / "direction"
VOL_MODEL_DIR = MODEL_DIR / "volatility" / "lstm"

