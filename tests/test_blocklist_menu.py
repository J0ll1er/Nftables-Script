"""fw-menu: настройка списка blocklist.de, строка в «Кто стучится», пометки в «Перебор на SSH», статус."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_bl", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


def load():
    menu = importlib.util.module_from_spec(SPEC)
    LOADER.exec_module(menu)
    return menu


class Configure(unittest.TestCase):
    def setUp(self):
        self.menu = load()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.menu.DIRECTORY = self.dir
        self.menu.LOCAL_CONFIG = self.dir / "local.conf"
        self.menu.admin_address = lambda: ""
        self.on = False
        self.menu.blocklist_setting = lambda: self.on

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, *answers):
        queue = list(answers)
        self.menu.input = lambda prompt="": queue.pop(0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.configure_blocklist_ssh()
        return out.getvalue()

    def test_enable_saves_block_and_keeps_other_settings(self):
        self.menu.LOCAL_CONFIG.write_text('API_PORTS="2222"\n', encoding="utf-8")
        output = self.drive("да")
        text = self.menu.LOCAL_CONFIG.read_text(encoding="utf-8")
        self.assertIn('API_PORTS="2222"', text)
        self.assertIn("# BEGIN FW-MENU BLOCKLIST\nBLOCKLIST_SSH=1\n# END FW-MENU BLOCKLIST\n", text)
        self.assertIn("Проверить и применить правила", output)

    def test_disable_writes_explicit_zero(self):
        self.on = True
        self.drive("да")
        self.assertIn("BLOCKLIST_SSH=0", self.menu.LOCAL_CONFIG.read_text(encoding="utf-8"))

    def test_refusal_and_enter_change_nothing(self):
        self.drive("нет")
        self.drive("")
        self.assertFalse(self.menu.LOCAL_CONFIG.exists())

    def test_second_save_replaces_the_block(self):
        self.drive("да")
        self.on = True
        self.drive("да")
        text = self.menu.LOCAL_CONFIG.read_text(encoding="utf-8")
        self.assertEqual(text.count("BEGIN FW-MENU BLOCKLIST"), 1)
        self.assertIn("BLOCKLIST_SSH=0", text)

    def test_ssh_block_is_left_alone(self):
        self.menu.LOCAL_CONFIG.write_text("# BEGIN FW-MENU SSH\nSSH_RATE_LIMIT=20\n# END FW-MENU SSH\n", encoding="utf-8")
        self.drive("да")
        text = self.menu.LOCAL_CONFIG.read_text(encoding="utf-8")
        self.assertIn("SSH_RATE_LIMIT=20", text)
        self.assertIn("BLOCKLIST_SSH=1", text)

    def test_menu_has_the_entry(self):
        source = (ROOT / "build" / "fw-menu").read_text(encoding="utf-8")
        self.assertIn("configure_blocklist_ssh),", source)

    def test_rule_kind_knows_the_comment(self):
        self.assertEqual(self.menu.rule_kind({"comment": "blocklist-ssh", "expr": []}), "blocklist")


class HitsLine(unittest.TestCase):
    ROWS = ("elements = { 198.51.100.7 . 22 counter packets 5 bytes 300 expires 6d, "
            "198.51.100.7 . 2222 counter packets 2 bytes 120 expires 6d, "
            "203.0.113.9 . 22 counter packets 3 bytes 180 expires 6d }")

    def setUp(self):
        self.menu = load()
        self.menu.lookup_owner = lambda address: ""
        self.menu.screen_columns = lambda: 120
        self.menu.show_ssh_attempts = lambda say: False
        self.sets = {}
        self.menu.probe_output = lambda *c, **_: self.sets.get(c[-1], "")

    def show(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_hits()
        return out.getvalue()

    def test_line_counts_attempts_and_addresses(self):
        self.sets = {"bs4": self.ROWS}
        text = self.show()
        self.assertIn("Из списка blocklist.de", text)
        self.assertIn("попыток на SSH: 10, адресов: 2", text)

    def test_enabled_but_quiet_says_so(self):
        self.sets = {"bs4": "elements = { }", "sn4": self.ROWS.replace("22", "443")}
        text = self.show()
        self.assertIn("Из списка blocklist.de", text)
        self.assertIn("никто из него не стучался", text)

    def test_nothing_without_the_sets(self):
        self.sets = {"sn4": self.ROWS.replace("22", "443")}
        self.assertNotIn("blocklist.de", self.show())


class Marks(unittest.TestCase):
    def setUp(self):
        self.menu = load()

    def test_listed_addresses_are_found_by_nft_get_element(self):
        calls = []

        def probe(*command, **_):
            calls.append(command)
            return "table inet fw { set bl4 { elements = { 198.51.100.7 } } }" if "198.51.100.7" in command[-1] else ""
        self.menu.probe_output = probe
        listed = self.menu.blocklist_listed(["198.51.100.7", "203.0.113.9", "garbage"])
        self.assertEqual(listed, {"198.51.100.7"})
        self.assertIn("bl4", calls[0])

    def test_ipv6_uses_the_v6_set(self):
        calls = []
        self.menu.probe_output = lambda *c, **_: calls.append(c) or ""
        self.menu.blocklist_listed(["2001:db8:5::1"])
        self.assertIn("bl6", calls[0])

    def test_attempts_block_marks_listed_addresses(self):
        menu = self.menu
        menu.password_login_open = lambda: False
        menu.ssh_login_log = lambda since: ["x"]
        menu.ssh_attempts = lambda log: {"198.51.100.7": {"conns": [1, 2], "users": {"root": 2}},
                                         "203.0.113.9": {"conns": [1], "users": {"admin": 1}}}
        menu.resolve_owners = lambda addresses, kind, sources: {}
        menu.blocklist_listed = lambda addresses: {"198.51.100.7"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.show_ssh_attempts(lambda text: print(text))
        lines = out.getvalue().splitlines()
        listed = next(line for line in lines if line.lstrip().startswith("198.51.100.7"))
        other = next(line for line in lines if line.lstrip().startswith("203.0.113.9"))
        self.assertIn("blocklist.de", listed)
        self.assertNotIn("blocklist.de", other)


class Updated(unittest.TestCase):
    def check(self, age_seconds):
        import os, time
        menu = load()
        with tempfile.TemporaryDirectory() as tmp:
            menu.DIRECTORY = Path(tmp)
            self.assertEqual(menu.blocklist_updated(), "")
            cache = Path(tmp) / "scanner-cache"
            cache.mkdir()
            (cache / "blocklist_ssh").write_text("198.51.100.7\n")
            past = time.time() - age_seconds
            os.utime(cache / "blocklist_ssh", (past, past))
            return menu.blocklist_updated()

    def test_ages_are_readable(self):
        self.assertEqual(self.check(600), "меньше часа назад")
        self.assertEqual(self.check(5 * 3600 + 100), "5 ч назад")
        self.assertEqual(self.check(3 * 86400), "3 дн. назад")


class StatusText(unittest.TestCase):
    def test_status_explains_the_rule(self):
        menu = load()
        menu.blocklist_updated = lambda: "3 ч назад"
        sets = [{"set": {"name": name}} for name in ("bl4", "bl6")]
        rule = {"rule": {"chain": "input", "comment": "blocklist-ssh", "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": {"set": [22]}}},
            {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": "@bl4"}},
            {"set": {"op": "add", "elem": {}, "set": "@bs4"}},
            {"counter": {"packets": 4, "bytes": 240}}, {"drop": None}]}}
        chain = {"chain": {"name": "input", "table": "fw"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.explain_rules([chain] + sets + [rule])
        text = out.getvalue()
        self.assertIn("blocklist.de", text)
        self.assertIn("порт 22", text)
        self.assertRegex(text, r"Отброшено пакетов: 4")
        self.assertRegex(text, r"Адреса blocklist\.de, блокируются на SSH")
        self.assertIn("обновляется вместе с правилами", text)
        self.assertIn("обновлён 3 ч назад", text)


class AddressCounts(unittest.TestCase):
    def pair(self, address, port):
        return {"elem": {"val": {"concat": [address, port]}, "expires": 600000, "counter": {"packets": 1, "bytes": 60}}}

    def render(self, stat_sets, comment, extra_expr=()):
        menu = load()
        sets = [{"set": {"name": name, "elem": elems}} for name, elems in stat_sets.items()]
        rule = {"rule": {"chain": "input", "comment": comment, "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": {"set": [22]}}},
            *extra_expr, {"counter": {"packets": 40, "bytes": 2400}}, {"drop": None}]}}
        chain = {"chain": {"name": "input", "table": "fw"}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.explain_rules([chain] + sets + [rule])
        return out.getvalue()

    def test_blocklist_step_counts_distinct_addresses_not_pairs(self):
        text = self.render({"bs4": [self.pair("198.51.100.7", 22), self.pair("198.51.100.7", 2222), self.pair("203.0.113.9", 22)],
                            "bs6": [self.pair("2001:db8:5::1", 22)]}, "blocklist-ssh",
                           [{"set": {"op": "add", "elem": {}, "set": "@bs4"}}])
        self.assertRegex(text, r"Отброшено пакетов: 40.*Адресов за 7 дней: 3")

    def test_scanner_step_gets_the_same_line(self):
        text = self.render({"ss4": [self.pair("192.0.2.5", 80), self.pair("192.0.2.6", 80)], "ss6": []}, "scanners")
        self.assertRegex(text, r"Отброшено пакетов: 40.*Адресов за 7 дней: 2")

    def test_no_address_count_without_the_stats_sets(self):
        text = self.render({}, "scanners")
        self.assertIn("Отброшено пакетов: 40", text)
        self.assertNotIn("Адресов за 7 дней", text)

    def test_packet_is_explained_once(self):
        text = self.render({"ss4": [self.pair("192.0.2.5", 80)], "ss6": []}, "scanners")
        self.assertEqual(text.count("Пакет — одно сетевое сообщение"), 1)

    def test_blocklist_step_says_how_the_list_is_refreshed(self):
        text = self.render({}, "blocklist-ssh")
        self.assertIn("обновляется вместе с правилами", text)


if __name__ == "__main__":
    unittest.main()
