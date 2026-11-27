"""update-fw.sh --blocklist: обновление только списка blocklist.de и цепочка разовых таймеров на 6 часов."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
LIST = "198.51.100.7\n198.51.100.8\n2001:db8:5::1"


class Refresh(unittest.TestCase):
    def run_case(self, name, conf, *args, ok=True, **extra):
        env = {**os.environ, "TEST_CONF_APPEND": conf, **extra}
        result = subprocess.run(["bash", str(ROOT / "tests/run_updater.sh"), name, *args],
                                cwd=ROOT, env=env, text=True, capture_output=True)
        text = result.stdout + result.stderr
        self.assertIn("код возврата: 0" if ok else "код возврата: 1", text, text)
        base = ROOT / "t" / name

        def read(file):
            return (base / file).read_text() if (base / file).exists() else ""
        return text, read("nft_snapshot"), read("systemd_run_calls"), read("fw/scanner-cache/blocklist_ssh"), base

    def test_refresh_replaces_only_blocklist_sets(self):
        text, rules, armed, cache, base = self.run_case("blr_ok", "", "--blocklist", SCEN_BL_CONTENT=LIST)
        self.assertIn("flush set inet fw bl4", rules)
        self.assertIn("flush set inet fw bl6", rules)
        self.assertRegex(rules, r"add element inet fw bl4 \{ 198\.51\.100\.7,198\.51\.100\.8 \}")
        self.assertRegex(rules, r"add element inet fw bl6 \{ 2001:db8:5::1 \}")
        self.assertNotIn("table inet fw", rules)  # таблица целиком не пересобиралась
        self.assertIn("2001:db8:5::1", cache)
        self.assertFalse((base / "ripe_calls").exists(), "другие списки не скачиваются")
        self.assertFalse((base / "ipverse_calls").exists())
        self.assertIn("blocklist.de SSH обновлён: 3 адр.", text)

    def test_refresh_arms_next_run_in_six_hours(self):
        _, _, armed, _, _ = self.run_case("blr_chain", "", "--blocklist", SCEN_BL_CONTENT=LIST)
        self.assertIn("--on-active=6h", armed)
        self.assertRegex(armed, r"update-fw\.sh --blocklist")
        self.assertIn("--collect", armed)

    def test_old_chain_is_stopped_before_arming(self):
        _, _, _, _, base = self.run_case("blr_stop", "", "--blocklist", SCEN_BL_CONTENT=LIST)
        calls = (base / "systemctl_calls").read_text()
        self.assertIn("stop fw-blocklist-*.timer", calls)

    def test_switched_off_does_nothing(self):
        text, rules, armed, cache, _ = self.run_case("blr_off", "BLOCKLIST_SSH=0", "--blocklist")
        self.assertEqual((rules, armed, cache), ("", "", ""))

    def test_disabled_autoupdate_does_nothing(self):
        text, rules, armed, cache, _ = self.run_case("blr_timer_off", "", "--blocklist",
                                                      SCEN_BL_CONTENT=LIST, STUB_TIMER_ENABLED="1")
        self.assertEqual((rules, armed, cache), ("", "", ""))

    def test_suspicious_list_keeps_old_one_and_still_arms(self):
        old = "\n".join(f"198.51.100.{n}" for n in range(1, 41))
        text, rules, armed, cache, _ = self.run_case("blr_growth", "", "--blocklist", SCEN_BL_CONTENT="198.51.100.200",
                                                      TEST_BL_CACHE=old, TEST_BL_CACHE_AGE_HOURS="7")
        self.assertNotIn("flush set", rules)
        self.assertEqual(cache.strip().splitlines(), old.splitlines())
        self.assertIn("оставляю прежний", text)
        self.assertIn("--on-active=6h", armed)

    def test_download_failure_keeps_old_one(self):
        text, rules, armed, _, _ = self.run_case("blr_down", "", "--blocklist", SCEN_BL="down")
        self.assertNotIn("flush set", rules)
        self.assertIn("оставляю прежний", text)
        self.assertIn("--on-active=6h", armed)

    def test_full_update_starts_the_chain(self):
        text, _, armed, _, _ = self.run_case("blr_full", "", SCEN_BL_CONTENT=LIST)
        self.assertIn("OK: применено", text)
        self.assertIn("--on-active=6h", armed)

    def test_dry_run_arms_nothing(self):
        _, _, armed, _, _ = self.run_case("blr_dry", "", "--dry", SCEN_BL_CONTENT=LIST)
        self.assertEqual(armed, "")

    def test_full_update_without_blocklist_arms_nothing(self):
        _, _, armed, _, _ = self.run_case("blr_full_off", "BLOCKLIST_SSH=0")
        self.assertEqual(armed, "")


if __name__ == "__main__":
    unittest.main()


class GeoMirrorIsNotAWarning(unittest.TestCase):
    def test_primary_down_mirror_up_stays_green(self):
        env = {**os.environ, "TEST_CONF_APPEND": 'ZKEEN_URL="https://primary.invalid/zkeenip.dat"\nBLOCKLIST_SSH=0',
               "SCEN_GEO_PRIMARY": "down"}
        result = subprocess.run(["bash", str(ROOT / "tests/run_updater.sh"), "geo_mirror", "--dry"],
                                cwd=ROOT, env=env, text=True, capture_output=True)
        text = result.stdout + result.stderr
        self.assertIn("код возврата: 0", text, text)
        self.assertNotIn("WARN: геобаза не скачалась", text)
