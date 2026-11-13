#!/usr/bin/env python3
"""Собирает новый fw-install.sh из orig-fw-install.sh: правки update-fw.sh и fw-menu,
пересчёт трёх контрольных сумм."""
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ORIG = ROOT / "orig-fw-install.sh"
OUT = ROOT / "fw-install.sh"
BUILD = ROOT / "build"

text = ORIG.read_bytes().decode("utf-8")


def heredoc(source, name):
    start_line = f"<<'{name}'\n"
    first = source.index(start_line)
    body_start = first + len(start_line)
    end = source.index(f"\n{name}\n", body_start - 1) + 1
    return body_start, end


def sha(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def replace_once(source, old, new, label):
    count = source.count(old)
    if count != 1:
        sys.exit(f"{label}: ожидалось одно совпадение, найдено {count}")
    return source.replace(old, new)


parts = {}
for key, marker in (("update", "FW_UPDATER_FILE"), ("menu", "FW_MENU_FILE"), ("seed", "FW_SEED_FILE")):
    a, b = heredoc(text, marker)
    parts[key] = (a, b, text[a:b])

old_hashes = {}
checks = re.search(
    r"printf '%s  %s\\n%s  %s\\n%s  %s\\n' \\\n\s+'([0-9a-f]{64})' \"\$stage/update-fw.sh\" \\\n"
    r"\s+'([0-9a-f]{64})' \"\$stage/fw-menu\" \\\n\s+'([0-9a-f]{64})' \"\$stage/seed.txt\"",
    text,
)
if not checks:
    sys.exit("не нашёл строку с контрольными суммами")
for key, value in zip(("update", "menu", "seed"), checks.groups()):
    old_hashes[key] = value
    if sha(parts[key][2]) != value:
        sys.exit(f"исходная контрольная сумма {key} не сходится: проверь границы heredoc")
print("исходные суммы сходятся")

# ------------------------------------------------------------ update-fw.sh
upd = parts["update"][2]

upd = replace_once(
    upd,
    'fail_keep() { echo "ERR: $1 — правила не тронуты" >&2; exit 1; }\n'
    'fail_save() { echo "ERR: правила применены, но $1" >&2; exit 1; }\n',
    'MODE=apply; [ "$DRY" = 1 ] && MODE=dry\n'
    'ERRLOG=$DIR/errors.log\n'
    '# note "WARN: …" или "ERR: …": в stderr и в журнал ошибок (fw-menu → «Журнал ошибок»).\n'
    'logline() { printf \'%s %s %s\\n\' "$(date \'+%F %T\')" "$MODE" "$1" >> "$ERRLOG" 2>/dev/null || true; }\n'
    'note() { echo "$1" >&2; logline "$1"; }\n'
    'fail_keep() { note "ERR: $1 — правила не тронуты"; exit 1; }\n'
    'fail_save() { note "ERR: правила применены, но $1"; exit 1; }\n',
    "note()",
)

upd = replace_once(
    upd,
    'flock -x 9 || fail_keep "не удалось заблокировать обновление"\n',
    'flock -x 9 || fail_keep "не удалось заблокировать обновление"\n'
    '# Журнал ошибок: создаётся закрытым, хранит последние ~500 записей.\n'
    '[ -e "$ERRLOG" ] || ( umask 077; : > "$ERRLOG" ) 2>/dev/null\n'
    'if [ -f "$ERRLOG" ] && [ "$(wc -l < "$ERRLOG")" -gt 500 ]; then\n'
    '  ( umask 077; tail -n 300 "$ERRLOG" > "$ERRLOG.new" ) && mv -f "$ERRLOG.new" "$ERRLOG"\n'
    'fi\n',
    "ротация журнала",
)

old_head = '''fetch_asn() {
  local asn=$1 destination=$2 number=${1#AS} reason base
  if curl -fsS --proto '=https' --retry 2 --retry-delay 1 --max-time 20 "https://stat.ripe.net/data/announced-prefixes/data.json?resource=$asn" > "$TMP/asn.json"; then
    if reason=$(asn_prefixes "$asn" "$TMP/asn.json" "$destination" 2>&1); then
      return 0
    fi
    rm -f -- "$destination"
    echo "WARN: ответ RIPEstat по $asn отклонён: $reason" >&2
  else
    echo "WARN: RIPEstat не ответил по $asn" >&2
  fi
  for base in \\
'''
new_head = '''RIPE_DOWN=0; SRC_RIPE=0; SRC_IPVERSE=0
fetch_asn() {
  local asn=$1 destination=$2 number=${1#AS} reason base
  # Если RIPEstat не ответил, остальные ASN этого запуска сразу идут в запасной источник.
  if [ "$RIPE_DOWN" = 0 ]; then
    if curl -fsS --proto '=https' --connect-timeout 4 --max-time 10 --retry 1 --retry-delay 1 "https://stat.ripe.net/data/announced-prefixes/data.json?resource=$asn" > "$TMP/asn.json"; then
      if reason=$(asn_prefixes "$asn" "$TMP/asn.json" "$destination" 2>&1); then
        SRC_RIPE=$((SRC_RIPE+1))
        return 0
      fi
      rm -f -- "$destination"
      note "WARN: ответ RIPEstat по $asn отклонён: $reason"
    else
      RIPE_DOWN=1
      note "WARN: RIPEstat недоступен, перехожу на резервный источник (ipverse)"
    fi
  fi
  for base in \\
'''
upd = replace_once(upd, old_head, new_head, "fetch_asn")

upd = replace_once(
    upd,
    '      echo "Запасной источник для $asn: ipverse"\n      return 0\n',
    '      SRC_IPVERSE=$((SRC_IPVERSE+1))\n      echo "Запасной источник для $asn: ipverse"\n      return 0\n',
    "счётчик ipverse",
)

upd = replace_once(upd, "asn_total=0; asn_cached=0\n", "asn_total=0; asn_cached=0; asn_missing=0\n", "asn_missing init")
upd = replace_once(
    upd,
    '    missing=$((missing+1))\n    echo "WARN: нет данных по $asn и кэша нет — в этот раз без него" >&2\n',
    '    missing=$((missing+1)); asn_missing=$((asn_missing+1))\n    echo "WARN: нет данных по $asn и кэша нет — в этот раз без него" >&2\n',
    "asn_missing счётчик",
)

ASN_SUMMARY = r'''if [ "$asn_total" -gt 0 ]; then
  # Итог по ASN: что получено и откуда. Всё, кроме «все от RIPEstat», попадает и в журнал ошибок.
  asn_fresh=$((SRC_RIPE+SRC_IPVERSE)); asn_warn=1
  if [ "$SRC_RIPE" -eq "$asn_total" ]; then
    asn_line="ASN: все $asn_total получены от RIPEstat"; asn_warn=0
  elif [ "$RIPE_DOWN" = 1 ] && [ "$SRC_IPVERSE" -eq "$asn_total" ]; then
    asn_line="ASN: RIPEstat недоступен — все $asn_total получены из резервного источника (ipverse)"
  elif [ "$RIPE_DOWN" = 1 ] && [ "$asn_fresh" -eq 0 ] && [ "$asn_cached" -eq "$asn_total" ]; then
    asn_line="ASN: RIPEstat и ipverse недоступны — взято из сохранённого кэша ($asn_cached), данные могут быть устаревшими"
  elif [ "$RIPE_DOWN" = 1 ] && [ "$asn_fresh" -eq 0 ] && [ "$asn_missing" -eq "$asn_total" ]; then
    asn_line="ASN: ни RIPEstat, ни ipverse не ответили, кэша нет — подсети этих ASN в правила не вошли"
  elif [ "$RIPE_DOWN" = 1 ]; then
    asn_line="ASN: RIPEstat недоступен — ipverse $SRC_IPVERSE, кэш $asn_cached, не получено $asn_missing"
  else
    asn_line="ASN: RIPEstat $SRC_RIPE, ipverse $SRC_IPVERSE, кэш $asn_cached, не получено $asn_missing"
  fi
  echo "$asn_line"
  if [ "$asn_warn" = 1 ]; then logline "WARN: $asn_line"; fi
fi
cat "$DIR/extra.txt" >> "$TMP/scan_raw" 2>/dev/null
'''
upd = replace_once(upd, 'cat "$DIR/extra.txt" >> "$TMP/scan_raw" 2>/dev/null\n', ASN_SUMMARY, "строка статуса ASN")

# Настройки, общие для новых и уже установленных экземпляров.
LAN_DEFAULT = ""
upd = replace_once(upd, 'API_PORTS="${API_PORTS:-}"; API_ALLOW="${API_ALLOW:-}"\n',
    'API_PORTS="${API_PORTS:-}"; API_ALLOW="${API_ALLOW:-}"\n'
    'LAN_ALLOW="${LAN_ALLOW-}"\n'
    'MAX_GROWTH="${MAX_GROWTH:-3}"; MIN_KEEP="${MIN_KEEP:-0.2}"; MAX_CACHE_DAYS="${MAX_CACHE_DAYS:-14}"\n', "параметры списков")
upd = replace_once(upd, 'TMP=$(mktemp -d) || fail_keep "не удалось создать временный каталог"\n',
    '''python3 - "$MAX_GROWTH" "$MIN_KEEP" "$MAX_CACHE_DAYS" <<'PY' || fail_keep "неверные лимиты списков"
import math
import sys
try:
    growth, keep = map(float, sys.argv[1:3])
    days = sys.argv[3]
    assert math.isfinite(growth) and math.isfinite(keep)
    assert growth >= 1 and 0 <= keep <= 1 and days.isdigit()
except (ValueError, AssertionError):
    sys.exit(1)
PY
TMP=$(mktemp -d) || fail_keep "не удалось создать временный каталог"
''', "валидация лимитов")
upd = replace_once(upd, 'DRY=0; [ "${1:-}" = "--dry" ] && DRY=1\n',
    'DRY=0; [ "${1:-}" = "--dry" ] && DRY=1\n'
    'PENDING=${FW_PENDING:-0}; [ "$PENDING" = 0 ] || [ "$PENDING" = 1 ] || exit 2\n', "режим pending")
upd = replace_once(upd, 'flock -x 9 || fail_keep "не удалось заблокировать обновление"\n',
    'flock -x 9 || fail_keep "не удалось заблокировать обновление"\n'
    '# Проверка под lock каталога; перед fw-menu отпускаем его: меню может\n'
    '# держать .apply.lock и ждать каталог при сборке pending.\n'
    'if [ "$PENDING" = 0 ] && [ "$DRY" = 0 ]; then\n'
    '  while :; do\n'
    '    rollback_state=\n'
    '    for state in "$DIR"/.rollback-*; do\n'
    '      if [ -d "$state" ]; then rollback_state=$state; break; fi\n'
    '    done\n'
    '    [ -n "$rollback_state" ] || break\n'
    '    token=${rollback_state##*/.rollback-}\n'
    '    note "WARN: найдено неподтверждённое применение, откатываю"\n'
    '    flock -u 9 || fail_keep "не удалось освободить блокировку обновления"\n'
    '    /usr/local/sbin/fw-menu --rollback "$token" || fail_keep "не удалось откатить применение"\n'
    '    flock -x 9 || fail_keep "не удалось заблокировать обновление"\n'
    '  done\n'
    'fi\n'
    '# Таймерный запуск не меняет правила, пока меню ждёт подтверждения.\n'
    'if [ "$PENDING" = 0 ] && [ "$DRY" = 0 ]; then\n'
    '  exec 8>"$DIR/.apply.lock" || fail_keep "не открылся lock применения"\n'
    '  flock -n -x 8 || fail_keep "ручное применение ещё не подтверждено"\n'
    'fi\n', "блокировка таймера")
upd = replace_once(upd, '# asn_prefixes ASN ОТВЕТ_RIPESTAT НАЗНАЧЕНИЕ\n',
    '''# Отбирает только актуальный кэш и сравнивает размеры списков по числу строк.
cache_fresh() {
  [ -s "$1" ] || return 1
  if ! python3 - "$1" "$MAX_CACHE_DAYS" <<'PY'
from pathlib import Path
import sys, time
path = Path(sys.argv[1])
sys.exit(0 if time.time() - path.stat().st_mtime <= int(sys.argv[2]) * 86400 else 1)
PY
  then
    note "WARN: кэш ${1##*/} старше $MAX_CACHE_DAYS дней, пропускаю"
    return 1
  fi
}
growth_ok() {
  local fresh=$1 cache=$2 label=$3
  cache_fresh "$cache" || return 0
  if ! python3 - "$fresh" "$cache" "$MAX_GROWTH" "$MIN_KEEP" <<'PY'
from pathlib import Path
import sys
import ipaddress
def sizes(path):
    networks = [ipaddress.ip_network(line.partition('#')[0].strip(), strict=False)
                for line in Path(path).read_text().splitlines() if line.partition('#')[0].strip()]
    return (len(networks),
            sum(network.num_addresses for network in networks if network.version == 4),
            sum(max(1, network.num_addresses // (1 << 64)) for network in networks if network.version == 6))
new, old = (sizes(p) for p in sys.argv[1:3])
growth, keep = float(sys.argv[3]), float(sys.argv[4])
sys.exit(0 if all(not previous or keep * previous <= current <= growth * previous
                  for current, previous in zip(new, old)) else 1)
PY
  then
    rm -f -- "$fresh"
    note "WARN: аномальный размер списка $label, использую кэш"
    return 1
  fi
}

# asn_prefixes ASN ОТВЕТ_RIPESTAT НАЗНАЧЕНИЕ
''', "лимиты кэша и роста")
upd = replace_once(upd, 'if fetch_scanner_file "$spec" "$fresh"; then\n',
    'if fetch_scanner_file "$spec" "$fresh" && growth_ok "$fresh" "$SCAN_CACHE/$name" "$label"; then\n', "рост списка")
upd = replace_once(upd, 'elif [ -s "$SCAN_CACHE/$name" ]; then\n',
    'elif cache_fresh "$SCAN_CACHE/$name"; then\n', "срок кэша сканеров")
upd = replace_once(upd, 'elif [ -s "$asn_cache" ]; then\n',
    'elif cache_fresh "$asn_cache"; then\n', "срок кэша ASN")
upd = replace_once(upd, 'normalize_scanners "$TMP/scan_raw" "$TMP/scan_normalized" || fail_keep "список сканеров некорректен"\n',
    '''normalize_scanners "$TMP/scan_raw" "$TMP/scan_normalized" || fail_keep "список сканеров некорректен"
# Исключить из внешних блокировок сети, которые затрагивают доверенные или LAN.
printf '%s\\n' "$LAN_ALLOW" | tr ' ,' '\\n\\n' > "$TMP/lan_raw"
{ cat "$TMP/lan_raw"; cat "$DIR/trusted.txt" "$DIR/trusted.local.txt" 2>/dev/null; } > "$TMP/protected_raw"
if ! removed=$(python3 - "$TMP/scan_normalized" "$TMP/protected_raw" <<'PY'
import ipaddress
from pathlib import Path
import sys
blocked, protected = map(Path, sys.argv[1:])
try:
    allowed = [ipaddress.ip_network(value) for line in protected.read_text().splitlines()
               if (value := line.partition('#')[0].strip())]
    lines = blocked.read_text().splitlines()
    kept = [line for line in lines if not any(
        (network := ipaddress.ip_network(line)).version == safe.version and network.overlaps(safe)
        for safe in allowed)]
    blocked.write_text(''.join(line + '\\n' for line in kept))
    print(len(lines) - len(kept))
except (OSError, ValueError) as error:
    sys.exit(str(error))
PY
); then fail_keep "не удалось проверить пересечения списков"; fi
[ "$removed" -eq 0 ] || note "WARN: из списков сканеров удалено пересечений с trusted/LAN_ALLOW: $removed"
''', "защита trusted и LAN")
upd = replace_once(upd, '# 2. Сканеры: списки + ASN + extra\n',
    '''# Проверяем число RU v4 относительно последнего успешного применения.
if [ -s "$DIR/geo.count" ]; then
  previous=$(cat "$DIR/geo.count")
  [[ "$previous" =~ ^[0-9]+$ ]] && [ "$previous" -gt 0 ] || fail_keep "geo.count повреждён"
  python3 - "$n" "$previous" "$MAX_GROWTH" "$MIN_KEEP" <<'PY' || fail_keep "аномальное изменение числа RU IPv4 в геобазе ($previous → $n)"
import sys
current, previous, growth, keep = map(float, sys.argv[1:])
sys.exit(0 if keep * previous <= current <= growth * previous else 1)
PY
fi

# 2. Сканеры: списки + ASN + extra
''', "рост геобазы")
upd = replace_once(upd, '  set_def tr4 ipv4_addr "$TMP/tr4"; set_def tr6 ipv6_addr "$TMP/tr6"\n',
    '  set_def tr4 ipv4_addr "$TMP/tr4"; set_def tr6 ipv6_addr "$TMP/tr6"\n'
    '  split "$TMP/lan_raw" "$TMP/lan4" "$TMP/lan6"\n'
    '  set_def lan4 ipv4_addr "$TMP/lan4"; set_def lan6 ipv6_addr "$TMP/lan6"\n', "набор LAN")
upd = replace_once(upd, '  echo "    ip6 saddr @tr6 accept"\n',
    '  echo "    ip6 saddr @tr6 accept"\n'
    '  echo "    ip saddr @lan4 accept comment \\"LAN_ALLOW\\""\n'
    '  echo "    ip6 saddr @lan6 accept comment \\"LAN_ALLOW\\""\n', "правила LAN")
upd = replace_once(upd, 'cp "$TMP/new.nft" "$CACHE" || fail_save "не сохранилась копия правил"\n',
    '''staged_rules=$(mktemp "$DIR/.fw.nft.XXXXXXXX") || fail_save "не создался временный файл правил"
target=$CACHE; count_target=$DIR/geo.count
if [ "$PENDING" = 1 ]; then target=$DIR/fw.nft.pending; count_target=$DIR/geo.count.pending; fi
if ! cat "$TMP/new.nft" > "$staged_rules" || ! sync "$staged_rules" || ! mv -f "$staged_rules" "$target"; then
  rm -f -- "$staged_rules"
  fail_save "не сохранилась копия правил"
fi
printf '%s\\n' "$n" > "$count_target" || fail_save "не сохранилось число RU IPv4"
''', "атомарный снимок")

# Итоговая строка запуска: ВСЁ В ПОРЯДКЕ (зелёная) / ЕСТЬ ЗАМЕЧАНИЯ (жёлтая) / ОШИБКА (красная).
upd = replace_once(upd, 'note() { echo "$1" >&2; logline "$1"; }\n'
    'fail_keep() { note "ERR: $1 — правила не тронуты"; exit 1; }\n'
    'fail_save() { note "ERR: правила применены, но $1"; exit 1; }\n',
    r'''WARN_LOG=$(mktemp) || WARN_LOG=/dev/null
trap 'rm -f "$WARN_LOG"' EXIT
note() {
  echo "$1" >&2; logline "$1"
  case "$1" in WARN:*) echo "${1#WARN: }" >> "$WARN_LOG" 2>/dev/null || true ;; esac
}
# paint КОД ТЕКСТ [ПОТОК]: цвет только в терминале (не в журнале systemd) и без NO_COLOR.
paint() {
  if [ -t "${3:-1}" ] && [ -z "${NO_COLOR:-}" ]; then printf '\033[%sm%s\033[0m\n' "$1" "$2"
  else printf '%s\n' "$2"; fi
}
record_result() { [ "$DRY" = 1 ] || printf '%s\n' "$1" > "$DIR/last-result" 2>/dev/null || true; }
fail_keep() {
  note "ERR: $1 — правила не тронуты"; record_result error
  if [ "$DRY" = 1 ]; then paint 31 "ИТОГ: ОШИБКА — проверка не прошла, правила не менялись. Причина: $1" 2 >&2
  else paint 31 "ИТОГ: ОШИБКА — правила не применены, действуют прежние. Причина: $1" 2 >&2; fi
  exit 1
}
fail_save() {
  note "ERR: правила применены, но $1"; record_result error
  paint 31 "ИТОГ: ОШИБКА — правила применены, но $1; после перезагрузки они могут не подняться" 2 >&2
  exit 1
}
verdict() {
  local what=$1 count=0
  [ -s "$WARN_LOG" ] && count=$(wc -l < "$WARN_LOG")
  if [ "$count" -eq 0 ]; then
    paint 32 "ИТОГ: ВСЁ В ПОРЯДКЕ — $what, все источники ответили"
    record_result ok
  else
    paint 33 "ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ — $what, но:"
    head -n 6 "$WARN_LOG" | sed 's/^/  • /'
    [ "$count" -le 6 ] || echo "  • … и ещё $((count-6)) (см. fw-menu → «Журнал ошибок»)"
    record_result warn
  fi
}
''', "итог запуска")
upd = replace_once(upd, "trap 'rm -rf \"$TMP\"' EXIT\n", "trap 'rm -rf \"$TMP\"; rm -f \"$WARN_LOG\"' EXIT\n", "trap TMP и WARN_LOG")
upd = replace_once(upd, 'if [ "$DRY" = 1 ]; then echo "DRY: синтаксис ок, ничего не применено"; exit 0; fi\n',
    'if [ "$DRY" = 1 ]; then echo "DRY: синтаксис ок, ничего не применено"; verdict "проверка прошла, правила не менялись"; exit 0; fi\n', "итог dry")
upd = replace_once(upd, 'echo "OK: применено"\n',
    'echo "OK: применено"\n'
    'if [ "$PENDING" = 1 ]; then verdict "правила загружены и ждут подтверждения"; else verdict "правила применены"; fi\n', "итог применения")
# тихий curl: сбой отдельной попытки не печатаем, итог и резервные источники сообщают сами
upd = upd.replace("curl -fsSL ", "curl -fsL ").replace("curl -fsS ", "curl -fs ")
upd = upd.replace('note "WARN: из списков сканеров удалено пересечений', 'note "INFO: из списков сканеров удалено пересечений')

# Слишком широкая сеть: пропускается только она сама, а не весь список; решение — в fw-menu (wide-ok.txt).
WIDE_LOAD = """approved = set()
okfile = os.environ.get("FW_WIDE_OK")
report_path = os.environ.get("FW_WIDE_REPORT")
label = os.environ.get("FW_WIDE_LABEL", "")
skipped = []
if okfile:
    try:
        for raw_ok in Path(okfile).read_text(encoding="utf-8").splitlines():
            text_ok = raw_ok.partition("#")[0].strip()
            if text_ok:
                try:
                    approved.add(ipaddress.ip_network(text_ok))
                except ValueError:
                    pass
    except OSError:
        pass
"""
WIDE_SAVE = """        with open(report_path, "a", encoding="utf-8") as report_file:
            for item in skipped:
                report_file.write(f"{label}\\t{item}\\n")
"""
upd = replace_once(upd, 'import ipaddress\nfrom pathlib import Path\nimport sys\n\nsource, destination = map(Path, sys.argv[1:3])\nminimum = {4: int(sys.argv[3]), 6: int(sys.argv[4])}\nby_version = {4: [], 6: []}\n',
    'import ipaddress\nimport os\nfrom pathlib import Path\nimport sys\n\nsource, destination = map(Path, sys.argv[1:3])\nminimum = {4: int(sys.argv[3]), 6: int(sys.argv[4])}\nby_version = {4: [], 6: []}\n' + WIDE_LOAD, "широкие сети: загрузка (normalize)")
upd = replace_once(upd, '''        network = ipaddress.ip_network(value)
        if network.prefixlen < minimum[network.version]:
            raise ValueError(f"слишком широкая сеть {network} (порог /{minimum[network.version]})")
        by_version[network.version].append(network)
''', '''        network = ipaddress.ip_network(value)
        if network.prefixlen < minimum[network.version] and network not in approved:
            if not report_path:
                raise ValueError(f"слишком широкая сеть {network} (порог /{minimum[network.version]})")
            skipped.append(network)
            continue
        by_version[network.version].append(network)
''', "широкие сети: пропуск (normalize)")
upd = replace_once(upd, '''    destination.write_text("".join(line + "\\n" for line in lines), encoding="utf-8")
except (OSError, ValueError) as error:
    sys.exit(str(error))
''', '''    destination.write_text("".join(line + "\\n" for line in lines), encoding="utf-8")
    if skipped and report_path:
''' + WIDE_SAVE + '''except (OSError, ValueError) as error:
    sys.exit(str(error))
''', "широкие сети: отчёт (normalize)")
upd = replace_once(upd, 'import ipaddress\nimport json\nfrom pathlib import Path\nimport sys\n\nasn, source, destination = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])\nminimum = {4: int(sys.argv[4]), 6: int(sys.argv[5])}\n',
    'import ipaddress\nimport json\nimport os\nfrom pathlib import Path\nimport sys\n\nasn, source, destination = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])\nminimum = {4: int(sys.argv[4]), 6: int(sys.argv[5])}\n' + WIDE_LOAD + 'label = asn\n', "широкие сети: загрузка (ASN)")
upd = replace_once(upd, '''        network = ipaddress.ip_network(item["prefix"])
        if network.prefixlen < minimum[network.version]:
            raise ValueError(f"слишком широкая сеть {network} (порог /{minimum[network.version]})")
        lines.append(str(network))
''', '''        network = ipaddress.ip_network(item["prefix"])
        if network.prefixlen < minimum[network.version] and network not in approved:
            if not report_path:
                raise ValueError(f"слишком широкая сеть {network} (порог /{minimum[network.version]})")
            skipped.append(network)
            continue
        lines.append(str(network))
''', "широкие сети: пропуск (ASN)")
upd = replace_once(upd, '''    destination.write_text("".join(line + "\\n" for line in sorted(set(lines))), encoding="utf-8")
except (OSError, AttributeError, KeyError, TypeError, ValueError) as error:''', '''    destination.write_text("".join(line + "\\n" for line in sorted(set(lines))), encoding="utf-8")
    if skipped and report_path:
''' + WIDE_SAVE + '''except (OSError, AttributeError, KeyError, TypeError, ValueError) as error:''', "широкие сети: отчёт (ASN)")
upd = replace_once(upd, 'TMP=$(mktemp -d) || fail_keep "не удалось создать временный каталог"\n',
    'TMP=$(mktemp -d) || fail_keep "не удалось создать временный каталог"\n'
    ': > "$TMP/wide"\n'
    'export FW_WIDE_REPORT="$TMP/wide" FW_WIDE_OK="$DIR/wide-ok.txt"\n', "отчёт о широких сетях")
upd = replace_once(upd, 'fetch_scanner_file() {\n  local spec=$1 destination=$2 url reason\n',
    'fetch_scanner_file() {\n  local spec=$1 destination=$2 url reason\n  export FW_WIDE_LABEL="${spec##*/}"\n', "метка источника")
upd = replace_once(upd, 'fetch_asn() {\n  local asn=$1 destination=$2 number=${1#AS} reason base\n',
    'fetch_asn() {\n  local asn=$1 destination=$2 number=${1#AS} reason base\n  export FW_WIDE_LABEL="$asn"\n', "метка ASN")
upd = replace_once(upd, '# normalize_scanners ИСТОЧНИК НАЗНАЧЕНИЕ [МИН_ПРЕФИКС_V4 МИН_ПРЕФИКС_V6]\n',
    r"""# wide_finish: итог по слишком широким сетям. Они пропущены и не блокируются, пока их не подтвердят в fw-menu.
wide_finish() {
  local count
  if [ -s "$TMP/wide" ]; then
    sort -u "$TMP/wide" > "$TMP/wide.sorted"
    count=$(wc -l < "$TMP/wide.sorted")
    if cp "$TMP/wide.sorted" "$DIR/wide-pending.txt.new" && mv -f "$DIR/wide-pending.txt.new" "$DIR/wide-pending.txt"; then :; else
      logline "WARN: не записался wide-pending.txt"
    fi
    note "WARN: пропущено слишком широких сетей: $count — они не блокируются, пока ты их не подтвердишь (fw-menu → «Проверить и применить правила»)"
  else
    rm -f "$DIR/wide-pending.txt"
  fi
}

# normalize_scanners ИСТОЧНИК НАЗНАЧЕНИЕ [МИН_ПРЕФИКС_V4 МИН_ПРЕФИКС_V6]
""", "функция wide_finish")
upd = replace_once(upd, 'if [ "$DRY" = 1 ]; then echo "DRY: синтаксис ок, ничего не применено"',
    'wide_finish\nif [ "$DRY" = 1 ]; then echo "DRY: синтаксис ок, ничего не применено"', "вызов wide_finish")

upd, replaced = re.subn(r'echo ("WARN: .*?") >&2', r"note \1", upd)
print(f"заменено echo WARN → note: {replaced}")
leftover = [line for line in upd.splitlines() if ">&2" in line]
# допустимо только внутри note()
unexpected = [line for line in leftover if 'echo "$1" >&2' not in line and 'paint ' not in line]
if unexpected:
    sys.exit("остались прямые записи в stderr:\n" + "\n".join(unexpected))

# ----------------------------------------------------------------- fw-menu
menu = parts["menu"][2]

menu = replace_once(
    menu,
    'SOURCES = DIRECTORY / "sources.txt"\n',
    'SOURCES = DIRECTORY / "sources.txt"\nERROR_LOG = DIRECTORY / "errors.log"\n',
    "ERROR_LOG",
)
menu = replace_once(menu, 'TIMER_FILE = Path("/etc/systemd/system/fw-update.timer")\n',
    'TIMER_FILE = Path("/etc/systemd/system/fw-update.timer")\n'
    'NFT_DROPIN = Path("/etc/systemd/system/nftables.service.d/10-fw-restore.conf")\n', "drop-in меню")
menu = replace_once(menu, 'SCHEDULE_FILE = TIMER_FILE.with_name(TIMER_FILE.name + ".d") / "10-fw-menu-schedule.conf"\n',
    'SCHEDULE_FILE = TIMER_FILE.with_name(TIMER_FILE.name + ".d") / "10-fw-menu-schedule.conf"\n'
    'SETTING_FILES = ("fw.conf", "local.conf", "extra.txt", "trusted.txt", "trusted.local.txt",\n'
    '                 "asn.txt", "sources.txt", "presets.conf")\n', "список настроек")
menu = replace_once(menu, '    installed_files = [MENU_FILE, SERVICE_FILE, TIMER_FILE]\n',
    '    installed_files = [MENU_FILE, SERVICE_FILE, TIMER_FILE]\n'
    '    if NFT_DROPIN.is_file():\n        installed_files.append(NFT_DROPIN)\n', "удаление drop-in")
menu = replace_once(menu, '        if path == SCHEDULE_FILE:\n',
    '        if path in (SCHEDULE_FILE, NFT_DROPIN):\n', "восстановление drop-in")

show_log = '''def show_error_log():
    try:
        lines = ERROR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    if not lines:
        print("Журнал ошибок пуст.")
        return
    shown = lines[-30:]
    print(f"Журнал ошибок {ERROR_LOG}: показаны последние {len(shown)} из {len(lines)}")
    print("Формат: дата время режим сообщение. Режим dry — пробный запуск, apply — применение.")
    for line in shown:
        print(f"  {line}")
    if input("Очистить журнал? Введи «да»: ").strip().casefold() != "да":
        return
    fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            current = ERROR_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            current = []
        # Записи, появившиеся после показа, не стираются.
        rest = current[len(lines):] if current[:len(lines)] == lines else current
        write_text_atomic(ERROR_LOG, "".join(line + "\\n" for line in rest))
    finally:
        os.close(fd)
    print("Журнал очищен.")


'''
menu = replace_once(menu, "def show_menu(title, entries, exit_label=\"Назад\"):\n",
                    show_log + "def show_menu(title, entries, exit_label=\"Назад\"):\n", "show_error_log")

menu = replace_once(
    menu,
    '            ("Удалить скрипт и настройки", uninstall),\n        )),\n    )\n',
    '            ("Удалить скрипт и настройки", uninstall),\n        )),\n        ("Журнал ошибок", show_error_log),\n    )\n',
    "пункт меню",
)
menu = replace_once(
    menu,
    '            ("Сохранённые правила nftables", DIRECTORY / "fw.nft"),\n',
    '            ("Сохранённые правила nftables", DIRECTORY / "fw.nft"),\n'
    '            ("Журнал ошибок обновления", ERROR_LOG),\n',
    "инвентарь файлов",
)
menu = replace_once(
    menu,
    '        SOURCES: "свои списки не добавлены",\n',
    '        SOURCES: "свои списки не добавлены",\n        ERROR_LOG: "ошибок не было",\n',
    "optional",
)

EXPLAIN = r'''def port_values(value):
    if isinstance(value, int):
        return [str(value)]
    if isinstance(value, dict) and "set" in value:
        return [port for item in value["set"] for port in port_values(item)]
    if isinstance(value, dict) and "range" in value:
        return [f"{value['range'][0]}-{value['range'][1]}"]
    raise ValueError("неожиданный формат порта")


def rule_ports(expressions):
    ports = []
    for expression in expressions:
        match = expression.get("match") or {}
        payload = (match.get("left") or {}).get("payload") or {}
        if payload.get("field") == "dport":
            ports.extend(port_values(match["right"]))
    return ports


def rule_protocols(expressions):
    names = set()
    for expression in expressions:
        match = expression.get("match") or {}
        left = match.get("left") or {}
        if (left.get("meta") or {}).get("key") == "l4proto":
            right = match["right"]
            names.update(right["set"] if isinstance(right, dict) else [right])
        payload = left.get("payload") or {}
        if payload.get("field") == "dport" and payload.get("protocol") in {"tcp", "udp"}:
            names.add(payload["protocol"])
    return sorted(names)


def rule_kind(rule):
    comments = {"api-lock": "api_lock", "api-allow": "api_allow", "LAN_ALLOW": "lan", "scanners": "scanners", "non-RU": "non_ru"}
    if rule.get("comment") in comments:
        return comments[rule["comment"]]
    expressions = rule.get("expr", [])
    lefts = [(expression.get("match") or {}).get("left") or {} for expression in expressions]
    if any("fib" in left for left in lefts):
        return "transit"
    if '"@tr4"' in json.dumps(expressions) or '"@tr6"' in json.dumps(expressions):
        return "trusted"
    if any((left.get("meta") or {}).get("key") in {"iif", "iifname"} for left in lefts):
        return "loopback"
    if any("ct" in left for left in lefts):
        return "established"
    return "other"


def rule_counter(rule):
    for expression in rule.get("expr", []):
        if "counter" in expression:
            return expression["counter"]["packets"], expression["counter"]["bytes"]
    return 0, 0


def dropped_text(group):
    text = f"отброшено пакетов: {group['packets']}"
    return text + (f", {format_size(group['size'])}" if group["packets"] else "")


def ports_phrase(ports, single, plural):
    return f"{single if len(ports) == 1 else plural} {', '.join(ports)}"


def protocols_phrase(protocols):
    return " и ".join(name.upper() for name in protocols)


def explain_rules(items):
    """Правила таблицы inet fw простым языком по JSON из `nft -j`."""
    sets = {item["set"]["name"]: len(item["set"].get("elem", [])) for item in items if "set" in item}
    chain = next(item["chain"] for item in items if "chain" in item and item["chain"]["name"] == "input")
    groups = []
    for item in items:
        rule = item.get("rule")
        if not rule or rule.get("chain") != "input":
            continue
        kind = rule_kind(rule)
        packets, size = rule_counter(rule)
        if groups and groups[-1]["kind"] == kind and kind in {"api_lock", "api_allow", "trusted", "lan", "scanners", "non_ru", "other"}:
            groups[-1]["packets"] += packets
            groups[-1]["size"] += size
            groups[-1]["count"] += 1
            continue
        groups.append({"kind": kind, "packets": packets, "size": size, "count": 1,
                       "ports": rule_ports(rule.get("expr", [])),
                       "protocols": rule_protocols(rule.get("expr", []))})
    kinds = [group["kind"] for group in groups]

    print("\nСписки адресов (записей после объединения подсетей):")
    rows = [("Доверенные, пропускаются всегда", "tr4", "tr6"),
            ("LAN_ALLOW, пропускаются всегда", "lan4", "lan6"),
            ("Сканеры и сети ведомств, блокируются", "sc4", "sc6"),
            ("Российские адреса, допускаются на защищённые порты", "ru4", "ru6")]
    if "api_lock" in kinds:
        rows.append(("Допущенные к порту управления", "ap4", "ap6"))
    for label, v4, v6 in rows:
        print(f"  {label} ".ljust(60, ".") + f" IPv4: {sets.get(v4, 0)}, IPv6: {sets.get(v6, 0)}")

    steps = []
    base_merged = "loopback" in kinds and "established" in kinds
    api_lock = next((group for group in groups if group["kind"] == "api_lock"), None)
    for group in groups:
        kind = group["kind"]
        if kind == "loopback":
            steps.append("Подключения самого сервера и ответы на соединения, которые он открыл, — пропускаются."
                         if base_merged else "Подключения самого сервера — пропускаются.")
        elif kind == "established":
            if not base_merged:
                steps.append("Ответы на соединения, которые открыл сам сервер, — пропускаются.")
        elif kind == "transit":
            steps.append("Трафик, адресованный не этому серверу (транзит), — не проверяется, пропускается.")
        elif kind == "api_lock":
            steps.append(f"{ports_phrase(group['ports'], 'Порт управления', 'Порты управления')} "
                         f"({protocols_phrase(group['protocols']) or 'TCP'}): адреса не из списка допущенных "
                         f"отбрасываются ({dropped_text(group)}), допущенные — пропускаются.")
        elif kind == "api_allow":
            if api_lock is None:
                steps.append(f"{ports_phrase(group['ports'], 'Порт управления', 'Порты управления')} — пропускается.")
        elif kind == "trusted":
            steps.append("Доверенные адреса — пропускаются на все порты.")
        elif kind == "lan":
            steps.append("Адреса LAN_ALLOW — пропускаются до списков сканеров и геофильтра.")
        elif kind == "scanners":
            steps.append(f"Сканеры и сети ведомств — отбрасываются на всех портах ({dropped_text(group)}).")
        elif kind == "non_ru":
            steps.append(f"{ports_phrase(group['ports'], 'Порт', 'Порты')} ({protocols_phrase(group['protocols'])}): "
                         f"адреса не из России — отбрасываются ({dropped_text(group)}).")
        else:
            steps.append(f"Прочие правила: {group['count']} шт. Их смысл смотри в техническом виде.")
    steps.append("Всё остальное — пропускается." if chain.get("policy") == "accept"
                 else "Всё остальное — отбрасывается.")

    print("\nКак проверяется каждое новое входящее подключение")
    print("(сверху вниз, срабатывает первое подходящее):")
    if chain.get("hook") == "prerouting":
        print("  Фильтр стоит до маршрутизации (prerouting), поэтому действует и на порты Docker-контейнеров.")
    for number, text in enumerate(steps, 1):
        print(textwrap.fill(f"{number}. {text}", width=92, initial_indent="  ", subsequent_indent="     "))


'''
menu = replace_once(menu, "def show_status():\n", EXPLAIN + "def show_status():\n", "explain_rules")

BOXED = r"""import threading

BOX_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def box_cells(raw):
    # Строка в виде списка (символ, цвет). Цвет хранится как накопленные коды ANSI.
    cells, color = [], ""
    position = 0
    for found in BOX_ANSI.finditer(raw):
        cells.extend((char, color) for char in raw[position:found.start()])
        code = found.group(0)
        color = "" if code in ("\x1b[0m", "\x1b[m") else color + code
        position = found.end()
    cells.extend((char, color) for char in raw[position:])
    return cells


def box_render(cells):
    out, current = [], ""
    for char, color in cells:
        if color != current:
            if current:
                out.append("\x1b[0m")
            out.append(color)
            current = color
        out.append(char)
    if current:
        out.append("\x1b[0m")
    return "".join(out)


def box_wrap(cells, width):
    # Перенос по пробелам; продолжение строки сдвигается так же, как начало, плюс два пробела.
    if len(cells) <= width:
        return [cells]
    indent = 0
    while indent < len(cells) and cells[indent][0] == " ":
        indent += 1
    indent = min(indent, width // 3)
    pad = [(" ", "")] * (indent + 2)
    lines, rest = [], cells
    while len(rest) > width:
        cut = -1
        for index in range(min(width, len(rest) - 1), indent + 1, -1):
            if rest[index][0] == " ":
                cut = index
                break
        if cut <= 0:
            cut = width
        lines.append(rest[:cut])
        rest = pad + [cell for cell in rest[cut:]][(1 if rest[cut][0] == " " else 0):]
    lines.append(rest)
    return lines


def print_box(title, text):
    columns = shutil.get_terminal_size((100, 24)).columns
    limit = max(30, columns - 4)
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").split("\n"):
        lines.extend(box_wrap(box_cells(raw.rstrip()), limit))
    inner = max([len(title) + 2] + [len(line) for line in lines])
    top = "┌─ " + title + " " + "─" * (inner + 2 - len(title) - 3) + "┐"
    print(top)
    for line in lines:
        print("│ " + box_render(line) + " " * (inner - len(line)) + " │")
    print("└" + "─" * (inner + 2) + "┘")


def show_status_boxed():
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        show_status()
        return
    import pty
    master, slave = pty.openpty()
    chunks = []

    def reader():
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(slave, 1)
    try:
        show_status()
        sys.stdout.flush()
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(slave)
    thread.join(timeout=5)
    os.close(master)
    print_box("Статус файрвола", b"".join(chunks).decode("utf-8", "replace"))


"""
menu = replace_once(menu, "def show_status():\n", BOXED + "def show_status():\n", "boxed_status")
menu = replace_once(menu, '("Статус файрвола", show_status),', '("Статус файрвола", show_status_boxed),', "boxed_entry")

OLD_TAIL = r'''    print("\nЗагруженные правила", flush=True)
    table = subprocess.run(["nft", "-j", "list", "table", "inet", "fw"],
                           text=True, capture_output=True, check=False)
    if table.returncode:
        print(table.stderr.strip() or "Таблица inet fw пока не загружена.")
        return
    for item in json.loads(table.stdout)["nftables"]:
        address_set = item.get("set")
        if address_set and address_set["name"] in {"ru4", "ru6", "sc4", "sc6", "tr4", "tr6"}:
            print(f"  {address_set['name']}: {len(address_set.get('elem', []))}", flush=True)
    run("nft", "-t", "list", "chain", "inet", "fw", "input")
'''
NEW_TAIL = r'''    print("\nЗагруженные правила (счётчики с момента последнего применения правил)", flush=True)
    table = subprocess.run(["nft", "-j", "list", "table", "inet", "fw"],
                           text=True, capture_output=True, check=False)
    if table.returncode:
        print(table.stderr.strip() or "Таблица inet fw пока не загружена.")
        return
    try:
        explain_rules(json.loads(table.stdout)["nftables"])
    except (AttributeError, KeyError, StopIteration, TypeError, ValueError):
        print("Не удалось разобрать правила, ниже технический вид.", flush=True)
        run("nft", "-t", "list", "chain", "inet", "fw", "input")
        return
    print("\nТехнический вид правил: nft list table inet fw")
'''
menu = replace_once(menu, OLD_TAIL, NEW_TAIL, "хвост show_status")

menu = replace_once(menu, 'def apply_rules():\n', r'''def settings_paths():
    return [(name, DIRECTORY / name) for name in SETTING_FILES] + [("schedule.conf", SCHEDULE_FILE)]


def save_settings(destination):
    destination.mkdir(mode=0o700)
    for name, path in settings_paths():
        if path.is_file():
            shutil.copy2(path, destination / name)
    # Метка полной копии: без неё restore_settings ничего не удаляет.
    (destination / ".complete").write_text("1\n")


def restore_settings(source):
    changed = []
    # Копия без метки (нет каталога, оборвана запись, состояние от старой версии)
    # не говорит, каких файлов не было: в этом случае настройки не трогаем.
    if not (source / ".complete").is_file():
        print(f"Копия настроек {source} неполная или отсутствует — настройки оставлены как есть.")
        return changed
    for name, path in settings_paths():
        saved = source / name
        if saved.is_file():
            if not path.is_file() or path.read_bytes() != saved.read_bytes():
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp = tempfile.mkstemp(prefix=".fw-restore-", dir=path.parent)
                os.close(fd)
                try:
                    shutil.copy2(saved, temp)
                    os.replace(temp, path)
                finally:
                    Path(temp).unlink(missing_ok=True)
                changed.append(str(path))
        elif path.exists():
            path.unlink()
            changed.append(str(path) + " (удалён)")
    return changed


def save_confirmed_settings():
    confirmed = DIRECTORY / "confirmed"
    staged = DIRECTORY / ".confirmed-new"
    previous = DIRECTORY / ".confirmed-old"
    if previous.exists() and not confirmed.exists():
        os.replace(previous, confirmed)
    if staged.exists():
        shutil.rmtree(staged)
    save_settings(staged)
    if previous.exists():
        shutil.rmtree(previous)
    if confirmed.exists():
        os.replace(confirmed, previous)
    os.replace(staged, confirmed)
    if previous.exists():
        shutil.rmtree(previous)


def rollback_rules(token, locked=False):
    state = DIRECTORY / (".rollback-" + token)
    if not state.is_dir():
        return
    fd = None if locked else os.open(DIRECTORY / ".apply.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # Меню держит lock до решения; отдельный процесс таймера ждёт здесь.
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        if not state.is_dir():
            return
        settings_source = DIRECTORY / "confirmed"
        if not settings_source.is_dir():
            settings_source = DIRECTORY / ".confirmed-old"
        if not settings_source.is_dir():
            settings_source = state / "settings"
        old = state / "active.nft"
        if old.exists():
            subprocess.run(["nft", "-f", str(old)], check=True)
        elif quiet("nft", "list", "table", "inet", "fw"):
            subprocess.run(["nft", "delete", "table", "inet", "fw"], check=True)
        snapshot = state / "fw.nft"
        if snapshot.exists():
            os.replace(snapshot, DIRECTORY / "fw.nft")
        else:
            (DIRECTORY / "fw.nft").unlink(missing_ok=True)
        count_snapshot = state / "geo.count"
        if count_snapshot.exists():
            os.replace(count_snapshot, DIRECTORY / "geo.count")
        else:
            (DIRECTORY / "geo.count").unlink(missing_ok=True)
        changed = restore_settings(settings_source)
        (DIRECTORY / "fw.nft.pending").unlink(missing_ok=True)
        (DIRECTORY / "geo.count.pending").unlink(missing_ok=True)
        shutil.rmtree(state)
        print("Правила и снимок восстановлены.")
        if changed:
            print("Возвращены настройки: " + ", ".join(changed))
    finally:
        if fd is not None:
            os.close(fd)


def confirm_timeout():
    script = ('source "$1/fw.conf"; [ -f "$1/local.conf" ] && source "$1/local.conf"; '
              'printf "%s" "${CONFIRM_TIMEOUT:-60}"')
    value = probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).strip()
    if not value.isdigit() or int(value) < 1:
        raise ValueError("CONFIRM_TIMEOUT должен быть положительным числом секунд")
    return int(value)


def paint(code, text, stream=None):
    stream = stream or sys.stdout
    if stream.isatty() and not os.environ.get("NO_COLOR"):
        print(f"\033[{code}m{text}\033[0m", file=stream)
    else:
        print(text, file=stream)


def last_result():
    try:
        return (DIRECTORY / "last-result").read_text().strip()
    except OSError:
        return ""


def ask_confirmation(timeout):
    import select
    print(f"Правила работают? Введите «да» за {timeout} с: ", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        print()
    return bool(ready and sys.stdin.readline().strip().casefold() == "да")


def apply_rules():
''', "откат меню")
old_apply = '''    if (run("systemctl", "start", "fw-update.service")
            and run("systemctl", "enable", "fw-update.service")
            and run("systemctl", "enable", "--now", "fw-update.timer")):
        print("Правила применены. Таймер включён.")
        reconcile_reinstall_guard()
    else:
        print("Одна из команд завершилась с ошибкой. Проверь «Статус файрвола» и журнал fw-update.service.")
'''
new_apply = r'''    import uuid
    lock_fd = os.open(DIRECTORY / ".apply.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print("Применение уже идёт в другом окне.")
        return
    timeout = confirm_timeout()
    token = uuid.uuid4().hex
    state = DIRECTORY / (".rollback-" + token)
    state.mkdir(mode=0o700)
    unit = "fw-rollback-" + token
    confirm_unit = unit + "-confirm"
    try:
        save_settings(state / "settings")
        if quiet("nft", "list", "table", "inet", "fw"):
            old = command_output("nft", "-s", "list", "table", "inet", "fw")
            (state / "active.nft").write_text("add table inet fw\ndelete table inet fw\n" + old,
                                              encoding="utf-8")
        snapshot = DIRECTORY / "fw.nft"
        if snapshot.exists():
            shutil.copy2(snapshot, state / "fw.nft")
        count_snapshot = DIRECTORY / "geo.count"
        if count_snapshot.exists():
            shutil.copy2(count_snapshot, state / "geo.count")
        subprocess.run(["systemd-run", "--quiet", "--collect", "--unit=" + unit,
                        "--on-active=15min", str(MENU_FILE), "--rollback", token],
                       check=True)
        env = {**os.environ, "FW_PENDING": "1"}
        if subprocess.run([str(DIRECTORY / "update-fw.sh")], env=env, check=False).returncode:
            raise RuntimeError("обновление завершилось с ошибкой")
        subprocess.run(["systemd-run", "--quiet", "--collect", "--unit=" + confirm_unit,
                        "--on-active=" + str(timeout) + "s", str(MENU_FILE), "--rollback", token],
                       check=True)
        run("systemctl", "stop", unit + ".timer")
        if not ask_confirmation(timeout):
            print("Подтверждение не получено. Восстанавливаю прежние правила.")
            rollback_rules(token, locked=True)
            run("systemctl", "stop", confirm_unit + ".timer")
            paint(33, "ИТОГ: ОТМЕНЕНО — подтверждение не получено, возвращены прежние правила")
            return
        confirmed = state.is_dir()
        if confirmed:
            os.replace(DIRECTORY / "fw.nft.pending", DIRECTORY / "fw.nft")
            os.replace(DIRECTORY / "geo.count.pending", DIRECTORY / "geo.count")
            if not NFT_DROPIN.exists():
                NFT_DROPIN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                subprocess.run(["install", "-m", "0644", str(DIRECTORY / "nftables-restore.conf"), str(NFT_DROPIN)], check=True)
                subprocess.run(["systemctl", "daemon-reload"], check=True)
            save_confirmed_settings()
            shutil.rmtree(state)
        run("systemctl", "stop", confirm_unit + ".timer")
        if not confirmed:
            paint(33, "ИТОГ: ОТМЕНЕНО — время подтверждения истекло, прежние правила уже восстановлены")
            return
        if run("systemctl", "enable", "fw-update.service") and run("systemctl", "enable", "--now", "fw-update.timer"):
            print("Правила подтверждены. Таймер включён.")
            result = last_result()
            if result == "ok":
                paint(32, "ИТОГ: ВСЁ В ПОРЯДКЕ — правила применены и подтверждены, таймер включён")
            elif result == "warn":
                paint(33, "ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ — правила применены и подтверждены, таймер включён; причины выше и в «Журнале ошибок»")
            else:
                paint(33, "ИТОГ: правила применены и подтверждены, таймер включён; итог проверки источников не определён")
            reconcile_reinstall_guard()
        else:
            paint(31, "ИТОГ: ОШИБКА — правила подтверждены, но включить таймер не удалось")
    except (OSError, RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        print(f"Ошибка применения: {error}. Восстанавливаю прежние правила.")
        rollback_rules(token, locked=True)
        run("systemctl", "stop", unit + ".timer")
        run("systemctl", "stop", confirm_unit + ".timer")
        paint(31, "ИТОГ: ОШИБКА — применение не удалось, возвращены прежние правила")
    finally:
        os.close(lock_fd)
'''
menu = replace_once(menu, old_apply, new_apply, "подтверждение применения")
menu = replace_once(menu, 'def remove_rules():\n', '''def remove_rules():
    lock_fd = os.open(DIRECTORY / ".apply.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        print("Применение уже идёт в другом окне.")
        return
    try:
        _remove_rules_locked()
    finally:
        os.close(lock_fd)


def _remove_rules_locked():
''', "lock удаления")
menu = replace_once(menu, '    directory_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)\n    try:\n        fcntl.flock(directory_fd, fcntl.LOCK_EX)\n        result = subprocess.run(["nft", "-j", "list", "tables"],',
    '    (DIRECTORY / "fw.nft").unlink(missing_ok=True)\n'
    '    (DIRECTORY / "fw.nft.pending").unlink(missing_ok=True)\n'
    '    (DIRECTORY / "geo.count.pending").unlink(missing_ok=True)\n'
    '    NFT_DROPIN.unlink(missing_ok=True)\n'
    '    run("systemctl", "daemon-reload")\n'
    '    directory_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)\n    try:\n        fcntl.flock(directory_fd, fcntl.LOCK_EX)\n        result = subprocess.run(["nft", "-j", "list", "tables"],', "удаление снимка")
menu = replace_once(menu, '    if sys.argv[1:] == ["--check-proxy"]:\n',
    '    if len(sys.argv) == 3 and sys.argv[1] == "--rollback" and re.fullmatch(r"[0-9a-f]{32}", sys.argv[2]):\n'
    '        rollback_rules(sys.argv[2])\n        return\n'
    '    if sys.argv[1:] == ["--check-proxy"]:\n', "команда отката")
menu = replace_once(menu, '    warn_reverse_proxy()\n    show_profile()\n',
    '''    warn_reverse_proxy()
    show_profile()
    script = ('source "$1/fw.conf"; [ -f "$1/local.conf" ] && source "$1/local.conf"; '
              'printf "%s\\n%s\\n%s\\n%s\\n%s\\n" "${LAN_ALLOW-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 fc00::/7 fe80::/10}" "${CONFIRM_TIMEOUT:-60}" "${MAX_GROWTH:-3}" "${MIN_KEEP:-0.2}" "${MAX_CACHE_DAYS:-14}"')
    values = probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).splitlines()
    if len(values) == 5:
        print("Настройки: LAN_ALLOW=" + (values[0] or "выключено") +
              f", CONFIRM_TIMEOUT={values[1]}, MAX_GROWTH={values[2]}, MIN_KEEP={values[3]}, MAX_CACHE_DAYS={values[4]}")
''', "показ настроек")
menu = replace_once(menu, '${LAN_ALLOW-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 fc00::/7 fe80::/10}',
                    '${LAN_ALLOW-}', "показ LAN")

# ---------------------------------------------------------------- версии, обновление и откат скрипта
VERSION = "1.2.0"
VERSIONS_CODE = r'''FW_VERSION = "@@VERSION@@"
FW_REPO = "J0ll1er/Nftables-Script"
BACKUP_ROOT = Path("/root/fw-rollbacks")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def fetch_text(url, limit=3_000_000):
    # file:// нужен только тестам и включается переменной окружения.
    if url.startswith("file://") and os.environ.get("FW_ALLOW_FILE") == "1":
        return Path(url[7:]).read_text(encoding="utf-8")
    result = subprocess.run(
        ["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "5",
         "--max-time", "30", "--max-filesize", str(limit), url], capture_output=True, check=False)
    if result.returncode:
        raise OSError(f"не скачалось: {url}")
    return result.stdout.decode("utf-8-sig")


def fetch_ref(ref, path):
    base = os.environ.get("FW_TEST_BASE") if os.environ.get("FW_ALLOW_FILE") == "1" else None
    urls = ([f"{base}/{ref}/{path}"] if base else
            [f"https://raw.githubusercontent.com/{FW_REPO}/{ref}/{path}",
             f"https://cdn.jsdelivr.net/gh/{FW_REPO}@{ref}/{path}",
             f"https://fastly.jsdelivr.net/gh/{FW_REPO}@{ref}/{path}"])
    last_error = None
    for url in urls:
        try:
            return fetch_text(url)
        except OSError as error:
            last_error = error
    raise OSError(str(last_error))


def parse_changelog(text):
    releases, current = [], None
    for line in text.splitlines():
        match = re.fullmatch(r"##\s+(" + VERSION_RE.pattern + r")(?:\s.*)?", line.strip())
        if match:
            current = {"version": match.group(1), "items": []}
            releases.append(current)
        elif current is not None and line.startswith("- "):
            current["items"].append(line[2:].strip())
    return sorted(releases, key=lambda release: version_key(release["version"]), reverse=True)


def load_releases():
    try:
        releases = parse_changelog(fetch_ref("main", "CHANGELOG.md"))
    except OSError:
        paint(31, "ОШИБКА — не удалось получить список версий: GitHub недоступен. Проверь сеть и повтори.")
        return None
    if not releases:
        paint(31, "ОШИБКА — список версий на GitHub пуст или в неожиданном формате.")
        return None
    return releases


def print_release(release):
    print(f"\n{release['version']}")
    for item in release["items"]:
        print(textwrap.fill(item, width=92, initial_indent="  • ", subsequent_indent="    "))


def show_versions():
    print(f"Установлена версия: {FW_VERSION}")
    releases = load_releases()
    if releases is None:
        return
    newer = [release for release in releases if version_key(release["version"]) > version_key(FW_VERSION)]
    if not newer:
        paint(32, "У вас последняя версия.")
        print("Доступные версии: " + ", ".join(release["version"] for release in releases))
        return
    paint(33, f"Доступна новая версия: {newer[0]['version']} (новее установленной: {len(newer)})")
    for release in newer:
        print_release(release)
    print("\nОбновить скрипт: пункт 7 → 2. Правила при обновлении не меняются.")


def install_version(version):
    try:
        installer = fetch_ref("v" + version, "fw-install.sh")
    except OSError:
        paint(31, f"ОШИБКА — версия {version} не найдена на GitHub (тег v{version}). Ничего не изменено.")
        return False
    if not installer.startswith("bash <<'INSTALL_FW'\n") or f"FW_VERSION='{version}'" not in installer:
        paint(31, f"ОШИБКА — скачанный файл не похож на установщик версии {version}. Ничего не изменено.")
        return False
    directory = Path(tempfile.mkdtemp(prefix=".fw-update.", dir="/root"))
    try:
        path = directory / "fw-install.sh"
        path.write_text(installer, encoding="utf-8")
        path.chmod(0o700)
        if subprocess.run(["bash", "-n", str(path)], check=False).returncode:
            paint(31, "ОШИБКА — в скачанном установщике синтаксическая ошибка. Ничего не изменено.")
            return False
        result = subprocess.run(["bash", str(path)], stdin=subprocess.DEVNULL, check=False)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if result.returncode:
        paint(31, "ОШИБКА — установщик завершился с ошибкой, причина выше. Вернуть прежнюю версию: пункт 7 → 3.")
        return False
    paint(32, f"ИТОГ: ВСЁ В ПОРЯДКЕ — скрипт {version} установлен, действующие правила не менялись. "
              "Запусти fw-menu заново.")
    return True


def update_script():
    releases = load_releases()
    if releases is None:
        return
    newer = [release for release in releases if version_key(release["version"]) > version_key(FW_VERSION)]
    if not newer:
        paint(32, f"У вас уже последняя версия ({FW_VERSION}).")
        return
    for release in newer:
        print_release(release)
    latest = newer[0]["version"]
    answer = input(f"\nОбновить скрипт {FW_VERSION} → {latest}? Правила не изменятся. Введи «да»: ")
    if answer.strip().casefold() != "да":
        return
    if install_version(latest):
        raise SystemExit(0)


def choose_number(count):
    answer = input("Номер (Enter — отмена): ").strip()
    if not answer:
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= count:
        print("Нет такого номера.")
        return None
    return int(answer) - 1


def rollback_remote():
    releases = load_releases()
    if releases is None:
        return
    choices = [release for release in releases if release["version"] != FW_VERSION]
    if not choices:
        print("Других версий на GitHub нет.")
        return
    print(f"Установлена версия: {FW_VERSION}")
    for number, release in enumerate(choices, start=1):
        first = release["items"][0] if release["items"] else ""
        print(f"{number}. {release['version']}  {first[:70]}")
    index = choose_number(len(choices))
    if index is None:
        return
    version = choices[index]["version"]
    if version_key(version) < version_key(FW_VERSION):
        print("Старая версия может не знать новых настроек (например LAN_ALLOW, CONFIRM_TIMEOUT): "
              "они останутся в настройках, но защиты не дадут.")
    answer = input(f"Установить версию {version}? Правила не изменятся. Введи «да»: ")
    if answer.strip().casefold() == "да" and install_version(version):
        raise SystemExit(0)


def backup_version(directory):
    try:
        match = re.search(r'^FW_VERSION = "(' + VERSION_RE.pattern + r')"', (directory / "fw-menu").read_text(encoding="utf-8"), re.M)
    except OSError:
        return "неизвестно"
    return match.group(1) if match else "до введения версий"


def local_backups():
    if not BACKUP_ROOT.is_dir():
        return []
    found = [path for path in BACKUP_ROOT.glob("upgrade.*")
             if (path / "update-fw.sh").is_file() and (path / "fw-menu").is_file()]
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)[:10]


def restore_file(source, target, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".fw-rollback-", dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temp)
        os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        Path(temp).unlink(missing_ok=True)


def rollback_local():
    backups = local_backups()
    if not backups:
        print("Локальных копий в /root/fw-rollbacks нет. Выбери версию с GitHub (пункт 7 → 3 → 2).")
        return
    print(f"Установлена версия: {FW_VERSION}")
    for number, backup in enumerate(backups, start=1):
        stamp = datetime.datetime.fromtimestamp(backup.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
        print(f"{number}. {stamp}  версия {backup_version(backup)}")
    index = choose_number(len(backups))
    if index is None:
        return
    backup = backups[index]
    answer = input("Вернуть скрипты из этой копии? Правила и настройки не изменятся. Введи «да»: ")
    if answer.strip().casefold() != "да":
        return
    restore_file(backup / "update-fw.sh", UPDATER, 0o755)
    restore_file(backup / "fw-menu", MENU_FILE, 0o755)
    for name, target in (("fw-update.service", SERVICE_FILE), ("fw-update.timer", TIMER_FILE)):
        if (backup / name).is_file():
            restore_file(backup / name, target, 0o644)
    if (backup / NFT_DROPIN.name).is_file():
        restore_file(backup / NFT_DROPIN.name, NFT_DROPIN, 0o644)
    else:
        NFT_DROPIN.unlink(missing_ok=True)
    run("systemctl", "daemon-reload")
    paint(32, f"ИТОГ: ВСЁ В ПОРЯДКЕ — скрипты возвращены к версии {backup_version(backup)}, "
              "действующие правила не менялись. Запусти fw-menu заново.")
    raise SystemExit(0)


def rollback_script():
    show_menu("Откатить скрипт", (
        ("Из локальной копии на этом сервере", rollback_local),
        ("Установить версию с GitHub", rollback_remote),
    ))


'''
menu = replace_once(menu, 'def show_menu(title, entries, exit_label="Назад"):\n',
                    VERSIONS_CODE.replace("@@VERSION@@", VERSION) + 'def show_menu(title, entries, exit_label="Назад"):\n', "версии и откат скрипта")
menu = replace_once(menu, "import fcntl\n", "import datetime\nimport fcntl\n", "import datetime")

WIDE_MENU_CODE = r'''WIDE_PENDING = DIRECTORY / "wide-pending.txt"
WIDE_OK = DIRECTORY / "wide-ok.txt"


def read_wide_networks(path, with_source=False):
    result = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        source, tab, value = line.partition("\t")
        if not tab:
            source, value = "", line
        value = value.partition("#")[0].strip()
        try:
            network = ipaddress.ip_network(value)
        except ValueError:
            continue
        result.append((source, network) if with_source else network)
    return result


def ask_wide_networks():
    """Сети, которые шире порога, скрипт пропускает. Здесь их можно подтвердить. True — список изменился."""
    approved = set(read_wide_networks(WIDE_OK))
    items = sorted({(source, network) for source, network in read_wide_networks(WIDE_PENDING, True)
                    if network not in approved}, key=lambda item: (item[0], str(item[1])))
    if not items:
        return False
    print()
    paint(33, f"Найдены слишком широкие сети: {len(items)}. Сейчас они пропускаются и не блокируются.")
    print("Если это ошибка источника, оставь как есть. Если сеть нужна — подтверди.")
    for source, network in items[:30]:
        total = f"{network.num_addresses:,}".replace(",", " ")
        print(f"  {network_text(network)} — {total} адресов — источник {source or 'не указан'}")
    if len(items) > 30:
        print(f"  … и ещё {len(items) - 30}")
    answer = input("Блокировать эти сети? Введи «да», Enter — не блокировать: ").strip().casefold()
    if answer != "да":
        print("Сети остаются неблокируемыми. Спросим снова при следующей проверке.")
        return False
    append_entries(WIDE_OK, [f"{network_text(network)}  # подтверждено {datetime.date.today()}, источник {source or '?'}"
                             for source, network in items])
    print("Сохранено в wide-ok.txt. Эти сети теперь блокируются при каждом обновлении.")
    return True


def dry_run():
    ok = run(UPDATER, "--dry")
    if ask_wide_networks():
        print("Повторяю проверку с учётом подтверждённых сетей.")
        ok = run(UPDATER, "--dry")
    return ok


'''
menu = replace_once(menu, 'def apply_rules():\n', WIDE_MENU_CODE + 'def apply_rules():\n', "подтверждение широких сетей")
menu = replace_once(menu, '("Проверить обновление без применения (--dry)", lambda: run(UPDATER, "--dry")),',
                    '("Проверить обновление без применения (--dry)", dry_run),', "пункт --dry")
menu = replace_once(menu, '    if input("Применить правила и включить ежедневное обновление? Введи «да»: ")',
                    '    ask_wide_networks()\n    if input("Применить правила и включить ежедневное обновление? Введи «да»: ")', "вопрос перед применением")
menu = replace_once(menu, '"asn.txt", "sources.txt", "presets.conf")', '"asn.txt", "sources.txt", "presets.conf", "wide-ok.txt")', "wide-ok в настройках")

OVERVIEW_CODE = r'''def read_lines(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def certbot_renewals():
    """Сертификаты certbot: имя и параметры продления из renewal/*.conf (только чтение)."""
    result = []
    for conf in sorted(Path("/etc/letsencrypt/renewal").glob("*.conf")):
        values = {}
        for line in read_lines(conf):
            key, separator, value = line.partition("=")
            if separator and key.strip() in ("authenticator", "pre_hook", "post_hook", "renew_hook", "deploy_hook"):
                values[key.strip()] = value.strip()
        result.append((conf.stem, values))
    return result


def acme_sh_present():
    return Path("/root/.acme.sh").is_dir()


def acme_sh_modes():
    """Как acme.sh проверяет домены: [(домен, режим)] по Le_Webroot в его настройках. Режим: dns, standalone, webroot или None."""
    result = []
    for conf in sorted(Path("/root/.acme.sh").glob("*/*.conf")):
        if conf.parent.name != conf.stem and conf.parent.name != conf.stem + "_ecc":
            continue
        for line in read_lines(conf):
            key, separator, value = line.partition("=")
            if separator and key.strip() == "Le_Webroot":
                value = value.strip().strip("'\"")
                mode = "dns" if value.startswith("dns") else "standalone" if value == "no" else "webroot" if value.startswith("/") else None
                result.append((conf.stem, mode))
    return result


def renewal_hook_files():
    base = Path("/etc/letsencrypt/renewal-hooks")
    found = {}
    for kind in ("pre", "post", "deploy"):
        folder = base / kind
        if folder.is_dir():
            files = [item.name for item in sorted(folder.iterdir()) if item.is_file() and os.access(item, os.X_OK)]
            if files:
                found[kind] = files
    return found


def renewal_cron_lines():
    """Задания cron, связанные с продлением сертификатов: [(где, строка)]."""
    sources = [("/etc/crontab", read_lines(Path("/etc/crontab")))]
    for path in sorted(Path("/etc/cron.d").glob("*")):
        if path.is_file():
            sources.append((str(path), read_lines(path)))
    sources.append(("crontab root", probe_output("crontab", "-l").splitlines()))
    found = []
    for place, lines in sources:
        for line in lines:
            text = line.strip()
            if text and not text.startswith("#") and re.search(r"certbot|acme\.sh|letsencrypt", text):
                found.append((place, text))
    return found


def port80_report(policy, listeners):
    """Строки о порте 80 и о том, чем он открывается на время продления. Не гадает: если не уверен, так и пишет."""
    lines = []
    listening = any(proto == "tcp" and port == 80 and not loopback for proto, port, _, loopback in listeners)
    exposed, _ = exposure(policy, "tcp", 80)
    if not listening:
        lines.append("Порт 80: никто не слушает")
    elif exposed == "closed":
        lines.append("Порт 80: слушает, снаружи закрыт ufw")
    elif exposed == "limited":
        lines.append("Порт 80: слушает, ufw пускает только часть адресов")
    else:
        lines.append("Порт 80: открыт" + ("" if policy is not None else " (ufw не активен)"))
    certificates = certbot_renewals()
    hooks = renewal_hook_files()
    cron = renewal_cron_lines()
    acme_sh = acme_sh_present()
    if not certificates and not cron and not acme_sh:
        lines.append("Продление сертификатов: certbot и acme.sh не найдены. Так бывает, если сертификаты самоподписанные, "
                     "выданы Cloudflare Origin или получены другим способом: им порт 80 для продления не нужен")
        return lines
    need_http = False
    for name, values in certificates:
        authenticator = values.get("authenticator", "не указан")
        if "dns" in authenticator:
            lines.append(f"Сертификат {name}: проверка через DNS ({authenticator}), порт 80 для продления не нужен")
        else:
            need_http = need_http or authenticator in ("nginx", "webroot", "standalone", "apache", "не указан")
            lines.append(f"Сертификат {name}: проверка через {authenticator}, нужен порт 80")
    if acme_sh:
        modes = acme_sh_modes()
        for name, mode in modes:
            if mode == "dns":
                lines.append(f"acme.sh {name}: проверка через DNS, порт 80 для продления не нужен")
            elif mode in ("standalone", "webroot"):
                need_http = True
                lines.append(f"acme.sh {name}: проверка через {mode}, нужен порт 80")
        if not modes or any(mode is None for _, mode in modes):
            lines.append("Найден acme.sh (/root/.acme.sh): как он проверяет часть доменов, не определяется")
    handled = False
    for kind, files in hooks.items():
        lines.append(f"Хуки certbot ({kind}): {', '.join(files)}")
        handled = handled or kind in ("pre", "post")
    for name, values in certificates:
        for key in ("pre_hook", "post_hook"):
            if values.get(key):
                lines.append(f"Хук в настройках {name}: {key} = {values[key]}")
                handled = True
    for place, text in cron:
        if re.search(r"ufw|iptables|nft|:80|\b80\b", text):
            lines.append(f"Задание cron ({place}) трогает файрвол или порт 80: {text[:140]}")
            handled = True
    if need_http and listening and exposed == "closed" and not handled:
        lines.append("ВНИМАНИЕ: порт закрыт, а способа открыть его на время продления не найдено — продление может не сработать")
    elif need_http and listening and exposed == "open" and handled:
        lines.append("Порт 80 открыт постоянно, хотя есть хук или задание, которые его открывают: проверь, что после продления он закрывается")
    elif need_http and not handled:
        lines.append("Открытие и закрытие порта 80 вокруг продления не найдено — если оно настроено иначе (скриптом, контейнером), здесь этого не видно")
    return lines


def ssh_ports():
    ports = sorted({int(value) for line in probe_output("sshd", "-T").splitlines()
                    for key, _, value in [line.partition(" ")] if key == "port" and value.isdigit()})
    if ports:
        return ports
    for line in read_lines(Path("/etc/ssh/sshd_config")):
        match = re.match(r"\s*Port\s+(\d+)", line)
        if match:
            return [int(match[1])]
    return [22]


NFT_TABLE_NOTES = {
    "inet fw": "наша",
    "ip remnanode": "плагины ноды Remnawave (Torrent Blocker и др.)",
    "ip6 remnanode6": "плагины ноды Remnawave (Torrent Blocker и др.)",
    "inet geoblock": "старый геофильтр, его заменяет inet fw",
}


def show_server_overview():
    print("\nЧто ещё есть на сервере (только чтение, ничего не меняется)")
    tables = [line.strip()[len("table "):] for line in probe_output("nft", "list", "tables").splitlines()
              if line.strip().startswith("table ")]
    print("Таблицы nftables:" if tables else "Таблицы nftables: не найдены")
    for table in tables:
        note = NFT_TABLE_NOTES.get(table)
        print(f"  {table}" + (f" — {note}" if note else " — чужая (Docker, ufw и т. д.), мы её не трогаем"))
    ufw_text = ""
    try:
        ufw_text = subprocess.run(["ufw", "status", "verbose"], text=True, capture_output=True, timeout=8,
                                  check=False, env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    policy = ufw_policy(ufw_text)
    if policy is None:
        prepared = [line.strip() for line in probe_output("ufw", "show", "added").splitlines()
                    if line.strip().startswith("ufw ")]
        print("ufw: не активен или не установлен" + (f"; заготовлены правила (не действуют): "
              f"{'; '.join(item[4:] for item in prepared)}" if prepared else ""))
    else:
        print("ufw: активен, входящие " + ("разрешены" if policy["allow"] else "закрыты кроме правил"))
    listeners = parse_listeners(probe_output("ss", "-H", "-lntup"))
    outside = sorted((port, proto, name) for proto, port, name, loopback in listeners if not loopback)
    print("Слушают не только локально:" if outside else "Снаружи ничего не слушает")
    for port, proto, name in outside:
        state, sources = exposure(policy, proto, port)
        label = {"open": "открыт", "closed": "закрыт ufw", "limited": "ufw пускает только " + ", ".join(sources)}[state]
        print(f"  {proto.upper()} {port} — {name or 'процесс не определён'}, {label}")
    for port in ssh_ports():
        state, _ = exposure(policy, "tcp", port)
        suffix = {"open": "открыт", "closed": "в ufw закрыт — при включённом ufw доступ по SSH пропадёт",
                  "limited": "ограничен адресами"}[state]
        print(f"SSH: порт {port} — {suffix}")
    for line in port80_report(policy, listeners):
        print(line)


'''
menu = replace_once(menu, 'def apply_rules():\n', OVERVIEW_CODE + 'def apply_rules():\n', "что ещё есть на сервере")
menu = replace_once(menu, '        ("Журнал ошибок", show_error_log),\n',
    '        ("Журнал ошибок", show_error_log),\n'
    '        ("Версии и обновление скрипта", (\n'
    '            ("Версии и что исправлено", show_versions),\n'
    '            ("Обновить скрипт до последней версии", update_script),\n'
    '            ("Откатить скрипт к другой версии", rollback_script),\n'
    '        )),\n', "пункт 7")
menu = replace_once(menu, '            ("Откатить скрипт к другой версии", rollback_script),\n        )),\n',
                    '            ("Откатить скрипт к другой версии", rollback_script),\n        )),\n'
                    '        ("Что ещё есть на сервере", show_server_overview),\n', "пункт 8")
menu = replace_once(menu, '    if os.geteuid() != 0:\n        raise SystemExit("Запусти fw-menu от root.")\n',
    '    if sys.argv[1:] == ["--version"]:\n        print(FW_VERSION)\n        return\n'
    '    if os.geteuid() != 0:\n        raise SystemExit("Запусти fw-menu от root.")\n', "--version")
menu = replace_once(menu, '    try:\n        try:\n            _, _, sizes = file_inventory()\n',
    '    print(f"Версия скрипта — {FW_VERSION}")\n    try:\n        try:\n            _, _, sizes = file_inventory()\n', "версия в заголовке")

# ---------------------------------------------------------------- сборка
new_text = text
for key, replacement in (("menu", menu), ("update", upd)):
    a, b, _ = parts[key]
    # заменяем с конца, чтобы смещения не сбивались: menu идёт после update
    new_text = new_text[:a] + replacement + new_text[b:]
    # пересчитать смещения оставшихся частей после правки
    a2, b2 = heredoc(new_text, {"menu": "FW_MENU_FILE", "update": "FW_UPDATER_FILE"}[key])
    parts[key] = (a2, b2, new_text[a2:b2])

# Установщик вне heredoc также патчится при сборке.
new_text = replace_once(new_text, 'TIMER=/etc/systemd/system/fw-update.timer\n',
    'TIMER=/etc/systemd/system/fw-update.timer\n'
    'NFT_DROPIN=/etc/systemd/system/nftables.service.d/10-fw-restore.conf\n', "путь drop-in")
new_text = replace_once(new_text, "cat > \"$stage/fw-update.timer\" <<'FW_TIMER_FILE'\n",
    "cat > \"$stage/nftables-restore.conf\" <<'FW_NFT_RESTORE_FILE'\n"
    "[Service]\n"
    "ExecStartPost=-/bin/sh -c 'for state in /opt/fw/.rollback-*; do [ -d \"$$state\" ] || continue; echo \"WARN: найдено неподтверждённое применение, откатываю\" >&2; /usr/local/sbin/fw-menu --rollback \"$${state##*/.rollback-}\" || exit 1; done; [ ! -s /opt/fw/fw.nft ] || { /usr/sbin/nft -c -f /opt/fw/fw.nft && /usr/sbin/nft -f /opt/fw/fw.nft; }'\n"
    "ExecReload=-/bin/sh -c '[ ! -s /opt/fw/fw.nft ] || { /usr/sbin/nft -c -f /opt/fw/fw.nft && /usr/sbin/nft -f /opt/fw/fw.nft; }'\n"
    "FW_NFT_RESTORE_FILE\n"
    "systemd_version=$(systemctl --version | awk 'NR == 1 { print $2 }')\n"
    "if [[ $systemd_version =~ ^[0-9]+$ ]] && [ \"$systemd_version\" -ge 259 ]; then\n"
    "  sed -i 's/^ExecReload=/ExecReloadPost=/' \"$stage/nftables-restore.conf\"\n"
    "fi\n\n"
    "cat > \"$stage/fw-update.timer\" <<'FW_TIMER_FILE'\n", "содержимое drop-in")
new_text = replace_once(new_text, '  for path in "$MENU" "$SERVICE" "$TIMER"; do\n',
    '  for path in "$MENU" "$SERVICE" "$TIMER" "$NFT_DROPIN"; do\n', "копия drop-in")
new_text = replace_once(new_text, '  install -m 0644 "$stage/fw-update.service" "$SERVICE"\n',
    '  install -m 0644 "$stage/fw-update.service" "$SERVICE"\n'
    '  install -m 0644 "$stage/nftables-restore.conf" "$FW/nftables-restore.conf"\n'
    '  if [ -e "$NFT_DROPIN" ] || [ -s "$FW/fw.nft" ]; then\n'
    '    install -d -m 0755 "${NFT_DROPIN%/*}"\n'
    '    install -m 0644 "$stage/nftables-restore.conf" "$NFT_DROPIN"\n'
    '  fi\n', "drop-in при обновлении")
new_text = replace_once(new_text, 'install -m 0644 "$stage/fw-update.timer" "$TIMER"\n',
    'install -m 0644 "$stage/fw-update.timer" "$TIMER"\n'
    'install -m 0644 "$stage/nftables-restore.conf" "$FW/nftables-restore.conf"\n'
    'install -d -m 0755 "${NFT_DROPIN%/*}"\n'
    'install -m 0644 "$stage/nftables-restore.conf" "$NFT_DROPIN"\n', "drop-in при установке")
start = '  if ! grep -qE \'^[[:space:]]*[^#[:space:]]\' "$FW/asn.txt"; then\n'
a = new_text.index(start)
b = new_text.index('\n  "$MENU" --profile', a)
new_text = new_text[:a] + new_text[b:]
new_text = replace_once(new_text, '  install_seed\n\n\n  "$MENU" --profile',
    '  install_seed\n'
    '  if ! grep -q "^[[:space:]]*LAN_ALLOW=" "$FW/fw.conf" && ! grep -q "^[[:space:]]*LAN_ALLOW=" "$FW/local.conf" 2>/dev/null; then\n'
    '    echo \'LAN_ALLOW выключен. Для включения добавь LAN_ALLOW="нужная_подсеть" в /opt/fw/local.conf.\'\n'
    '  fi\n'
    '  ssh_client=$(detect_admin)\n'
    '  if [ -n "$ssh_client" ] && ! grep -Fxq "$ssh_client" "$FW/trusted.txt"; then\n'
    '    printf "%s\\n" "$ssh_client" >> "$FW/trusted.txt"\n'
    '    echo "Адрес администратора ($ssh_client) добавлен в trusted.txt."\n'
    '  fi\n\n  "$MENU" --profile', "LAN при обновлении")
new_text = replace_once(new_text, "  echo 'API_ALLOW=\"\"'\n",
    "  echo 'API_ALLOW=\"\"'\n" + '  printf \'LAN_ALLOW="%s"\\n\' "$lan_default"\n', "LAN в новом конфиге")
old_ssh = '''ssh_client=${SSH_CONNECTION:-}
ssh_client=${ssh_client%% *}
if [ -n "$ssh_client" ]; then
  printf '%s\\n' "$ssh_client" > "$FW/trusted.txt"
  echo "Адрес этого SSH-подключения ($ssh_client) добавлен в доверенные."
fi
'''
new_ssh = '''ssh_client=$(python3 - <<'PY'
import ipaddress
import os
import re
import subprocess

def valid(value):
    try:
        return str(ipaddress.ip_address(value.strip("[]")))
    except ValueError:
        return ""

connection = os.environ.get("SSH_CONNECTION", "").split()
client = valid(connection[0]) if connection else ""
if not client:
    for command in (("who", "-m", "--ips"), ("who", "am", "i")):
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        match = re.search(r"\\(([^()]*)\\)\\s*$", result.stdout.strip())
        client = valid(match.group(1)) if match else ""
        if client:
            break
if not client:
    pids = set()
    pid = os.getppid()
    for _ in range(32):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            pid = int(open(f"/proc/{pid}/stat").read().split(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    session = os.environ.get("XDG_SESSION_ID", "")
    if session:
        result = subprocess.run(("loginctl", "show-session", session, "-p", "Leader", "--value"),
                                text=True, capture_output=True, check=False)
        if result.stdout.strip().isdigit():
            pids.add(int(result.stdout.strip()))
    result = subprocess.run(("ss", "-Htnp", "state", "established"), text=True,
                            capture_output=True, check=False)
    for line in result.stdout.splitlines():
        if not any(f"pid={pid}," in line for pid in pids) or "sshd" not in line:
            continue
        fields = line.split()
        if len(fields) > 4:
            client = valid(fields[-2].rsplit(":", 1)[0])
            if client:
                break
print(client)
PY
)
if [ -n "$ssh_client" ]; then
  printf '%s\\n' "$ssh_client" > "$FW/trusted.txt"
  echo "Адрес этого SSH-подключения ($ssh_client) добавлен в доверенные."
else
  echo 'WARN: адрес администратора не определён. Добавь свой IPv4/IPv6 в /opt/fw/trusted.txt до применения правил.' >&2
fi
'''
detect_body = new_ssh.split('if [ -n "$ssh_client" ]; then', 1)[0]
detect_body = detect_body.replace('ssh_client=$(python3', 'detect_admin() {\npython3', 1)
detect_body = detect_body.replace('PY\n)\n', 'PY\n}\n', 1)
new_text = replace_once(new_text, '# ---------------------------------------------------------------- обновление\n',
                            detect_body + '\n# ---------------------------------------------------------------- обновление\n', "общий поиск администратора")
new_text = replace_once(new_text, old_ssh,
    'ssh_client=$(detect_admin)\n'
    'if [ -n "$ssh_client" ]; then\n'
    '  printf "%s\\n" "$ssh_client" > "$FW/trusted.txt"\n'
    '  echo "Адрес этого SSH-подключения ($ssh_client) добавлен в доверенные."\n'
    'else\n'
    "  echo 'WARN: адрес администратора не определён. Добавь свой IPv4/IPv6 в /opt/fw/trusted.txt до применения правил.' >&2\n"
    'fi\n', "адрес администратора")
new_text = replace_once(new_text, 'install -d -m 0755 /opt\n',
    '''lan_default=""
server_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}' || true)
lan_candidate=$(python3 - "$server_ip" <<'PY'
import ipaddress, sys
try:
    address = ipaddress.ip_address(sys.argv[1])
except ValueError:
    address = None
for subnet in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"):
    if address is not None and address in ipaddress.ip_network(subnet):
        print(subnet)
        break
PY
)
if [ -n "$lan_candidate" ]; then
  ask enable_lan "Адрес сервера $server_ip находится в $lan_candidate. Разрешить эту подсеть через LAN_ALLOW? да/нет" да
  if [ "$enable_lan" = да ]; then lan_default=$lan_candidate; fi
fi

install -d -m 0755 /opt
''', "предложение LAN")

new_text = replace_once(new_text, "      echo 'Применить новые: systemctl start fw-update.service'\n",
    "      echo 'Применить новые правила с подтверждением и автооткатом: fw-menu → 4 → 2'\n"
    "      echo 'Команда systemctl start fw-update.service применяет сразу, без подтверждения и отката.'\n", "подсказка применения")
new_text = replace_once(new_text,
    "      echo 'Применить и включить автозапуск: systemctl enable fw-update.service && systemctl enable --now fw-update.timer && systemctl start fw-update.service'\n",
    "      echo 'Применить и включить автозапуск с подтверждением и автооткатом: fw-menu → 4 → 2'\n", "подсказка автозапуска")

new_text = replace_once(new_text, "umask 022\n\nif [ \"$(id -u)\" -ne 0 ]; then\n",
    "umask 022\nFW_VERSION='" + VERSION + "'\n\nif [ \"$(id -u)\" -ne 0 ]; then\n", "версия установщика")
new_text = replace_once(new_text, "FW=/opt/fw\nMENU=/usr/local/sbin/fw-menu\n",
    "echo \"Nftables-Script, версия $FW_VERSION\"\nFW=/opt/fw\nMENU=/usr/local/sbin/fw-menu\n", "печать версии")

new_hashes = {}
for key, marker in (("update", "FW_UPDATER_FILE"), ("menu", "FW_MENU_FILE"), ("seed", "FW_SEED_FILE")):
    a, b = heredoc(new_text, marker)
    new_hashes[key] = sha(new_text[a:b])
    if key != "seed":
        parts[key] = (a, b, new_text[a:b])

for key in ("update", "menu", "seed"):
    new_text = replace_once(new_text, old_hashes[key], new_hashes[key], f"хеш {key}") if old_hashes[key] != new_hashes[key] else new_text

OUT.write_bytes(new_text.encode("utf-8"))
BUILD.mkdir(exist_ok=True)
a, b = heredoc(new_text, "FW_UPDATER_FILE")
(BUILD / "update-fw.sh").write_text(new_text[a:b], encoding="utf-8")
a, b = heredoc(new_text, "FW_MENU_FILE")
(BUILD / "fw-menu").write_text(new_text[a:b], encoding="utf-8")
a, b = heredoc(new_text, "FW_SEED_FILE")
(BUILD / "seed.txt").write_text(new_text[a:b], encoding="utf-8")

# Регрессионные проверки добавляются генератором: исходным патчем остаётся build.py.
tests_file = ROOT / "tests/test_updater_limits.py"
tests_text = tests_file.read_text(encoding="utf-8")
test_marker = "    def test_restart_rolls_back_before_normal_update(self):\n"
if test_marker not in tests_text:
    new_tests = '''    def test_restart_rolls_back_before_normal_update(self):
        _, snapshot = self.run_case("restart_rollback")
        fw = snapshot.parent / "fw"
        token = "a" * 32
        (fw / (".rollback-" + token)).mkdir()
        (fw / "fw.nft.pending").write_text("pending rules\\n")
        stub = snapshot.parent / "fw-menu-stub"
        stub.write_text("#!/bin/sh\\nprintf '%s\\n' \\"$*\\" >> \\"$STUB_DIR/menu_calls\\"\\n"
                        "rm -rf \\"$STUB_DIR/fw/.rollback-$2\\"\\n"
                        "rm -f \\"$STUB_DIR/fw/fw.nft.pending\\"\\n")
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

'''
    tests_file.write_text(tests_text.replace("class InstallerAsn(unittest.TestCase):\n",
                                            new_tests + "class InstallerAsn(unittest.TestCase):\n"),
                          encoding="utf-8")
print("готово:", OUT)
for key in ("update", "menu", "seed"):
    print(key, old_hashes[key][:12], "->", new_hashes[key][:12])
