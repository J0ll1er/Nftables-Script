"""Списки сканеров: Stretchoid входит в пресет «classic» и имеет подпись в меню."""
import importlib.machinery
import importlib.util
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
UPDATER = (ROOT / "build" / "update-fw.sh").read_text(encoding="utf-8")
LOADER = importlib.machinery.SourceFileLoader("fwmenu_scan_sources", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


def scan_files():
    block = re.search(r"^SCAN_FILES=\(\n(.*?)^\)", UPDATER, re.S | re.M).group(1)
    return re.findall(r'^\s*"([^"]+)"', block, re.M)


def preset_of(name):
    # Те же правила, что в обновлялке: имя файла → пресет, остальное — classic.
    rules = re.search(r'case "\$\{spec##\*/\}" in\n(.*?)\n  esac', UPDATER, re.S).group(1)
    for line in rules.splitlines():
        match = re.match(r"\s*([^)]+)\)\s*preset=(\w+)", line)
        if match and name in match.group(1).split("|"):
            return match.group(2)
        if line.strip().startswith("*)"):
            return re.search(r"preset=(\w+)", line).group(1)
    raise AssertionError("не нашёл правило пресета")


class Stretchoid(unittest.TestCase):
    def test_list_is_downloaded(self):
        self.assertIn("$OPENFILTERS/strechoid_v4.txt", scan_files())

    def test_list_belongs_to_classic_preset(self):
        self.assertEqual(preset_of("strechoid_v4.txt"), "classic")

    def test_existing_sources_keep_their_presets(self):
        self.assertEqual(preset_of("censys_v4.txt"), "classic")
        self.assertEqual(preset_of("fofa_v4.txt"), "fofa")
        self.assertEqual(preset_of("skipa.list"), "skipa")

    def test_menu_shows_a_readable_name(self):
        menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(menu)
        self.assertEqual(menu.source_label("strechoid_v4.txt"), "Stretchoid")

    def test_no_source_is_listed_twice(self):
        names = [item.rsplit("/", 1)[-1] for item in scan_files()]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
