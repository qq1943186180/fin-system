# -*- coding: utf-8 -*-
"""启动自动备份：日期判定与分层保留策略的回归测试。

背景（2026-09-10 修复的两个真实缺陷，详见 docs/architecture-review-2026-09-10-followup.md §2.1）：

1. 日期匹配失效
   备份文件名是 ``finance-20260910-222249-908177.db``（%Y%m%d），
   而旧代码用 ``date.today().isoformat()`` 生成 ``finance-2026-09-10`` 做前缀判断，
   两者永不相等 → 「每天首启备份一次」退化成「每次启动都备份」；
   叠加 MAX_BACKUPS=5，同一天多次启动就会把历史备份全部滚掉。

2. 保留策略过短 + 失败静默
   旧实现只保留最近 5 份（≈ 最近 5 次启动），且整个函数被 ``except: pass`` 包住，
   备份失败用户完全无感。

这些用例锁住修复后的行为，避免将来又被改回去。
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui import main as gui_main  # noqa: E402

_BACKUP_NAME_RE = gui_main._BACKUP_NAME_RE


def _stamp_of(age_days: int, *, seconds: int = 0) -> str:
    """生成 age_days 天前的备份文件时间戳部分。"""
    moment = dt.datetime.now() - dt.timedelta(days=age_days, seconds=seconds)
    return moment.strftime("%Y%m%d-%H%M%S-%f")


class _FakeApp:
    """借用 FinanceApp 的备份方法，但把备份目录指向临时目录（不碰真实数据根）。"""

    BACKUP_KEEP_DAYS = 7
    BACKUP_KEEP_WEEKS = 4
    BACKUP_KEEP_MONTHS = 6

    _backup_directory = gui_main.FinanceApp._backup_directory
    _prune_backups = gui_main.FinanceApp._prune_backups
    _run_startup_backup = gui_main.FinanceApp._run_startup_backup
    _backup_status_text = gui_main.FinanceApp._backup_status_text

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _backup_directory(self) -> Path:  # type: ignore[override]
        return self._directory


class BackupFileNameTest(unittest.TestCase):
    """文件名正则必须精确匹配自动备份，且不能误伤迁移前备份。"""

    def test_matches_automatic_backup_names(self) -> None:
        self.assertIsNotNone(_BACKUP_NAME_RE.match("finance-20260910-222249-908177.db"))
        self.assertIsNotNone(_BACKUP_NAME_RE.match("finance-20260101-000000-000000.db"))

    def test_ignores_migration_and_manual_backups(self) -> None:
        for name in (
            "finance-pre-v4-20260910-222249-908177.db",  # 迁移前备份：必须长期保留
            "source-pre-upgrade-20260710-234425.zip",
            "finance_20260910_222249.db",                # 手动备份的下划线命名
            "finance-20260910-222249.db",                # 缺微秒段
            "notes.txt",
        ):
            self.assertIsNone(_BACKUP_NAME_RE.match(name), name)


class StartupBackupTest(unittest.TestCase):
    """每天只备份一次 —— 这正是原日期匹配 bug 破坏的行为。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fin-backup-")
        self.backup_dir = Path(self._tmp.name)
        self.app = _FakeApp(self.backup_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _patch_backup(self) -> tuple[list[str], mock._patch]:
        created: list[str] = []

        def fake_backup(*_args, **_kwargs):
            name = "finance-%s.db" % dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            (self.backup_dir / name).write_bytes(b"sqlite")
            created.append(name)
            return str(self.backup_dir / name)

        return created, mock.patch.object(gui_main.db, "backup_database", side_effect=fake_backup)

    def test_backup_runs_only_once_per_day(self) -> None:
        created, patcher = self._patch_backup()
        with patcher:
            self.app._run_startup_backup()
            self.app._run_startup_backup()
            self.app._run_startup_backup()

        self.assertEqual(len(created), 1, "同一天多次启动只应产生一份备份")
        self.assertEqual(len(list(self.backup_dir.glob("*.db"))), 1)

    def test_backup_failure_is_recorded_not_swallowed(self) -> None:
        with mock.patch.object(gui_main.db, "backup_database", side_effect=OSError("disk full")):
            self.app._run_startup_backup()

        self.assertIn("disk full", self.app._backup_error)
        self.assertIn("disk full", self.app._backup_status_text())

    def test_status_text_reports_count_and_latest(self) -> None:
        (self.backup_dir / ("finance-%s.db" % _stamp_of(0))).write_bytes(b"x")
        (self.backup_dir / ("finance-%s.db" % _stamp_of(3))).write_bytes(b"x")

        text = self.app._backup_status_text()
        self.assertIn("2 份", text)

    def test_status_text_when_no_backup_yet(self) -> None:
        self.assertIn("尚无", self.app._backup_status_text())


class BackupRetentionTest(unittest.TestCase):
    """分层保留：近 7 天逐日、第 8~35 天每周、第 36 天~6 月每月各留最新一份。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fin-prune-")
        self.backup_dir = Path(self._tmp.name)
        self.app = _FakeApp(self.backup_dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make(self, age_days: int, *, seconds: int = 0) -> Path:
        path = self.backup_dir / ("finance-%s.db" % _stamp_of(age_days, seconds=seconds))
        path.write_bytes(b"sqlite")
        return path

    def test_keeps_one_per_day_within_first_week(self) -> None:
        # 同一天写 3 份（模拟当天多次启动留下的历史残留），只应保留最新一份。
        # 注意 seconds 为正值表示「更早」，负值会跑到未来而落入另一个桶。
        kept_newest = self._make(0, seconds=0)
        self._make(0, seconds=60)
        self._make(0, seconds=120)
        for age in range(1, self.app.BACKUP_KEEP_DAYS):
            self._make(age)

        removed = self.app._prune_backups(self.backup_dir)

        self.assertEqual(removed, 2, "同一天的 3 份应只留 1 份")
        self.assertTrue(kept_newest.exists())
        self.assertEqual(len(list(self.backup_dir.glob("*.db"))), self.app.BACKUP_KEEP_DAYS)

    def test_keeps_one_per_week_and_per_month_then_drops_ancient(self) -> None:
        daily = [self._make(age) for age in range(0, self.app.BACKUP_KEEP_DAYS)]
        weekly = [self._make(age) for age in (10, 20, 30)]
        monthly = [self._make(age) for age in (40, 70, 100, 130, 160)]
        ancient = self._make(400)

        self.app._prune_backups(self.backup_dir)

        expected_kept = len(daily) + len(weekly) + len(monthly)
        self.assertEqual(len(list(self.backup_dir.glob("*.db"))), expected_kept)
        self.assertFalse(ancient.exists(), "超过约 6 个月的备份应被淘汰")
        for path in daily + weekly + monthly:
            self.assertTrue(path.exists(), path.name)

    def test_weekly_bucket_keeps_only_latest_in_same_week(self) -> None:
        # 周桶按 age_days // 7 划分：age 10 与 age 12 同属桶 1，只应留更新的 age 10
        newer = self._make(10)
        older = self._make(12)
        self.assertEqual(10 // 7, 12 // 7, "前置条件：两者应落在同一周桶")

        self.app._prune_backups(self.backup_dir)

        self.assertTrue(newer.exists())
        self.assertFalse(older.exists())

    def test_migration_backups_are_never_pruned(self) -> None:
        migration = self.backup_dir / "finance-pre-v4-20260101-000000-000000.db"
        migration.write_bytes(b"x")
        self._make(400)  # 一份应被淘汰的普通备份

        self.app._prune_backups(self.backup_dir)

        self.assertTrue(migration.exists(), "迁移前备份是 schema 变更的安全网，不得参与轮转删除")
        self.assertEqual(len(list(self.backup_dir.glob("*.db"))), 1)

    def test_prune_on_empty_directory_is_safe(self) -> None:
        self.assertEqual(self.app._prune_backups(self.backup_dir), 0)


if __name__ == "__main__":
    unittest.main()
