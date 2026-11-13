"""Блок «Что ещё есть на сервере»: порт 80, хуки продления, SSH, таблицы nftables."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import types
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_overview", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
UFW_ACTIVE = "Status: active\nDefault: deny (incoming), allow (outgoing), disabled (routed)\n\n22/tcp ALLOW IN Anywhere\n443/tcp ALLOW IN Anywhere\n"
LISTEN = {("tcp", 80, "nginx", False), ("tcp", 443, "nginx", False), ("tcp", 22, "sshd", False)}


class Overview(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.menu.certbot_renewals = lambda: [("example.com", {"authenticator": "nginx"})]
        self.menu.renewal_hook_files = lambda: {}
        self.menu.renewal_cron_lines = lambda: []
        self.menu.acme_sh_present = lambda: False
        self.menu.acme_sh_modes = lambda: []
        self.policy = self.menu.ufw_policy(UFW_ACTIVE)

    def report(self, policy="default", listeners=LISTEN):
        return "\n".join(self.menu.port80_report(self.policy if policy == "default" else policy, listeners))

    def test_closed_port_without_any_way_to_open_it_warns(self):
        text = self.report()
        self.assertIn("слушает, снаружи закрыт ufw", text)
        self.assertIn("ВНИМАНИЕ", text)

    def test_renewal_hooks_are_recognised(self):
        self.menu.renewal_hook_files = lambda: {"pre": ["open80.sh"], "post": ["close80.sh"]}
        text = self.report()
        self.assertIn("Хуки certbot (pre): open80.sh", text)
        self.assertNotIn("ВНИМАНИЕ", text)

    def test_hook_in_renewal_conf_is_recognised(self):
        self.menu.certbot_renewals = lambda: [("example.com", {"authenticator": "standalone", "pre_hook": "ufw allow 80"})]
        self.assertNotIn("ВНИМАНИЕ", self.report())

    def test_cron_with_ufw_counts_as_a_way_to_open_the_port(self):
        self.menu.renewal_cron_lines = lambda: [("/etc/cron.d/renew", "0 3 * * 1 root ufw allow 80 && certbot renew && ufw delete allow 80")]
        text = self.report()
        self.assertIn("Задание cron", text)
        self.assertNotIn("ВНИМАНИЕ", text)

    def test_dns_challenge_does_not_need_port_80(self):
        self.menu.certbot_renewals = lambda: [("example.com", {"authenticator": "dns-cloudflare"})]
        text = self.report()
        self.assertIn("порт 80 для продления не нужен", text)
        self.assertNotIn("ВНИМАНИЕ", text)

    def test_nothing_found_says_so(self):
        self.menu.certbot_renewals = lambda: []
        self.assertIn("certbot и acme.sh не найдены", self.report())

    def test_acme_sh_is_not_guessed_about(self):
        self.menu.certbot_renewals = lambda: []
        self.menu.acme_sh_present = lambda: True
        text = self.report()
        self.assertIn("acme.sh", text)
        self.assertIn("не определяется", text)

    def test_self_signed_or_cloudflare_origin_need_no_port_80(self):
        self.menu.certbot_renewals = lambda: []
        text = self.report()
        self.assertIn("самоподписанные", text)
        self.assertIn("Cloudflare Origin", text)
        self.assertNotIn("ВНИМАНИЕ", text)

    def test_acme_sh_dns_mode_needs_no_port_80(self):
        self.menu.certbot_renewals = lambda: []
        self.menu.acme_sh_present = lambda: True
        self.menu.acme_sh_modes = lambda: [("example.com", "dns")]
        text = self.report()
        self.assertIn("acme.sh example.com: проверка через DNS", text)
        self.assertNotIn("ВНИМАНИЕ", text)

    def test_acme_sh_webroot_needs_port_80(self):
        self.menu.certbot_renewals = lambda: []
        self.menu.acme_sh_present = lambda: True
        self.menu.acme_sh_modes = lambda: [("example.com", "webroot")]
        self.assertIn("ВНИМАНИЕ", self.report())

    def test_inactive_ufw_is_not_reported_as_closed_port(self):
        text = self.report(policy=None)
        self.assertIn("Порт 80: открыт (ufw не активен)", text)
        self.assertNotIn("ВНИМАНИЕ", text)

    def test_overview_prints_tables_ufw_ports_and_ssh(self):
        outputs = {
            ("nft", "list", "tables"): "table inet fw\ntable ip remnanode\ntable ip filter\n",
            ("ss", "-H", "-lntup"): 'tcp LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=1,fd=8))\n'
                                     'tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=2,fd=3))\n'
                                     'udp UNCONN 0 0 [::1]:323 [::]:* users:(("chronyd",pid=3,fd=6))\n',
            ("sshd", "-T"): "port 22\nlistenaddress 0.0.0.0\n",
            ("ufw", "show", "added"): "Added user rules (see 'ufw status' for running firewall):\nufw allow 22/tcp\nufw allow 80/tcp\n",
        }
        self.menu.probe_output = lambda *command: outputs.get(command, "")
        self.menu.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout="Status: inactive\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.menu.show_server_overview()
        text = output.getvalue()
        self.assertIn("inet fw — наша", text)
        self.assertIn("ip remnanode — плагины ноды Remnawave", text)
        self.assertIn("ip filter — чужая", text)
        self.assertIn("заготовлены правила (не действуют): allow 22/tcp; allow 80/tcp", text)
        self.assertIn("TCP 80 — nginx, открыт", text)
        self.assertNotIn("323", text)
        self.assertIn("SSH: порт 22 — открыт", text)

    def test_permanently_open_port_80_is_stated_plainly(self):
        policy = self.menu.ufw_policy(UFW_ACTIVE.replace("443/tcp", "80/tcp"))
        text = self.report(policy=policy)
        self.assertIn("Порт 80 открыт постоянно, поэтому открывать его на время продления не нужно", text)
        self.assertNotIn("не найдено", text)

    def test_systemd_socket_and_service_are_one_line_and_warp_is_named(self):
        outputs = {
            ("nft", "list", "tables"): "table inet vps_warp\ntable ip filter\n",
            ("ss", "-H", "-lntup"): 'tcp LISTEN 0 4096 0.0.0.0:11022 0.0.0.0:* users:(("sshd",pid=1,fd=3),("systemd",pid=1,fd=89))\n'
                                     'tcp LISTEN 0 4096 [::]:11022 [::]:* users:(("sshd",pid=1,fd=4),("systemd",pid=1,fd=90))\n',
            ("sshd", "-T"): "port 11022\n",
        }
        self.menu.probe_output = lambda *command: outputs.get(command, "")
        self.menu.subprocess.run = lambda *a, **k: types.SimpleNamespace(stdout="Status: inactive\n")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.menu.show_server_overview()
        text = output.getvalue()
        self.assertEqual(text.count("TCP 11022"), 1)
        self.assertIn("sshd (через сокет systemd)", text)
        self.assertIn("inet vps_warp — чужая, Cloudflare WARP", text)
        self.assertIn("ip filter — чужая, ведётся iptables-nft", text)


if __name__ == "__main__":
    unittest.main()
