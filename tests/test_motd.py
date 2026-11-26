"""Уведомление о новой версии при входе по SSH."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import unittest.mock


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_motd", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class Motd(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "motd.d").mkdir()
        self.menu.MOTD_STATE = base / "motd-notice"
        self.menu.MOTD_CACHE = base / "latest.json"
        self.menu.MOTD_DIR = base / "motd.d"
        self.menu.MOTD_HOOK = base / "motd.d" / "99-fw-update"
        self.menu.MOTD_PROFILE_HOOK = base / "fw-update-notice.sh"
        self.spawned = []

    def tearDown(self):
        self.tmp.cleanup()

    def notice(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch.object(subprocess, "Popen", lambda *a, **k: self.spawned.append(a)):
            self.menu.motd_notice()
        return out.getvalue()

    def cache(self, version, age=0):
        self.menu.save_latest_cache(version, time.time() - age)

    def test_notice_when_newer(self):
        self.cache("99.0.0")
        text = self.notice()
        self.assertIn("99.0.0", text)
        self.assertIn(self.menu.FW_VERSION, text)
        self.assertEqual(self.spawned, [])

    def test_silent_when_current_or_older(self):
        self.cache(self.menu.FW_VERSION)
        self.assertEqual(self.notice(), "")
        self.cache("0.0.1")
        self.assertEqual(self.notice(), "")

    def test_garbage_cache_is_silent(self):
        self.menu.MOTD_CACHE.write_text("не json")
        self.assertEqual(self.notice(), "")
        self.cache("1.2.3; rm -rf /")
        self.assertEqual(self.notice(), "")

    def test_stale_cache_starts_background_refresh_once(self):
        self.cache("99.0.0", age=3 * 24 * 3600)
        self.assertIn("99.0.0", self.notice())
        self.assertEqual(len(self.spawned), 1)
        self.notice()  # кэш уже помечен свежим: второй вход проверку не запускает
        self.assertEqual(len(self.spawned), 1)

    def test_refresh_takes_top_version_and_survives_network_error(self):
        self.menu.fetch_ref = lambda ref, path: "# Журнал\n\n## 3.2.1\n- x\n\n## 3.2.0\n- y\n"
        self.menu.refresh_latest_version()
        self.assertEqual(self.menu.load_latest_cache()[0], "3.2.1")

        def broken(ref, path):
            raise OSError("нет сети")
        self.menu.fetch_ref = broken
        self.menu.refresh_latest_version()
        self.assertEqual(self.menu.load_latest_cache()[0], "3.2.1")

    def test_first_start_enables_and_remembers_choice(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.menu.ensure_motd_notice()
        self.assertTrue(self.menu.MOTD_HOOK.exists())
        self.assertEqual(self.menu.motd_state(), "on")
        with contextlib.redirect_stdout(io.StringIO()):
            self.menu.toggle_motd_notice()
        self.assertFalse(self.menu.MOTD_HOOK.exists())
        self.assertEqual(self.menu.motd_state(), "off")
        with contextlib.redirect_stdout(io.StringIO()):
            self.menu.ensure_motd_notice()  # выбор «выключено» не перезаписывается
        self.assertFalse(self.menu.MOTD_HOOK.exists())

    def test_hooks_are_valid_shell(self):
        for path in (self.menu.MOTD_HOOK, self.menu.MOTD_PROFILE_HOOK):
            text = self.menu.motd_hook_text(path)
            self.assertIn("--motd", text)
            self.assertEqual(subprocess.run(["sh", "-n"], input=text, text=True, capture_output=True).returncode, 0, text)

    def test_profile_hook_used_without_motd_dir(self):
        self.menu.MOTD_DIR = Path(self.tmp.name) / "нет"
        self.assertEqual(self.menu.motd_hook_path(), self.menu.MOTD_PROFILE_HOOK)


if __name__ == "__main__":
    unittest.main()
