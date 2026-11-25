"""fw-menu: «Статус файрвола» — итог сверху, настройки по строке, замечания."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_status", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class Status(unittest.TestCase):
    def setUp(self):
        self.menu = menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for name in ("fw.conf", "asn.txt", "extra.txt", "trusted.txt", "update-fw.sh"):
            (self.dir / name).write_text("", encoding="utf-8")
        (self.dir / "update-fw.sh").chmod(0o755)
        menu.DIRECTORY = self.dir
        menu.TRUSTED = self.dir / "trusted.txt"
        self.settings = ["", "60", "3", "0.2", "14", "0"]
        self.chain = ""
        self.profile = []
        self.service = {"LoadState": "loaded", "UnitFileState": "enabled", "Result": "success"}
        self.timer = {"ActiveState": "active", "UnitFileState": "enabled"}
        self.table_ok = True
        self.admin = ""

        def probe(*command, **kwargs):
            if command[0] == "bash":
                return "\n".join(self.settings) + "\n"
            if command[0] == "nft":
                return self.chain
            return ""
        menu.probe_output = probe
        menu.trusted_networks = lambda: []
        menu.trusted_names = lambda: []
        menu.warn_old_trusted = lambda: None
        menu.warn_reverse_proxy = lambda: None
        menu.show_profile = lambda: self.profile
        menu.systemd_properties = lambda unit, *names: self.timer if unit.endswith(".timer") else self.service
        menu.explain_rules = lambda items: print("(правила простым языком)")
        menu.admin_address = lambda: self.admin
        menu.lan_allow_networks = lambda: []

        def fake_run(command, **kwargs):
            if self.table_ok:
                return subprocess.CompletedProcess(command, 0, stdout='{"nftables": []}', stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        self.patches = [mock.patch.object(menu.subprocess, "run", fake_run),
                        mock.patch.object(menu.shutil, "which", lambda name: "/usr/bin/" + name)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.show_status()
        return out.getvalue()

    def test_everything_fine_gives_one_green_line_first(self):
        text = self.status()
        first = text.splitlines()[0]
        self.assertTrue(first.startswith("ИТОГ: ВСЁ В ПОРЯДКЕ"), first)
        self.assertLess(text.index("ИТОГ"), text.index("Проверка файлов и команд"))
        self.assertNotIn("ЕСТЬ ЗАМЕЧАНИЯ", text)
        self.assertIn("(правила простым языком)", text)

    def test_settings_are_one_per_line(self):
        text = self.status()
        for name in ("LAN_ALLOW", "SSH_RATE_LIMIT", "CONFIRM_TIMEOUT", "MAX_GROWTH", "MIN_KEEP", "MAX_CACHE_DAYS"):
            self.assertRegex(text, rf"(?m)^  {name} \.+ \S")
        self.assertRegex(text, r"LAN_ALLOW \.+ выключено")
        self.assertRegex(text, r"CONFIRM_TIMEOUT \.+ 60 с")
        self.assertNotIn("не по умолчанию", text)
        self.assertNotIn("Настройки:", text)

    def test_non_default_values_are_marked(self):
        self.settings[1] = "120"
        self.settings[4] = "30"
        text = self.status()
        self.assertRegex(text, r"CONFIRM_TIMEOUT \.+ 120 с   ← не по умолчанию")
        self.assertRegex(text, r"MAX_CACHE_DAYS \.+ 30   ← не по умолчанию")
        self.assertNotRegex(text, r"MAX_GROWTH.*не по умолчанию")

    def test_enabled_ssh_limit_is_marked_as_enabled(self):
        self.settings[5] = "20"
        self.chain = 'counter drop comment "ssh-rate"'
        text = self.status()
        self.assertRegex(text, r"SSH_RATE_LIMIT \.+ 20 в минуту   ← включено")
        self.assertIn("ИТОГ: ВСЁ В ПОРЯДКЕ", text)

    def test_missing_file_is_a_problem(self):
        (self.dir / "asn.txt").unlink()
        text = self.status()
        self.assertIn("ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ (1)", text)
        self.assertRegex(text, r"! нет файла .*asn\.txt")

    def test_rules_not_loaded_is_a_problem(self):
        self.table_ok = False
        text = self.status()
        self.assertIn("ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ (1)", text)
        self.assertIn("правила ещё не загружены", text.split("Проверка файлов")[0])

    def test_failed_update_and_inactive_timer_are_problems(self):
        self.service["Result"] = "exit-code"
        self.timer["ActiveState"] = "inactive"
        head = self.status().split("Проверка файлов")[0]
        self.assertIn("ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ (2)", head)
        self.assertIn("последнее обновление завершилось с ошибкой", head)
        self.assertIn("таймер обновления не активен", head)

    def test_unknown_timer_state_is_not_a_problem(self):
        self.timer = {}
        self.service = {}
        self.assertIn("ИТОГ: ВСЁ В ПОРЯДКЕ", self.status())

    def test_profile_findings_are_counted(self):
        self.profile = ["первое", "второе"]
        head = self.status().split("Проверка файлов")[0]
        self.assertIn("расхождения с настройками: 2", head)

    def test_ssh_not_loaded_and_exposed_admin_are_problems(self):
        self.settings[5] = "20"
        self.admin = "198.51.100.7"
        text = self.status()
        head = text.split("Проверка файлов")[0]
        self.assertIn("ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ (2)", head)
        self.assertIn("лимит SSH настроен, но правило ещё не загружено", head)
        self.assertIn("адрес твоего подключения не в доверенных", head)
        self.assertIn("ты подключён с 198.51.100.7", text)  # полный текст остаётся в блоке настроек

    def test_exception_in_body_still_prints_what_was_collected(self):
        self.menu.show_profile = lambda: (_ for _ in ()).throw(RuntimeError("сбой"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(RuntimeError):
            self.menu.show_status()
        self.assertIn("Проверка файлов и команд", out.getvalue())
        self.assertNotIn("ИТОГ", out.getvalue())

    def test_box_heading_for_settings_exists(self):
        self.assertIn("Настройки", self.menu.BOX_HEADINGS)


class BoxedLayout(Status):
    def test_summary_is_first_and_settings_become_a_section(self):
        text = self.status()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.print_box("Статус файрвола", text, self.menu.BOX_HEADINGS)
        lines = out.getvalue().splitlines()
        self.assertIn("ИТОГ: ВСЁ В ПОРЯДКЕ", lines[1])
        self.assertTrue(any(line.startswith("├─ Настройки ") for line in lines))
        self.assertTrue(any(line.startswith("├─ Проверка файлов и команд") for line in lines))


if __name__ == "__main__":
    unittest.main()
