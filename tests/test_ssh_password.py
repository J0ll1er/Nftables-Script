"""Отключение и возврат входа по паролю, блок «Перебор на SSH»."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import tempfile
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
