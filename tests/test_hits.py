"""«Кто стучится»: разбор счётчиков nft, сводка и экран; сбор статистики в правилах."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
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
        self.menu.lookup_owner = lambda address: ""

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


class Owners(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.menu.DIRECTORY = Path(self.tmp.name)
        cache = Path(self.tmp.name) / "scanner-cache"
        cache.mkdir()
        (cache / "censys_v4.txt").write_text("162.216.149.0/24\n198.51.100.0/24\n")
        (cache / "paloaltonetworks_v4.txt").write_text("# c\n147.185.132.0/24\n")
        (cache / "skipa_cidr.txt").write_text("198.51.100.0/24\nмусор\n")
        (cache / "custom_ab12cd34_my.txt").write_text("203.0.113.0/24\n")
        (Path(self.tmp.name) / "asn_AS12345.cache").write_text("192.0.2.0/24\n")
        (Path(self.tmp.name) / "extra.txt").write_text("192.0.3.5\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_labels_of_known_files(self):
        label = self.menu.source_label
        self.assertEqual(label("censys_v4.txt"), "Censys")
        self.assertEqual(label("shodan_v4.txt"), "Shodan")
        self.assertEqual(label("paloaltonetworks_v4.txt"), "Palo Alto Networks")
        self.assertEqual(label("skipa_cidr.txt"), "СКИПА")
        self.assertEqual(label("custom_ab12cd34_my.txt"), "свой список")
        self.assertEqual(label("asn_AS12345.cache"), "AS12345")
        self.assertEqual(label("extra.txt"), "ваш список")

    def test_owner_found_by_lists(self):
        sources = self.menu.load_scanner_sources()
        owner = self.menu.owner_of
        self.assertEqual(owner("162.216.149.179", sources), "Censys")
        self.assertEqual(owner("147.185.132.72", sources), "Palo Alto Networks")
        self.assertEqual(owner("198.51.100.9", sources), "Censys, СКИПА")
        self.assertEqual(owner("192.0.2.77", sources), "AS12345")
        self.assertEqual(owner("192.0.3.5", sources), "ваш список")
        self.assertEqual(owner("8.8.8.8", sources), "")
        self.assertEqual(owner("2001:db8::1", sources), "")

    def test_sources_survive_missing_directory(self):
        self.menu.DIRECTORY = Path(self.tmp.name) / "нет"
        self.assertEqual(self.menu.load_scanner_sources(), [])

    def test_short_host(self):
        short = self.menu.short_host
        self.assertEqual(short("ec2-1-2-3-4.compute-1.amazonaws.com"), "amazonaws.com")
        self.assertEqual(short("host.example.co.uk"), "example.co.uk")
        self.assertEqual(short("localhost"), "localhost")
        self.assertEqual(short(""), "")

    def test_screen_shows_owner_and_reverse_name(self):
        menu = self.menu
        menu.probe_output = lambda *c: (
            "elements = { 162.216.149.179 . 22 counter packets 5 bytes 1 expires 1d }" if c[-1] == "ss4" else
            "elements = { 20.98.155.81 . 443 counter packets 1 bytes 1 expires 1d }" if c[-1] == "sn4" else "")
        menu.reverse_name = lambda address: "vm.compute.azure.com"
        menu.lookup_owner = lambda address: ""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.show_hits()
        text = out.getvalue()
        line = next(item for item in text.splitlines() if "162.216.149.179" in item)
        self.assertIn("Censys", line)
        line = next(item for item in text.splitlines() if "20.98.155.81" in item)
        self.assertIn("azure.com", line)


RIPE_ANSWER = json.dumps({"status": "ok", "data": {"resource": "20.96.0.0/14", "announced": True,
    "asns": [{"asn": 8075, "holder": "MICROSOFT-CORP-MSN-AS-BLOCK - Microsoft Corporation"}]}})


WHOIS_ANSWER = json.dumps({"status": "ok", "data": {"records": [[
    {"key": "inetnum", "value": "172.110.223.0/24"}, {"key": "netname", "value": "dedires"},
    {"key": "country", "value": "HK"}, {"key": "org", "value": "ORG-DL455-RIPE"}]],
    "irr_records": [[{"key": "route", "value": "172.110.223.0/24"}]]}})
WHOIS_ARIN = json.dumps({"data": {"records": [[
    {"key": "NetName", "value": "AMAZO-4"}, {"key": "OrgName", "value": "Amazon Technologies Inc."},
    {"key": "Country", "value": "US"}]]}})


class Lookup(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.menu.DIRECTORY = Path(self.tmp.name)
        self.calls = []

        def fetch(address):
            self.calls.append(address)
            return RIPE_ANSWER
        self.menu.fetch_ripe = fetch
        self.menu.fetch_whois = lambda address: ""

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_answer(self):
        self.assertEqual(self.menu.parse_ripe_owner(RIPE_ANSWER), "Microsoft Corporation AS8075")

    def test_parse_keeps_plain_holder_without_dash(self):
        text = json.dumps({"data": {"asns": [{"asn": 24940, "holder": "HETZNER-AS"}]}})
        self.assertEqual(self.menu.parse_ripe_owner(text), "HETZNER-AS AS24940")

    def test_parse_whois_takes_best_name_and_country(self):
        self.assertEqual(self.menu.parse_whois_owner(WHOIS_ANSWER), ("dedires", "HK"))
        self.assertEqual(self.menu.parse_whois_owner(WHOIS_ARIN), ("Amazon Technologies Inc.", "US"))
        descr = json.dumps({"data": {"records": [[{"key": "netname", "value": "X-NET"},
                {"key": "descr", "value": "Blix Solutions AS"}, {"key": "descr", "value": "второе"}]]}})
        self.assertEqual(self.menu.parse_whois_owner(descr), ("Blix Solutions AS", ""))

    def test_parse_whois_bad_answers(self):
        for bad in ("", "x", "{}", '{"data": {"records": []}}', '{"data": {"records": [[]]}}'):
            self.assertEqual(self.menu.parse_whois_owner(bad), ("", ""), bad)

    def test_owner_combines_registry_name_country_and_as(self):
        self.menu.fetch_whois = lambda address: WHOIS_ANSWER
        self.assertEqual(self.menu.lookup_owner("172.110.223.81"), "dedires, HK, AS8075")

    def test_owner_without_whois_falls_back_to_as_holder(self):
        self.assertEqual(self.menu.lookup_owner("172.110.223.81"), "Microsoft Corporation AS8075")

    def test_whois_not_asked_when_registry_is_down(self):
        asked = []
        self.menu.fetch_ripe = lambda address: ""
        self.menu.fetch_whois = lambda address: asked.append(address) or WHOIS_ANSWER
        self.assertEqual(self.menu.lookup_owner("172.110.223.81"), "")
        self.assertEqual(asked, [])

    def test_long_owner_is_cut_with_ellipsis_on_screen(self):
        menu = self.menu
        long = "Очень длинное название организации которое не помещается AS1"
        menu.lookup_owner = lambda address: long
        menu.probe_output = lambda *c: (
            "elements = { 20.98.155.81 . 443 counter packets 1 bytes 1 expires 1d }" if c[-1] == "sn4" else "")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.show_hits()
        line = next(item for item in out.getvalue().splitlines() if "20.98.155.81" in item)
        self.assertIn("…", line)
        self.assertNotIn(long, line)

    def test_parse_bad_answers_give_empty(self):
        for bad in ("", "не json", "{}", '{"status": "error"}', '{"data": {"asns": []}}',
                    '{"data": {"asns": [{"holder": "X"}]}}'):
            self.assertEqual(self.menu.parse_ripe_owner(bad), "", bad)

    def test_lookup_asks_once_then_uses_cache(self):
        self.assertEqual(self.menu.lookup_owner("20.98.155.81"), "Microsoft Corporation AS8075")
        self.assertEqual(self.menu.lookup_owner("20.98.155.81"), "Microsoft Corporation AS8075")
        self.assertEqual(self.calls, ["20.98.155.81"])
        self.assertTrue((Path(self.tmp.name) / "owner-cache.json").exists())

    def test_expired_cache_is_asked_again(self):
        self.menu.lookup_owner("20.98.155.81")
        path = Path(self.tmp.name) / "owner-cache.json"
        data = json.loads(path.read_text())
        for entry in data.values():
            entry[1] = 1
        path.write_text(json.dumps(data))
        self.menu.lookup_owner("20.98.155.81")
        self.assertEqual(len(self.calls), 2)

    def test_failure_is_not_cached_and_does_not_raise(self):
        self.menu.fetch_ripe = lambda address: ""
        self.assertEqual(self.menu.lookup_owner("20.98.155.81"), "")
        self.assertFalse((Path(self.tmp.name) / "owner-cache.json").exists())

    def test_private_and_broken_addresses_are_not_sent(self):
        for address in ("10.0.0.1", "127.0.0.1", "192.168.1.5", "не адрес"):
            self.assertEqual(self.menu.lookup_owner(address), "")
        self.assertEqual(self.calls, [])

    def test_broken_cache_file_is_ignored(self):
        (Path(self.tmp.name) / "owner-cache.json").write_text("{не json")
        self.assertEqual(self.menu.lookup_owner("20.98.155.81"), "Microsoft Corporation AS8075")

    def test_screen_prefers_lists_then_registry_then_dns(self):
        menu = self.menu
        menu.probe_output = lambda *c: (
            "elements = { 20.98.155.81 . 443 counter packets 1 bytes 1 expires 1d }" if c[-1] == "sn4" else "")
        menu.reverse_name = lambda address: "dns.example.com"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.show_hits()
        self.assertIn("Microsoft Corporation AS8075", out.getvalue())
        self.assertNotIn("dns.example.com", out.getvalue())


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
