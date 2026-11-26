"""Подтверждение после применения правил: лишний Enter и опечатка не откатывают правила до конца времени."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_confirm", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class Confirmation(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)

    def ask(self, typed, timeout=2):
        read, write = os.pipe()
        os.write(write, typed.encode())
        old = sys.stdin
        sys.stdin = os.fdopen(read)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                result = self.menu.ask_confirmation(timeout)
        finally:
            sys.stdin.close()
            sys.stdin = old
            os.close(write)
        return result, out.getvalue()

    def test_yes_confirms(self):
        self.assertTrue(self.ask("да\n")[0])

    def test_stray_enter_before_yes_does_not_cancel(self):
        result, text = self.ask("\nда\n")
        self.assertTrue(result)
        self.assertIn("Ответ пустой", text)

    def test_wrong_layout_is_explained_then_yes_confirms(self):
        result, text = self.ask("lf\nда\n")
        self.assertTrue(result)
        self.assertIn("английская раскладка", text)

    def test_no_cancels_at_once(self):
        self.assertFalse(self.ask("нет\n")[0])

    def test_closed_input_cancels(self):
        read, write = os.pipe()
        os.close(write)
        old = sys.stdin
        sys.stdin = os.fdopen(read)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = self.menu.ask_confirmation(2)
        finally:
            sys.stdin.close()
            sys.stdin = old
        self.assertFalse(result)

    def test_silence_until_timeout_cancels(self):
        read, write = os.pipe()
        old = sys.stdin
        sys.stdin = os.fdopen(read)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = self.menu.ask_confirmation(1)
        finally:
            sys.stdin.close()
            sys.stdin = old
            os.close(write)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
