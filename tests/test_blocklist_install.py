"""Установщик: вопрос про blocklist.de при первой установке и запись выбора в local.conf."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "fw-install.sh").read_text(encoding="utf-8")
ASK = TEXT[TEXT.index("# --- вопрос про blocklist.de ---"):TEXT.index("# --- конец вопроса про blocklist.de ---")]
WRITE_START = TEXT.index('if [ "$blocklist_ssh" = нет ]; then')
WRITE = TEXT[WRITE_START:TEXT.index("install_seed\n", WRITE_START)]


def run_ask(answer, env_value=None):
    # ask заменён заглушкой: отдаёт ответ пользователя или значение по умолчанию.
    script = ('ask() { printf -v "$1" "%s" "${ANSWER:-$3}"; }\n' + ASK + 'echo "[$blocklist_ssh]"\n')
    env = {k: v for k, v in os.environ.items() if k != "FW_BLOCKLIST_SSH"}
    env["ANSWER"] = answer
    if env_value is not None:
        env["FW_BLOCKLIST_SSH"] = env_value
    return subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)


class Install(unittest.TestCase):
    def test_default_answer_is_yes(self):
        result = run_ask("")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[да]", result.stdout)
        self.assertIn("blocklist.de", result.stdout)
        self.assertIn("каждые 6 часов", result.stdout)

    def test_no_answer(self):
        self.assertIn("[нет]", run_ask("нет").stdout)

    def test_variable_skips_the_question(self):
        result = run_ask("", env_value="нет")
        self.assertIn("[нет]", result.stdout)
        self.assertNotIn("Включить блокировку", result.stdout)

    def test_english_answers_are_accepted(self):
        self.assertIn("[да]", run_ask("yes").stdout)
        self.assertIn("[нет]", run_ask("n").stdout)

    def test_garbage_stops_before_anything_is_installed(self):
        result = run_ask("может быть")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Ничего не установлено", result.stderr)

    def run_write(self, choice, existing=""):
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "local.conf"
            if existing:
                conf.write_text(existing)
            script = f'FW={directory}; blocklist_ssh={choice}\n{WRITE}'
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return conf.read_text() if conf.exists() else "", result.stdout

    def test_no_writes_managed_block_menu_understands(self):
        conf, out = self.run_write("нет")
        self.assertEqual(conf, "# BEGIN FW-MENU BLOCKLIST\nBLOCKLIST_SSH=0\n# END FW-MENU BLOCKLIST\n")
        self.assertIn("fw-menu → 2 → 7", out)

    def test_no_keeps_existing_panel_block(self):
        panel = "# BEGIN FW-MENU API\nAPI_PORTS='2222'\n# END FW-MENU API\n"
        conf, _ = self.run_write("нет", panel)
        self.assertTrue(conf.startswith(panel))
        self.assertIn("BLOCKLIST_SSH=0", conf)

    def test_yes_writes_nothing(self):
        conf, out = self.run_write("да")
        self.assertEqual(conf, "")
        self.assertIn("включён", out)


if __name__ == "__main__":
    unittest.main()


class FreshInstallChain(unittest.TestCase):
    def body(self):
        text = (ROOT / "build" / "fw-menu").read_text(encoding="utf-8")
        start = text.index("def apply_rules():")
        return text[start:text.index("\ndef remove_rules", start)]

    def test_chain_starts_after_the_timer_is_enabled_and_the_apply_lock_is_free(self):
        # На чистой установке первый update-fw.sh идёт до включения таймера и цепочку не ставит. Запускать
        # --blocklist прямо в apply_rules нельзя: пока меню держит .apply.lock, updater отвечает ошибкой
        # «ручное применение ещё не подтверждено» и пишет её в журнал и в last-result. Поэтому разовый запуск через systemd-run.
        body = self.body()
        enabled = body.index('"enable", "--now", "fw-update.timer"')
        started = body.index('"--on-active=1min"')
        self.assertGreater(started, enabled)
        self.assertLess(started, body.index("reconcile_reinstall_guard()"))
        self.assertNotIn('subprocess.run([str(UPDATER), "--blocklist"]', body)

    def test_old_chain_is_stopped_before_the_first_run_is_armed(self):
        body = self.body()
        self.assertLess(body.index('"stop", "fw-blocklist-*.timer"'), body.index('"--on-active=1min"'))
