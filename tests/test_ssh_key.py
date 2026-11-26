"""SSH-ключ для входа: создание, подстановка пользователя и адреса, удаление приватной части после первого входа."""
import base64
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_sshkey", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
ME = pwd.getpwuid(os.getuid()).pw_name


def blob(letter):
    return base64.b64encode((letter * 40).encode()).decode()


class SshKey(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.menu.DIRECTORY = self.dir
        self.menu.SSH_KEYS_FILE = self.dir / "ssh-keys.json"
        self.authorized = self.home / ".ssh" / "authorized_keys"
        self.menu.authorized_keys_path = lambda user: self.authorized
        self.letter = "a"
        self.log = ""
        self.calls = []
        self.environ = {"SSH_CONNECTION": "100.64.0.5 50000 100.64.0.10 22", "SUDO_USER": ME}
        os.environ.update(self.environ)
        self.addCleanup(lambda: [os.environ.pop(key, None) for key in self.environ])

        def fake_generate(path, comment, with_passphrase):
            path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nQUJD\n-----END OPENSSH PRIVATE KEY-----\n")
            Path(str(path) + ".pub").write_text(f"ssh-ed25519 {blob(self.letter)} {comment}\n")
        self.menu.generate_ssh_key = fake_generate
        self.menu.schedule_ssh_sweep = lambda: self.calls.append("schedule")
        self.menu.ssh_login_log = lambda since: self.log

        def probe(*command, **kwargs):
            if command[:2] == ("sshd", "-T"):
                return "pubkeyauthentication yes\n"
            return ""
        self.menu.probe_output = probe
        real_pwnam = pwd.getpwnam

        def fake_pwnam(name):
            entry = real_pwnam(name)
            return pwd.struct_passwd((entry.pw_name, entry.pw_passwd, entry.pw_uid, entry.pw_gid,
                                      entry.pw_gecos, str(self.home), entry.pw_shell))
        self.addCleanup(setattr, pwd, "getpwnam", real_pwnam)
        pwd.getpwnam = fake_pwnam
        self.run_original = subprocess.run
        self.menu.subprocess.run = lambda command, *args, **kwargs: (
            self.calls.append(list(command)) or subprocess.CompletedProcess(command, 0, "", ""))
        self.addCleanup(setattr, self.menu.subprocess, "run", self.run_original)

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, function, *answers):
        queue = list(answers)
        self.menu.input = lambda prompt="": queue.pop(0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            function()
        return out.getvalue()

    def create(self):
        return self.drive(self.menu.create_ssh_key, "", "")

    def test_create_adds_public_key_and_keeps_private_file_with_safe_mode(self):
        text = self.create()
        item = self.menu.load_ssh_keys()[0]
        self.assertEqual(item["user"], ME)
        self.assertEqual(item["state"], "pending")
        self.assertIn(blob("a"), self.authorized.read_text())
        private = Path(item["file"])
        self.assertTrue(private.is_file())
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(private.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(Path(str(private) + ".pub").exists())
        self.assertIn("-----BEGIN OPENSSH PRIVATE KEY-----", text)
        self.assertIn(item["fingerprint"], text)
        self.assertIn("schedule", self.calls)

    def test_scp_hint_has_user_address_and_path(self):
        text = self.create()
        item = self.menu.load_ssh_keys()[0]
        self.assertIn(f"scp {ME}@100.64.0.10:{item['file']} .", text)
        self.assertNotIn("-P", text.split("scp", 1)[1].split("\n", 1)[0])

    def test_non_default_port_is_added(self):
        os.environ["SSH_CONNECTION"] = "100.64.0.5 50000 100.64.0.10 2222"
        self.assertIn(f"scp -P 2222 {ME}@100.64.0.10:", self.create())

    def test_ipv6_address_goes_in_brackets(self):
        os.environ["SSH_CONNECTION"] = "100.64.0.5 50000 5f00::1 22"
        self.assertIn(f"scp {ME}@[5f00::1]:", self.create())

    def test_private_address_is_replaced_by_placeholder(self):
        os.environ["SSH_CONNECTION"] = "100.64.0.5 50000 10.0.0.5 22"
        text = self.create()
        self.assertIn("@IP_ВАШЕГО_СЕРВЕРА:", text)
        self.assertIn("публичный адрес сервера", text)

    def test_user_comes_from_sudo_user_and_defaults_to_root(self):
        self.assertEqual(self.menu.ssh_key_user(), ME)
        os.environ.pop("SUDO_USER")
        self.menu.probe_output = lambda *command, **kwargs: ""
        self.assertEqual(self.menu.ssh_key_user(), "root")

    def test_unknown_user_is_refused(self):
        out = self.drive(self.menu.create_ssh_key, "no-such-user-xyz", "")
        self.assertIn("нет", out)
        self.assertEqual(self.menu.load_ssh_keys(), [])

    def test_private_part_is_removed_after_first_login(self):
        self.create()
        item = self.menu.load_ssh_keys()[0]
        self.menu.sweep_ssh_keys()
        self.assertTrue(Path(item["file"]).is_file())
        self.log = f"Accepted publickey for {ME} from 100.64.0.5 port 1 ssh2: ED25519 {item['fingerprint']}\n"
        self.menu.sweep_ssh_keys()
        self.assertFalse(Path(item["file"]).exists())
        saved = self.menu.load_ssh_keys()[0]
        self.assertEqual(saved["state"], "used")
        self.assertIn(blob("a"), self.authorized.read_text())

    def test_login_with_another_key_does_not_count(self):
        self.create()
        item = self.menu.load_ssh_keys()[0]
        self.log = f"Accepted publickey for {ME} from 100.64.0.5 port 1 ssh2: ED25519 SHA256:другой\n"
        self.menu.sweep_ssh_keys()
        self.assertTrue(Path(item["file"]).is_file())

    def test_empty_fingerprint_never_counts_as_a_login(self):
        self.create()
        items = self.menu.load_ssh_keys()
        items[0]["fingerprint"] = ""
        self.menu.save_ssh_keys(items)
        self.log = f"Accepted publickey for {ME} from 100.64.0.5 port 1 ssh2: ED25519 SHA256:любой\n"
        self.menu.sweep_ssh_keys()
        self.assertTrue(Path(items[0]["file"]).is_file())
        self.assertEqual(self.menu.load_ssh_keys()[0]["state"], "pending")

    def test_unused_key_expires_after_a_day_and_is_revoked(self):
        self.create()
        items = self.menu.load_ssh_keys()
        items[0]["created"] = time.time() - self.menu.SSH_KEY_TTL - 10
        self.menu.save_ssh_keys(items)
        self.menu.sweep_ssh_keys()
        self.assertEqual(self.menu.load_ssh_keys(), [])
        self.assertNotIn(blob("a"), self.authorized.read_text())
        self.assertFalse(Path(items[0]["file"]).exists())

    def test_show_again_works_until_first_login(self):
        self.create()
        self.assertIn("-----BEGIN OPENSSH PRIVATE KEY-----", self.drive(self.menu.show_ssh_key_again))
        item = self.menu.load_ssh_keys()[0]
        self.log = f"Accepted publickey for {ME} ... {item['fingerprint']}\n"
        self.assertIn("Нет ключей", self.drive(self.menu.show_ssh_key_again))

    def test_remove_deletes_only_the_chosen_key(self):
        self.authorized.parent.mkdir(parents=True)
        self.authorized.write_text(f"ssh-ed25519 {blob('z')} чужой-ключ\n")
        self.create()
        self.letter = "b"
        self.create()
        self.assertEqual(len(self.menu.load_ssh_keys()), 2)
        self.drive(self.menu.remove_ssh_key, "1", "да")
        text = self.authorized.read_text()
        self.assertNotIn(blob("a"), text)
        self.assertIn(blob("b"), text)
        self.assertIn(blob("z"), text)
        self.assertEqual(len(self.menu.load_ssh_keys()), 1)

    def test_same_second_keys_get_different_files(self):
        self.create()
        self.letter = "b"
        self.create()
        files = {item["file"] for item in self.menu.load_ssh_keys()}
        self.assertEqual(len(files), 2)

    def test_warns_when_pubkey_auth_is_off(self):
        self.menu.probe_output = lambda *command, **kwargs: "pubkeyauthentication no\n" if command[:2] == ("sshd", "-T") else ""
        self.menu.paint = lambda code, text, stream=None: print(text)
        self.assertIn("PubkeyAuthentication no", self.create())

    def test_fingerprint_matches_openssh_style(self):
        fingerprint = self.menu.ssh_fingerprint(f"ssh-ed25519 {blob('a')} x")
        self.assertTrue(fingerprint.startswith("SHA256:"))
        self.assertNotIn("=", fingerprint)


if __name__ == "__main__":
    unittest.main()
