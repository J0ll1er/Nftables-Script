#!/usr/bin/env python3
"""Проверка пункта «Журнал ошибок» и меню fw-menu (запускать с python3 -I)."""
import builtins
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

if os.geteuid() != 0:
    sys.exit("нужен root: main() проверяет euid")

loader = importlib.machinery.SourceFileLoader("fwmenu", str(__import__("pathlib").Path(__file__).resolve().parent.parent / "build" / "fw-menu"))
spec = importlib.util.spec_from_loader("fwmenu", loader)
menu = importlib.util.module_from_spec(spec)
loader.exec_module(menu)

tmp = Path(tempfile.mkdtemp(prefix="fwmenu-test-"))
menu.DIRECTORY = tmp
menu.ERROR_LOG = tmp / "errors.log"
menu.TRUSTED = tmp / "trusted.txt"
menu.UPDATER = tmp / "update-fw.sh"
menu.ASN_FILE = tmp / "asn.txt"
menu.EXTRA = tmp / "extra.txt"
menu.PRESETS_FILE = tmp / "presets.conf"
menu.SOURCES = tmp / "sources.txt"
menu.LOCAL_CONFIG = tmp / "local.conf"


def run(func, answers):
    queue = list(answers)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return queue.pop(0)

    original = builtins.input
    builtins.input = fake_input
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            func()
    finally:
        builtins.input = original
    return out.getvalue(), prompts, queue


def check(condition, label):
    print(("OK   " if condition else "FAIL ") + label)
    if not condition:
        check.failed = True


check.failed = False

# 1. журнала нет
text, prompts, _ = run(menu.show_error_log, [])
check("пуст" in text and not prompts, "нет файла: «Журнал ошибок пуст», вопросов нет")

# 2. пустой файл
menu.ERROR_LOG.write_text("", encoding="utf-8")
text, prompts, _ = run(menu.show_error_log, [])
check("пуст" in text and not prompts, "пустой файл: «Журнал ошибок пуст»")

# 3. 40 записей: показываются последние 30, отказ от очистки не стирает
lines = [f"2026-11-11 10:{i:02d}:00 dry WARN: запись {i}" for i in range(40)]
menu.ERROR_LOG.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
text, prompts, _ = run(menu.show_error_log, ["нет"])
check("последние 30 из 40" in text, "показаны последние 30 из 40")
check("запись 39" in text and "запись 10" in text and "запись 9\n" not in text, "в выводе записи 10..39, без 0..9")
check(len(menu.ERROR_LOG.read_text(encoding="utf-8").splitlines()) == 40, "ответ «нет» оставляет журнал целым")

# 4. очистка по «да»
text, _, _ = run(menu.show_error_log, ["да"])
check(menu.ERROR_LOG.read_text(encoding="utf-8") == "", "ответ «да» очищает журнал")
check("Журнал очищен" in text, "сообщение «Журнал очищен»")

# 5. запись, появившаяся между показом и очисткой, не стирается
menu.ERROR_LOG.write_text("".join(line + "\n" for line in lines[:5]), encoding="utf-8")


def with_late_append():
    original = builtins.input

    def late(prompt=""):
        with menu.ERROR_LOG.open("a", encoding="utf-8") as handle:
            handle.write("2026-11-11 11:00:00 apply WARN: поздняя запись\n")
        return "да"

    builtins.input = late
    try:
        menu.show_error_log()
    finally:
        builtins.input = original


with redirect_stdout(io.StringIO()):
    with_late_append()
left = menu.ERROR_LOG.read_text(encoding="utf-8").splitlines()
check(left == ["2026-11-11 11:00:00 apply WARN: поздняя запись"], "поздняя запись пережила очистку")
check(oct(menu.ERROR_LOG.stat().st_mode & 0o777) == "0o600", "права журнала после очистки 0600")

# 6. меню 1.6.0: шесть пунктов, журнал и «что ещё» в подменю «Статус и наблюдение»
captured = {}
original_show = menu.show_menu


def spy(title, entries, exit_label="Назад"):
    captured["entries"] = entries


menu.show_menu = spy
with redirect_stdout(io.StringIO()):
    menu.main()
menu.show_menu = original_show
titles = [label for label, _ in captured["entries"]]
check(titles == ["Статус и наблюдение", "Настроить доступ", "Настроить блокировки",
                 "Проверить и применить правила", "Версии и обновление скрипта", "Отключить или удалить"],
      "шесть пунктов меню в новом порядке")
observe = dict(captured["entries"][0][1])
check(getattr(observe["Журнал ошибок"], "__wrapped__", observe["Журнал ошибок"]) is menu.show_error_log,
      "«Журнал ошибок» в подменю вызывает show_error_log")
check(getattr(observe["Что ещё есть на сервере"], "__wrapped__", observe["Что ещё есть на сервере"]) is menu.show_server_overview,
      "«Что ещё есть на сервере» в подменю вызывает show_server_overview")
check(isinstance(dict(captured["entries"])["Версии и обновление скрипта"], tuple), "«Версии и обновление скрипта» — подменю")

# 7. список файлов знает про журнал
menu.ERROR_LOG.unlink()
text, _, _ = run(menu.show_file_paths, [])
check("Журнал ошибок обновления" in text and "ошибок не было" in text, "«Показать пути к файлам»: журнал есть, «ошибок не было»")

print("ИТОГ:", "есть ошибки" if check.failed else "всё прошло")
sys.exit(1 if check.failed else 0)
