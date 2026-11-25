"""fw-menu: защита от самоблокировки при включении лимита SSH (адрес администратора)."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
LOADER = importlib.machinery.SourceFileLoader("fwmenu_ssh_admin", str(ROOT / "build" / "fw-menu"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)


class Base(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.menu.DIRECTORY = self.dir
        self.menu.TRUSTED = self.dir / "trusted.txt"
        self.menu.LOCAL_CONFIG = self.dir / "local.conf"
        self.menu.ssh_ports = lambda: [22]
        self.menu.ssh_rate_setting = lambda: (0, [])
        self.menu.lan_allow_networks = lambda: []
        self.admin = "198.51.100.7"
        self.menu.admin_address = lambda: self.admin
        self.menu.TRUSTED.write_text("203.0.113.1\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self, *answers):
        queue = list(answers)
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return queue.pop(0)
        self.menu.input = fake_input
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.menu.configure_ssh_limit()
        self.assertEqual(queue, [], "остались неиспользованные ответы")
        return out.getvalue(), prompts

    def trusted_text(self):
        return self.menu.TRUSTED.read_text(encoding="utf-8")

    def saved(self):
        return self.menu.LOCAL_CONFIG.exists() and "SSH_RATE_LIMIT=20" in self.menu.LOCAL_CONFIG.read_text(encoding="utf-8")


class Question(Base):
    def test_trusted_admin_gets_no_question(self):
        self.admin = "203.0.113.1"
        output, prompts = self.drive("20", "да")
        self.assertIn("он в доверенных, лимит тебе не помешает", output)
        self.assertEqual(len(prompts), 2)  # число и сохранение: лишних вопросов нет
        self.assertTrue(self.saved())

    def test_admin_inside_trusted_subnet_is_fine(self):
        self.menu.TRUSTED.write_text("198.51.100.0/24\n", encoding="utf-8")
        output, prompts = self.drive("20", "да")
        self.assertIn("в доверенных", output)
        self.assertEqual(len(prompts), 2)

    def test_lan_allow_admin_is_fine(self):
        import ipaddress
        self.menu.lan_allow_networks = lambda: [ipaddress.ip_network("198.51.100.0/24")]
        output, prompts = self.drive("20", "да")
        self.assertIn("он в LAN_ALLOW", output)
        self.assertEqual(len(prompts), 2)

    def test_foreign_admin_yes_adds_to_trusted(self):
        output, _ = self.drive("20", "да", "да")
        self.assertIn("его нет в списке доверенных", output)
        self.assertIn("198.51.100.7\n", self.trusted_text())
        self.assertIn("203.0.113.1\n", self.trusted_text())
        self.assertTrue(self.saved())
        self.assertNotIn("Внимание: адрес 198.51.100.7, с которого ты работаешь", output)

    def test_foreign_admin_no_saves_but_warns_at_the_end(self):
        output, _ = self.drive("20", "нет", "да")
        self.assertNotIn("198.51.100.7", self.trusted_text())
        self.assertTrue(self.saved())
        self.assertIn("Внимание: адрес 198.51.100.7, с которого ты работаешь, не в доверенных", output)

    def test_enter_does_not_decide_the_question(self):
        output, prompts = self.drive("20", "", "что-то", "да", "да")
        self.assertIn("Ответ пустой, поэтому вопрос задан ещё раз", output)
        self.assertIn("Ответ «что-то» не понят, поэтому вопрос задан ещё раз", output)
        self.assertIn("198.51.100.7\n", self.trusted_text())

    def test_cancel_saves_nothing_and_adds_nothing(self):
        output, _ = self.drive("20", "отмена")
        self.assertFalse(self.menu.LOCAL_CONFIG.exists())
        self.assertNotIn("198.51.100.7", self.trusted_text())
        self.assertIn("Отменено", output)

    def test_unknown_admin_warns_and_still_saves(self):
        self.admin = ""
        output, prompts = self.drive("20", "да")
        self.assertIn("определить не удалось", output)
        self.assertIn("адрес этого подключения не определён", output)
        self.assertEqual(len(prompts), 2)
        self.assertTrue(self.saved())

    def test_turning_off_does_not_ask(self):
        self.menu.ssh_rate_setting = lambda: (20, [])
        self.menu.admin_address = lambda: self.fail("при выключении адрес не проверяется")
        self.drive("0", "да")

    def test_ipv6_admin_is_added_exactly_and_explained(self):
        self.admin = "2001:db8:1:2::5"
        output, _ = self.drive("20", "да", "да")
        self.assertIn("/64", output)
        self.assertIn("2001:db8:1:2::5\n", self.trusted_text())

    def test_ipv6_admin_not_matched_by_ipv4_entries(self):
        self.admin = "2001:db8:1:2::5"
        self.assertEqual(self.menu.admin_protection(self.admin), "")


class Mistyped(Base):
    def setUp(self):
        super().setUp()
        self.admin = "203.0.113.1"  # доверенный: вопроса про адрес нет, остаётся подтверждение сохранения

    def test_english_layout_is_recognized_and_the_question_is_repeated(self):
        output, prompts = self.drive("20", "lf", "да")
        self.assertTrue(self.saved())
        self.assertEqual(sum("Сохранить настройку" in prompt for prompt in prompts), 2)
        self.assertIn("Ответ «lf» не понят, поэтому вопрос задан ещё раз", output)
        self.assertIn("включена английская раскладка", output)
        self.assertIn("«lf» в ней то же, что «да»", output)

    def test_english_layout_for_no(self):
        output, _ = self.drive("20", "ytn", "нет")
        self.assertIn("то же, что «нет»", output)
        self.assertFalse(self.menu.LOCAL_CONFIG.exists())

    def test_other_garbage_is_repeated_without_the_layout_hint(self):
        output, prompts = self.drive("20", "может", "да")
        self.assertTrue(self.saved())
        self.assertIn("не понят, поэтому вопрос задан ещё раз", output)
        self.assertNotIn("английская", output)

    def test_enter_and_no_cancel_with_a_message(self):
        for answer in ("", "нет"):
            output, _ = self.drive("20", answer)
            self.assertIn("Отменено, настройка не изменена.", output)
            self.assertFalse(self.menu.LOCAL_CONFIG.exists())

    def test_layout_hint_in_the_admin_question_too(self):
        self.admin = "198.51.100.7"
        output, _ = self.drive("20", "lf", "да", "да")
        self.assertIn("включена английская раскладка", output)
        self.assertIn("198.51.100.7\n", self.trusted_text())


class StatusWarning(Base):
    def test_warns_only_when_exposed(self):
        self.assertIn("198.51.100.7", self.menu.ssh_admin_warning(20))
        self.assertIn("лимит SSH (20 в минуту)", self.menu.ssh_admin_warning(20))
        self.admin = "203.0.113.1"
        self.assertEqual(self.menu.ssh_admin_warning(20), "")
        self.admin = ""
        self.assertEqual(self.menu.ssh_admin_warning(20), "")


class Detection(unittest.TestCase):
    def setUp(self):
        self.menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(self.menu)

    def run_with(self, env, probe):
        self.menu.probe_output = probe
        with mock.patch.dict(os.environ, env, clear=False):
            if "SSH_CONNECTION" not in env:
                os.environ.pop("SSH_CONNECTION", None)
            return self.menu.admin_address()

    def test_ssh_connection_variable_is_first(self):
        found = self.run_with({"SSH_CONNECTION": "203.0.113.9 51514 10.0.0.1 22"}, lambda *a, **k: self.fail("лишний вызов"))
        self.assertEqual(found, "203.0.113.9")

    def test_who_is_used_when_sudo_dropped_the_variable(self):
        def probe(*command, **kwargs):
            return "root pts/0 2026-11-25 21:00 (203.0.113.9)\n" if command[0] == "who" else ""
        self.assertEqual(self.run_with({}, probe), "203.0.113.9")

    def test_sshd_socket_of_a_parent_process_is_the_last_resort(self):
        pid = os.getpid()

        def probe(*command, **kwargs):
            if command[0] == "ss":
                return f'0 0 10.0.0.1:22 203.0.113.9:51514 users:(("sshd",pid={pid},fd=4))\n'
            return ""
        with mock.patch.object(os, "getppid", return_value=pid):
            self.assertEqual(self.run_with({}, probe), "203.0.113.9")

    def test_unknown_when_nothing_is_found(self):
        self.assertEqual(self.run_with({}, lambda *a, **k: ""), "")

    def test_garbage_in_who_is_ignored(self):
        self.assertEqual(self.run_with({}, lambda *a, **k: "user tty1 (:0)\n"), "")

    def test_ssh_client_finds_address_after_sudo_dropped_the_variable(self):
        # ssh_client() используют «ssh» в добавлении доверенных, apply_rules, удаление и --dry.
        def probe(*command, **kwargs):
            return "root pts/0 2026-11-25 21:00 (203.0.113.9)\n" if command[0] == "who" else ""
        self.menu.probe_output = probe
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSH_CONNECTION", None)
            self.assertEqual(self.menu.ssh_client(), "203.0.113.9")

    def test_ssh_client_is_empty_without_any_ssh_session(self):
        self.menu.probe_output = lambda *a, **k: ""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSH_CONNECTION", None)
            self.assertEqual(self.menu.ssh_client(), "")

    def test_ssh_client_keeps_using_the_variable_first(self):
        self.menu.probe_output = lambda *a, **k: self.fail("лишний вызов")
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "203.0.113.9 51514 10.0.0.1 22"}):
            self.assertEqual(self.menu.ssh_client(), "203.0.113.9")


class Protection(unittest.TestCase):
    def test_lan_allow_parsing_reads_config(self):
        menu = importlib.util.module_from_spec(SPEC)
        LOADER.exec_module(menu)
        menu.probe_output = lambda *a, **k: "10.0.0.0/8, 192.168.1.0/24 мусор"
        found = [str(item) for item in menu.lan_allow_networks()]
        self.assertEqual(found, ["10.0.0.0/8", "192.168.1.0/24"])


if __name__ == "__main__":
    unittest.main()
