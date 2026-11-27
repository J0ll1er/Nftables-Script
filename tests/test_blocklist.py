"""Список blocklist.de для SSH в update-fw.sh: источник, кэш, наборы, место правила в цепочке."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
LIST = "198.51.100.7\n198.51.100.8\n2001:db8:5::1"


class Blocklist(unittest.TestCase):
    def run_case(self, name, conf, ok=True, **extra):
        env = {**os.environ, "TEST_CONF_APPEND": conf, **extra}
        result = subprocess.run(["bash", str(ROOT / "tests/run_updater.sh"), name, "--dry"],
                                cwd=ROOT, env=env, text=True, capture_output=True)
        text = result.stdout + result.stderr
        self.assertIn("код возврата: 0" if ok else "код возврата: 1", text, text)
        snapshot = ROOT / "t" / name / "nft_snapshot"
        calls = ROOT / "t" / name / "blocklist_calls"
        return text, snapshot.read_text() if snapshot.exists() else "", \
            len(calls.read_text().split()) if calls.exists() else 0

    def test_on_by_default(self):
        text, rules, calls = self.run_case("bl_default", "", SCEN_BL_CONTENT=LIST)
        self.assertIn("blocklist-ssh", rules)
        self.assertEqual(calls, 1)

    def test_explicit_zero_turns_it_off(self):
        text, rules, calls = self.run_case("bl_off", "BLOCKLIST_SSH=0")
        self.assertNotIn("bl4", rules)
        self.assertNotIn("blocklist-ssh", rules)
        self.assertEqual(calls, 0)

    def test_on_builds_sets_and_rule_on_ssh_ports_only(self):
        text, rules, _ = self.run_case("bl_on", "BLOCKLIST_SSH=1\nSSH_PORTS=22", SCEN_BL_CONTENT=LIST)
        self.assertRegex(rules, r"set bl4 \{ type ipv4_addr; flags interval; auto-merge;\s+elements = \{[^}]*198\.51\.100\.7")
        self.assertRegex(rules, r"set bl6 \{ type ipv6_addr;")
        self.assertIn("set bs4 { type ipv4_addr . inet_service; size 65536; flags dynamic,timeout; timeout 7d; counter;", rules)
        four = next(line for line in rules.splitlines() if "@bl4" in line and "add" in line)
        self.assertIn("tcp dport { 22 } ip saddr @bl4 add @bs4 { ip saddr . tcp dport }", four)
        self.assertTrue(four.rstrip().endswith('counter drop comment "blocklist-ssh"'), four)
        self.assertIn("blocklist.de SSH", text)

    def test_rule_stands_after_trusted_and_before_geo_and_rate(self):
        _, rules, _ = self.run_case("bl_order", "BLOCKLIST_SSH=1\nSSH_PORTS=22\nSSH_RATE_LIMIT=20", SCEN_BL_CONTENT=LIST)
        self.assertLess(rules.index("@tr4 accept"), rules.index("blocklist-ssh"))
        self.assertLess(rules.index("ct state established,related accept"), rules.index("blocklist-ssh"))
        self.assertLess(rules.index("blocklist-ssh"), rules.index("ssh-rate"))
        self.assertLess(rules.index("blocklist-ssh"), rules.index('"non-RU"'))

    def test_ports_come_from_sshd_when_not_set(self):
        _, rules, _ = self.run_case("bl_ports", "BLOCKLIST_SSH=1", SCEN_BL_CONTENT=LIST)
        self.assertRegex(rules, r"tcp dport \{ [0-9,]+ \} ip saddr @bl4 add")

    def test_download_failure_without_cache_keeps_going(self):
        text, rules, _ = self.run_case("bl_down", "BLOCKLIST_SSH=1\nSSH_PORTS=22", SCEN_BL="down")
        self.assertIn("blocklist.de", text)
        self.assertIn("WARN", text)
        self.assertNotIn("blocklist-ssh", rules)

    def test_download_failure_uses_cache(self):
        text, rules, _ = self.run_case("bl_cache", "BLOCKLIST_SSH=1\nSSH_PORTS=22", SCEN_BL="down",
                                       TEST_BL_CACHE="198.51.100.77", TEST_BL_CACHE_AGE_HOURS="30")
        self.assertIn("198.51.100.77", rules)
        self.assertIn("blocklist-ssh", rules)

    def test_recent_cache_is_reused_without_download(self):
        _, rules, calls = self.run_case("bl_recent", "BLOCKLIST_SSH=1\nSSH_PORTS=22",
                                        TEST_BL_CACHE="198.51.100.77", TEST_BL_CACHE_AGE_HOURS="1")
        self.assertEqual(calls, 0)
        self.assertIn("198.51.100.77", rules)

    def test_old_cache_triggers_download(self):
        _, rules, calls = self.run_case("bl_old", "BLOCKLIST_SSH=1\nSSH_PORTS=22", SCEN_BL_CONTENT=LIST,
                                        TEST_BL_CACHE="198.51.100.77", TEST_BL_CACHE_AGE_HOURS="7")
        self.assertEqual(calls, 1)
        self.assertIn("198.51.100.8", rules)
        self.assertNotIn("198.51.100.77", rules)

    def test_collapsed_list_is_rejected_in_favour_of_cache(self):
        big = "\n".join(f"198.51.100.{i}" for i in range(1, 101))
        text, rules, _ = self.run_case("bl_shrunk", "BLOCKLIST_SSH=1\nSSH_PORTS=22", SCEN_BL_CONTENT="198.51.100.200",
                                       TEST_BL_CACHE=big, TEST_BL_CACHE_AGE_HOURS="12")
        self.assertIn("198.51.100.50", rules)
        self.assertNotIn("198.51.100.200", rules)

    def test_private_and_loopback_entries_are_ignored(self):
        _, rules, _ = self.run_case("bl_private", "BLOCKLIST_SSH=1\nSSH_PORTS=22",
                                    SCEN_BL_CONTENT="10.1.2.3\n127.0.0.1\n192.168.1.1\n198.51.100.7")
        elements = rules[rules.index("set bl4"):rules.index("}", rules.index("set bl4"))]
        self.assertIn("198.51.100.7", elements)
        for bad in ("10.1.2.3", "127.0.0.1", "192.168.1.1"):
            self.assertNotIn(bad, elements)

    def test_bad_value_is_rejected(self):
        self.run_case("bl_bad", "BLOCKLIST_SSH=yes", ok=False)


if __name__ == "__main__":
    unittest.main()
