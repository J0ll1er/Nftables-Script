"""«Кто стучится»: разбор счётчиков nft, сводка и экран; сбор статистики в правилах."""
import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_hits", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)

SET_TEXT = """table inet fw {
	set ss4 {
		type ipv4_addr . inet_service
		size 4096
		flags dynamic,timeout
		counter
		timeout 7d
		elements = { 198.51.100.7 . 22222 counter packets 40 bytes 2400 expires 6d23h59m1s,
			     198.51.100.7 . 3389 counter packets 10 bytes 600 expires 6d23h59m1s,
			     203.0.113.9 . 443 counter packets 5 bytes 300 expires 6d1h }
	}
}
"""
SET_TEXT_V6 = """table inet fw {
	set ss6 {
		type ipv6_addr . inet_service
		elements = { 2001:db8::1 . 80 counter packets 3 bytes 180 expires 1d }
	}
}
"""


class Hits(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)

    def fake_nft(self, outputs):
        def probe(*command):
            return outputs.get(command[-1], "")
        self.menu.probe_output = probe

    def test_parse_reads_v4_and_v6_rows(self):
        rows = self.menu.parse_hits(SET_TEXT + SET_TEXT_V6)
        self.assertIn(("198.51.100.7", 22222, 40, 2400), rows)
        self.assertIn(("203.0.113.9", 443, 5, 300), rows)
        self.assertIn(("2001:db8::1", 80, 3, 180), rows)
        self.assertEqual(len(rows), 4)

    def test_parse_ignores_garbage(self):
        self.assertEqual(self.menu.parse_hits(""), [])
        self.assertEqual(self.menu.parse_hits("Error: No such file or directory"), [])

    def test_summary_sums_by_port_and_by_source(self):
        summary = self.menu.summarize_hits(self.menu.parse_hits(SET_TEXT))
        self.assertEqual(summary["packets"], 55)
        self.assertEqual(summary["sources"], 2)
        self.assertEqual(summary["ports"][0], (22222, 40))
        self.assertEqual(summary["top_sources"][0], ("198.51.100.7", 50, 2))
        self.assertEqual(summary["top_sources"][1], ("203.0.113.9", 5, 1))

    def test_summary_limits_to_top_five(self):
        rows = [(f"198.51.100.{i}", 1000 + i, i, i * 60) for i in range(1, 12)]
        summary = self.menu.summarize_hits(rows)
        self.assertEqual(len(summary["ports"]), 5)
        self.assertEqual(len(summary["top_sources"]), 5)
        self.assertEqual(summary["top_sources"][0][0], "198.51.100.11")

    def test_port_labels_are_in_russian_words(self):
        self.assertEqual(self.menu.port_label(3389), "RDP, удалённый рабочий стол")
        self.assertEqual(self.menu.port_label(443), "HTTPS")
        self.assertEqual(self.menu.port_label(54321), "")

    def test_bar_is_proportional_and_fixed_width(self):
        bar = self.menu.hits_bar(6, 20, 10)
        self.assertEqual(bar.count("▮") + bar.count("▯"), 10)
        self.assertEqual(bar.count("▮"), 3)  # 6/20 от 10 палочек
        self.assertEqual(self.menu.hits_bar(20, 20, 10).count("▮"), 10)
        self.assertEqual(self.menu.hits_bar(0, 20, 10).count("▮"), 0)
        self.assertGreaterEqual(self.menu.hits_bar(1, 1000, 10).count("▮"), 1)
        # палочки разделены пробелами, поэтому ширина одинакова у любой полоски
        self.assertEqual(len(self.menu.hits_bar(1, 20, 10)), len(self.menu.hits_bar(20, 20, 10)))

    def test_colored_bar_keeps_width_and_is_plain_without_color(self):
        plain = self.menu.hits_bar(5, 20, 10)
        self.assertEqual(self.menu.hits_bar_view(5, 20, 10, False), plain)
        colored = self.menu.hits_bar_view(5, 20, 10, True)
        stripped = colored.replace("\033[32m", "").replace("\033[90m", "").replace("\033[0m", "")
        self.assertEqual(stripped, plain)
        self.assertIn("\033[32m", colored)
        self.assertIn("\033[90m", colored)

    def show(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_hits()
        return out.getvalue()

    def test_screen_shows_both_groups_with_ports_and_sources(self):
        self.fake_nft({"ss4": SET_TEXT, "ss6": SET_TEXT_V6,
                       "sn4": SET_TEXT.replace("198.51.100.7", "192.0.2.50"), "sn6": ""})
        text = self.show()
        self.assertIn("Сканеры и сети ведомств", text)
        self.assertIn("Не из России", text)
        self.assertIn("22222", text)
        self.assertIn("RDP", text)
        self.assertIn("198.51.100.7", text)
        self.assertIn("192.0.2.50", text)
        self.assertIn("SSH", text)  # заметка: порт управления не виден

    def test_screen_when_nothing_collected_yet(self):
        self.fake_nft({"ss4": "table inet fw {\n\tset ss4 {\n\t\ttype ipv4_addr . inet_service\n\t}\n}\n",
                       "ss6": "", "sn4": "", "sn6": ""})
        text = self.show()
        self.assertIn("пока никого", text.lower())

    def test_screen_when_sets_missing_explains_in_words(self):
        self.fake_nft({})
        text = self.show()
        self.assertIn("не ведётся", text)
        self.assertNotIn("Traceback", text)

    def test_status_screen_ignores_stat_rules(self):
        base = [
            {"set": {"name": name, "elem": []}} for name in ("tr4", "tr6", "sc4", "sc6", "ru4", "ru6")
        ] + [{"chain": {"name": "input"}}]
        drop = {"rule": {"chain": "input", "comment": "scanners", "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": "@sc4"}},
            {"counter": {"packets": 4, "bytes": 116}}, {"drop": None}]}}
        stat = {"rule": {"chain": "input", "expr": [
            {"match": {"op": "==", "left": {"payload": {"protocol": "ip", "field": "saddr"}}, "right": "@sc4"}},
            {"set": {"op": "add", "elem": {"concat": []}, "set": "@ss4"}}]}}

        def render(items):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.menu.explain_rules(items)
            return out.getvalue()

        self.assertEqual(render(base + [stat, drop]), render(base + [drop]))

    def test_menu_has_entry_under_status_submenu(self):
        source = (ROOT / "build" / "fw-menu").read_text()
        self.assertIn('("Кто стучится", boxed("Кто стучится", show_hits', source)


class Rules(unittest.TestCase):
    """Что update-fw.sh кладёт в правила."""

    @classmethod
    def setUpClass(cls):
        cls.upd = (ROOT / "build" / "update-fw.sh").read_text()

    def test_stat_rules_stand_before_the_drops_and_drops_unchanged(self):
        for kind, saddr, name in (("sc", "ip saddr @sc4", "ss4"), ("sc", "ip6 saddr @sc6", "ss6")):
            stat = re.search(r'echo "    meta l4proto \{ tcp, udp \} ' + re.escape(saddr) +
                             r' add @' + name + r' \{ [^"]*th dport[^"]*\}"', self.upd)
            self.assertTrue(stat, saddr)
        self.assertIn('echo "    ip saddr @sc4 counter drop comment \\"scanners\\""', self.upd)
        self.assertIn('counter drop comment \\"non-RU\\""', self.upd)
        self.assertLess(self.upd.index(" add @ss4 "), self.upd.index('ip saddr @sc4 counter drop'))
        self.assertLess(self.upd.index(" add @sn4 "), self.upd.index('ip saddr != @ru4 counter drop'))

    def test_stats_are_optional_and_never_block_the_rules(self):
        self.assertIn("STATS=", self.upd)
        # накопленное переносится из действующей таблицы
        self.assertIn('nft -n list set inet fw "$stat"', self.upd)
        # если nft -c не принял набор со статистикой, правила собираются заново без неё
        self.assertRegex(self.upd, r"STATS=0\n\s+emit_rules > \"\$TMP/new.nft\"")
        self.assertLess(self.upd.index("STATS=0\n"), self.upd.index('nft -c -f "$TMP/new.nft" || fail_keep'))

    @unittest.skipUnless(shutil.which("nft"), "нет nft")
    def test_nft_accepts_stat_set_and_rule(self):
        text = """table inet fwt {
  set ss4 { type ipv4_addr . inet_service; size 4096; flags dynamic,timeout; timeout 7d; counter;
    elements = { 198.51.100.7 . 22222 counter packets 40 bytes 2400 expires 6d23h59m1s } }
  set sc4 { type ipv4_addr; flags interval; auto-merge; }
  chain input { type filter hook input priority -10; policy accept;
    meta l4proto { tcp, udp } ip saddr @sc4 add @ss4 { ip saddr . th dport }
    ip saddr @sc4 counter drop comment "scanners"
  }
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.nft"
            path.write_text(text)
            result = subprocess.run(["nft", "-c", "-f", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
