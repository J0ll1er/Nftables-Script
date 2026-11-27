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


class StatusText(unittest.TestCase):
    def test_status_explains_the_rule(self):
        menu = load()
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


if __name__ == "__main__":
    unittest.main()
