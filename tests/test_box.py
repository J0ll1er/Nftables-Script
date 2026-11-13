"""Рамка вокруг вывода «Статус файрвола»: ровные границы, перенос длинных строк, цвета."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_box", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Box(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)

    def draw(self, text, columns=60):
        self.menu.shutil.get_terminal_size = lambda fallback=None: type("S", (), {"columns": columns})()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.print_box("Статус файрвола", text)
        return out.getvalue().splitlines()

    def test_all_lines_have_same_visible_width(self):
        lines = self.draw("Короткая строка\n\x1b[32mOK\x1b[0m /opt/fw/fw.conf\n" + "длинный текст " * 20)
        widths = {len(ANSI.sub("", line)) for line in lines}
        self.assertEqual(len(widths), 1)
        self.assertLessEqual(widths.pop(), 60)

    def test_frame_has_title_and_corners(self):
        lines = self.draw("строка")
        self.assertTrue(lines[0].startswith("┌─ Статус файрвола "))
        self.assertTrue(lines[0].endswith("┐"))
        self.assertTrue(lines[-1].startswith("└") and lines[-1].endswith("┘"))

    def test_colors_are_kept_and_closed(self):
        lines = self.draw("\x1b[31m" + "красный " * 20 + "\x1b[0m")
        body = [line for line in lines[1:-1] if "\x1b[31m" in line]
        self.assertGreater(len(body), 1)
        for line in body:
            self.assertIn("\x1b[0m", line)


if __name__ == "__main__":
    unittest.main()
