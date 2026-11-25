"""fw-menu: настройка ограничения частоты SSH и пояснение правила в статусе."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_ssh", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class SshMenu(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.menu.DIRECTORY = self.dir
        self.menu.LOCAL_CONFIG = self.dir / "local.conf"
        self.menu.ssh_ports = lambda: [22]
        self.menu.admin_address = lambda: ""  # адрес администратора проверяет test_ssh_admin.py
        self.setting = (0, [])
        self.menu.ssh_rate_setting = lambda: self.setting

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, *answers):
        queue = list(answers)
        self.menu.input = lambda prompt="": queue.pop(0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.configure_ssh_limit()
        return out.getvalue()

    def test_saves_block_after_confirmation(self):
        self.menu.LOCAL_CONFIG.write_text('API_PORTS="2222"\n', encoding="utf-8")
        output = self.drive("20", "да")
        text = self.menu.LOCAL_CONFIG.read_text(encoding="utf-8")
        self.assertIn('API_PORTS="2222"', text)
        self.assertIn("# BEGIN FW-MENU SSH\nSSH_RATE_LIMIT=20\n# END FW-MENU SSH\n", text)
        self.assertIn("Проверить и применить правила", output)

    def test_enter_and_no_change_nothing(self):
        self.drive("")
        self.assertFalse(self.menu.LOCAL_CONFIG.exists())
        self.drive("20", "нет")
        self.assertFalse(self.menu.LOCAL_CONFIG.exists())

    def test_too_small_or_wrong_value_is_rejected(self):
        for value in ("3", "abc", "-5", "100000"):
            with self.assertRaises(ValueError):
                self.drive(value)
        self.assertFalse(self.menu.LOCAL_CONFIG.exists())

    def test_zero_writes_explicit_off_that_overrides_fw_conf(self):
        self.setting = (20, [])
        self.drive("0", "да")
        self.assertIn("SSH_RATE_LIMIT=0", self.menu.LOCAL_CONFIG.read_text(encoding="utf-8"))

    def test_second_save_replaces_the_block_not_duplicates_it(self):
        self.drive("20", "да")
        self.setting = (20, [])
        self.drive("40", "да")
        text = self.menu.LOCAL_CONFIG.read_text(encoding="utf-8")
        self.assertEqual(text.count("BEGIN FW-MENU SSH"), 1)
        self.assertIn("SSH_RATE_LIMIT=40", text)
        self.assertNotIn("SSH_RATE_LIMIT=20", text)

    def test_same_value_is_not_rewritten(self):
        self.setting = (20, [])
        self.assertIn("уже действующее", self.drive("20"))
        self.assertFalse(self.menu.LOCAL_CONFIG.exists())

    def test_menu_has_the_entry(self):
        source = (ROOT / "build" / "fw-menu").read_text(encoding="utf-8")
        self.assertIn('("Ограничить частоту новых SSH-подключений", configure_ssh_limit)', source)


@unittest.skipUnless(shutil.which("nft") and shutil.which("unshare"), "нужны nft и unshare")
class StatusExplains(unittest.TestCase):
    def test_status_describes_the_ssh_rule(self):
        menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(menu)
        ruleset = ('add table inet fw\ntable inet fw {\n set tr4 { type ipv4_addr; flags interval; }\n set tr6 { type ipv6_addr; flags interval; }\n'
                   ' set sr4 { type ipv4_addr . inet_service; flags dynamic,timeout; timeout 7d; counter; }\n'
                   ' set sr6 { type ipv6_addr . inet_service; flags dynamic,timeout; timeout 7d; counter; }\n'
                   ' set sc4 { type ipv4_addr; flags interval; }\n set sc6 { type ipv6_addr; flags interval; }\n'
                   ' set ru4 { type ipv4_addr; flags interval; }\n set ru6 { type ipv6_addr; flags interval; }\n'
                   ' chain input { type filter hook input priority -10; policy accept;\n  ct state established,related accept\n'
                   '  ip saddr @tr4 accept\n'
                   '  tcp dport { 22, 11022 } ct state new meter ssh4 { ip saddr timeout 5m limit rate over 20/minute burst 10 packets } add @sr4 { ip saddr . tcp dport } counter drop comment "ssh-rate"\n'
                   '  tcp dport { 22, 11022 } ct state new meter ssh6 { ip6 saddr & ffff:ffff:ffff:ffff:: timeout 5m limit rate over 20/minute burst 10 packets } add @sr6 { ip6 saddr . tcp dport } counter drop comment "ssh-rate"\n'
                   '  meta l4proto { tcp, udp } th dport { 443 } ip saddr != @ru4 counter drop comment "non-RU"\n }\n}\n')
        out = subprocess.run(["unshare", "-n", "sh", "-c", "nft -f - && nft -j list table inet fw"], input=ruleset,
                             text=True, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            menu.explain_rules(json.loads(out.stdout)["nftables"])
        text = buffer.getvalue()
        self.assertIn("не больше 20 новых подключений в минуту", text)
        self.assertIn("22, 11022", text)
        self.assertEqual(text.count("не больше 20 новых"), 1)  # два правила (IPv4 и IPv6) — одна строка
        # счётчик стоит отдельной строкой под пунктом, а не внутри предложения
        self.assertRegex(text, r"не затрагивает\.\n\s+Отброшено пакетов: 0")
        self.assertNotIn("(отброшено", text)



class StatusSettingsLine(unittest.TestCase):
    def status(self, rate, chain_text, admin=""):
        menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(menu)

        def probe(*command, **kwargs):
            if command[0] == "bash":
                return f"\n60\n3\n0.2\n14\n{rate}\n"
            if command[0] == "nft":
                return chain_text
            return ""
        menu.probe_output = probe
        menu.trusted_networks = lambda: []
        menu.trusted_names = lambda: []
        menu.admin_address = lambda: admin
        menu.lan_allow_networks = lambda: []
        for name in ("warn_old_trusted", "warn_reverse_proxy", "show_profile"):
            setattr(menu, name, lambda *args, **kwargs: None)
        menu.systemd_properties = lambda *args, **kwargs: {}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                menu.show_status()
            except Exception:
                pass  # дальше идёт чтение systemd и nft, в тесте их нет; нужная строка уже выведена
        return out.getvalue()

    def test_shows_setting_and_warns_when_rule_is_not_loaded(self):
        text = self.status(20, "chain input { }")
        self.assertRegex(text, r"SSH_RATE_LIMIT \.+ 20 в минуту")
        self.assertIn("правило ещё не загружено", text)

    def test_no_warning_when_rule_is_loaded(self):
        text = self.status(20, 'counter drop comment "ssh-rate"')
        self.assertRegex(text, r"SSH_RATE_LIMIT \.+ 20 в минуту")
        self.assertNotIn("ещё не загружено", text)

    def test_warns_when_admin_address_is_not_trusted(self):
        text = self.status(20, 'counter drop comment "ssh-rate"', admin="198.51.100.7")
        self.assertIn("ты подключён с 198.51.100.7, его нет в доверенных", text)

    def test_no_admin_warning_when_limit_is_off(self):
        self.assertNotIn("его нет в доверенных", self.status(0, "", admin="198.51.100.7"))

    def test_off_is_shown_without_warning(self):
        text = self.status(0, "chain input { }")
        self.assertRegex(text, r"SSH_RATE_LIMIT \.+ выключено")
        self.assertNotIn("ещё не загружено", text)


if __name__ == "__main__":
    unittest.main()
