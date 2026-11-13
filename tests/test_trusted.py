"""Доверенные адреса: пометка дат, показ, удаление из меню, предупреждение о старых автоадресах."""
import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_trusted", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class Trusted(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.menu.DIRECTORY = self.dir
        self.menu.TRUSTED = self.dir / "trusted.txt"
        self.old = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        self.today = datetime.date.today().isoformat()
        self.menu.TRUSTED.write_text(
            f"62.182.199.50  # авто, добавлен {self.old}\n169.40.1.155  # авто, добавлен {self.today}\n", encoding="utf-8")
        (self.dir / "trusted.local.txt").write_text("203.0.113.7  # свой\n# просто комментарий\n", encoding="utf-8")
        self.menu.ssh_client = lambda: "169.40.1.155"

    def tearDown(self):
        self.tmp.cleanup()

    def run_with_input(self, function, *answers):
        queue = list(answers)
        self.menu.input = lambda prompt="": queue.pop(0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            function()
        return out.getvalue()

    def test_records_know_auto_and_date(self):
        records = self.menu.trusted_records()
        self.assertEqual([r["value"] for r in records], ["62.182.199.50", "169.40.1.155", "203.0.113.7"])
        self.assertTrue(records[0]["auto"] and not records[2]["auto"])
        self.assertEqual(self.menu.trusted_age(records[0]), 30)

    def test_show_prints_notes(self):
        text = self.run_with_input(self.menu.show_trusted)
        self.assertIn("62.182.199.50  — добавлен установщиком", text)
        self.assertIn("30 дн. назад", text)
        self.assertIn("203.0.113.7", text)

    def test_old_auto_address_gets_a_warning_and_fresh_one_does_not(self):
        self.menu.paint = lambda code, text, stream=None: print(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.warn_old_trusted()
        self.assertIn("62.182.199.50", out.getvalue())
        self.assertNotIn("169.40.1.155", out.getvalue())

    def test_remove_by_number_keeps_other_lines_and_comments(self):
        text = self.run_with_input(self.menu.remove_trusted, "1", "да")
        self.assertIn("Удалено: 62.182.199.50", text)
        self.assertEqual(self.menu.TRUSTED.read_text(encoding="utf-8"), f"169.40.1.155  # авто, добавлен {self.today}\n")
        self.assertIn("# просто комментарий", (self.dir / "trusted.local.txt").read_text(encoding="utf-8"))

    def test_remove_by_address_and_current_connection_warning(self):
        text = self.run_with_input(self.menu.remove_trusted, "169.40.1.155", "да")
        self.assertIn("адрес вашего текущего подключения", text)
        self.assertNotIn("169.40.1.155", self.menu.TRUSTED.read_text(encoding="utf-8"))

    def test_cancel_and_unknown_do_not_change_files(self):
        before = self.menu.TRUSTED.read_text(encoding="utf-8")
        self.run_with_input(self.menu.remove_trusted, "1", "нет")
        self.run_with_input(self.menu.remove_trusted, "9.9.9.9")
        self.assertEqual(self.menu.TRUSTED.read_text(encoding="utf-8"), before)

    def test_parsers_still_ignore_comments(self):
        nets = {str(n) for n in self.menu.trusted_networks()}
        self.assertEqual(nets, {"62.182.199.50/32", "169.40.1.155/32", "203.0.113.7/32"})


if __name__ == "__main__":
    unittest.main()
