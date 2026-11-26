"""Защитные проверки обновлятора: SSH-порт под геофильтром и предел размера загрузок."""
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
MAJOR = int(subprocess.check_output(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], text=True))


@unittest.skipIf(MAJOR < 4, "обновлятор использует Bash 4+")
class Guards(unittest.TestCase):
    def run_case(self, name, **values):
        env = {**os.environ, **values}
        result = subprocess.run(["bash", str(ROOT / "tests/run_updater.sh"), name, "--dry"],
                                cwd=ROOT, env=env, text=True, capture_output=True)
        return result.stdout + result.stderr

    # --- SSH-порт под геофильтром
    def test_ssh_port_in_geo_ports_warns(self):
        output = self.run_case("ssh_geo", TEST_CONF_APPEND='GEO_PORTS="22 443"')
        self.assertIn("порт SSH 22 входит в GEO_PORTS", output)
        self.assertIn("ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ", output)

    def test_custom_ssh_port_in_geo_ports_warns(self):
        output = self.run_case("ssh_geo_custom", STUB_SSH_PORT="11022",
                               TEST_CONF_APPEND='GEO_PORTS="443 11022"')
        self.assertIn("порт SSH 11022 входит в GEO_PORTS", output)

    def test_ssh_port_outside_geo_ports_is_quiet(self):
        output = self.run_case("ssh_geo_quiet", STUB_SSH_PORT="11022")
        self.assertNotIn("порт SSH", output)
        self.assertIn("ИТОГ: ВСЁ В ПОРЯДКЕ", output)

    def test_similar_port_number_is_not_a_match(self):
        output = self.run_case("ssh_geo_similar", TEST_CONF_APPEND='GEO_PORTS="2222 443"')
        self.assertNotIn("порт SSH", output)

    # --- предел размера загрузок
    def test_oversized_geobase_is_rejected(self):
        output = self.run_case("big_geo", TEST_CONF_APPEND="MAX_GEO_BYTES=1000")
        self.assertIn("больше предела", output)
        self.assertIn("ИТОГ: ОШИБКА", output)

    def test_geobase_at_the_limit_is_accepted(self):
        probe = ROOT / "t" / "geo_size_probe.dat"
        probe.parent.mkdir(exist_ok=True)
        subprocess.run(["python3", "-I", str(ROOT / "tests/mkgeo.py"), str(probe)], check=True)
        size = probe.stat().st_size
        output = self.run_case("exact_geo", TEST_CONF_APPEND=f"MAX_GEO_BYTES={size}")
        self.assertNotIn("больше предела", output)
        self.assertIn("ИТОГ: ВСЁ В ПОРЯДКЕ", output)

    def test_oversized_scanner_list_is_rejected(self):
        output = self.run_case("big_list", TEST_CONF_APPEND="MAX_LIST_BYTES=10",
                               SCEN_LIST_CONTENT="203.0.113.0/24\n" * 20)
        self.assertIn("больше предела", output)

    def test_oversized_asn_answer_is_rejected(self):
        output = self.run_case("big_asn", TEST_CONF_APPEND="MAX_LIST_BYTES=40")
        self.assertIn("больше предела", output)

    def test_invalid_limit_is_an_error(self):
        output = self.run_case("bad_limit", TEST_CONF_APPEND="MAX_GEO_BYTES=много")
        self.assertIn("MAX_GEO_BYTES", output)
        self.assertIn("ИТОГ: ОШИБКА", output)


if __name__ == "__main__":
    unittest.main()


import importlib.machinery
import importlib.util
import types

LOADER = importlib.machinery.SourceFileLoader("fwmenu_guards", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class MenuDownloads(unittest.TestCase):
    """Меню тоже скачивает: проверка релизов, ответы RIPEstat. Большой ответ отбрасывается."""

    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.calls = []

    def fake_run(self, payload, code=0):
        def run(command, **kwargs):
            self.calls.append(command)
            return types.SimpleNamespace(returncode=code, stdout=payload, stderr="")
        self.menu.subprocess.run = run

    def test_release_text_over_limit_is_refused(self):
        self.fake_run(b"x" * 101)
        with self.assertRaises(OSError):
            self.menu.fetch_text("https://example.invalid/file", limit=100)

    def test_release_text_within_limit_is_read(self):
        self.fake_run(b"x" * 100)
        self.assertEqual(self.menu.fetch_text("https://example.invalid/file", limit=100), "x" * 100)

    def test_ripe_answer_over_limit_is_refused(self):
        self.fake_run("x" * (self.menu.MAX_REPLY_BYTES + 1))
        with self.assertRaises(ValueError) as caught:
            self.menu.announced_prefixes("AS64500")
        self.assertIn("больше предела", str(caught.exception))

    def test_ripe_request_carries_size_limit(self):
        self.fake_run("{}")
        with self.assertRaises(ValueError):
            self.menu.announced_prefixes("AS64500")
        self.assertIn("--max-filesize", self.calls[0])

    def test_owner_answer_over_limit_is_empty(self):
        self.fake_run("x" * (self.menu.MAX_OWNER_BYTES + 1))
        self.assertEqual(self.menu.fetch_ripe("203.0.113.5"), "")
        self.assertEqual(self.menu.fetch_whois("203.0.113.5"), "")
        self.assertEqual(self.menu.fetch_rdap("203.0.113.5"), "")

    def test_owner_request_carries_size_limit(self):
        self.fake_run("{}")
        self.menu.fetch_ripe("203.0.113.5")
        self.menu.fetch_whois("203.0.113.5")
        self.menu.fetch_rdap("203.0.113.5")
        for command in self.calls:
            self.assertIn("--max-filesize", command)


class InstallerSshWarning(unittest.TestCase):
    """В установщике предупреждение берётся из самого fw-install.sh: его кусок запускается отдельно."""

    @staticmethod
    def snippet():
        text = (ROOT / "fw-install.sh").read_text(encoding="utf-8")
        begin = text.index("# SSH-порт и геофильтр (установка)")
        end = text.index("# конец проверки SSH-порта", begin)
        return text[begin:end]

    def run_snippet(self, geo_ports, connection="", sshd_port="22"):
        stubs = ROOT / "t" / "installer_stubs"
        stubs.mkdir(parents=True, exist_ok=True)
        (stubs / "sshd").write_text(f"#!/bin/bash\necho 'port {sshd_port}'\n")
        (stubs / "sshd").chmod(0o755)
        env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "SSH_CONNECTION": connection}
        result = subprocess.run(["bash", "-c", f'geo_ports="{geo_ports}"\n' + self.snippet()],
                                env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout + result.stderr

    def test_warns_when_sshd_port_is_filtered(self):
        self.assertIn("порт SSH 22 входит в геофильтр", self.run_snippet("22 443"))

    def test_warns_when_connection_port_is_filtered(self):
        output = self.run_snippet("443 11022", connection="203.0.113.7 50000 192.0.2.10 11022", sshd_port="22")
        self.assertIn("порт SSH 11022 входит в геофильтр", output)

    def test_quiet_without_overlap(self):
        self.assertNotIn("входит в геофильтр",
                         self.run_snippet("443 8443", connection="203.0.113.7 50000 192.0.2.10 11022"))

    def test_similar_numbers_are_not_a_match(self):
        self.assertNotIn("входит в геофильтр", self.run_snippet("2222 443"))
