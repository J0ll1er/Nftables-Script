"""fw-menu 1.6.0: шесть пунктов, подменю «Статус и наблюдение», шапка с состоянием."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_layout", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class Layout(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.menu.DIRECTORY = Path(self.tmp.name)
        self.table = True
        self.timer = {"ActiveState": "active", "UnitFileState": "enabled",
                      "NextElapseUSecRealtime": "Sat 2026-11-14 05:24:33 EET"}
        self.menu.quiet = lambda *command: self.table
        self.menu.systemd_properties = lambda *args: self.timer
        self.menu.trusted_networks = lambda: ["a", "b", "a"]
        self.menu.trusted_names = lambda: ["home.example.com"]

    def tearDown(self):
        self.tmp.cleanup()

    def entries(self):
        captured = {}

        def spy(title, entries, exit_label="Назад"):
            captured["entries"] = entries
            captured["exit"] = exit_label
        self.menu.show_menu = spy
        self.menu.os.geteuid = lambda: 0
        self.menu.sys.argv = ["fw-menu"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.menu.main()
        return captured["entries"]

    def test_six_top_level_items_in_the_chosen_order(self):
        titles = [label for label, _ in self.entries()]
        self.assertEqual(titles, ["Статус и наблюдение", "Настроить доступ", "Настроить блокировки",
                                  "Проверить и применить правила", "Версии и обновление скрипта",
                                  "Отключить или удалить"])

    def test_observation_submenu_holds_status_log_and_overview(self):
        first = self.entries()[0][1]
        self.assertIsInstance(first, tuple)
        self.assertEqual([label for label, _ in first],
                         ["Статус файрвола", "Журнал ошибок", "Что ещё есть на сервере"])
        wrapped = [getattr(action, "__wrapped__", action) for _, action in first]
        self.assertEqual(wrapped, [self.menu.show_status, self.menu.show_error_log, self.menu.show_server_overview])

    def test_old_sections_moved_without_losing_actions(self):
        entries = dict(self.entries())
        self.assertIsInstance(entries["Версии и обновление скрипта"], tuple)
        self.assertEqual([label for label, _ in entries["Отключить или удалить"]],
                         ["Удалить правила и автообновление", "Удалить скрипт и настройки"])

    def header(self, result="ok"):
        (self.menu.DIRECTORY / "last-result").write_text(result + "\n")
        return self.menu.menu_header_text(color=False)

    def test_header_when_everything_is_fine(self):
        text = self.header("ok")
        self.assertIn("Правила работают", text)
        self.assertIn("Последнее обновление: успешно", text)
        self.assertIn("Следующее обновление: 14.11 в 05:24", text)
        self.assertIn("Доверенных: 3", text)

    def test_header_flags_problems_in_words(self):
        self.table = False
        self.timer = {"ActiveState": "inactive"}
        text = self.header("error")
        self.assertIn("Правила не загружены", text)
        self.assertIn("Последнее обновление: ошибка", text)
        self.assertIn("Таймер выключен", text)

    def test_header_warn_and_unknown(self):
        self.assertIn("есть замечания", self.header("warn"))
        (self.menu.DIRECTORY / "last-result").unlink()
        self.assertIn("ещё не было", self.menu.menu_header_text(color=False))

    def test_header_failure_never_breaks_the_menu(self):
        def broken(*args):
            raise OSError("нет systemctl")
        self.menu.systemd_properties = broken
        self.assertIsInstance(self.menu.menu_header_text(color=False), str)

    def run_menu(self, tty, *answers):
        queue = list(answers)
        self.menu.input = lambda prompt="": queue.pop(0)
        self.menu.menu_color = lambda: tty
        self.menu.stdout_is_tty = lambda: tty
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_menu("Файрвол IPv4 / IPv6", (("Один", lambda: print("ВЫЗВАН")), ("Два", lambda: None)),
                                "Выход")
        return out.getvalue()

    def test_plain_output_without_a_terminal_keeps_old_format(self):
        text = self.run_menu(False, "1", "0")
        self.assertIn("1. Один", text)
        self.assertIn("0. Выход", text)
        self.assertIn("ВЫЗВАН", text)
        self.assertNotIn("\033[", text)

    def test_terminal_output_has_numbers_and_arrow_prompt(self):
        queue = ["0"]
        prompts = []
        self.menu.input = lambda prompt="": (prompts.append(prompt), queue.pop(0))[1]
        self.menu.menu_color = lambda: True
        self.menu.stdout_is_tty = lambda: True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_menu("Подменю", (("Один", lambda: None),), "Назад")
        self.assertEqual(prompts, ["Выбор › "])
        self.assertIn("Один", out.getvalue())
        self.assertIn("Назад", out.getvalue())


if __name__ == "__main__":
    unittest.main()
