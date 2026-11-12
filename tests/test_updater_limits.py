"""Поведенческие проверки обновлятора через run_updater.sh и заглушки."""
import os
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
MAJOR = int(subprocess.check_output(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], text=True))


@unittest.skipIf(MAJOR < 4, "обновлятор использует Bash 4+ (${asn^^})")
class UpdaterLimits(unittest.TestCase):
    def run_case(self, name, **values):
        env = {**os.environ, **values}
        result = subprocess.run(["bash", str(ROOT / "tests/run_updater.sh"), name, "--dry"],
                                cwd=ROOT, env=env, text=True, capture_output=True, check=True)
        self.assertIn("код возврата: 0", result.stdout)
        return result.stdout + result.stderr, ROOT / "t" / name / "nft_snapshot"

    def test_lan_is_before_scanners_and_geo(self):
        _, snapshot = self.run_case("lan", TEST_LAN="10.0.0.0/8 fc00::/7",
                                    SCEN_LIST_CONTENT="10.2.0.0/16")
        rules = snapshot.read_text()
        self.assertIn("set lan4", rules)
        self.assertIn("set lan6", rules)
        self.assertLess(rules.index("@tr4 accept"), rules.index("@lan4 accept"))
        self.assertLess(rules.index("@lan4 accept"), rules.index("@sc4 counter drop"))
        self.assertLess(rules.index("@lan4 accept"), rules.index('"non-RU"'))
        self.assertNotIn("10.2.0.0/16", re.search(r"set sc4.*?\n  }", rules, re.S).group())

    def test_missing_lan_setting_is_empty(self):
        _, snapshot = self.run_case("default_lan", SCEN_LIST_CONTENT="10.2.0.0/16")
        rules = snapshot.read_text()
        self.assertNotIn("elements = {", re.search(r"set lan4.*?\n  }", rules, re.S).group())
        self.assertIn("10.2.0.0/16", re.search(r"set sc4.*?\n  }", rules, re.S).group())

    def test_same_line_count_but_ipv4_address_growth_uses_cache(self):
        output, snapshot = self.run_case("address_growth", TEST_CACHE_NAME="skipa_cidr.txt",
            TEST_CACHE_CONTENT="192.0.2.1", SCEN_LIST_CONTENT="198.51.100.0/24")
        self.assertIn("аномальный размер списка skipa_cidr.txt", output)
        self.assertIn("192.0.2.1", snapshot.read_text())

    def test_same_line_count_but_ipv6_address_growth_uses_cache(self):
        output, snapshot = self.run_case("address_growth_v6", TEST_CACHE_NAME="skipa_cidr.txt",
            TEST_CACHE_CONTENT="2001:db8:1::/64", SCEN_LIST_CONTENT="2001:db8::/48")
        self.assertIn("аномальный размер списка skipa_cidr.txt", output)
        self.assertIn("2001:db8:1::/64", snapshot.read_text())

    def test_external_broad_prefix_rejected(self):
        _, snapshot = self.run_case("broad_prefix", SCEN_LIST_CONTENT="198.0.0.0/7")
        self.assertNotIn("198.0.0.0/7", snapshot.read_text())

    def test_wide_scanner_prefix_up_to_slash8_is_kept(self):
        _, snapshot = self.run_case("wide_prefix", SCEN_LIST_CONTENT="198.0.0.0/8")
        self.assertIn("198.0.0.0/8", snapshot.read_text())

    def test_wide_network_is_skipped_alone_and_reported(self):
        output, snapshot = self.run_case("wide_skip", SCEN_LIST_CONTENT="198.0.0.0/7\n203.0.113.0/24")
        sets = re.search(r"set sc4.*?\n  }", snapshot.read_text(), re.S).group()
        self.assertNotIn("198.0.0.0/7", sets)
        self.assertIn("203.0.113.0/24", sets)
        self.assertIn("пропущено слишком широких сетей", output)
        pending = output.split("--- wide-pending:")[1].split("--- errors.log:")[0]
        self.assertIn("198.0.0.0/7", pending)
        self.assertIn("ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ", output)

    def test_approved_wide_network_is_blocked(self):
        output, snapshot = self.run_case("wide_ok", SCEN_LIST_CONTENT="198.0.0.0/7\n203.0.113.0/24",
                                         TEST_WIDE_OK="198.0.0.0/7")
        sets = re.search(r"set sc4.*?\n  }", snapshot.read_text(), re.S).group()
        self.assertIn("198.0.0.0/7", sets)
        self.assertNotIn("пропущено слишком широких сетей", output)
        self.assertIn("(нет)", output.split("--- wide-pending:")[1])

    def test_no_wide_networks_means_no_report(self):
        output, _ = self.run_case("wide_none", SCEN_LIST_CONTENT="203.0.113.0/24")
        self.assertIn("(нет)", output.split("--- wide-pending:")[1])

    def test_geo_growth_fails_before_rules_are_applied(self):
        _, snapshot = self.run_case("geo_growth")
        fw = snapshot.parent / "fw"
        (fw / "geo.count").write_text("100\n")
        env = {**os.environ, "STUB_DIR": str(snapshot.parent), "GEO_DAT": str(snapshot.parent / "geo.dat"),
               "PATH": str(snapshot.parent / "stubs") + os.pathsep + os.environ["PATH"]}
        result = subprocess.run([str(fw / "update-fw.sh")], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("аномальное изменение числа RU IPv4", result.stderr)
        self.assertFalse((fw / "fw.nft").exists())

    def test_pending_mode_does_not_replace_committed_snapshot(self):
        _, snapshot = self.run_case("pending")
        fw = snapshot.parent / "fw"
        (fw / "fw.nft").write_text("old rules\n")
        env = {**os.environ, "FW_PENDING": "1", "STUB_DIR": str(snapshot.parent),
               "GEO_DAT": str(snapshot.parent / "geo.dat"),
               "PATH": str(snapshot.parent / "stubs") + os.pathsep + os.environ["PATH"]}
        result = subprocess.run([str(fw / "update-fw.sh")], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((fw / "fw.nft").read_text(), "old rules\n")
        self.assertTrue((fw / "fw.nft.pending").is_file())
        self.assertTrue((fw / "geo.count.pending").is_file())
        self.assertFalse((fw / "geo.count").exists())

    def test_empty_lan_disables_exception(self):
        _, snapshot = self.run_case("no_lan", TEST_LAN="", SCEN_LIST_CONTENT="10.2.0.0/16")
        rules = snapshot.read_text()
        self.assertIn("10.2.0.0/16", rules)
        self.assertNotIn("elements = {", re.search(r"set lan4.*?\n  }", rules, re.S).group())

    def test_anomalous_growth_uses_cache(self):
        output, snapshot = self.run_case("growth", TEST_CACHE_NAME="skipa_cidr.txt",
            TEST_CACHE_CONTENT="192.0.2.1", SCEN_LIST_CONTENT="\n".join(
                f"198.51.100.{2 * n + 1}" for n in range(10)))
        self.assertIn("аномальный размер списка skipa_cidr.txt", output)
        self.assertIn("192.0.2.1", snapshot.read_text())

    def test_anomalous_shrink_uses_cache(self):
        output, snapshot = self.run_case("shrink", TEST_CACHE_NAME="skipa_cidr.txt",
            TEST_CACHE_CONTENT="\n".join(f"192.0.2.{2 * n + 1}" for n in range(20)),
            SCEN_LIST_CONTENT="198.51.100.1")
        self.assertIn("аномальный размер списка skipa_cidr.txt", output)
        self.assertIn("192.0.2.1", snapshot.read_text())

    def test_trusted_overlap_is_removed(self):
        output, snapshot = self.run_case("trusted_overlap", TEST_LAN="",
            TEST_TRUSTED="203.0.113.5", SCEN_LIST_CONTENT="203.0.113.0/24")
        self.assertIn("удалено пересечений с trusted/LAN_ALLOW", output)
        self.assertNotIn("203.0.113.0/24", snapshot.read_text())

    def test_old_scanner_cache_is_skipped(self):
        output, _ = self.run_case("old_scanner", SCEN_LISTS="down",
            TEST_CACHE_NAME="skipa_cidr.txt", TEST_CACHE_AGE_DAYS="20")
        self.assertIn("кэш skipa_cidr.txt старше 14 дней", output)
        self.assertIn("без него", output)

    def test_old_asn_cache_is_skipped(self):
        output, _ = self.run_case("old_asn", SCEN_RIPE="down", SCEN_IPVERSE="down",
            CACHE_ASN="1", TEST_ASN_CACHE_AGE_DAYS="20")
        self.assertIn("кэш asn_AS61280.cache старше 14 дней", output)
        self.assertIn("нет данных по AS61280", output)


    def test_verdict_ok_when_all_sources_answer(self):
        output, _ = self.run_case("verdict_ok")
        self.assertIn("ИТОГ: ВСЁ В ПОРЯДКЕ", output)
        self.assertNotIn("ЕСТЬ ЗАМЕЧАНИЯ", output)

    def test_verdict_warns_when_reserve_source_is_used(self):
        output, _ = self.run_case("verdict_warn", SCEN_RIPE="down")
        self.assertIn("ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ", output)
        self.assertIn("RIPEstat недоступен", output)

    def test_verdict_error_when_geo_is_unavailable(self):
        env = {**os.environ, "SCEN_GEO": "down"}
        result = subprocess.run(["bash", str(ROOT / "tests/run_updater.sh"), "verdict_err", "--dry"],
                                cwd=ROOT, env=env, text=True, capture_output=True)
        self.assertIn("ИТОГ: ОШИБКА", result.stdout + result.stderr)
        self.assertNotIn("curl: (", result.stdout + result.stderr)

    def test_trusted_overlap_is_info_not_warning(self):
        output, _ = self.run_case("overlap_info", TEST_LAN="", TEST_TRUSTED="203.0.113.5",
                                  SCEN_LIST_CONTENT="203.0.113.0/24")
        self.assertIn("INFO: из списков сканеров удалено пересечений", output)
        self.assertIn("ИТОГ: ВСЁ В ПОРЯДКЕ", output)

    def test_restart_rolls_back_before_normal_update(self):
        _, snapshot = self.run_case("restart_rollback")
        fw = snapshot.parent / "fw"
        token = "a" * 32
        (fw / (".rollback-" + token)).mkdir()
        (fw / "fw.nft.pending").write_text("pending rules\n")
        stub = snapshot.parent / "fw-menu-stub"
        stub.write_text("#!/bin/sh\nprintf '%s\n' \"$*\" >> \"$STUB_DIR/menu_calls\"\n"
                        "rm -rf \"$STUB_DIR/fw/.rollback-$2\"\n"
                        "rm -f \"$STUB_DIR/fw/fw.nft.pending\"\n")
        stub.chmod(0o755)
        script = fw / "update-fw.sh"
        script.write_text(script.read_text().replace("/usr/local/sbin/fw-menu", str(stub)))
        env = {**os.environ, "STUB_DIR": str(snapshot.parent),
               "GEO_DAT": str(snapshot.parent / "geo.dat"),
               "PATH": str(snapshot.parent / "stubs") + os.pathsep + os.environ["PATH"]}
        result = subprocess.run([str(script)], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARN: найдено неподтверждённое применение, откатываю", result.stderr)
        self.assertEqual((snapshot.parent / "menu_calls").read_text().strip(), "--rollback " + token)
        self.assertFalse((fw / "fw.nft.pending").exists())
        self.assertFalse((fw / (".rollback-" + token)).exists())
        self.assertTrue((fw / "fw.nft").is_file())

    def test_pending_update_does_not_start_recovery(self):
        _, snapshot = self.run_case("pending_recovery")
        fw = snapshot.parent / "fw"
        token = "b" * 32
        (fw / (".rollback-" + token)).mkdir()
        env = {**os.environ, "FW_PENDING": "1", "STUB_DIR": str(snapshot.parent),
               "GEO_DAT": str(snapshot.parent / "geo.dat"),
               "PATH": str(snapshot.parent / "stubs") + os.pathsep + os.environ["PATH"]}
        result = subprocess.run([str(fw / "update-fw.sh")], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((fw / (".rollback-" + token)).is_dir())
        self.assertTrue((fw / "fw.nft.pending").is_file())

    def test_nftables_dropin_rolls_back_before_loading_rules(self):
        script = (ROOT / "fw-install.sh").read_text()
        line = next(line for line in script.splitlines() if line.startswith("ExecStartPost="))
        self.assertLess(line.index("fw-menu --rollback"), line.index("/usr/sbin/nft -f"))
        self.assertNotIn("fw.nft.pending", line)

class InstallerAsn(unittest.TestCase):
    def test_upgrade_does_not_offer_default_asn(self):
        script = (ROOT / "fw-install.sh").read_text()
        upgrade = script.split("if [ \"$mode\" = upgrade ]; then", 1)[1].split(
            "# ----------------------------------------------------------- новая установка", 1)[0]
        self.assertNotIn("Добавить штатный набор", upgrade)
        self.assertNotIn('"$DEFAULT_ASN" >> "$FW/asn.txt"', upgrade)

    def test_upgrade_does_not_add_lan_allow(self):
        script = (ROOT / "fw-install.sh").read_text()
        upgrade = script.split('if [ "$mode" = upgrade ]; then', 1)[1].split(
            "# ----------------------------------------------------------- новая установка", 1)[0]
        self.assertNotIn('echo \'LAN_ALLOW=', upgrade)
        self.assertIn('LAN_ALLOW выключен.', upgrade)

    def test_new_install_offers_only_server_subnet(self):
        script = (ROOT / "fw-install.sh").read_text()
        install = script.split("# ----------------------------------------------------------- новая установка", 1)[1]
        self.assertIn('ip -4 route get 1.1.1.1', install)
        self.assertIn('if [ "$enable_lan" = да ]; then lan_default=$lan_candidate; fi', install)
        self.assertIn('printf \'LAN_ALLOW="%s"\\n\' "$lan_default"', install)
