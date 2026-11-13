"""fw-menu: DDNS-имена в доверенных и расписание «каждые N часов»."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_ddns", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class DdnsMenu(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.menu.DIRECTORY = self.dir
        self.menu.TRUSTED = self.dir / "trusted.txt"
        self.menu.TRUSTED.write_text("1.2.3.4\n", encoding="utf-8")
        self.menu.TIMER_FILE = self.dir / "fw-update.timer"
        self.menu.TIMER_FILE.write_text("[Timer]\n")
        self.menu.SCHEDULE_FILE = self.dir / "timer.d" / "10-fw-menu-schedule.conf"
        self.menu.reload_timer = lambda active: True
        self.menu.show_schedule_status = lambda: None
        self.menu.run = lambda *command: True

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, function, *answers):
        queue = list(answers)
        self.menu.input = lambda prompt="": queue.pop(0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            function()
        return out.getvalue()

    def test_add_name_is_stored_lowercase_and_hints_at_schedule(self):
        text = self.drive(self.menu.add_trusted, "Home.Example.com")
        self.assertIn("home.example.com", self.menu.TRUSTED.read_text(encoding="utf-8").split())
        self.assertIn("раз в 3 часа", text)

    def test_add_name_and_ip_together_and_no_duplicates(self):
        self.drive(self.menu.add_trusted, "home.example.com 5.6.7.8")
        text = self.drive(self.menu.add_trusted, "home.example.com 5.6.7.8")
        self.assertIn("уже есть", text)
        self.assertEqual(self.menu.TRUSTED.read_text(encoding="utf-8").split().count("home.example.com"), 1)

    def test_invalid_names_are_refused_and_nothing_is_written(self):
        before = self.menu.TRUSTED.read_text(encoding="utf-8")
        for bad in ("bad_name", "localhost", "a..b.com", "-x.example.com"):
            with self.assertRaises(ValueError):
                self.drive(self.menu.add_trusted, bad)
        self.assertEqual(self.menu.TRUSTED.read_text(encoding="utf-8"), before)

    def test_remove_name_by_number(self):
        self.drive(self.menu.add_trusted, "home.example.com")
        self.drive(self.menu.remove_trusted, "2", "да")
        self.assertNotIn("home.example.com", self.menu.TRUSTED.read_text(encoding="utf-8"))

    def test_schedule_every_three_hours(self):
        self.drive(self.menu.configure_schedule, "3", "да")
        text = self.menu.SCHEDULE_FILE.read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 0/3:00:00", text)

    def test_schedule_words_are_understood(self):
        self.drive(self.menu.configure_schedule, "каждые 4 часа", "да")
        self.assertIn("0/4:00:00", self.menu.SCHEDULE_FILE.read_text(encoding="utf-8"))

    def test_schedule_rejects_values_that_do_not_divide_the_day(self):
        for bad in ("5", "1", "24", "0"):
            with self.assertRaises(ValueError):
                self.drive(self.menu.configure_schedule, bad)
        self.assertFalse(self.menu.SCHEDULE_FILE.exists())

    def test_schedule_clock_time_still_works(self):
        self.drive(self.menu.configure_schedule, "04:30", "да")
        self.assertIn("OnCalendar=*-*-* 04:30:00", self.menu.SCHEDULE_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
