import importlib.machinery
import importlib.util
import pathlib
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_prefetch", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader("fwmenu_prefetch", LOADER)
MENU = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(MENU)


class Prefetch(unittest.TestCase):
    def setUp(self):
        MENU.STATUS_PREFETCH.clear()

    def test_profile_is_collected_in_background_and_used_once(self):
        calls = []
        MENU.collect_profile = lambda: calls.append(threading.current_thread().name) or {"profile": "x"}
        MENU.subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
        MENU.prefetch_status()
        self.assertEqual(MENU.prefetched("profile", lambda: "запасной"), {"profile": "x"})
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], threading.current_thread().name)
        self.assertEqual(MENU.prefetched("profile", lambda: "запасной"), "запасной")

    def test_without_prefetch_runs_fallback(self):
        self.assertEqual(MENU.prefetched("table", lambda: 5), 5)


if __name__ == "__main__":
    unittest.main()
