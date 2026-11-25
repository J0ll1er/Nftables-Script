"""Ограничение частоты SSH в update-fw.sh: что попадает в правила, проверка настроек, место в цепочке."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent


class SshLimit(unittest.TestCase):
    def run_case(self, name, conf, ok=True):
        env = {**os.environ, "TEST_CONF_APPEND": conf}
        result = subprocess.run(["bash", str(ROOT / "tests/run_updater.sh"), name, "--dry"],
                                cwd=ROOT, env=env, text=True, capture_output=True)
        text = result.stdout + result.stderr
        self.assertIn("код возврата: 0" if ok else "код возврата: 1", text, text)
        snapshot = ROOT / "t" / name / "nft_snapshot"
        return text, snapshot.read_text() if snapshot.exists() else ""

    def test_off_by_default(self):
        text, rules = self.run_case("ssh_off", "")
        self.assertNotIn("ssh-rate", rules)
        self.assertNotIn("SSH: не больше", text)

    def test_zero_means_off(self):
        _, rules = self.run_case("ssh_zero", "SSH_RATE_LIMIT=0")
        self.assertNotIn("ssh-rate", rules)
        self.assertNotIn("sr4", rules)

    def test_stats_sets_exist_only_with_the_limit(self):
        _, off = self.run_case("ssh_off", "")
        self.assertNotIn("set sr4", off)
        _, on = self.run_case("ssh_on", "SSH_RATE_LIMIT=20\nSSH_PORTS=22")
        self.assertIn("set sr4 { type ipv4_addr . inet_service; size 65536; flags dynamic,timeout; timeout 7d; counter;", on)
        self.assertIn("set sr6 { type ipv6_addr . inet_service;", on)

    def test_exceeding_addresses_are_recorded_after_the_meter_before_the_drop(self):
        _, rules = self.run_case("ssh_on", "SSH_RATE_LIMIT=20\nSSH_PORTS=22")
        four = next(line for line in rules.splitlines() if "meter ssh4" in line)
        six = next(line for line in rules.splitlines() if "meter ssh6" in line)
        self.assertRegex(four, r"packets \} add @sr4 \{ ip saddr \. tcp dport \} counter drop comment \"ssh-rate\"")
        self.assertRegex(six, r"packets \} add @sr6 \{ ip6 saddr \. tcp dport \} counter drop comment \"ssh-rate\"")

    def test_rules_for_given_ports_and_rate(self):
        text, rules = self.run_case("ssh_on", 'SSH_RATE_LIMIT=20\nSSH_PORTS="22 11022"')
        self.assertIn("tcp dport { 22,11022 } ct state new meter ssh4 { ip saddr timeout 5m limit rate over 20/minute burst 10 packets }", rules)
        self.assertIn("ip6 saddr & ffff:ffff:ffff:ffff:: timeout 5m limit rate over 20/minute burst 10 packets", rules)
        self.assertIn("SSH: не больше 20 новых подключений в минуту", text)
        self.assertIn("порты: 22 11022", text)

    def test_burst_has_a_floor_of_five(self):
        _, rules = self.run_case("ssh_burst", "SSH_RATE_LIMIT=4\nSSH_PORTS=22")
        self.assertIn("limit rate over 4/minute burst 5 packets", rules)

    def test_rule_stands_after_trusted_and_scanners_before_geo(self):
        _, rules = self.run_case("ssh_order", "SSH_RATE_LIMIT=20\nSSH_PORTS=22")
        self.assertLess(rules.index("@tr4 accept"), rules.index("ssh-rate"))
        self.assertLess(rules.index("ct state established,related accept"), rules.index("ssh-rate"))
        self.assertLess(rules.index("@sc4 counter drop"), rules.index("ssh-rate"))
        self.assertLess(rules.index("ssh-rate"), rules.index('"non-RU"'))

    def test_invalid_value_is_rejected_and_rules_are_kept(self):
        text, _ = self.run_case("ssh_bad", "SSH_RATE_LIMIT=много", ok=False)
        self.assertIn("SSH_RATE_LIMIT", text)

    def test_invalid_port_is_rejected(self):
        text, _ = self.run_case("ssh_bad_port", 'SSH_RATE_LIMIT=20\nSSH_PORTS="22 70000"', ok=False)
        self.assertIn("SSH_PORTS", text)

    def test_without_ports_the_default_is_22_or_detected(self):
        text, rules = self.run_case("ssh_auto", "SSH_RATE_LIMIT=20")
        self.assertIn("ssh-rate", rules)
        self.assertRegex(text, r"порты: \d+")


if __name__ == "__main__":
    unittest.main()
