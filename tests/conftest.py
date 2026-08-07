# tests/conftest.py —— 必须在文件最顶部，任何项目 import 之前
import os
import sys
from pathlib import Path

# 在 app_paths 被 import 之前设置隔离根
os.environ.setdefault("FINANCE_SYSTEM_HOME", str(Path(__file__).resolve().parent.parent / ".test-home"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402

REAL_DB_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / "FinanceSystem" / "db" / "finance.db"


def _assert_not_real_db() -> None:
    if db.DB_PATH == str(REAL_DB_PATH):
        raise AssertionError(
            f"TEST ISOLATION BREACH: db.DB_PATH points to real production DB!\n"
            f"  {db.DB_PATH}\n"
            f"Set FINANCE_SYSTEM_HOME before importing app modules."
        )


def test_not_writing_to_real_db() -> None:
    """Autouse guard: fail fast if any test touches the real database."""
    _assert_not_real_db()


# 注册为 autouse fixture（pytest 会自动调用）
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _guard_real_db() -> None:
    _assert_not_real_db()
    yield
