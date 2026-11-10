"""Where index files live (data/search/<dataset>.sqlite, not in git)."""
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "search"


def index_path(dataset: str) -> Path:
    return OUT_DIR / f"{dataset}.sqlite"
