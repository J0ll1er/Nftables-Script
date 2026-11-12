"""Проверки пункта меню «Версии и обновление скрипта» без сети и без установки на систему."""
import builtins
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_versions", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
CHANGELOG = """# Журнал изменений

## 1.1.0
- [исправление] Новый фикс
- [безопасность] Новая защита

## 1.0.0
- [новое] Базовая версия
"""


def fake_installer(version, marker):
    return f"bash <<'INSTALL_FW'\nFW_VERSION='{version}'\necho ok > '{marker}'\nINSTALL_FW\n"


class Versions(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.menu.FW_VERSION = "1.0.0"
        self.tmp = Path(tempfile.mkdtemp(prefix="fw-test-versions."))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.base = self.tmp / "remote"
        (self.base / "main").mkdir(parents=True)
        (self.base / "main" / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        os.environ["FW_ALLOW_FILE"] = "1"
        os.environ["FW_TEST_BASE"] = "file://" + str(self.base)
        self.addCleanup(lambda: [os.environ.pop(name, None) for name in ("FW_ALLOW_FILE", "FW_TEST_BASE")])

    def call(self, function, answers=()):
        answers = iter(answers)
        original = builtins.input
        builtins.input = lambda prompt="": next(answers)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                try:
                    function()
                except SystemExit as exit_info:
                    return output.getvalue(), exit_info.code
        finally:
            builtins.input = original
        return output.getvalue(), None

    def test_ordering_and_parsing(self):
        releases = self.menu.parse_changelog(CHANGELOG)
        self.assertEqual([release["version"] for release in releases], ["1.1.0", "1.0.0"])
        self.assertEqual(len(releases[0]["items"]), 2)
        self.assertGreater(self.menu.version_key("1.0.1"), self.menu.version_key("1.0.0"))
        self.assertGreater(self.menu.version_key("1.10.0"), self.menu.version_key("1.9.0"))

    def test_real_changelog_is_parsable(self):
        releases = self.menu.parse_changelog((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertTrue(releases)
        installer = (ROOT / "fw-install.sh").read_text(encoding="utf-8")
        self.assertIn(f"FW_VERSION='{releases[0]['version']}'", installer)

    def test_shows_newer_versions_and_fixes(self):
        output, _ = self.call(self.menu.show_versions)
        self.assertIn("Доступна новая версия: 1.1.0", output)
        self.assertIn("Новый фикс", output)
        self.assertNotIn("Базовая версия", output)

    def test_latest_version_message(self):
        self.menu.FW_VERSION = "1.1.0"
        output, _ = self.call(self.menu.show_versions)
        self.assertIn("У вас последняя версия.", output)

    def test_github_unavailable_is_reported(self):
        (self.base / "main" / "CHANGELOG.md").unlink()
        output, _ = self.call(self.menu.show_versions)
        self.assertIn("ОШИБКА — не удалось получить список версий", output)

    def test_install_version_runs_installer(self):
        marker = self.tmp / "installed"
        (self.base / "v1.1.0").mkdir()
        (self.base / "v1.1.0" / "fw-install.sh").write_text(fake_installer("1.1.0", marker))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertTrue(self.menu.install_version("1.1.0"))
        self.assertTrue(marker.is_file())
        self.assertIn("скрипт 1.1.0 установлен", output.getvalue())

    def test_install_version_rejects_mismatched_installer(self):
        marker = self.tmp / "installed"
        (self.base / "v1.1.0").mkdir()
        (self.base / "v1.1.0" / "fw-install.sh").write_text(fake_installer("1.0.9", marker))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.menu.install_version("1.1.0"))
        self.assertFalse(marker.exists())
        self.assertIn("не похож на установщик версии 1.1.0", output.getvalue())

    def test_install_version_missing_tag(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.menu.install_version("1.1.0"))
        self.assertIn("не найдена на GitHub", output.getvalue())

    def test_update_script_needs_confirmation(self):
        marker = self.tmp / "installed"
        (self.base / "v1.1.0").mkdir()
        (self.base / "v1.1.0" / "fw-install.sh").write_text(fake_installer("1.1.0", marker))
        self.call(self.menu.update_script, ["нет"])
        self.assertFalse(marker.exists())
        _, code = self.call(self.menu.update_script, ["да"])
        self.assertEqual(code, 0)
        self.assertTrue(marker.is_file())

    def test_local_rollback_restores_scripts_and_removes_new_dropin(self):
        menu = self.menu
        backup_root = self.tmp / "rollbacks"
        backup = backup_root / "upgrade.AAAA"
        backup.mkdir(parents=True)
        (backup / "update-fw.sh").write_text("#!/bin/sh\necho old updater\n")
        (backup / "fw-menu").write_text('FW_VERSION = "0.9.0"\n')
        menu.BACKUP_ROOT = backup_root
        menu.UPDATER = self.tmp / "opt" / "update-fw.sh"
        menu.MENU_FILE = self.tmp / "sbin" / "fw-menu"
        menu.SERVICE_FILE = self.tmp / "unit" / "fw-update.service"
        menu.TIMER_FILE = self.tmp / "unit" / "fw-update.timer"
        menu.NFT_DROPIN = self.tmp / "unit" / "10-fw-restore.conf"
        menu.NFT_DROPIN.parent.mkdir(parents=True)
        menu.NFT_DROPIN.write_text("[Service]\n")
        menu.run = lambda *command: True
        output, code = self.call(menu.rollback_local, ["1", "да"])
        self.assertEqual(code, 0)
        self.assertIn("версии 0.9.0", output)
        self.assertEqual(menu.UPDATER.read_text(), "#!/bin/sh\necho old updater\n")
        self.assertTrue(os.access(menu.UPDATER, os.X_OK))
        self.assertFalse(menu.NFT_DROPIN.exists())

    def test_backup_without_version_marker(self):
        backup = self.tmp / "old"
        backup.mkdir()
        (backup / "fw-menu").write_text("print('old')\n")
        self.assertEqual(self.menu.backup_version(backup), "до введения версий")

    def test_no_local_backups_message(self):
        self.menu.BACKUP_ROOT = self.tmp / "nothing"
        output, _ = self.call(self.menu.rollback_local)
        self.assertIn("Локальных копий", output)


if __name__ == "__main__":
    unittest.main()
