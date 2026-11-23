"""«Кто стучится»: разбор счётчиков nft, сводка и экран; сбор статистики в правилах."""
import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unicodedata
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
        self.menu.screen_columns = lambda: 120

    def fake_nft(self, outputs):
        def probe(*command, **_):
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
        self.assertIn("Из других стран", text)
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

    def test_full_set_is_detected_by_size_line(self):
        full = SET_TEXT.replace("size 4096", "size 3")
        self.assertTrue(self.menu.set_is_full(full))
        self.assertFalse(self.menu.set_is_full(SET_TEXT))  # 3 записи из 4096
        self.assertFalse(self.menu.set_is_full(SET_TEXT_V6))  # строки size нет: не гадаем
        self.assertFalse(self.menu.set_is_full(SET_TEXT.replace("size 4096", "size 0")))
        self.assertFalse(self.menu.set_is_full(""))

    def test_screen_warns_when_stat_set_is_full(self):
        # полный набор ядро не пополняет: новые сканеры молча не попадают в статистику
        self.menu.reverse_name = lambda address: ""
        self.fake_nft({"ss4": SET_TEXT.replace("size 4096", "size 3"), "ss6": "",
                       "sn4": SET_TEXT.replace("198.51.100.7", "192.0.2.50"), "sn6": ""})
        text = self.show()
        scanners, _, other = text.partition("Из других стран")
        self.assertIn("место для статистики закончилось", scanners)
        self.assertIn("Блокировка при этом работает", scanners)
        self.assertNotIn("место для статистики закончилось", other)

    def test_screen_has_no_warning_when_there_is_room(self):
        self.menu.reverse_name = lambda address: ""
        self.fake_nft({"ss4": SET_TEXT, "ss6": SET_TEXT_V6, "sn4": "", "sn6": ""})
        self.assertNotIn("закончилось", self.show())

    def slow_nft(self, slow, outputs):
        # как probe_output при таймауте: для «медленных» наборов возвращает on_timeout
        self.asked = []

        def probe(*command, timeout=4, on_timeout=""):
            self.asked.append(timeout)
            return on_timeout if command[-1] in slow else outputs.get(command[-1], "")
        self.menu.probe_output = probe
        self.menu.reverse_name = lambda address: ""

    def test_slow_set_is_reported_and_other_group_still_shown(self):
        self.slow_nft({"ss4"}, {"sn4": SET_TEXT.replace("198.51.100.7", "192.0.2.50")})
        text = self.show()
        self.assertIn("не прочитана (ss4)", text)
        self.assertIn("Блокировка от этого не зависит", text)
        self.assertIn("Из других стран", text)
        self.assertNotIn("Сканеры и сети ведомств (любые порты)", text)
        self.assertNotIn("не ведётся", text)

    def test_all_sets_slow_is_not_called_missing_statistics(self):
        # раньше таймаут выглядел как пустой ответ: «Статистика не ведётся… примените правила заново»
        self.slow_nft({"ss4", "ss6", "sn4", "sn6"}, {})
        text = self.show()
        self.assertIn("не прочитана (ss4, ss6, sn4, sn6)", text)
        self.assertNotIn("не ведётся", text)
        self.assertNotIn("Примените правила заново", text)
        self.assertNotIn("Пока никого", text)

    def test_unread_set_is_not_reported_as_nobody_knocking(self):
        empty = "table inet fw {\n\tset sn4 {\n\t\ttype ipv4_addr . inet_service\n\t}\n}\n"
        self.slow_nft({"ss4"}, {"sn4": empty})
        text = self.show()
        self.assertIn("не прочитана (ss4)", text)
        self.assertNotIn("Пока никого", text)

    def test_sets_are_read_with_a_long_timeout(self):
        # полный набор на 65536 записей на слабом сервере читается дольше обычных 4 секунд
        self.slow_nft(set(), {"ss4": SET_TEXT})
        self.show()
        self.assertEqual(len(self.asked), 4)
        self.assertTrue(all(timeout >= 30 for timeout in self.asked), self.asked)

    def test_probe_output_timeout_is_optional(self):
        if not shutil.which("sleep"):
            self.skipTest("нет sleep")
        self.assertEqual(self.menu.probe_output("sleep", "2", timeout=0.2), "")  # по умолчанию как раньше
        self.assertIsNone(self.menu.probe_output("sleep", "2", timeout=0.2, on_timeout=None))
        self.assertEqual(self.menu.probe_output("/nonexistent/cmd", on_timeout=None), "")
        self.assertEqual(self.menu.probe_output("echo", "hi"), "hi\n")

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


class Readable(unittest.TestCase):
    """Экран «Куда стучатся» объясняет сам себя: что за числа, какая доля, куда делись остальные порты."""
    PORTS = {80: 49, 443: 44, 22: 18, 143: 17, 25: 16, 27: 40, 600: 30, 1071: 20, 830: 10, 1050: 6}

    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.menu.lookup_owner = lambda address: ""
        self.menu.screen_columns = lambda: 120
        rows = ", ".join(f"198.51.100.{n} . {port} counter packets {count} bytes 1 expires 1d"
                         for n, (port, count) in enumerate(self.PORTS.items(), start=1))
        self.text = "elements = { " + rows + " }"
        self.menu.probe_output = lambda *c, **_: self.text if c[-1] == "sn4" else ""

    def render(self, columns=120):
        self.menu.screen_columns = lambda: columns
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_hits()
        return out.getvalue().splitlines()

    def test_summary_counts_the_ports_that_are_not_shown(self):
        summary = self.menu.summarize_hits(self.menu.parse_hits(self.text))
        total = sum(self.PORTS.values())
        top = sum(packets for _port, packets in summary["ports"])
        self.assertEqual(summary["other_ports"], len(self.PORTS) - 5)
        self.assertEqual(summary["other_packets"], total - top)

    def test_no_rest_row_when_all_ports_fit(self):
        summary = self.menu.summarize_hits(self.menu.parse_hits(
            "elements = { 198.51.100.1 . 80 counter packets 5 bytes 1 expires 1d }"))
        self.assertEqual((summary["other_ports"], summary["other_packets"]), (0, 0))

    def test_share_is_a_whole_percent_and_never_zero_for_real_hits(self):
        self.assertEqual(self.menu.share(49, 2807), "2%")
        self.assertEqual(self.menu.share(1, 2807), "<1%")
        self.assertEqual(self.menu.share(2807, 2807), "100%")
        self.assertEqual(self.menu.share(0, 100), "0%")
        self.assertEqual(self.menu.share(5, 0), "")

    def test_heading_says_what_the_numbers_are(self):
        text = "\n".join(self.render())
        self.assertIn("число — сколько раз отклонена попытка на этот порт", text)

    def test_every_port_row_has_count_and_share(self):
        total = sum(self.PORTS.values())
        lines = self.render()
        row = next(line for line in lines if line.strip().startswith("80 "))
        self.assertIn(" 49 ", row + " ")
        self.assertIn(self.menu.share(49, total), row)

    def test_rest_row_makes_the_numbers_add_up(self):
        total = sum(self.PORTS.values())
        top = sorted(self.PORTS.values(), reverse=True)[:5]
        rest = total - sum(top)
        lines = self.render()
        row = next(line for line in lines if line.strip().startswith("ещё "))
        self.assertIn(f"ещё {len(self.PORTS) - 5} портов", row)
        self.assertIn(str(rest), row)
        self.assertIn(self.menu.share(rest, total), row)

    def test_rest_row_is_plain_without_a_bar(self):
        row = next(line for line in self.render() if line.strip().startswith("ещё "))
        self.assertNotIn("▮", row)
        self.assertNotIn("▯", row)

    def test_owner_and_address_blocks_have_a_legend(self):
        text = "\n".join(self.render())
        self.assertIn("По владельцам (попыток, адресов, чаще всего порты)", text)
        self.assertIn("Самые активные адреса (попыток, на сколько портов стучался)", text)

    def test_a_phone_keeps_every_line_inside_the_box(self):
        too_long = [line for line in self.render(58) if len(line) > 54]
        self.assertEqual(too_long, [])
        text = "\n".join(self.render(58))
        self.assertIn(f"ещё {len(self.PORTS) - 5} {self.menu.ports_word(len(self.PORTS) - 5)}", text)

    def test_word_form_of_ports_is_correct(self):
        for count, word in ((1, "порт"), (2, "порта"), (5, "портов"), (11, "портов"), (21, "порт"), (22, "порта")):
            self.assertEqual(self.menu.ports_word(count), word, count)


class Narrow(unittest.TestCase):
    """Экран на телефоне: около 58 колонок, рамка оставляет строке columns - 4."""
    ROWS = ("elements = { 185.93.89.10 . 443 counter packets 59 bytes 1 expires 1d, "
            "185.12.59.118 . 443 counter packets 7 bytes 1 expires 1d, "
            "192.248.150.180 . 3389 counter packets 1 bytes 1 expires 1d }")

    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        owners = {"185.93.89.10": "LimitedNetwork-AS Limited…, AE, AS213790",
                  "185.12.59.118": "BLIX Blix Solutions AS, NO, AS50304",
                  "192.248.150.180": "The Constant Company, LLC, GB, AS20473"}
        self.menu.lookup_owner = lambda address: owners.get(address, "")
        self.menu.probe_output = lambda *c, **_: self.ROWS if c[-1] == "sn4" else ""

    def render(self, columns):
        self.menu.screen_columns = lambda: columns
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_hits()
        return out.getvalue().splitlines()

    def test_no_line_is_longer_than_the_box_on_a_phone(self):
        lines = self.render(58)
        too_long = [line for line in lines if len(line) > 54]
        self.assertEqual(too_long, [])

    def test_owner_goes_under_the_address(self):
        lines = self.render(58)
        index = next(i for i, line in enumerate(lines) if "185.93.89.10" in line)
        self.assertNotIn("LimitedNetwork", lines[index])
        self.assertIn("LimitedNetwork-AS Limited…, AE, AS213790", lines[index + 1])
        self.assertIn("59", lines[index])

    def test_wide_screen_keeps_everything_on_one_line(self):
        lines = self.render(120)
        line = next(item for item in lines if "185.93.89.10" in item)
        self.assertIn("LimitedNetwork-AS Limited…, AE, AS213790", line)
        self.assertIn("портов: 1", line)

    def test_port_label_is_shortened_not_wrapped_on_a_phone(self):
        self.menu.probe_output = lambda *c, **_: ("elements = { 1.2.3.4 . 3389 counter packets 30 bytes 1 expires 1d }"
                                             if c[-1] == "sn4" else "")
        line = next(item for item in self.render(58) if item.strip().startswith("3389"))
        self.assertLessEqual(len(line), 54)

    def test_screen_columns_falls_back_to_wide(self):
        menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(menu)
        columns = menu.screen_columns()
        self.assertIsInstance(columns, int)
        self.assertGreater(columns, 0)


class Owners(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.menu.screen_columns = lambda: 120
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
        menu.probe_output = lambda *c, **_: (
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


class Grouped(unittest.TestCase):
    """«По владельцам»: одна строка на владельца, простые слова на экране."""
    ROWS = ("elements = { 185.93.89.10 . 443 counter packets 50 bytes 1 expires 1d, "
            "185.93.89.11 . 443 counter packets 30 bytes 1 expires 1d, "
            "185.93.89.11 . 8443 counter packets 5 bytes 1 expires 1d, "
            "192.248.150.180 . 3389 counter packets 9 bytes 1 expires 1d }")
    OWNERS = {"185.93.89.10": "LimitedNetwork, AE, AS213790", "185.93.89.11": "LimitedNetwork, AE, AS213790",
              "192.248.150.180": "The Constant Company, GB, AS20473"}

    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.calls = []

        def lookup(address):
            self.calls.append(address)
            return self.OWNERS.get(address, "")
        self.menu.lookup_owner = lookup
        self.menu.reverse_name = lambda address: ""
        self.menu.probe_output = lambda *c, **_: self.ROWS if c[-1] == "sn4" else ""

    def render(self, columns=120):
        self.menu.screen_columns = lambda: columns
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_hits()
        return out.getvalue()

    def test_group_sums_addresses_packets_and_ports(self):
        rows = self.menu.parse_hits(self.ROWS)
        groups = self.menu.group_by_owner(rows, lambda address: self.OWNERS[address])
        self.assertEqual([group["owner"] for group in groups],
                         ["LimitedNetwork, AE, AS213790", "The Constant Company, GB, AS20473"])
        first = groups[0]
        self.assertEqual((first["addresses"], first["packets"]), (2, 85))
        self.assertEqual(first["ports"], [443, 8443])

    def test_group_keeps_at_most_three_ports_ordered_by_packets(self):
        rows = [("1.1.1.1", port, packets, 0) for port, packets in ((1, 1), (2, 9), (3, 5), (4, 7))]
        group = self.menu.group_by_owner(rows, lambda address: "X")[0]
        self.assertEqual(group["ports"], [2, 4, 3])

    def test_screen_has_owner_section_with_counts(self):
        text = self.render()
        self.assertIn("По владельцам", text)
        line = next(item for item in text.splitlines() if "LimitedNetwork" in item)
        self.assertIn("2 адр.", line)
        self.assertIn("443, 8443", line)
        self.assertIn("85", line)

    def test_owner_lines_fit_a_phone(self):
        lines = self.render(58).splitlines()
        start = next(i for i, line in enumerate(lines) if "По владельцам" in line)
        for line in lines[start:start + 9]:
            self.assertLessEqual(len(line), 54, line)
        self.assertTrue(any("LimitedNetwork" in line for line in lines[start:]))

    def test_only_the_busiest_addresses_are_looked_up(self):
        many = ", ".join(f"10.0.{i // 250}.{i % 250 + 1} . 443 counter packets {1000 - i} bytes 1 expires 1d"
                         for i in range(60))
        self.menu.probe_output = lambda *c, **_: f"elements = {{ {many} }}" if c[-1] == "sn4" else ""
        self.menu.lookup_owner = lambda address: self.calls.append(address) or ""
        text = self.render()
        self.assertLessEqual(len(set(self.calls)), self.menu.OWNER_LOOKUPS)
        self.assertIn("другие адреса", text)  # остаток не теряется, показывается одной строкой

    def test_leftover_line_has_no_bar_and_does_not_set_the_scale(self):
        many = ", ".join(f"10.0.{i // 250}.{i % 250 + 1} . 443 counter packets {1000 - i} bytes 1 expires 1d"
                         for i in range(60))
        self.menu.probe_output = lambda *c, **_: f"elements = {{ {many} }}" if c[-1] == "sn4" else ""
        self.menu.lookup_owner = lambda address: "Один владелец, XX, AS1"
        lines = self.render().splitlines()
        rest = next(item for item in lines if item.strip().startswith("другие адреса"))
        self.assertNotIn("▮", rest)
        self.assertNotIn("▯", rest)
        named = next(item for item in lines if item.strip().startswith("Один владелец"))
        self.assertEqual(named.count("▮"), 8)  # самый активный владелец занимает всю полоску

    def test_failed_lookup_does_not_break_the_screen(self):
        def broken(address):
            raise RuntimeError("сеть упала")
        self.menu.lookup_owner = broken
        text = self.render()
        self.assertIn("неизвестно", text)
        self.assertNotIn("Traceback", text)

    def test_plain_words_instead_of_jargon(self):
        text = self.render()
        for word in ("Попыток:", "с разных адресов", "Из других стран", "Чего здесь нет"):
            self.assertIn(word, text)
        for old in ("Всего пакетов", "пакеты, отброшенные", "Что здесь не видно", "чей адрес"):
            self.assertNotIn(old, text)


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
        self.menu.screen_columns = lambda: 120
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

    def test_parse_without_holder_shows_only_as_number(self):
        # RIPEstat иногда отдаёт holder: null; на экране не должно быть «None AS64500»
        for holder in (None, "", "  "):
            text = json.dumps({"data": {"asns": [{"asn": 64500, "holder": holder}]}})
            self.assertEqual(self.menu.parse_ripe_owner(text), "AS64500")

    def test_parse_whois_takes_best_name_and_country(self):
        self.assertEqual(self.menu.parse_whois_owner(WHOIS_ANSWER), ("dedires", "HK"))
        self.assertEqual(self.menu.parse_whois_owner(WHOIS_ARIN), ("Amazon Technologies Inc.", "US"))
        descr = json.dumps({"data": {"records": [[{"key": "netname", "value": "X-NET"},
                {"key": "descr", "value": "Blix Solutions AS"}, {"key": "descr", "value": "второе"}]]}})
        self.assertEqual(self.menu.parse_whois_owner(descr), ("Blix Solutions AS", ""))

    def test_parse_whois_skips_street_address_in_descr(self):
        text = json.dumps({"data": {"records": [[{"key": "netname", "value": "CHOOPA-NET"},
                {"key": "descr", "value": "11 Hanbury St"}, {"key": "country", "value": "GB"}]]}})
        self.assertEqual(self.menu.parse_whois_owner(text), ("CHOOPA-NET", "GB"))

    def test_parse_whois_bad_answers(self):
        for bad in ("", "x", "{}", '{"data": {"records": []}}', '{"data": {"records": [[]]}}'):
            self.assertEqual(self.menu.parse_whois_owner(bad), ("", ""), bad)

    def test_owner_without_holder_uses_registry_name_or_as_number(self):
        self.menu.fetch_ripe = lambda address: json.dumps({"data": {"asns": [{"asn": 64500, "holder": None}]}})
        self.assertEqual(self.menu.lookup_owner("8.8.4.4"), "AS64500")
        whois = json.dumps({"data": {"records": [[{"key": "netname", "value": "example-net"},
                                                  {"key": "country", "value": "NL"}]]}})
        self.menu.fetch_whois = lambda address: whois
        self.assertEqual(self.menu.lookup_owner("9.9.9.9"), "example-net, NL, AS64500")
        self.assertNotIn("None", (self.menu.DIRECTORY / "owner-cache.json").read_text())

    def test_owner_combines_registry_name_country_and_as(self):
        self.menu.fetch_whois = lambda address: WHOIS_ANSWER
        self.assertEqual(self.menu.lookup_owner("172.110.223.81"), "dedires, HK, AS8075")

    def test_technical_uppercase_netname_is_replaced_by_as_holder(self):
        for technical in ("MSFT", "NET-V4-192-248-128-0-18", "NO-BLIXCUST10463"):
            answer = json.dumps({"data": {"records": [[{"key": "netname", "value": technical},
                                                       {"key": "country", "value": "GB"}]]}})
            self.menu.fetch_whois = lambda address, answer=answer: answer
            self.assertEqual(self.menu.lookup_owner("20.98.155.81"), "Microsoft Corporation, GB, AS8075", technical)
            (Path(self.tmp.name) / "owner-cache.json").unlink(missing_ok=True)

    def test_long_name_is_shortened_but_country_and_as_stay(self):
        long = "LimitedNetwork-AS Limited Network LTD and sons of something"
        self.menu.fetch_whois = lambda address: json.dumps(
            {"data": {"records": [[{"key": "descr", "value": long}, {"key": "country", "value": "GB"}]]}})
        owner = self.menu.lookup_owner("20.98.155.81")
        self.assertTrue(owner.endswith(", GB, AS8075"), owner)
        self.assertIn("…", owner)
        self.assertLessEqual(len(owner), 40)

    def test_long_as_holder_fallback_keeps_as_number(self):
        long = "VERY-LONG-HOLDER - Очень длинное название владельца автономной системы Inc"
        self.menu.fetch_ripe = lambda address: json.dumps({"data": {"asns": [{"asn": 64500, "holder": long}]}})
        owner = self.menu.lookup_owner("20.98.155.81")
        self.assertTrue(owner.endswith(" AS64500"), owner)
        self.assertLessEqual(len(owner), 40)
        self.assertIn("…", owner)

    def test_lowercase_netname_is_kept(self):
        self.menu.fetch_whois = lambda address: WHOIS_ANSWER
        self.assertTrue(self.menu.lookup_owner("172.110.223.81").startswith("dedires, HK"))

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
        menu.probe_output = lambda *c, **_: (
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

    def test_parallel_lookups_keep_every_cache_entry(self):
        addresses = [f"20.98.{i}.1" for i in range(24)]
        owners = self.menu.resolve_owners(addresses, "non_ru", [])
        self.assertEqual(len(owners), 24)
        cache = json.loads((Path(self.tmp.name) / self.menu.OWNER_CACHE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(sorted(cache), sorted(addresses))

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
        menu.probe_output = lambda *c, **_: (
            "elements = { 20.98.155.81 . 443 counter packets 1 bytes 1 expires 1d }" if c[-1] == "sn4" else "")
        menu.reverse_name = lambda address: "dns.example.com"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.show_hits()
        self.assertIn("Microsoft Corporation AS8075", out.getvalue())
        self.assertNotIn("dns.example.com", out.getvalue())


EVIL = "Evil Net\x1b]0;owned\x07\x1b[2J\x1b[31m\u202egpj.exe\r"


class Sanitize(unittest.TestCase):
    """Чужие тексты (whois, RIPEstat, PTR, имена процессов) не должны нести управляющие символы."""

    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.menu.DIRECTORY = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assertClean(self, text):
        bad = [char for char in text if unicodedata.category(char) in ("Cc", "Cf")]
        self.assertEqual(bad, [], repr(text))

    def test_ripe_holder_is_cleaned(self):
        text = json.dumps({"data": {"asns": [{"asn": 64500, "holder": EVIL}]}})
        owner = self.menu.parse_ripe_owner(text)
        self.assertClean(owner)
        self.assertIn("Evil Net", owner)

    def test_whois_name_and_country_are_cleaned(self):
        text = json.dumps({"data": {"records": [[{"key": "descr", "value": EVIL},
                                                 {"key": "country", "value": "R\x1bU"}]]}})
        name, country = self.menu.parse_whois_owner(text)
        self.assertClean(name)
        self.assertClean(country)
        self.assertIn("Evil Net", name)

    def test_poisoned_cache_entry_is_cleaned_on_read(self):
        now = int(datetime.datetime.now().timestamp())
        (Path(self.tmp.name) / "owner-cache.json").write_text(json.dumps({"8.8.4.4": [EVIL, now]}))
        self.menu.fetch_ripe = lambda address: self.fail("кэш свежий, сеть не нужна")
        self.assertClean(self.menu.lookup_owner("8.8.4.4"))

    def test_reverse_name_is_cleaned(self):
        class Result:
            returncode = 0
            stdout = "8.8.4.4 host.evil\x1b[2J.example.com\n"
        self.menu.subprocess.run = lambda *args, **kwargs: Result()
        self.assertClean(self.menu.reverse_name("8.8.4.4"))

    def test_process_name_from_ss_is_cleaned(self):
        line = 'tcp LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:* users:(("evil\x1b[2Jd",pid=1,fd=3))'
        for _, _, name, _ in self.menu.parse_listeners(line):
            self.assertClean(name)

    def test_box_keeps_colors_but_drops_other_escapes(self):
        cells = self.menu.box_cells("\x1b[31mred\x1b[0m \x1b]0;title\x07\x1b[2Jtext")
        text = "".join(char for char, _ in cells)
        self.assertClean(text)
        self.assertEqual(text, "red text")
        self.assertEqual(cells[0][1], "\x1b[31m")


class Carry(unittest.TestCase):
    """Перенос накопленного: разбор вывода `nft list set` в update-fw.sh (shell-функция stat_elements)."""

    @classmethod
    def setUpClass(cls):
        upd = (ROOT / "build" / "update-fw.sh").read_text()
        match = re.search(r"^stat_elements\(\) \{\n.*?^\}\n", upd, re.S | re.M)
        cls.func = match.group(0) if match else ""

    def run_func(self, text):
        self.assertTrue(self.func, "функция stat_elements не найдена")
        result = subprocess.run(["bash", "-c", self.func + "\nstat_elements"], input=text, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_carries_more_than_the_old_limit_of_4096(self):
        # за неделю набирается порядка 19 тысяч пар: старый предел в 4096 обрезал бы перенос
        lines = [f"10.{i // 65536}.{i // 256 % 256}.{i % 256} . 443 counter packets 1 bytes 40 expires 6d"
                 for i in range(20000)]
        out = self.run_func("elements = { " + ",\n".join(lines) + " }")
        self.assertEqual(len(out), 20000)

    def test_carry_is_capped_at_the_set_size(self):
        lines = [f"10.{i // 65536}.{i // 256 % 256}.{i % 256} . 443 counter packets 1 bytes 40 expires 6d"
                 for i in range(70000)]
        out = self.run_func("elements = { " + ",\n".join(lines) + " }")
        self.assertEqual(len(out), 65536)

    def test_keeps_elements_with_and_without_timeout_word(self):
        # вывод с вашего сервера: у новых записей между bytes и expires стоит «timeout 7d»
        text = """table inet fw {
	set ss4 {
		elements = { 216.25.89.89 . 2083 counter packets 1 bytes 44 timeout 7d expires 6d23h59m30s725ms,
			     216.25.89.83 . 8020 counter packets 0 bytes 0 expires 6d23h17m33s386ms,
			     147.185.133.16 . 9335 counter packets 7 bytes 308 timeout 7d expires 6d23h59m15s143ms }
	}
}
"""
        self.assertEqual(self.run_func(text), [
            "216.25.89.89 . 2083 counter packets 1 bytes 44 expires 6d23h59m30s725ms",
            "216.25.89.83 . 8020 counter packets 0 bytes 0 expires 6d23h17m33s386ms",
            "147.185.133.16 . 9335 counter packets 7 bytes 308 expires 6d23h59m15s143ms"])

    def test_ipv6_and_garbage(self):
        text = "elements = { 2001:db8::1 . 80 counter packets 3 bytes 180 timeout 7d expires 1d2h }\nError: no such set\n"
        self.assertEqual(self.run_func(text), ["2001:db8::1 . 80 counter packets 3 bytes 180 expires 1d2h"])
        self.assertEqual(self.run_func(""), [])

    def test_restored_elements_are_accepted_by_nft(self):
        if not shutil.which("nft"):
            self.skipTest("нет nft")
        lines = self.run_func("elements = { 1.2.3.4 . 443 counter packets 5 bytes 220 timeout 7d expires 6d1h }")
        table = ("table inet fwt {\n set ss4 { type ipv4_addr . inet_service; size 4096; flags dynamic,timeout;"
                 " timeout 7d; counter;\n  elements = { " + ", ".join(lines) + " } }\n}\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.nft"
            path.write_text(table)
            result = subprocess.run(["nft", "-c", "-f", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class FullSet(unittest.TestCase):
    """Настоящее ядро: набор на 2 записи после 5 разных портов заполнен, а drop считает все пакеты."""
    RULES = """table inet fwt {
 set ss4 { type ipv4_addr . inet_service; size 2; flags dynamic,timeout; timeout 7d; counter; }
 chain input { type filter hook input priority -10; policy accept;
  meta l4proto { tcp, udp } ip saddr 127.0.0.1 th dport 9001-9005 add @ss4 { ip saddr . th dport }
  ip saddr 127.0.0.1 udp dport 9001-9005 counter drop comment "scanners"
 }
}
"""

    def test_kernel_stops_adding_and_menu_sees_it(self):
        if not (shutil.which("nft") and shutil.which("unshare")):
            self.skipTest("нет nft или unshare")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.nft"
            path.write_text(self.RULES)
            lo_up = ("import fcntl, socket, struct; s = socket.socket(); "
                     "fcntl.ioctl(s, 0x8914, struct.pack('16sH14s', b'lo', 0x41, b''))")  # без команды ip
            script = (f'python3 -c "{lo_up}" && nft -f "$1" || exit 77; '
                      'for p in 9001 9002 9003 9004 9005; do echo x > /dev/udp/127.0.0.1/$p; done; '
                      'nft -n list set inet fwt ss4 && nft list chain inet fwt input')
            result = subprocess.run(["unshare", "-n", "bash", "-c", script, "t", str(path)],
                                    capture_output=True, text=True)
        if result.returncode == 77:
            self.skipTest("нет прав на сетевое пространство или nft")
        self.assertEqual(result.returncode, 0, result.stderr)
        menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(menu)
        listing = result.stdout.split("table inet fwt {\n\tchain")[0]
        self.assertEqual(len(menu.parse_hits(listing)), 2)  # третья и дальше пары не записались
        self.assertTrue(menu.set_is_full(listing))
        self.assertIn("packets 5", result.stdout)  # а drop посчитал все пять пакетов


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

    def test_stat_sets_have_room_for_a_week_of_scanners(self):
        self.assertIn("size 65536;", self.upd)
        self.assertNotIn("size 4096;", self.upd)
        self.assertIn("head -n 65536", self.upd)

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
