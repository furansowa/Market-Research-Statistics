import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(scope="session")
def conn():
    db_path = ROOT / "data" / "db" / "lookup.sqlite"
    c = sqlite3.connect(str(db_path))
    yield c
    c.close()
