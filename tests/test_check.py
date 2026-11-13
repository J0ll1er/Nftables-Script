"""fw-menu --check: одна строка и код выхода 0/1/2 для мониторинга."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_check", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class Check(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.menu.DIRECTORY = Path(self.tmp.name)
        self.table = True
        self.timer = True
        self.menu.quiet = self.fake_quiet

    def tearDown(self):
        self.tmp.cleanup()

    def fake_quiet(self, *command):
        if command[:2] == ("nft", "-t"):
            return self.table
        if "is-active" in command:
            return self.timer
        return False

    def check(self, result=None):
        if result is not None:
            (self.menu.DIRECTORY / "last-result").write_text(result + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.menu.health_check()
        return code, out.getvalue().strip()

    def test_ok(self):
        code, line = self.check("ok")
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith("ok:"), line)
        self.assertEqual(len(line.splitlines()), 1)

    def test_warn_when_last_update_warned(self):
        code, line = self.check("warn")
        self.assertEqual(code, 1)
        self.assertTrue(line.startswith("warn:"), line)

    def test_warn_when_timer_is_off(self):
        self.timer = False
        code, line = self.check("ok")
        self.assertEqual(code, 1)
        self.assertIn("таймер", line)

    def test_error_when_table_missing(self):
        self.table = False
        code, line = self.check("ok")
        self.assertEqual(code, 2)
        self.assertTrue(line.startswith("error:"), line)

    def test_error_when_last_update_failed(self):
        code, line = self.check("error")
        self.assertEqual(code, 2)

    def test_unknown_result_is_warn(self):
        code, line = self.check()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
