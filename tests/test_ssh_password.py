"""Отключение и возврат входа по паролю, блок «Перебор на SSH»."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_sshpass", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)

LOG = """\
Nov 26 10:00:01 h sshd[1]: Invalid user admin from 5f00::1 port 1111
Nov 26 10:00:02 h sshd[1]: Failed password for invalid user admin from 5f00::1 port 1111 ssh2
Nov 26 10:00:03 h sshd[2]: Invalid user test from 5f00::1 port 2222
Nov 26 10:00:04 h sshd[3]: Failed password for root from 100.64.0.9 port 3333 ssh2
Nov 26 10:00:05 h sshd[4]: Accepted publickey for root from 100.64.0.1 port 4444 ssh2: ED25519 SHA256:x
"""


class Password(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.menu.SSH_NOPASS_FILE = Path(self.tmp.name) / "10-fw.conf"

    def tearDown(self):
        self.tmp.cleanup()

    def sshd(self, text):
        self.menu.probe_output = lambda *command, **kw: text if command[:2] == ("sshd", "-T") else ""

    def test_password_open_by_default(self):
        self.sshd("")
        self.assertTrue(self.menu.password_login_open())

    def test_pam_path_keeps_password_open(self):
        self.sshd("passwordauthentication no\nkbdinteractiveauthentication yes\n")
        self.assertTrue(self.menu.password_login_open())

    def test_both_off_is_closed(self):
        self.sshd("passwordauthentication no\nkbdinteractiveauthentication no\n")
        self.assertFalse(self.menu.password_login_open())

    def test_attempts_count_connections_once(self):
        found = self.menu.ssh_attempts(LOG)
        self.assertEqual(set(found), {"5f00::1", "100.64.0.9"})
        self.assertEqual(len(found["5f00::1"]["conns"]), 2)
        self.assertEqual(found["5f00::1"]["users"], {"admin": 1, "test": 1})
        self.assertNotIn("100.64.0.1", found)

    def test_block_hidden_while_password_open(self):
        self.sshd("")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(self.menu.show_ssh_attempts(print))
        self.assertEqual(out.getvalue(), "")

    def test_block_shown_when_keys_only(self):
        self.sshd("passwordauthentication no\nkbdinteractiveauthentication no\n")
        self.menu.ssh_login_log = lambda since: LOG
        self.menu.resolve_owners = lambda addresses, kind, sources: {a: "Тест-хостинг" for a in addresses}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(self.menu.show_ssh_attempts(print))
        text = out.getvalue()
        self.assertIn("Перебор на SSH", text)
        self.assertIn("5f00::1", text)
        self.assertIn("admin", text)
        self.assertIn("Тест-хостинг", text)

    def test_short_owner_keeps_country_and_as(self):
        text = self.menu.short_owner("DMZHOST TECHOFF SRV LIMITED LONG NAME, AD, AS47890", 40)
        self.assertTrue(text.endswith(", AD, AS47890"))
        self.assertLessEqual(len(text), 40)
        self.assertEqual(self.menu.short_owner("bshield, TR, AS154383", 40), "bshield, TR, AS154383")

    def test_owner_falls_back_to_registry_without_asn(self):
        self.menu.DIRECTORY = Path(self.tmp.name)
        calls = []
        self.menu.fetch_ripe = lambda address: calls.append(address) or '{"data": {"asns": []}}'
        whois = '{"data": {"records": [[{"key": "netname", "value": "EXAMPLE-NET"}, {"key": "country", "value": "NL"}]]}}'
        self.menu.fetch_whois = lambda address: whois
        self.assertEqual(self.menu.lookup_owner("5f00::1"), "EXAMPLE-NET, NL")
        self.assertEqual(len(calls), 1)

    def test_owner_request_is_repeated_once_on_failure(self):
        self.menu.DIRECTORY = Path(self.tmp.name)
        calls = []
        self.menu.fetch_ripe = lambda address: calls.append(address) or ""
        self.menu.fetch_whois = lambda address: self.fail("при недоступном RIPEstat whois не нужен")
        self.menu.fetch_rdap = lambda address: ""
        self.assertEqual(self.menu.lookup_owner("5f00::1"), "")
        self.assertEqual(len(calls), 2)

    def test_rdap_is_used_when_ripestat_is_silent(self):
        self.menu.DIRECTORY = Path(self.tmp.name)
        self.menu.fetch_ripe = lambda address: ""
        self.menu.fetch_whois = lambda address: self.fail("whois не нужен")
        self.menu.fetch_rdap = lambda address: (
            '{"name": "NET-1", "country": "US", "entities": ['
            '{"roles": ["abuse"], "vcardArray": ["vcard", [["fn", {}, "text", "Abuse"]]]},'
            '{"roles": ["registrant"], "vcardArray": ["vcard", [["fn", {}, "text", "Example Hosting LLC"]]]}]}')
        self.assertEqual(self.menu.lookup_owner("5f00::1"), "Example Hosting LLC, US")

    def test_rdap_abuse_contact_is_not_owner(self):
        text = '{"name": "BESTDC_LIMITED", "country": "AD", "entities": [{"roles": ["abuse"], "vcardArray": ["vcard", [["fn", {}, "text", "Abuse"]]]}]}'
        self.assertEqual(self.menu.parse_rdap_owner(text), ("BESTDC_LIMITED", "AD"))

    def test_rdap_garbage_is_empty(self):
        self.assertEqual(self.menu.parse_rdap_owner("не json"), ("", ""))
        self.assertEqual(self.menu.parse_rdap_owner("[]"), ("", ""))

    def test_logins_group_by_method_user_address(self):
        log = ("sshd[1]: Accepted publickey for root from 100.64.0.1 port 1 ssh2: ED25519 SHA256:a\n"
               "sshd[2]: Accepted publickey for root from 100.64.0.1 port 2 ssh2: ED25519 SHA256:a\n"
               "sshd[3]: Accepted password for bob from 5f00::1 port 3 ssh2\n")
        found = self.menu.ssh_logins(log)
        self.assertEqual(found[("publickey", "root", "100.64.0.1")], 2)
        self.assertEqual(found[("password", "bob", "5f00::1")], 1)

    def test_logins_screen_names_method_and_address(self):
        self.menu.ssh_login_log = lambda since: "sshd[3]: Accepted password for bob from 5f00::1 port 3 ssh2\n"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_ssh_logins()
        self.assertIn("по паролю", out.getvalue())
        self.assertIn("5f00::1", out.getvalue())

    def test_key_presence_shows_addresses_and_online(self):
        log = ("sshd[1]: Accepted publickey for root from 100.64.0.1 port 11 ssh2: ED25519 SHA256:abc\n"
               "sshd[2]: Accepted publickey for root from 100.64.0.1 port 12 ssh2: ED25519 SHA256:abc\n"
               "sshd[3]: Accepted publickey for root from 5f00::1 port 13 ssh2: ED25519 SHA256:other\n")
        online = self.menu.key_presence_lines("SHA256:abc", log, {("100.64.0.1", "12")})
        self.assertEqual(online[0], "входили с адресов: 100.64.0.1 (2)")
        self.assertEqual(online[1], "сейчас в сети, адрес: 100.64.0.1")
        offline = self.menu.key_presence_lines("SHA256:abc", log, set())
        self.assertEqual(offline[1], "сейчас не в сети")
        self.assertEqual(self.menu.key_presence_lines("SHA256:none", log, set()), [])
        self.assertEqual(self.menu.key_presence_lines("", log, set()), [])

    def test_established_clients_parse_v4_and_v6(self):
        self.menu.probe_output = lambda *c, **k: "0 0 100.64.0.2:22 100.64.0.1:5555\n0 0 [5f00::2]:22 [5f00::1]:6666\n"
        self.assertEqual(self.menu.ssh_established_clients(), {("100.64.0.1", "5555"), ("5f00::1", "6666")})

    def test_owner_name_has_no_url_decoration(self):
        self.assertEqual(self.menu.tidy_owner("https://dmzhost.co, NL, AS47890"), "dmzhost.co, NL, AS47890")
        self.assertEqual(self.menu.tidy_owner("http://www.example.com/, DE, AS1"), "example.com, DE, AS1")
        self.assertEqual(self.menu.tidy_owner("TM ONE Cloud, MY, AS18206"), "TM ONE Cloud, MY, AS18206")

    def test_cached_owner_is_tidied_on_read(self):
        self.menu.DIRECTORY = Path(self.tmp.name)
        (Path(self.tmp.name) / self.menu.OWNER_CACHE_FILE).write_text(
            '{"5f00::1": ["https://dmzhost.co, NL, AS47890", %d]}' % int(time.time()))
        self.assertEqual(self.menu.lookup_owner("5f00::1"), "dmzhost.co, NL, AS47890")

    def test_restore_removes_file(self):
        self.menu.SSH_NOPASS_FILE.write_text("x")
        self.menu.sshd_config_valid = lambda: True
        self.menu.reload_sshd = lambda: True
        self.menu.stop_nopass_timer = lambda: None
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.menu.restore_password_login())
        self.assertFalse(self.menu.SSH_NOPASS_FILE.exists())

    def test_disable_refused_without_key_session(self):
        self.sshd("")
        self.menu.current_session_used_key = lambda: False
        self.menu.ssh_session_sockets = lambda: (("100.64.0.2", 22), ("100.64.0.7", 5555))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.disable_password_login()
        self.assertIn("не по ключу", out.getvalue())
        self.assertIn("100.64.0.7", out.getvalue())
        self.assertFalse(self.menu.SSH_NOPASS_FILE.exists())


if __name__ == "__main__":
    unittest.main()
