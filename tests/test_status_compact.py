"""fw-menu: короткий «Статус файрвола» (1 → 1) и подробный (1 → 5)."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_compact", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)

FULL = """Проверка файлов и команд
  OK /opt/fw/fw.conf
  OK nft
  Доверенных адресов и подсетей — 20
Проверка reverse proxy и Inbound на хосте
Inbound — TCP *:443 (rw-core)

Профиль сервера — нода RemnaWave
ufw — активен
Расхождений с настройками нет.

Настройки
  LAN_ALLOW ............ выключено
  SSH_RATE_LIMIT ....... 20 в минуту   ← включено
  BLOCKLIST_SSH ........ включено
  CONFIRM_TIMEOUT ...... 60 с

Служба и таймер
Служба обновления — загружена
Последнее обновление — успешно
Запуск службы при загрузке сервера — включён
Автозапуск таймера — включён
Статус таймера — активен
Следующее обновление — 28.11.2026 в 05:19:22 (МСК)

Загруженные правила (счётчики с момента последнего применения правил)
Списки адресов (записей после объединения подсетей):
  Доверенные, пропускаются всегда ........ IPv4: 19, IPv6: 1

Как проверяется каждое новое входящее подключение
(сверху вниз, срабатывает первое подходящее):
  1. Всё остальное — пропускается.

Технический вид правил: nft list table inet fw
"""


def load():
    menu = importlib.util.module_from_spec(SPEC)
    LOADER.exec_module(menu)
    return menu


class Compact(unittest.TestCase):
    def setUp(self):
        self.menu = load()

    def test_healthy_screen_drops_the_checklists(self):
        text = self.menu.compact_status(FULL)
        for gone in ("OK /opt/fw/fw.conf", "Проверка reverse proxy", "Inbound — TCP", "Профиль сервера", "LAN_ALLOW",
                     "CONFIRM_TIMEOUT", "Служба обновления", "Автозапуск таймера", "Списки адресов", "Технический вид"):
            self.assertNotIn(gone, text, gone)

    def test_keeps_what_is_on_and_what_is_next(self):
        text = self.menu.compact_status(FULL)
        self.assertIn("SSH_RATE_LIMIT", text)
        self.assertIn("Последнее обновление — успешно", text)
        self.assertIn("Следующее обновление — 28.11.2026", text)
        self.assertIn("Как проверяется каждое новое входящее подключение", text)
        self.assertIn("Всё остальное — пропускается.", text)

    def test_problems_stay_visible(self):
        broken = (FULL.replace("  OK nft", "  ПРОВЕРЬ nft")
                  .replace("Расхождений с настройками нет.", "Расхождения с настройками:\n  - порт 22 открыт")
                  .replace("Статус таймера — активен", "Статус таймера — ошибка")
                  .replace("CONFIRM_TIMEOUT ...... 60 с", "CONFIRM_TIMEOUT ...... 120 с   ← не по умолчанию"))
        text = self.menu.compact_status(broken)
        self.assertIn("ПРОВЕРЬ nft", text)
        self.assertIn("Расхождения с настройками:", text)
        self.assertIn("порт 22 открыт", text)
        self.assertIn("Статус таймера — ошибка", text)
        self.assertIn("не по умолчанию", text)

    def test_footer_points_to_the_detailed_screen(self):
        self.assertIn("1 → 5", self.menu.compact_status(FULL))

    def test_colored_lines_are_recognised(self):
        colored = FULL.replace("  OK nft", "  \033[31mПРОВЕРЬ nft\033[0m")
        self.assertIn("ПРОВЕРЬ nft", self.menu.compact_status(colored))

    def test_rules_not_loaded_message_survives(self):
        text = self.menu.compact_status("Загруженные правила (счётчики)\nТаблица inet fw пока не загружена.\n")
        self.assertIn("Таблица inet fw пока не загружена.", text)


class ShortSteps(unittest.TestCase):
    def pair(self, address, port):
        return {"elem": {"val": {"concat": [address, port]}, "expires": 600000, "counter": {"packets": 1, "bytes": 60}}}

    def render(self, stat_sets, comment, compact=True):
        menu = load()
        menu.STATUS_COMPACT = compact
        sets = [{"set": {"name": name, "elem": elems}} for name, elems in stat_sets.items()]
        rule = {"rule": {"chain": "input", "comment": comment, "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": {"set": [22]}}},
            {"counter": {"packets": 40, "bytes": 2400}}, {"drop": None}]}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.explain_rules([{"chain": {"name": "input", "table": "fw"}}] + sets + [rule])
        return out.getvalue()

    def test_blocklist_step_is_one_line_with_addresses(self):
        text = self.render({"bs4": [self.pair("198.51.100.7", 22), self.pair("198.51.100.7", 2222)], "bs6": []}, "blocklist-ssh")
        self.assertIn("SSH, порт 22: адреса из blocklist.de блокируются", text)
        self.assertRegex(text, r"отброшено за 7 дней: 1 адрес\n")
        self.assertNotIn("перебирали SSH за последние 48 часов", text)
        self.assertNotIn("Пакет — одно сетевое сообщение", text)

    def test_step_without_stats_shows_packets(self):
        text = self.render({}, "blocklist-ssh")
        self.assertRegex(text, r"отброшено пакетов: 40")

    def test_full_mode_is_unchanged(self):
        text = self.render({"bs4": [self.pair("198.51.100.7", 22)], "bs6": []}, "blocklist-ssh", compact=False)
        self.assertIn("перебирали SSH за последние 48 часов", text)
        self.assertIn("Пакет — одно сетевое сообщение", text)


class MenuEntries(unittest.TestCase):
    def test_short_first_detailed_last(self):
        source = (ROOT / "build" / "fw-menu").read_text(encoding="utf-8")
        a = source.index('("Статус файрвола", boxed("Статус файрвола", show_status_short')
        b = source.index('("Кто стучится", boxed("Кто стучится"')
        c = source.index('("Статус файрвола подробно"')
        self.assertLess(a, b)
        self.assertLess(b, c)
        self.assertLess(c, source.index('("Настроить доступ"'))


class SkipsWastedWork(unittest.TestCase):
    # Короткий статус не показывает проверку reverse proxy, значит и запускать её (docker, ss, nsenter) незачем.
    def setUp(self):
        from tests import test_status_layout as layout
        self.base = layout.Status("test_everything_fine_gives_one_green_line_first")
        self.base.setUp()
        self.menu = self.base.menu
        self.calls = []
        self.menu.warn_reverse_proxy = lambda: self.calls.append("proxy")

    def tearDown(self):
        self.base.tearDown()

    def run_status(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            self.menu.show_status(**kwargs)

    def test_short_status_does_not_probe_the_proxies(self):
        self.run_status(compact=True)
        self.assertEqual(self.calls, [])

    def test_detailed_status_still_does(self):
        self.run_status()
        self.assertEqual(self.calls, ["proxy"])


if __name__ == "__main__":
    unittest.main()
