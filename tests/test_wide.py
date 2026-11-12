"""Подтверждение слишком широких сетей в fw-menu."""
import builtins
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_wide", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class WideNetworks(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = Path(tempfile.mkdtemp(prefix="fw-test-wide."))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.menu.WIDE_PENDING = self.tmp / "wide-pending.txt"
        self.menu.WIDE_OK = self.tmp / "wide-ok.txt"
        self.menu.WIDE_PENDING.write_text("censys_v4.txt\t198.0.0.0/7\nAS1234\t2000::/15\n", encoding="utf-8")
        self.runs = []
        self.menu.run = lambda *command: self.runs.append(command) or True

    def ask(self, function, answers):
        answers = iter(answers)
        original = builtins.input
        builtins.input = lambda prompt="": next(answers)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                result = function()
        finally:
            builtins.input = original
        return result, output.getvalue()

    def test_shows_networks_and_saves_on_yes(self):
        changed, output = self.ask(self.menu.ask_wide_networks, ["да"])
        self.assertTrue(changed)
        self.assertIn("198.0.0.0/7", output)
        self.assertIn("censys_v4.txt", output)
        saved = self.menu.WIDE_OK.read_text(encoding="utf-8")
        self.assertIn("198.0.0.0/7", saved)
        self.assertIn("2000::/15", saved)

    def test_enter_keeps_networks_unapproved(self):
        changed, output = self.ask(self.menu.ask_wide_networks, [""])
        self.assertFalse(changed)
        self.assertFalse(self.menu.WIDE_OK.exists())
        self.assertIn("остаются неблокируемыми", output)

    def test_nothing_to_ask_when_all_approved(self):
        self.menu.WIDE_OK.write_text("198.0.0.0/7\n2000::/15\n", encoding="utf-8")
        changed, output = self.ask(self.menu.ask_wide_networks, [])
        self.assertFalse(changed)
        self.assertEqual(output, "")

    def test_no_pending_file_means_no_question(self):
        self.menu.WIDE_PENDING.unlink()
        changed, _ = self.ask(self.menu.ask_wide_networks, [])
        self.assertFalse(changed)

    def test_dry_run_repeats_after_approval(self):
        self.ask(self.menu.dry_run, ["да"])
        self.assertEqual(len(self.runs), 2)

    def test_dry_run_single_pass_without_approval(self):
        self.ask(self.menu.dry_run, [""])
        self.assertEqual(len(self.runs), 1)

    def test_wide_ok_is_a_setting_file(self):
        self.assertIn("wide-ok.txt", self.menu.SETTING_FILES)


if __name__ == "__main__":
    unittest.main()
