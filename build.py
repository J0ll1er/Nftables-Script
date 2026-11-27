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


# 1.7.2: Stretchoid (OpenFilters/internet-scanners) входит в пресет «classic»
upd = replace_once(
    upd,
    '  "$OPENFILTERS/quake_v4.txt"\n)\n',
    '  "$OPENFILTERS/quake_v4.txt"\n  "$OPENFILTERS/strechoid_v4.txt"\n)\n',
    "источники: Stretchoid",
)


upd = replace_once(
    upd,
    'fail_keep() { echo "ERR: $1 — правила не тронуты" >&2; exit 1; }\n'
    'fail_save() { echo "ERR: правила применены, но $1" >&2; exit 1; }\n',
    'MODE=apply; [ "$DRY" = 1 ] && MODE=dry\n'
    'ERRLOG=$DIR/errors.log\n'
    '# note "WARN: …" или "ERR: …": в stderr и в журнал ошибок (fw-menu → 1 → 3 «Журнал ошибок»).\n'
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
import re
import sys
blocked, protected = map(Path, sys.argv[1:])
# DDNS-имя из trusted*.txt здесь пропускается: его IP уже добавлены строками из ddns_ips.
# Любая другая строка, не похожая на адрес, по-прежнему останавливает обновление.
HOST = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$")
try:
    allowed = [ipaddress.ip_network(value) for line in protected.read_text().splitlines()
               if (value := line.partition('#')[0].strip()) and not HOST.match(value.lower())]
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
# Широкая сеть в RU-списке (0.0.0.0/1, ::/1) отключила бы геофильтр, а число строк такую подмену не выдаёт.
upd = replace_once(upd, '''  [ "$url" = "$ZKEEN_URL" ] || echo "Зеркало для геобазы: ${url#https://}"\n''',
    '''  if ! reason=$(python3 - "$TMP/ru4" "$TMP/ru6" <<'PY'
import ipaddress
from pathlib import Path
import sys
LIMITS = {4: 8, 6: 16}  # самая широкая допустимая сеть: IPv4 не шире /8, IPv6 не шире /16
for name in sys.argv[1:]:
    for line in Path(name).read_text().splitlines():
        network = ipaddress.ip_network(line, strict=False)
        if network.prefixlen < LIMITS[network.version]:
            sys.exit(f"содержит слишком широкую сеть {network}")
PY
  ); then
    note "WARN: геобаза с ${url#https://} отклонена: $reason"
    continue
  fi
  [ "$url" = "$ZKEEN_URL" ] || echo "Зеркало для геобазы: ${url#https://}"\n''', "ширина сетей RU")
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

# ------------------------------------------------ предел размера загрузок (1.7.0)
# Всё, что скачивает обновлятор, режется по размеру: --max-filesize ловит большой Content-Length,
# а head -c обрезает ответ без длины. Больше предела — ответ отбрасывается как недоступный источник.
upd = replace_once(upd, '[[ "$MIN_PREFIX6" =~ ^([1-9][0-9]?|1[01][0-9]|12[0-8])$ ]] || fail_keep "MIN_PREFIX6 должен быть от 1 до 128"\n',
    '[[ "$MIN_PREFIX6" =~ ^([1-9][0-9]?|1[01][0-9]|12[0-8])$ ]] || fail_keep "MIN_PREFIX6 должен быть от 1 до 128"\n'
    '# Предельный размер ответа в байтах: геобаза и всё остальное (списки сканеров, ответы по ASN).\n'
    'MAX_GEO_BYTES="${MAX_GEO_BYTES:-50000000}"; MAX_LIST_BYTES="${MAX_LIST_BYTES:-20000000}"\n'
    '[[ "$MAX_GEO_BYTES" =~ ^[1-9][0-9]{0,11}$ ]] || fail_keep "MAX_GEO_BYTES должен быть числом байт"\n'
    '[[ "$MAX_LIST_BYTES" =~ ^[1-9][0-9]{0,11}$ ]] || fail_keep "MAX_LIST_BYTES должен быть числом байт"\n',
    "пределы загрузок")
upd = replace_once(upd, '# fetch_asn ASN НАЗНАЧЕНИЕ\n',
    '# dl ПРЕДЕЛ_БАЙТ ФАЙЛ АРГУМЕНТЫ_CURL...: скачивает в файл не больше предела; больше — отказ (код 1).\n'
    'dl() {\n'
    '  local limit=$1 target=$2 code size\n'
    '  shift 2\n'
    '  command curl -fs --max-filesize "$limit" "$@" | head -c "$((limit + 1))" > "$target"\n'
    '  code=${PIPESTATUS[0]}\n'
    '  size=$(wc -c < "$target" 2>/dev/null || echo 0)\n'
    '  if [ "$size" -gt "$limit" ]; then\n'
    '    rm -f -- "$target"\n'
    '    echo "WARN: ответ больше предела $limit байт — отброшен" >&2\n'
    '    return 1\n'
    '  fi\n'
    '  return "$code"\n'
    '}\n\n'
    '# fetch_asn ASN НАЗНАЧЕНИЕ\n', "функция dl")
upd = replace_once(upd,
    """curl -fs --proto '=https' --connect-timeout 4 --max-time 10 --retry 1 --retry-delay 1 "https://stat.ripe.net/data/announced-prefixes/data.json?resource=$asn" > "$TMP/asn.json\"""",
    """dl "$MAX_LIST_BYTES" "$TMP/asn.json" --proto '=https' --connect-timeout 4 --max-time 10 --retry 1 --retry-delay 1 "https://stat.ripe.net/data/announced-prefixes/data.json?resource=$asn\"""",
    "лимит RIPEstat")
upd = replace_once(upd,
    """    curl -fsL --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$base/ipv4-aggregated.txt" > "$TMP/asn_alt" || continue
    curl -fsL --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$base/ipv6-aggregated.txt" >> "$TMP/asn_alt" || continue
""",
    """    dl "$MAX_LIST_BYTES" "$TMP/asn_alt" -L --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$base/ipv4-aggregated.txt" || continue
    dl "$MAX_LIST_BYTES" "$TMP/asn_alt6" -L --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$base/ipv6-aggregated.txt" || continue
    cat "$TMP/asn_alt6" >> "$TMP/asn_alt"
""", "лимит ipverse")
upd = replace_once(upd,
    """curl -fsL --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$url" > "$TMP/one" || continue""",
    """dl "$MAX_LIST_BYTES" "$TMP/one" -L --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$url" || continue""",
    "лимит списков сканеров")
upd = replace_once(upd,
    """if ! curl -fsL --proto '=https' --proto-redir '=https' --connect-timeout 8 --max-time 60 -o "$TMP/geo.dat" "$url"; then""",
    """if ! dl "$MAX_GEO_BYTES" "$TMP/geo.dat" -L --proto '=https' --proto-redir '=https' --connect-timeout 8 --max-time 60 "$url"; then""",
    "лимит геобазы")

# ------------------------------------------------ SSH-порт под геофильтром (1.7.0)
upd = replace_once(upd, '# 5. Сборка nft\n',
    '# SSH-порт под геофильтром: туда пускают только Россию и доверенные адреса, и это легко сделать по ошибке.\n'
    'ssh_ports_now=$(timeout 5 sshd -T 2>/dev/null | awk \'$1=="port" {print $2}\')\n'
    '[ -n "$ssh_ports_now" ] || ssh_ports_now=$(awk \'tolower($1)=="port" && $2 ~ /^[0-9]+$/ {print $2}\' '
    '/etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null)\n'
    '[ -n "$ssh_ports_now" ] || ssh_ports_now=22\n'
    'geo_ports_now=$(printf \'%s \' $GEO_PORTS)\n'
    'for ssh_port in $ssh_ports_now; do\n'
    '  case " $geo_ports_now" in\n'
    '    *" $ssh_port "*) echo "WARN: порт SSH $ssh_port входит в GEO_PORTS — вход по SSH будет только из России и с доверенных адресов; '
    'если так не задумано, уберите порт из GEO_PORTS" >&2 ;;\n'
    '  esac\n'
    'done\n\n'
    '# 5. Сборка nft\n', "предупреждение про SSH-порт")

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

# DDNS-имена в trusted*.txt: при каждом обновлении превращаются в адреса.
DDNS_BASH = r"""
# DDNS: DDNS-адреса из trusted*.txt превращаются в IP. Ответ принимается только из глобальных адресов,
# не больше 8 на DDNS-адрес; при сбое DNS берётся кэш не старше DDNS_CACHE_HOURS часов.
DDNS_CACHE_HOURS="${DDNS_CACHE_HOURS:-72}"
ddns_resolve() {
  local names="$TMP/ddns_names" out
  cat "$DIR/trusted.txt" "$DIR/trusted.local.txt" 2>/dev/null > "$names"
  : > "$TMP/ddns_ips"
  grep -qE '[A-Za-z]' "$names" || return 0
  if ! out=$(timeout 60 python3 - "$names" "$DIR/ddns-cache.txt" "$TMP/ddns_ips" "$DRY" "$DDNS_CACHE_HOURS" <<'PY_DDNS'
import ipaddress
import re
import socket
import sys
import threading
import time
from pathlib import Path

names_file, cache_file, out_file, dry, max_hours = sys.argv[1:6]
max_age = int(max_hours) * 3600
NAME = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_ADDRESSES = 8
now = int(time.time())

names = []
for line in Path(names_file).read_text(encoding="utf-8", errors="replace").splitlines():
    value = "".join(line.partition("#")[0].split()).lower()
    if not value:
        continue
    try:
        ipaddress.ip_network(value)
        continue
    except ValueError:
        pass
    if NAME.match(value) and value not in names:
        names.append(value)

cache = {}
try:
    for line in Path(cache_file).read_text(encoding="utf-8").splitlines():
        name, address, stamp = line.split()
        cache.setdefault(name, []).append((address, int(stamp)))
except (OSError, ValueError):
    cache = {}


def resolve(name):
    result = []

    def work():
        try:
            for item in socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP):
                result.append(item[4][0])
        except (OSError, UnicodeError):
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(8)
    return list(result)


new_cache = {}
addresses = []
for name in names:
    found = []
    for text in resolve(name):
        try:
            address = ipaddress.ip_address(text.split("%")[0])
        except ValueError:
            continue
        if address.is_global and not address.is_multicast and str(address) not in found:
            found.append(str(address))
    if found and len(found) <= MAX_ADDRESSES:
        new_cache[name] = [(address, now) for address in found]
        addresses.extend(found)
        continue
    reason = "ответ пустой или без глобальных адресов" if not found else f"слишком много адресов ({len(found)})"
    kept = [(address, stamp) for address, stamp in cache.get(name, []) if now - stamp <= max_age]
    if kept:
        new_cache[name] = kept
        addresses.extend(address for address, _ in kept)
        print(f"WARN: DDNS-адрес {name}: {reason}, использую прежние IP из кэша")
    else:
        print(f"WARN: DDNS-адрес {name}: {reason}, IP нет — в правила не попал")

Path(out_file).write_text("".join(address + "\n" for address in dict.fromkeys(addresses)), encoding="utf-8")
if dry != "1":
    try:
        Path(cache_file).write_text("".join(f"{name} {address} {stamp}\n"
                                            for name, items in new_cache.items() for address, stamp in items),
                                    encoding="utf-8")
    except OSError:
        print("WARN: не записался кэш DDNS-адресов")
PY_DDNS
  ); then
    note "WARN: не удалось разрешить DDNS-адреса из trusted, использую только обычные адреса"
    : > "$TMP/ddns_ips"
    return 0
  fi
  [ -z "$out" ] || while IFS= read -r line; do note "$line"; done <<< "$out"
}
ddns_resolve
"""

upd = replace_once(
    upd,
    "# Исключить из внешних блокировок сети, которые затрагивают доверенные или LAN.\n",
    DDNS_BASH.lstrip("\n") + "# Исключить из внешних блокировок сети, которые затрагивают доверенные или LAN.\n", "ddns_resolve")
upd = replace_once(
    upd, '{ cat "$TMP/lan_raw"; cat "$DIR/trusted.txt" "$DIR/trusted.local.txt" 2>/dev/null; } > "$TMP/protected_raw"',
    '{ cat "$TMP/lan_raw" "$TMP/ddns_ips"; cat "$DIR/trusted.txt" "$DIR/trusted.local.txt" 2>/dev/null; } > "$TMP/protected_raw"',
    "ddns в protected")
upd = replace_once(
    upd, 'cat "$DIR/trusted.txt" "$DIR/trusted.local.txt" 2>/dev/null > "$TMP/tr_raw"',
    'cat "$DIR/trusted.txt" "$DIR/trusted.local.txt" "$TMP/ddns_ips" 2>/dev/null > "$TMP/tr_raw"', "ddns в trusted")

# ----------------------------------------------------------------- fw-menu
menu = parts["menu"][2]
# Меню: ответ RIPEstat про ASN тоже ограничен по размеру.
menu = replace_once(menu, '''    result = subprocess.run(["curl", "-fsS", "--max-time", "30", url],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"RIPEstat не ответил для {asn}: {result.stderr.strip() or 'запрос не прошёл'}")
''', '''    result = subprocess.run(["curl", "-fsS", "--max-time", "30", "--max-filesize", str(MAX_REPLY_BYTES), url],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"RIPEstat не ответил для {asn}: {result.stderr.strip() or 'запрос не прошёл'}")
    if len(result.stdout) > MAX_REPLY_BYTES:
        raise ValueError(f"ответ RIPEstat для {asn} больше предела {MAX_REPLY_BYTES} байт")
''', "предел ответа RIPEstat в меню")

# Имя процесса из ss попадает на экран администратора: управляющие символы убираем (clean_text ниже, в блоке BOXED).
menu = replace_once(menu, '''    return "xray" if raw.startswith("xray-linux") else raw''',
    '''    return "xray" if raw.startswith("xray-linux") else clean_text(raw)''', "очистка имени процесса")
menu = replace_once(menu, """def probe_output(*command):
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=4, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
""", """def probe_output(*command, timeout=4, on_timeout=""):
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return on_timeout
    except OSError:
        return ""
""", "probe_output: необязательный таймаут")

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
import unicodedata

BOX_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Управляющие последовательности терминала: CSI («ESC [ …»), OSC («ESC ] … BEL/ST») и одиночные «ESC x».
TERMINAL_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]?")


def clean_text(value):
    # Чужой текст (whois, RIPEstat, PTR, имя процесса) без управляющих последовательностей и символов
    # категорий Cc/Cf (в том числе перевороты направления текста): на экран root попадает только видимое.
    text = TERMINAL_ESCAPES.sub("", str(value))
    return "".join(char for char in text if unicodedata.category(char) not in ("Cc", "Cf"))


def tidy_owner(owner):
    # Название владельца без ссылочной обвязки: «https://www.example.co/» превращается в «example.co».
    return re.sub(r"\bhttps?://(?:www\.)?", "", owner, flags=re.I).replace("/,", ",").rstrip("/ ")


def box_cells(raw):
    # Строка в виде списка (символ, цвет). Цвет хранится как накопленные коды ANSI.
    # Из остального текста убираются любые другие управляющие последовательности: цвет — единственное, что пропускаем.
    cells, color = [], ""
    position = 0
    for found in BOX_ANSI.finditer(raw):
        cells.extend((char, color) for char in clean_text(raw[position:found.start()]))
        code = found.group(0)
        color = "" if code in ("\x1b[0m", "\x1b[m") else color + code
        position = found.end()
    cells.extend((char, color) for char in clean_text(raw[position:]))
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


BOX_HEADINGS = (
    "Проверка файлов и команд",
    "Проверка reverse proxy",
    "Профиль сервера",
    "Служба и таймер",
    "Загруженные правила",
    "Списки адресов",
    "Как проверяется каждое новое входящее подключение",
    "Технический вид правил",
)


def print_box(title, text, headings=()):
    columns = shutil.get_terminal_size((100, 24)).columns
    limit = max(30, columns - 4)
    entries = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").strip("\n").split("\n"):
        raw = raw.rstrip()
        plain = BOX_ANSI.sub("", raw)
        if not plain and entries and entries[-1][0] == "rule":
            continue
        previous = next((item for item in reversed(entries) if item[0] == "rule" or item[1]), None)
        if (headings and plain.startswith(tuple(headings)) and len(plain) <= limit - 4
                and not (previous and previous[0] == "rule")):
            # Заголовок блока становится разделителем; пустая строка перед ним не нужна.
            while entries and entries[-1][0] == "line" and not entries[-1][1]:
                entries.pop()
            entries.append(("rule", plain) if entries else ("line", box_cells(raw)))
            continue
        for part in box_wrap(box_cells(raw), limit):
            entries.append(("line", part))
    if entries and entries[-1][0] == "rule":
        # Разделитель без текста под ним не нужен: оставляем строку как обычную.
        entries[-1] = ("line", box_cells(entries[-1][1]))
    inner = max([len(title) + 2] + [len(item) + (4 if kind == "rule" else 0) for kind, item in entries])
    print("┌─ " + title + " " + "─" * (inner + 2 - len(title) - 3) + "┐")
    for kind, item in entries:
        if kind == "rule":
            print("├─ " + item + " " + "─" * (inner + 2 - len(item) - 3) + "┤")
        else:
            print("│ " + box_render(item) + " " * (inner - len(item)) + " │")
    print("└" + "─" * (inner + 2) + "┘")


def boxed(title, function, headings=()):
    # Выводит всё, что печатает function, в рамке. Если функция спрашивает input(),
    # рамка закрывается до вопроса: приглашение и ответ идут уже без неё.
    def wrapper():
        if not (sys.stdout.isatty() and sys.stdin.isatty()):
            return function()
        import builtins
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
        state = {"saved": os.dup(1), "done": False}
        os.dup2(slave, 1)

        def finish():
            if state["done"]:
                return
            state["done"] = True
            sys.stdout.flush()
            os.dup2(state["saved"], 1)
            os.close(state["saved"])
            os.close(slave)
            thread.join(timeout=5)
            os.close(master)
            print_box(title, b"".join(chunks).decode("utf-8", "replace"), headings)

        real_input = builtins.input

        def boxed_input(*args, **kwargs):
            finish()
            return real_input(*args, **kwargs)

        builtins.input = boxed_input
        try:
            return function()
        finally:
            builtins.input = real_input
            finish()
    wrapper.__wrapped__ = function
    return wrapper


"""
menu = replace_once(menu, "def show_status():\n", BOXED + "def show_status():\n", "boxed_status")
menu = replace_once(menu, '("Статус файрвола", show_status),', '("Статус файрвола", boxed("Статус файрвола", show_status, BOX_HEADINGS)),', "boxed_entry")

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
# Номер версии берётся из верхнего заголовка «## X.Y.Z» в CHANGELOG.md: он записан в одном месте.
_top_version = re.search(r"^##\s+(\d+\.\d+\.\d+)\s*$", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
if not _top_version:
    sys.exit("в CHANGELOG.md нет заголовка вида «## 1.2.3»")
VERSION = _top_version.group(1)
VERSIONS_CODE = r'''import hashlib

FW_VERSION = "@@VERSION@@"
FW_REPO = "J0ll1er/Nftables-Script"
HASH_LINE = re.compile(r"^\s*sha256 fw-install\.sh:\s*([0-9a-fA-F]{64})\s*$", re.MULTILINE)
BACKUP_ROOT = Path("/root/fw-rollbacks")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
MAX_REPLY_BYTES = 20_000_000  # ответ RIPEstat про ASN
MAX_OWNER_BYTES = 1_000_000  # ответ RIPEstat про один адрес


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
    if len(result.stdout) > limit:
        raise OSError(f"ответ больше предела {limit} байт: {url}")
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


def published_hash(version):
    """(хеш установщика, причина). Хеш берётся из описания релиза на api.github.com: это другой хост,
    чем у самого скрипта (raw.githubusercontent.com, jsDelivr), поэтому сбой или подмена кэша CDN
    не подделывают оба сразу. Нет хеша — (None, причина)."""
    base = os.environ.get("FW_TEST_BASE") if os.environ.get("FW_ALLOW_FILE") == "1" else None
    url = (f"{base}/releases/v{version}.json" if base
           else f"https://api.github.com/repos/{FW_REPO}/releases/tags/v{version}")
    try:
        body = json.loads(fetch_text(url, 300_000)).get("body") or ""
    except (OSError, ValueError, AttributeError):
        return None, "описание релиза на api.github.com не получено"
    found = {value.lower() for value in HASH_LINE.findall(str(body))}
    if len(found) > 1:
        return "conflict", "противоречивые хеши в описании релиза"
    if not found:
        return None, "в описании релиза хеш не опубликован"
    return found.pop(), ""


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
    print("\nОбновить скрипт: пункт 5 → 2. Правила при обновлении не меняются.")


def install_version(version):
    try:
        installer = fetch_ref("v" + version, "fw-install.sh")
    except OSError:
        paint(31, f"ОШИБКА — версия {version} не найдена на GitHub (тег v{version}). Ничего не изменено.")
        return False
    if not installer.startswith("bash <<'INSTALL_FW'\n") or f"FW_VERSION='{version}'" not in installer:
        paint(31, f"ОШИБКА — скачанный файл не похож на установщик версии {version}. Ничего не изменено.")
        return False
    expected, reason = published_hash(version)
    actual = hashlib.sha256(installer.encode("utf-8")).hexdigest()
    if expected == "conflict":
        paint(31, f"ОШИБКА — {reason}: проверить скрипт нельзя, возможна подмена. Ничего не изменено.")
        return False
    if expected is None:
        paint(33, f"Контрольная сумма версии {version} не опубликована ({reason}).")
        print(f"Скачанный файл: sha256 {actual}")
        print("Установка без проверки доверяет только GitHub. Сверь хеш вручную на странице релиза "
              f"https://github.com/{FW_REPO}/releases/tag/v{version}")
        if input("Установить без проверки? Введи «без проверки»: ").strip().casefold() != "без проверки":
            print("Отменено. Ничего не изменено.")
            return False
    elif actual != expected:
        paint(31, f"ОШИБКА — контрольная сумма не совпала: скачано {actual[:16]}…, в релизе {expected[:16]}…. "
                  "Скрипт могли подменить или он повреждён. Ничего не изменено.")
        return False
    else:
        paint(32, f"Контрольная сумма совпала (sha256 {actual[:16]}…).")
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
        paint(31, "ОШИБКА — установщик завершился с ошибкой, причина выше. Вернуть прежнюю версию: пункт 5 → 3.")
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
        print("Локальных копий в /root/fw-rollbacks нет. Выбери версию с GitHub (пункт 5 → 3 → 2).")
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
    elif need_http and listening and exposed == "open" and not handled:
        lines.append("Порт 80 открыт постоянно, поэтому открывать его на время продления не нужно. "
                     "Хотите закрывать его между продлениями: настройте хуки certbot pre и post (открыть и закрыть порт в ufw)")
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
    "ip filter": "чужая, ведётся iptables-nft (обычно ufw или Docker), мы её не трогаем",
    "ip6 filter": "чужая, ведётся iptables-nft (обычно ufw или Docker), мы её не трогаем",
    "ip nat": "чужая, ведётся iptables-nft (обычно Docker), мы её не трогаем",
    "ip6 nat": "чужая, ведётся iptables-nft (обычно Docker), мы её не трогаем",
    "inet vps_warp": "чужая, Cloudflare WARP (подрезка MSS и метки пакетов), мы её не трогаем",
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
    names_by_port = {}
    for port, proto, name in outside:
        names_by_port.setdefault((port, proto), []).append(name)
    merged = []
    for (port, proto), names in sorted(names_by_port.items()):
        real = [name for name in names if name and name != "systemd"]
        if real and "systemd" in names:
            # Один и тот же порт слушают служба и сокет-активация systemd: показываем одной строкой.
            merged.append((port, proto, ", ".join(dict.fromkeys(real)) + " (через сокет systemd)"))
        else:
            merged.extend((port, proto, name) for name in dict.fromkeys(names))
    for port, proto, name in merged:
        state, sources = exposure(policy, proto, port)
        label = {"open": "открыт", "closed": "закрыт ufw", "limited": "ufw пускает только " + ", ".join(sources)}[state]
        print(f"  {proto.upper()} {port} — {name or 'процесс не определён'}, {label}")
    for port in ssh_ports():
        state, _ = exposure(policy, "tcp", port)
        suffix = {"open": "открыт", "closed": "в ufw закрыт — при включённом ufw доступ по SSH пропадёт",
                  "limited": "ограничен адресами"}[state]
        print(f"SSH: порт {port} — {suffix}")
    width = max(60, min(shutil.get_terminal_size((100, 24)).columns - 2, 110))
    for line in port80_report(policy, listeners):
        print(textwrap.fill(line, width=width, subsequent_indent="  ", break_long_words=False, break_on_hyphens=False))


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

OLD_SHOW_TRUSTED = r'''def show_trusted():
    for path in (TRUSTED, DIRECTORY / "trusted.local.txt"):
        if path.exists():
            print(f"{path}:")
            print("\n".join(entries(path)) or "(пусто)")
'''
NEW_TRUSTED = r'''TRUSTED_OLD_DAYS = 14


def trusted_records():
    # Записи обоих файлов доверенных: файл, адрес, комментарий, признак «добавлен установщиком» и дата.
    records = []
    for path in (TRUSTED, DIRECTORY / "trusted.local.txt"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            body, _, comment = line.partition("#")
            value = "".join(body.split())
            if not value:
                continue
            comment = comment.strip()
            found = re.search(r"\d{4}-\d{2}-\d{2}", comment)
            added = None
            if found:
                try:
                    added = datetime.date.fromisoformat(found[0])
                except ValueError:
                    added = None
            records.append({"path": path, "value": value, "auto": comment.startswith("авто"), "added": added})
    return records


def trusted_age(record):
    return (datetime.date.today() - record["added"]).days if record["added"] else None


def trusted_note(record):
    age = trusted_age(record)
    if record["auto"]:
        return f"  — добавлен установщиком {record['added']}, {age} дн. назад" if record["added"] else "  — добавлен установщиком"
    if record["added"]:
        return f"  — добавлен {record['added']}"
    return ""


def warn_old_trusted():
    for record in trusted_records():
        age = trusted_age(record)
        if record["auto"] and age is not None and age > TRUSTED_OLD_DAYS:
            paint(33, f"  Внимание: адрес {record['value']} добавлен установщиком {age} дн. назад. "
                      "Если он больше не нужен, удали его: «Настроить доступ» → «Удалить доверенные адреса».")


def show_trusted():
    records = trusted_records()
    for path in (TRUSTED, DIRECTORY / "trusted.local.txt"):
        if path.exists():
            print(f"{path}:")
            items = [record for record in records if record["path"] == path]
            if not items:
                print("(пусто)")
            for record in items:
                print(f"  {record['value']}{trusted_note(record)}")


def remove_trusted():
    records = trusted_records()
    if not records:
        print("Доверенных адресов нет.")
        return
    client = ssh_client()
    for number, record in enumerate(records, 1):
        mark = "  ← адрес этого подключения" if client and record["value"] == client else ""
        print(f"  {number}. {record['value']}{trusted_note(record)}{mark}")
    answer = input("Номера или адреса через пробел для удаления (Enter — отмена): ").strip()
    if not answer:
        return
    chosen = set()
    for token in answer.replace(",", " ").split():
        if token.isdigit() and 1 <= int(token) <= len(records):
            chosen.add(records[int(token) - 1]["value"])
        elif any(record["value"] == token for record in records):
            chosen.add(token)
        else:
            print(f"«{token}» в списке нет. Ничего не удалено.")
            return
    if input(f"Удалить из доверенных: {', '.join(sorted(chosen))}? Введи «да»: ").strip().casefold() != "да":
        print("Отменено.")
        return
    removed = []
    with (DIRECTORY / ".trusted.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Файлы перечитываются под блокировкой: удаляются строки с выбранными адресами, остальное не меняется.
        for path in (TRUSTED, DIRECTORY / "trusted.local.txt"):
            if not path.exists():
                continue
            kept = []
            for line in path.read_text(encoding="utf-8").splitlines():
                value = "".join(line.partition("#")[0].split())
                if value in chosen:
                    removed.append(value)
                else:
                    kept.append(line)
            write_text_atomic(path, "".join(item + "\n" for item in kept))
    print(f"Удалено: {', '.join(dict.fromkeys(removed))}.")
    if client and client in chosen:
        print("Это адрес вашего текущего подключения: если он не российский, после применения правил 443 для него закроется. SSH это не затрагивает.")
    print("Чтобы правила обновились, открой «Проверить и применить правила» → «Применить правила и включить таймер».")
'''
menu = replace_once(menu, OLD_SHOW_TRUSTED, NEW_TRUSTED, "доверенные адреса")

# DDNS-имена в доверенных и расписание «каждые N часов».
OLD_ADD_TRUSTED = r"""def add_trusted():
    value = input("IPv4, IPv6 или CIDR через пробел (или ssh для текущего подключения): ").strip()
    if not value:
        return
    if value.lower() == "ssh":
        value = ssh_client()
        if not value:
            print("Адрес SSH-подключения недоступен. Введи его вручную.")
            return
    networks = dict.fromkeys(parse_network(item) for item in value.split())

    with (DIRECTORY / ".trusted.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = set(trusted_networks())
        addresses = [network_text(network) for network in networks if network not in existing]
        if not addresses:
            print("Все указанные записи уже есть.")
            return
        append_entries(TRUSTED, addresses)
    print(f"Добавлено: {', '.join(addresses)}. Чтобы обновить правила, открой «Проверить и применить правила».")
"""
NEW_ADD_TRUSTED = r"""HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def parse_trusted(value):
    # Адрес или подсеть возвращаются как сеть, иначе допускается только имя хоста (DDNS), например home.example.com.
    try:
        ipaddress.ip_network(value)
    except ValueError:
        name = value.lower()
        if HOSTNAME_RE.match(name):
            return name
        raise ValueError(f"«{value}» — не IP-адрес, не подсеть и не DDNS-адрес вида home.example.com")
    return parse_network(value)


def trusted_names():
    names = []
    for path in (TRUSTED, DIRECTORY / "trusted.local.txt"):
        for value in entries(path):
            name = value.lower()
            try:
                ipaddress.ip_network(value)
            except ValueError:
                if HOSTNAME_RE.match(name) and name not in names:
                    names.append(name)
    return names


def add_trusted():
    value = input("IPv4, IPv6, CIDR или DDNS-адрес через пробел (или ssh для текущего подключения): ").strip()
    if not value:
        return
    if value.lower() == "ssh":
        value = ssh_client()
        if not value:
            print("Адрес SSH-подключения недоступен. Введи его вручную.")
            return
    items = dict.fromkeys(parse_trusted(item) for item in value.split())

    with (DIRECTORY / ".trusted.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = set(trusted_networks()) | set(trusted_names())
        addresses = [item if isinstance(item, str) else network_text(item) for item in items if item not in existing]
        if not addresses:
            print("Все указанные записи уже есть.")
            return
        append_entries(TRUSTED, addresses)
    print(f"Добавлено: {', '.join(addresses)}. Чтобы обновить правила, открой «Проверить и применить правила».")
    if any(isinstance(item, str) for item in items):
        print("DDNS-адрес превращается в IP при каждом плановом обновлении. Чтобы смену IP подхватывало быстрее,")
        print("задай частоту: «Проверить и применить правила» → «Настроить расписание обновления» → 3 (раз в 3 часа).")
"""
menu = replace_once(menu, OLD_ADD_TRUSTED, NEW_ADD_TRUSTED, "добавление доверенных и DDNS-имён")

HOURS_CHOICES = "2, 3, 4, 6, 8 или 12"
menu = replace_once(menu,
    '    value = input("Введи время ЧЧ:ММ по часам VPS, «сброс» для штатного графика или Enter для отмены: ").strip().casefold()\n'
    '    if not value:\n        return\n'
    '    if value != "сброс" and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):\n'
    '        raise ValueError("введи время в формате ЧЧ:ММ от 00:00 до 23:59")\n',
    '    print("Раз в сутки: время ЧЧ:ММ по часам VPS. Чаще: число часов — 2, 3, 4, 6, 8 или 12 (каждые N часов).")\n'
    '    value = input("Введи время, число часов, «сброс» для штатного графика или Enter для отмены: ").strip().casefold()\n'
    '    if not value:\n        return\n'
    '    hours = re.fullmatch(r"(?:каждые\\s+)?(\\d{1,2})(?:\\s*(?:ч|час|часа|часов))?", value)\n'
    '    if hours and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):\n'
    '        if int(hours[1]) not in (2, 3, 4, 6, 8, 12):\n'
    '            raise ValueError("частота: 2, 3, 4, 6, 8 или 12 часов (меньше нельзя: каждое обновление скачивает списки)")\n'
    '        value = f"every:{int(hours[1])}"\n'
    '    elif value != "сброс" and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):\n'
    '        raise ValueError("введи время ЧЧ:ММ от 00:00 до 23:59 или число часов: 2, 3, 4, 6, 8, 12")\n',
    "ввод расписания")
menu = replace_once(menu,
    '            write_text_atomic(SCHEDULE_FILE, f"[Timer]\\nOnCalendar=\\nOnCalendar=*-*-* {value}:00\\n"\n',
    '            calendar = f"0/{value[6:]}:00:00" if value.startswith("every:") else f"{value}:00"\n'
    '            write_text_atomic(SCHEDULE_FILE, f"[Timer]\\nOnCalendar=\\nOnCalendar=*-*-* {calendar}\\n"\n',
    "запись расписания")
menu = replace_once(menu, '    print(f"  Доверенных адресов и подсетей — {len(set(trusted_networks()))}")\n',
    '    print(f"  Доверенных адресов и подсетей — {len(set(trusted_networks()))}")\n'
    '    if trusted_names():\n        print(f"  Доверенных DDNS-адресов — {len(trusted_names())} (IP подставляется при каждом обновлении)")\n'
    '    warn_old_trusted()\n', "предупреждение о старых")
menu = replace_once(menu, '("Показать доверенные адреса", show_trusted),',
    '("Показать доверенные адреса", show_trusted),\n            ("Удалить доверенные адреса", remove_trusted),', "пункт удаления")

# Рамка вокруг пунктов, которые только выводят информацию (с вопросом input() рамка закрывается до вопроса).
for old, title in (
    ('("Показать доверенные адреса", show_trusted),', "Доверенные адреса"),
    ('("Показать пути к файлам на VPS", show_file_paths),', "Пути к файлам"),
    ('("Журнал ошибок", show_error_log),', "Журнал ошибок"),
    ('("Версии и что исправлено", show_versions),', "Версии"),
    ('("Что ещё есть на сервере", show_server_overview),', "Что ещё есть на сервере"),
):
    name = old.split(", ")[-1].rstrip("),")
    new = old.replace(name + "),", f'boxed("{title}", {name})),')
    menu = replace_once(menu, old, new, "рамка " + title)
HEALTH_CHECK = r'''def health_check():
    """Одна строка «ok|warn|error: причина» и код выхода 0/1/2 — для мониторинга."""
    if not quiet("nft", "-t", "list", "table", "inet", "fw"):
        print("error: таблица inet fw не загружена, фаервол не работает")
        return 2
    result = last_result()
    if result == "error":
        print("error: последнее обновление списков закончилось ошибкой, см. журнал ошибок")
        return 2
    if result == "warn":
        print("warn: последнее обновление списков прошло с предупреждениями")
        return 1
    if not quiet("systemctl", "is-active", "--quiet", "fw-update.timer"):
        print("warn: таймер обновления fw-update.timer не запущен")
        return 1
    if result != "ok":
        print("warn: результат последнего обновления неизвестен")
        return 1
    print("ok: правила загружены, последнее обновление без ошибок")
    return 0


'''
menu = replace_once(menu, "def ask_confirmation(timeout):", HEALTH_CHECK + "def ask_confirmation(timeout):", "health_check")
menu = replace_once(menu, '    if os.geteuid() != 0:\n        raise SystemExit("Запусти fw-menu от root.")\n',
    '    if sys.argv[1:] == ["--check"]:\n        if os.geteuid() != 0:\n            print("error: для --check нужен root")\n            raise SystemExit(2)\n        raise SystemExit(health_check())\n'
    '    if os.geteuid() != 0:\n        raise SystemExit("Запусти fw-menu от root.")\n', "--check")
menu = replace_once(menu, '    if os.geteuid() != 0:\n        raise SystemExit("Запусти fw-menu от root.")\n',
    '    if sys.argv[1:] == ["--version"]:\n        print(FW_VERSION)\n        return\n'
    '    if os.geteuid() != 0:\n        raise SystemExit("Запусти fw-menu от root.")\n', "--version")

# Меню 1.6.0: шесть пунктов, подменю «Статус и наблюдение», шапка с состоянием.
OLD_SHOW_MENU = """def show_menu(title, entries, exit_label="Назад"):
    choices = {str(number): entry for number, entry in enumerate(entries, start=1)}
    while True:
        print(f"\\n{title}")
        for number, (label, _) in choices.items():
            print(f"{number}. {label}")
        print(f"0. {exit_label}")
        try:
            choice = input("Выбор: ").strip()
"""
NEW_SHOW_MENU = r"""MAIN_MENU_TITLE = "Файрвол IPv4 / IPv6"


def stdout_is_tty():
    return sys.stdout.isatty()


def menu_color():
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def tint(code, text, color):
    return f"\033[{code}m{text}\033[0m" if color else text


def menu_header_text(color=None):
    # Короткое состояние файрвола для шапки меню. Любой сбой не должен ломать меню.
    color = menu_color() if color is None else color
    codes = {"ok": "32", "warn": "33", "bad": "31", "off": "90"}
    try:
        try:
            loaded = quiet("nft", "-t", "list", "table", "inet", "fw")
        except (OSError, subprocess.SubprocessError):
            loaded = False
        rules = ("ok", "Правила работают") if loaded else ("bad", "Правила не загружены")
        result = last_result()
        last = {"ok": ("ok", "Последнее обновление: успешно"), "warn": ("warn", "Последнее обновление: есть замечания"),
                "error": ("bad", "Последнее обновление: ошибка")}.get(result, ("off", "Последнее обновление: ещё не было"))
        count = len(set(trusted_networks())) + len(trusted_names())
        trusted = ("off", f"Доверенных: {count}")
        try:
            properties = systemd_properties("fw-update.timer", "ActiveState", "NextElapseUSecRealtime")
        except (OSError, subprocess.SubprocessError):
            properties = {}
        match = re.search(r"[0-9]{4}-([0-9]{2})-([0-9]{2}) ([0-9]{2}:[0-9]{2}):[0-9]{2}", properties.get("NextElapseUSecRealtime", ""))
        if properties.get("ActiveState") == "active" and match:
            timer = ("ok", f"Следующее обновление: {match[2]}.{match[1]} в {match[3]}")
        elif properties.get("ActiveState") == "active":
            timer = ("warn", "Следующее обновление: не запланировано")
        else:
            timer = ("warn", "Таймер выключен")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""

    def cell(item):
        return f"{tint(codes[item[0]], '●', color)} {item[1]}"
    columns = shutil.get_terminal_size((100, 24)).columns
    pairs = ((rules, last), (trusted, timer))
    if columns < 72:
        return "\n".join(cell(item) for pair in pairs for item in pair)
    width = max(2 + len(left[1]) for left, _ in pairs) + 3
    return "\n".join(cell(left) + " " * (width - 2 - len(left[1])) + cell(right) for left, right in pairs)


def show_menu(title, entries, exit_label="Назад"):
    choices = {str(number): entry for number, entry in enumerate(entries, start=1)}
    while True:
        if stdout_is_tty():
            color = menu_color()
            print()
            header = menu_header_text(color) if title == MAIN_MENU_TITLE else ""
            if header:
                print_box(f"{title} · v{FW_VERSION}", header)
            else:
                print(tint("90", f"── {title} " + "─" * max(4, 54 - len(title)), color))
            print()
            for number, (label, _) in choices.items():
                print(f"  {tint('1;36', number, color)}  {label}")
            print(f"  {tint('90', '0', color)}  {tint('90', exit_label, color)}\n")
            prompt = "Выбор › "
        else:
            print(f"\n{title}")
            for number, (label, _) in choices.items():
                print(f"{number}. {label}")
            print(f"0. {exit_label}")
            prompt = "Выбор: "
        try:
            choice = input(prompt).strip()
"""
menu = replace_once(menu, OLD_SHOW_MENU, NEW_SHOW_MENU, "новый вид меню")

OLD_ENTRIES_HEAD = '    entries = (\n        ("Статус файрвола", boxed("Статус файрвола", show_status, BOX_HEADINGS)),\n'
NEW_ENTRIES_HEAD = (
    '    entries = (\n        ("Статус и наблюдение", (\n'
    '            ("Статус файрвола", boxed("Статус файрвола", show_status, BOX_HEADINGS)),\n'
    '            ("Кто стучится", boxed("Кто стучится", show_hits, HITS_HEADINGS)),\n'
    '            ("Журнал ошибок", boxed("Журнал ошибок", show_error_log)),\n'
    '            ("Что ещё есть на сервере", boxed("Что ещё есть на сервере", show_server_overview)),\n'
    '        )),\n')
menu = replace_once(menu, OLD_ENTRIES_HEAD, NEW_ENTRIES_HEAD, "подменю наблюдения")
OLD_TAIL = """        ("Отключить или удалить", (
            ("Удалить правила и автообновление", remove_rules),
            ("Удалить скрипт и настройки", uninstall),
        )),
        ("Журнал ошибок", boxed("Журнал ошибок", show_error_log)),
        ("Версии и обновление скрипта", (
            ("Версии и что исправлено", boxed("Версии", show_versions)),
            ("Обновить скрипт до последней версии", update_script),
            ("Откатить скрипт к другой версии", rollback_script),
        )),
        ("Что ещё есть на сервере", boxed("Что ещё есть на сервере", show_server_overview)),
    )
    try:
        try:
            _, _, sizes = file_inventory()
            total = sum(size for size in sizes.values() if size is not None)
            print(f"Размер файлов файрвола — {format_size(total)}")
        except OSError:
            print("Размер файлов файрвола — не удалось определить")
        show_menu("""
NEW_TAIL = """        ("Версии и обновление скрипта", (
            ("Версии и что исправлено", boxed("Версии", show_versions)),
            ("Обновить скрипт до последней версии", update_script),
            ("Откатить скрипт к другой версии", rollback_script),
        )),
        ("Отключить или удалить", (
            ("Удалить правила и автообновление", remove_rules),
            ("Удалить скрипт и настройки", uninstall),
        )),
    )
    try:
        show_menu("""
menu = replace_once(menu, OLD_TAIL, NEW_TAIL, "порядок пунктов и заголовок")

# ------------------------------------------------------ «Кто стучится» (1.6.0)
# Статистика: динамические наборы nft ss4/ss6 (сканеры) и sn4/sn6 (не из России
# на защищённых портах) считают пакеты по паре «адрес + порт». Правила drop не
# меняются: перед ними добавлена отдельная строка без вердикта. Накопленное
# переносится в новую таблицу при каждом обновлении; если nft или ядро не умеют
# такие наборы, правила собираются как раньше, без статистики.
upd = replace_once(upd, '''P="{ $(echo $GEO_PORTS | tr ' ' ',') }"
{
  echo "add table inet fw"
''', r'''P="{ $(echo $GEO_PORTS | tr ' ' ',') }"
# Статистика «Кто стучится»: наборы со счётчиками по паре «адрес + порт».
# stat_elements: из вывода `nft list set` берёт записи для переноса. У разных версий nft между
# счётчиком и сроком может стоять «timeout 7d»; в перенос оно не нужно (срок задаёт сам набор).
stat_elements() {
  grep -oE '[0-9a-fA-F:.]+ \. [0-9]+ counter packets [0-9]+ bytes [0-9]+( timeout [0-9a-z]+)? expires [0-9a-z]+' \
    | sed -E 's/ timeout [0-9a-z]+//' | head -n 65536
}
stat_set() {
  echo "  set $1 { type $2 . inet_service; size 65536; flags dynamic,timeout; timeout 7d; counter;"
  [ -s "$TMP/stats.$1" ] && echo "    elements = { $(paste -sd, "$TMP/stats.$1") }"
  echo "  }"
}
emit_rules() {
  echo "add table inet fw"
''', "статистика: начало сборки")
upd = replace_once(upd, '''  set_def ap4 ipv4_addr "$TMP/ap4"; set_def ap6 ipv6_addr "$TMP/ap6"
''', r'''  set_def ap4 ipv4_addr "$TMP/ap4"; set_def ap6 ipv6_addr "$TMP/ap6"
  if [ "$STATS" = 1 ]; then
    stat_set ss4 ipv4_addr; stat_set ss6 ipv6_addr; stat_set sn4 ipv4_addr; stat_set sn6 ipv6_addr
  fi
''', "статистика: наборы")
upd = replace_once(upd, r'''  echo "    ip saddr @sc4 counter drop comment \"scanners\""
  echo "    ip6 saddr @sc6 counter drop comment \"scanners\""
  echo "    meta l4proto { tcp, udp } th dport $P ip saddr != @ru4 counter drop comment \"non-RU\""
''', r'''  if [ "$STATS" = 1 ]; then
    echo "    meta l4proto { tcp, udp } ip saddr @sc4 add @ss4 { ip saddr . th dport }"
    echo "    meta l4proto { tcp, udp } ip6 saddr @sc6 add @ss6 { ip6 saddr . th dport }"
  fi
  echo "    ip saddr @sc4 counter drop comment \"scanners\""
  echo "    ip6 saddr @sc6 counter drop comment \"scanners\""
  if [ "$STATS" = 1 ]; then
    echo "    meta l4proto { tcp, udp } th dport $P ip saddr != @ru4 add @sn4 { ip saddr . th dport }"
    echo "    meta l4proto { tcp, udp } th dport $P ip6 saddr != @ru6 add @sn6 { ip6 saddr . th dport }"
  fi
  echo "    meta l4proto { tcp, udp } th dport $P ip saddr != @ru4 counter drop comment \"non-RU\""
''', "статистика: правила")
upd = replace_once(upd, '''} > "$TMP/new.nft"
''', r'''}
# Накопленную статистику переносим из действующей таблицы. Если что-то не
# принялось, сначала собираем без перенесённых записей, затем без статистики:
# правила важнее неё и всегда применяются.
STATS=1
for stat in ss4 ss6 sn4 sn6; do
  : > "$TMP/stats.$stat"
  nft -n list set inet fw "$stat" 2>/dev/null | stat_elements > "$TMP/stats.$stat" || true
done
emit_rules > "$TMP/new.nft"
if ! nft -c -f "$TMP/new.nft" >/dev/null 2>&1; then
  for stat in ss4 ss6 sn4 sn6; do : > "$TMP/stats.$stat"; done
  emit_rules > "$TMP/new.nft"
  if ! nft -c -f "$TMP/new.nft" >/dev/null 2>&1; then
    STATS=0
    emit_rules > "$TMP/new.nft"
  fi
fi
''', "статистика: сборка и откат")

# ----------------------------------------- ограничение частоты SSH (1.7.0)
# Необязательно, по умолчанию выключено: SSH_RATE_LIMIT=N (новых подключений в минуту с одного
# адреса; 0 или пусто — выключено), SSH_PORTS (по умолчанию порты из sshd -T, иначе 22).
# Считаются только новые подключения: уже открытые сессии, доверенные адреса и LAN_ALLOW не затрагиваются.
upd = replace_once(upd, 'LAN_ALLOW="${LAN_ALLOW-}"\n',
                   'LAN_ALLOW="${LAN_ALLOW-}"\n'
                   'SSH_RATE_LIMIT="${SSH_RATE_LIMIT:-0}"; SSH_PORTS="${SSH_PORTS-}"\n', "SSH: настройки")
upd = replace_once(upd, '[ "$HOOK" = input ] || [ "$HOOK" = prerouting ] || fail_keep "HOOK должен быть input или prerouting"\n',
                   '[ "$HOOK" = input ] || [ "$HOOK" = prerouting ] || fail_keep "HOOK должен быть input или prerouting"\n'
                   '[[ "$SSH_RATE_LIMIT" =~ ^[0-9]{1,4}$ ]] || fail_keep "SSH_RATE_LIMIT: число новых подключений в минуту (0 — выключено)"\n'
                   'for ssh_port in $SSH_PORTS; do\n'
                   '  [[ "$ssh_port" =~ ^[0-9]{1,5}$ ]] && [ "$ssh_port" -ge 1 ] && [ "$ssh_port" -le 65535 ] \\\n'
                   '    || fail_keep "SSH_PORTS: порты от 1 до 65535"\n'
                   'done\n', "SSH: проверка настроек")
upd = replace_once(upd, 'emit_rules() {\n  echo "add table inet fw"\n',
                   r'''# Порты SSH: из sshd -T (действующая настройка), иначе из sshd_config, иначе 22.
ssh_ports_detect() {
  local found
  found=$(sshd -T 2>/dev/null | awk '$1 == "port" && $2 ~ /^[0-9]+$/ { print $2 }' | sort -un | tr '\n' ' ')
  [ -n "$found" ] || found=$(awk 'tolower($1) == "port" && $2 ~ /^[0-9]+$/ { print $2 }' /etc/ssh/sshd_config 2>/dev/null | sort -un | tr '\n' ' ')
  echo "${found:-22}"
}
SSH_RULES=0
if [ "$SSH_RATE_LIMIT" -gt 0 ]; then
  SSH_LIST=$(echo ${SSH_PORTS:-$(ssh_ports_detect)})
  SSH_BURST=$(( SSH_RATE_LIMIT / 2 )); [ "$SSH_BURST" -ge 5 ] || SSH_BURST=5
  SP="{ $(echo $SSH_LIST | tr ' ' ',') }"
  SSH_RULES=1
  # Проверка на старом nft: если синтаксис meter не принят, правило не добавляется, остальное работает.
  if ! printf 'add table inet fw_probe\nadd chain inet fw_probe c { type filter hook input priority 0; }\nadd rule inet fw_probe c tcp dport %s ct state new meter p { ip saddr timeout 5m limit rate over 20/minute burst 5 packets } drop\nadd rule inet fw_probe c tcp dport %s ct state new meter q { ip6 saddr & ffff:ffff:ffff:ffff:: timeout 5m limit rate over 20/minute burst 5 packets } drop\n' "$SP" "$SP" | nft -c -f - >/dev/null 2>&1; then
    SSH_RULES=0
    note "WARN: ограничение частоты SSH не применено: эта версия nft не принимает такое правило"
  fi
fi
emit_rules() {
  echo "add table inet fw"
''', "SSH: функции и проверка nft")
upd = replace_once(upd, '''  if [ "$STATS" = 1 ]; then
    echo "    meta l4proto { tcp, udp } th dport $P ip saddr != @ru4 add @sn4 { ip saddr . th dport }"
''', '''  if [ "$SSH_RULES" = 1 ]; then
    # Отбрасывается только избыток: больше SSH_RATE_LIMIT новых подключений в минуту с одного адреса
    # (для IPv6 считается вся сеть /64). Через минуту доступ возвращается сам.
    echo "    tcp dport $SP ct state new meter ssh4 { ip saddr timeout 5m limit rate over $SSH_RATE_LIMIT/minute burst $SSH_BURST packets } counter drop comment \\"ssh-rate\\""
    echo "    tcp dport $SP ct state new meter ssh6 { ip6 saddr & ffff:ffff:ffff:ffff:: timeout 5m limit rate over $SSH_RATE_LIMIT/minute burst $SSH_BURST packets } counter drop comment \\"ssh-rate\\""
  fi
  if [ "$STATS" = 1 ]; then
    echo "    meta l4proto { tcp, udp } th dport $P ip saddr != @ru4 add @sn4 { ip saddr . th dport }"
''', "SSH: правила")
upd = replace_once(upd, '''wide_finish
if [ "$DRY" = 1 ]''', '''[ "$SSH_RULES" = 0 ] || echo "SSH: не больше $SSH_RATE_LIMIT новых подключений в минуту с одного адреса (порты: $SSH_LIST)"
wide_finish
if [ "$DRY" = 1 ]''', "SSH: строка итога")

menu = replace_once(menu, "import datetime\nimport fcntl\n", "import concurrent.futures\nimport datetime\nimport fcntl\n", "импорт потоков")
menu = replace_once(menu, "import tempfile\nimport textwrap\n", "import tempfile\nimport textwrap\nimport threading\n", "импорт блокировки")
menu = replace_once(menu, "def show_status():\n", r'''HIT_SETS = (("scanners", "ss4"), ("scanners", "ss6"), ("non_ru", "sn4"), ("non_ru", "sn6"))
HIT_RE = re.compile(r"([0-9a-fA-F:.]+) \. (\d+) counter packets (\d+) bytes (\d+)")
PORT_NAMES = {
    21: "FTP", 22: "SSH", 23: "Telnet", 25: "почта (SMTP)", 53: "DNS", 80: "HTTP", 110: "почта (POP3)",
    143: "почта (IMAP)", 443: "HTTPS", 445: "SMB, общие папки Windows", 587: "почта (SMTP)",
    993: "почта (IMAPS)", 1433: "MS SQL", 1521: "Oracle", 1723: "PPTP VPN", 2375: "Docker без защиты",
    3306: "MySQL", 3389: "RDP, удалённый рабочий стол", 5060: "SIP, телефония", 5432: "PostgreSQL",
    5900: "VNC, удалённый экран", 6379: "Redis", 8080: "HTTP (запасной)", 8443: "HTTPS (запасной)",
    9200: "Elasticsearch", 11211: "Memcached", 27017: "MongoDB",
}


def parse_hits(text):
    """Строки «адрес . порт counter packets N bytes M» из вывода `nft list set`."""
    return [(address, int(port), int(packets), int(size)) for address, port, packets, size in HIT_RE.findall(text)]


HIT_AGE_RE = re.compile(r"([0-9a-fA-F:.]+) \. \d+ counter packets \d+ bytes \d+(?: timeout \S+)? expires ([0-9dhms]+)")
HIT_TTL = 7 * 24 * 3600  # срок записи в наборах статистики: с него отсчитывается «возраст» записи
DAY = 24 * 3600


def parse_duration(text):
    """«6d23h59m1s», «259197s» или «1d» в секунды; непонятное значение даёт None."""
    parts = re.findall(r"(\d+)([dhms])", text)
    if not parts or "".join(number + unit for number, unit in parts) != text:
        return None
    return sum(int(number) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit] for number, unit in parts)


def format_left(seconds):
    """Остаток времени словами: «2 д 5 ч 7 мин»."""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days} д {hours} ч {rest // 60} мин"


def parse_hit_ages(text):
    """{адрес: возраст самой старой записи в секундах}. Запись не продлевается (правило `add`, а не `update`),
    поэтому возраст = срок набора минус остаток срока: сколько прошло с первого отклонённого пакета."""
    ages = {}
    for address, expires in HIT_AGE_RE.findall(text):
        left = parse_duration(expires)
        if left is not None:
            ages[address] = max(ages.get(address, 0), max(0, HIT_TTL - left))
    return ages


HITS_READ_TIMEOUT = 30  # полный набор (65536 записей) на слабом сервере читается дольше обычных 4 секунд


def set_is_full(text):
    """Набор статистики заполнен: ядро не добавляет новые записи, пока старые не истекут."""
    match = re.search(r"^\s*size (\d+)\s*$", text, re.M)
    return bool(match) and int(match.group(1)) > 0 and len(HIT_RE.findall(text)) >= int(match.group(1))


def summarize_hits(rows, top=5):
    by_port, by_source, ports_of = {}, {}, {}
    for address, port, packets, _size in rows:
        by_port[port] = by_port.get(port, 0) + packets
        by_source[address] = by_source.get(address, 0) + packets
        ports_of.setdefault(address, set()).add(port)
    ports = sorted(by_port.items(), key=lambda item: (-item[1], item[0]))[:top]
    sources = sorted(by_source.items(), key=lambda item: (-item[1], item[0]))[:top]
    shown_packets = sum(packets for _port, packets in ports)
    return {"packets": sum(by_port.values()), "sources": len(by_source), "ports": ports,
            "other_ports": len(by_port) - len(ports), "other_packets": sum(by_port.values()) - shown_packets,
            "top_sources": [(address, packets, len(ports_of[address])) for address, packets in sources]}


def port_label(port):
    return PORT_NAMES.get(port, "")


def share(part, total):
    """Доля в целых процентах; настоящая, но малая — «<1%», чтобы строка не казалась пустой."""
    if total <= 0:
        return ""
    percent = round(100 * part / total)
    return "<1%" if part > 0 and percent == 0 else f"{percent}%"


def ports_word(count):
    if count % 100 in range(11, 15):
        return "портов"
    return {1: "порт", 2: "порта", 3: "порта", 4: "порта"}.get(count % 10, "портов")


OWNER_LOOKUPS = 15  # владельца узнаём у стольких самых активных адресов; остальные идут одной строкой
OWNER_WORKERS = 6
OTHER_ADDRESSES = "другие адреса"


def group_by_owner(rows, owner_for):
    """Строки счётчиков -> группы по владельцу: адреса, попытки, три главных порта. Самые активные первыми."""
    groups = {}
    for address, port, packets, _size in rows:
        group = groups.setdefault(owner_for(address), {"addresses": set(), "packets": 0, "by_port": {}})
        group["addresses"].add(address)
        group["packets"] += packets
        group["by_port"][port] = group["by_port"].get(port, 0) + packets
    result = []
    for owner, group in groups.items():
        ports = sorted(group["by_port"].items(), key=lambda item: (-item[1], item[0]))[:3]
        result.append({"owner": owner, "addresses": len(group["addresses"]), "packets": group["packets"],
                       "ports": [port for port, _count in ports]})
    result.sort(key=lambda group: (-group["packets"], group["owner"]))
    return result


def fit_ports(prefix, count, ports, room):
    """«N адр.  порты: 443, 8443» целиком, а если не влезает в room, то с меньшим числом портов."""
    head = f"  {count} адр."
    shown = list(ports)
    while shown:
        text = f"{head}  порты: {', '.join(str(port) for port in shown)}"
        if len(prefix) + len(text) <= room:
            return text
        shown.pop()
    return head


def hits_bar(value, maximum, width=10):
    # Полоска из отдельных палочек (как у индикаторов доступности): width штук через пробел.
    if maximum <= 0 or value <= 0:
        filled = 0
    else:
        filled = max(1, round(width * value / maximum))
    return " ".join(["▮"] * filled + ["▯"] * (width - filled))


def hits_bar_view(value, maximum, width, color):
    bar = hits_bar(value, maximum, width)
    if not color:
        return bar
    filled = bar.count("▮")
    head = " ".join(["▮"] * filled)
    tail = bar[len(head):]
    return tint("32", head, True) + tint("90", tail, True)


SOURCE_LABELS = {
    "skipa_cidr.txt": "СКИПА", "skipa.list": "СКИПА", "antiscanner.list": "антисканер",
    "censys_v4.txt": "Censys", "shodan_v4.txt": "Shodan", "paloaltonetworks_v4.txt": "Palo Alto Networks",
    "shadowserver_v4.txt": "Shadowserver", "driftnet_v4.txt": "Driftnet", "onyphe_v4.txt": "ONYPHE",
    "zoomeye_v4.txt": "ZoomEye", "leakix_v4.txt": "LeakIX", "rapid7_v4.txt": "Rapid7",
    "fofa_v4.txt": "FOFA", "quake_v4.txt": "Quake", "strechoid_v4.txt": "Stretchoid", "extra.txt": "ваш список",
}


def source_label(name):
    if name in SOURCE_LABELS:
        return SOURCE_LABELS[name]
    if name.startswith("custom_"):
        return "свой список"
    match = re.fullmatch(r"asn_(AS\d+)\.cache", name)
    return match.group(1) if match else name


def load_scanner_sources():
    """Копии списков сканеров, сохранённые скриптом: [(подпись, [сети])]. Ошибки чтения пропускаются."""
    files = []
    try:
        files += sorted((DIRECTORY / "scanner-cache").iterdir())
        files += sorted(DIRECTORY.glob("asn_AS*.cache"))
        files.append(DIRECTORY / "extra.txt")
    except OSError:
        pass
    sources = []
    for path in files:
        networks = []
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                value = line.partition("#")[0].strip()
                if not value:
                    continue
                try:
                    networks.append(ipaddress.ip_network(value, strict=False))
                except ValueError:
                    continue
        except OSError:
            continue
        if networks:
            sources.append((source_label(path.name), networks))
    return sources


def owner_of(address, sources):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return ""
    found = []
    for label, networks in sources:
        if label not in found and any(network.version == ip.version and ip in network for network in networks):
            found.append(label)
    return ", ".join(found[:2])


def short_host(name):
    labels = [part for part in name.strip(".").split(".") if part]
    if len(labels) <= 2:
        return ".".join(labels)
    keep = 3 if len(labels[-1]) == 2 and labels[-2] in {"co", "com", "net", "org", "ac", "gov"} else 2
    return ".".join(labels[-keep:])


def reverse_name(address):
    """Имя из обратной DNS-записи (домен провайдера), если ответ пришёл быстро."""
    try:
        result = subprocess.run(["getent", "hosts", address], text=True, capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    fields = result.stdout.split()
    return short_host(clean_text(fields[1])) if result.returncode == 0 and len(fields) > 1 else ""


OWNER_CACHE_FILE = "owner-cache.json"
OWNER_CACHE_DAYS = 30
OWNER_CACHE_LIMIT = 500
OWNER_CACHE_LOCK = threading.Lock()
OWNER_TEXT_WIDTH = 40


def fetch_ripe(address):
    """Ответ RIPEstat про адрес (номер AS и владелец); пусто, если сеть не ответила."""
    url = f"https://stat.ripe.net/data/prefix-overview/data.json?resource={address}"
    try:
        result = subprocess.run(["curl", "-fsS", "--proto", "=https", "--connect-timeout", "3", "--max-time", "4",
                                 "--max-filesize", str(MAX_OWNER_BYTES), url],
                                text=True, capture_output=True, timeout=6, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 and len(result.stdout) <= MAX_OWNER_BYTES else ""


def parse_ripe_owner(text):
    try:
        asns = json.loads(text)["data"]["asns"]
        first = asns[0]
        holder = clean_text(first["holder"] or "").strip()  # у части AS владелец не указан (null)
        # «ИМЯ-AS-BLOCK - Читаемое название»: показываем читаемую часть
        holder = holder.split(" - ", 1)[1].strip() if " - " in holder and holder.split(" - ", 1)[1].strip() else holder
        return f"{holder} AS{int(first['asn'])}" if holder else f"AS{int(first['asn'])}"
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def fetch_whois(address):
    """Запись о блоке адресов у регистратора через RIPEstat; пусто, если сеть не ответила."""
    url = f"https://stat.ripe.net/data/whois/data.json?resource={address}"
    try:
        result = subprocess.run(["curl", "-fsS", "--proto", "=https", "--connect-timeout", "3", "--max-time", "4",
                                 "--max-filesize", str(MAX_OWNER_BYTES), url],
                                text=True, capture_output=True, timeout=6, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 and len(result.stdout) <= MAX_OWNER_BYTES else ""


def parse_whois_owner(text):
    """(название, страна) из первой записи; у регистраторов поля называются по-разному."""
    try:
        fields = {}
        for item in json.loads(text)["data"]["records"][0]:
            fields.setdefault(clean_text(item["key"]).lower(), clean_text(item["value"]).strip())
    except (ValueError, KeyError, IndexError, TypeError):
        return "", ""
    # В descr у RIPE часто стоит адрес улицы («11 Hanbury St»): такое не берём, остаётся netname.
    if fields.get("descr", " ")[:1].isdigit():
        fields.pop("descr")
    name = next((fields[key] for key in ("descr", "orgname", "owner", "netname") if fields.get(key)), "")
    return name, fields.get("country", "")


def fetch_rdap(address):
    """Запись о блоке адресов через RDAP (rdap.org сам отправляет запрос нужному региональному регистратору)."""
    url = f"https://rdap.org/ip/{address}"
    try:
        result = subprocess.run(["curl", "-fsS", "-L", "--max-redirs", "3", "--proto", "=https", "--proto-redir", "=https",
                                 "--connect-timeout", "3", "--max-time", "5", "--max-filesize", str(MAX_OWNER_BYTES),
                                 "-H", "Accept: application/rdap+json", url],
                                text=True, capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 and len(result.stdout) <= MAX_OWNER_BYTES else ""


def parse_rdap_owner(text):
    """(название, страна) из ответа RDAP: имя организации-владельца (registrant), иначе название блока."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return "", ""
        name = ""
        for entity in data.get("entities") or []:
            if "registrant" not in (entity.get("roles") or []):
                continue  # контакты («Abuse», «Technical») названием владельца не считаются
            card = entity.get("vcardArray") or []
            for field in (card[1] if len(card) > 1 else []):
                if field and field[0] == "fn" and isinstance(field[3], str):
                    name = name or clean_text(field[3]).strip()
        name = name or clean_text(str(data.get("name") or "")).strip()
        return name, clean_text(str(data.get("country") or "")).strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return "", ""


def lookup_owner(address):
    """Владелец адреса по RIPEstat; ответ запоминается на 30 дней. Любой сбой даёт пустую строку."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return ""
    if not ip.is_global:
        return ""
    path = DIRECTORY / OWNER_CACHE_FILE
    cache = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cache = loaded
    except (OSError, ValueError):
        pass
    now = int(datetime.datetime.now().timestamp())
    entry = cache.get(address)
    if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[1], int) \
            and now - entry[1] < OWNER_CACHE_DAYS * 86400 and isinstance(entry[0], str):
        return tidy_owner(clean_text(entry[0]))
    answer = fetch_ripe(address) or fetch_ripe(address)  # при сбое сети или лимите RIPEstat пробуем ещё раз
    owner = parse_ripe_owner(answer)
    from_ripe = bool(owner)
    if not owner and answer:
        # сеть не анонсируется (нет номера AS) или RIPEstat не знает адрес: берём название блока у регистратора
        name, country = parse_whois_owner(fetch_whois(address))
        owner = ", ".join(part for part in (name[:OWNER_TEXT_WIDTH], country) if part)
    if not owner:
        # RIPEstat не помог (молчит или не знает адрес): RDAP — независимый источник со всех регистраторов
        name, country = parse_rdap_owner(fetch_rdap(address))
        owner = ", ".join(part for part in (name[:OWNER_TEXT_WIDTH], country) if part)
    if from_ripe:
        # название из записи блока (регистратор) точнее владельца AS: берём его, если оно есть
        name, country = parse_whois_owner(fetch_whois(address))
        holder, _, asn = owner.rpartition(" ")
        if not name and len(owner) > OWNER_TEXT_WIDTH:
            owner = holder[:max(8, OWNER_TEXT_WIDTH - len(asn) - 2)].rstrip(" ,-") + "… " + asn
        if name and name.isupper() and holder:
            name = holder  # «MSFT», «NET-V4-…»: технический код сети, понятнее название владельца AS
        if name:
            tail = ", ".join(part for part in (country, asn) if part)
            room = OWNER_TEXT_WIDTH - len(tail) - 2
            if len(name) > room:
                name = name[:max(8, room - 1)].rstrip(" ,-") + "…"
            owner = f"{name}, {tail}"
    if owner:
        with OWNER_CACHE_LOCK:
            try:  # запросы идут параллельно: перед записью перечитываем файл, чтобы не потерять чужие записи
                loaded = json.loads(path.read_text(encoding="utf-8"))
                cache = loaded if isinstance(loaded, dict) else cache
            except (OSError, ValueError):
                pass
            owner = tidy_owner(owner)
            cache[address] = [owner, now]
            fresh = sorted(cache.items(), key=lambda item: -item[1][1] if isinstance(item[1], list)
                           and len(item[1]) == 2 and isinstance(item[1][1], int) else 0)[:OWNER_CACHE_LIMIT]
            try:
                write_text_atomic(path, json.dumps(dict(fresh), ensure_ascii=False))
            except OSError:
                pass
    return tidy_owner(owner)


def screen_columns():
    """Ширина настоящего терминала (вывод внутри рамки идёт через pty, поэтому берём stderr или stdin)."""
    for fd in (2, 0, 1):
        try:
            columns = os.get_terminal_size(fd).columns
        except OSError:
            continue
        if columns > 0:
            return columns
    try:
        return max(20, int(os.environ.get("COLUMNS", "")))
    except ValueError:
        return 100


COMPACT_BELOW = 100


HITS_HEADINGS = ("Сканеры и сети ведомств", "Из других стран", "Чего здесь нет")


def resolve_owners(addresses, kind, sources):
    """{адрес: владелец}: списки сканеров, затем RIPEstat (с кэшем), затем DNS. Запросы идут параллельно,
    любой сбой даёт «неизвестно»."""
    def one(address):
        try:
            owner = owner_of(address, sources) if kind == "scanners" else ""
            return owner or lookup_owner(address) or reverse_name(address) or "неизвестно"
        except Exception:
            return "неизвестно"
    if not addresses:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=OWNER_WORKERS) as pool:
        return dict(zip(addresses, pool.map(one, addresses)))


def show_hits():
    color = menu_color()
    columns = screen_columns()
    compact = columns < COMPACT_BELOW

    def say(text):  # длинные пояснения переносятся по ширине экрана (рамка оставляет columns - 4)
        print(textwrap.fill(text, width=max(30, columns - 4)))

    print()
    say("Кто стучится: попытки подключения, которые файрвол отклонил. "
        "Счёт идёт с включения статистики, данные хранятся до 7 дней.")
    groups = (("scanners", "Сканеры и сети ведомств (любые порты)"),
              ("non_ru", "Из других стран (на защищённых портах)"))
    collected = {"scanners": [], "non_ru": []}
    ages = {"scanners": {}, "non_ru": {}}
    full = set()
    sources = load_scanner_sources()
    seen = False
    slow = []

    def read_set(item):
        return probe_output("nft", "-n", "list", "set", "inet", "fw", item[1],
                            timeout=HITS_READ_TIMEOUT, on_timeout=None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(HIT_SETS)) as pool:
        answers = list(pool.map(read_set, HIT_SETS))  # параллельно: самый долгий ответ, а не сумма
    for (kind, name), text in zip(HIT_SETS, answers):
        if text is None:
            slow.append(name)
        elif text:
            seen = True
            collected[kind].extend(parse_hits(text))
            for address, age in parse_hit_ages(text).items():
                ages[kind][address] = max(ages[kind].get(address, 0), age)
            if set_is_full(text):
                full.add(kind)
    oldest = max((age for group in ages.values() for age in group.values()), default=None)
    if oldest is not None:
        say("Общего сброса нет: каждая запись хранится 7 дней с первого отклонённого пакета. "
            f"Самая старая сотрётся через {format_left(max(0, HIT_TTL - oldest))}.")
    if slow:
        print()
        say(f"Часть статистики не прочитана ({', '.join(slow)}): nft не ответил за {HITS_READ_TIMEOUT} с. "
            "Блокировка от этого не зависит, откройте экран позже.")
    if not seen and slow:
        return
    if not seen:
        print()
        say("Статистика не ведётся: правила загружены без неё (старая версия nft или ядра) или ещё не обновлены.")
        say("Примените правила заново: «Проверить и применить правила» → «Применить правила и включить таймер».")
        return
    if not any(collected.values()) and not slow:
        print()
        say("Пока никого: статистика включена, но отклонённых попыток ещё нет. Загляните позже.")
    for kind, title in groups:
        rows = collected[kind]
        if not rows:
            continue
        summary = summarize_hits(rows)
        print(f"\n{title}")
        print(f"Отклонено пакетов: {summary['packets']}, с разных адресов: {summary['sources']}")
        if ages[kind]:
            fresh = sum(1 for age in ages[kind].values() if age < DAY)
            say(f"Из них впервые замечены за последние сутки: {fresh} адр. (остальные стучатся дольше: это постоянный фон).")
        if kind in full:
            say("Внимание: место для статистики закончилось. Новые адреса и порты не записываются, "
                "пока старые записи не устареют (до 7 дней). Блокировка при этом работает как обычно.")
        say("Куда стучатся (число — сколько раз отклонена попытка на этот порт, % — доля от всех попыток):")
        biggest = summary["ports"][0][1]
        for port, packets in summary["ports"]:
            label = port_label(port)
            if compact:
                room = columns - 4 - 32 - 2
                label = label if len(label) <= room else label[:max(0, room - 1)].rstrip(" ,") + "…" if room > 1 else ""
                print(f"  {port:>5}  {hits_bar_view(packets, biggest, 6, color)}  {packets:>5} "
                      f"{share(packets, summary['packets']):>4}" + (f"  {label}" if label else ""))
            else:
                print(f"  {port:>5}  {hits_bar_view(packets, biggest, 10, color)}  {packets:>6} "
                      f"{share(packets, summary['packets']):>4}" + (f"  {label}" if label else ""))
        if summary["other_ports"]:
            # Остальное одной строкой без полоски: так сумма строк равна числу пакетов и видно, куда ушла масса
            name = f"ещё {summary['other_ports']} {ports_word(summary['other_ports'])}"
            gap = 5 + 2 + len(hits_bar(0, 1, 6 if compact else 10))
            print(f"  {name:<{gap}}  {summary['other_packets']:>{5 if compact else 6}} "
                  f"{share(summary['other_packets'], summary['packets']):>4}")
        by_address = {}
        for address, _port, packets, _size in rows:
            by_address[address] = by_address.get(address, 0) + packets
        busiest = [address for address, _packets in sorted(by_address.items(), key=lambda item: (-item[1], item[0]))]
        owner_of_address = resolve_owners(busiest[:OWNER_LOOKUPS], kind, sources)
        owner_groups = group_by_owner(rows, lambda address: owner_of_address.get(address, OTHER_ADDRESSES))
        named = [group for group in owner_groups if group["owner"] != OTHER_ADDRESSES]
        rest = [group for group in owner_groups if group["owner"] == OTHER_ADDRESSES]
        shown = named[:5] + rest
        say("По владельцам (попыток, адресов, чаще всего порты):")
        biggest = max(group["packets"] for group in (named[:5] or shown))  # «другие адреса» масштаб не задают
        owner_width = min(40, max(len(group["owner"]) for group in shown))
        for group in shown:
            owner = group["owner"] if len(group["owner"]) <= owner_width else group["owner"][:owner_width - 1].rstrip() + "…"
            leftover = group["owner"] == OTHER_ADDRESSES  # без полоски: сумма остатка не сравнима с владельцами
            if compact:
                room = columns - 4
                print("  " + (owner if len(owner) <= room - 2 else owner[:room - 3].rstrip() + "…"))
                prefix = f"    {hits_bar(group['packets'], biggest, 6)} {group['packets']:>5}"
                tail = fit_ports(prefix, group["addresses"], group["ports"], room)
                bar = " " * len(hits_bar(0, 1, 6)) if leftover else hits_bar_view(group["packets"], biggest, 6, color)
                print(f"    {bar} {group['packets']:>5}{tail}")
            else:
                prefix = f"  {owner:<{owner_width}}  {hits_bar(group['packets'], biggest, 8)} {group['packets']:>6}"
                tail = fit_ports(prefix, group["addresses"], group["ports"], 10 ** 6)
                bar = " " * len(hits_bar(0, 1, 8)) if leftover else hits_bar_view(group["packets"], biggest, 8, color)
                print(f"  {owner:<{owner_width}}  {bar} {group['packets']:>6}{tail}")
        say("Самые активные адреса (попыток, на сколько портов стучался):")
        biggest = summary["top_sources"][0][1]
        owners = [owner_of_address.get(address, "неизвестно") for address, _p, _c in summary["top_sources"]]
        width = min(39, max(len(address) for address, _p, _c in summary["top_sources"]))
        owner_width = min(40, max(len(owner) for owner in owners))
        owners = [owner if len(owner) <= owner_width else owner[:owner_width - 1].rstrip() + "…" for owner in owners]
        for (address, packets, count), owner in zip(summary["top_sources"], owners):
            if compact:
                # узкий экран: адрес, полоска и число в первой строке, владелец под ними
                extra = f"  портов: {count}" if count > 1 else ""
                print(f"  {address:<{width}}  {hits_bar_view(packets, biggest, 6, color)} {packets:>5}{extra}")
                room = columns - 4 - 4
                print("    " + (owner if len(owner) <= room else owner[:room - 1].rstrip() + "…"))
            else:
                print(f"  {address:<{width}}  {owner[:owner_width]:<{owner_width}}  "
                      f"{hits_bar_view(packets, biggest, 8, color)} {packets:>6}  портов: {count}")
    print("\nЧего здесь нет")
    say("Попытки входа по SSH сюда не попадают: порт SSH файрвол не фильтрует (смотрите журнал sshd).")
    say("Считаются отдельные пакеты, а не подключения: один сканер может прислать несколько пакетов на один порт.")


''' + "def show_status():\n", "функции «Кто стучится»")
menu = replace_once(menu, """        rule = item.get("rule")
        if not rule or rule.get("chain") != "input":
            continue
""", """        rule = item.get("rule")
        if not rule or rule.get("chain") != "input":
            continue
        if any("set" in expression for expression in rule.get("expr", [])):
            continue  # служебная строка статистики «Кто стучится», не правило отбора
""", "статус: скрыть строки статистики")

# ----------------------------------- ограничение частоты SSH в fw-menu (1.7.0)
menu = replace_once(menu, '''    comments = {"api-lock": "api_lock", "api-allow": "api_allow", "LAN_ALLOW": "lan", "scanners": "scanners", "non-RU": "non_ru"}
''', '''    comments = {"api-lock": "api_lock", "api-allow": "api_allow", "LAN_ALLOW": "lan", "scanners": "scanners",
                "non-RU": "non_ru", "ssh-rate": "ssh_rate"}
''', "SSH: вид правила")
menu = replace_once(menu, '''def rule_counter(rule):
''', '''def rule_rate(expressions):
    """Допустимое число новых подключений в минуту из правила с meter (None, если правило другое)."""
    for expression in expressions:
        limit = ((expression.get("meter") or {}).get("stmt") or {}).get("limit")
        if limit:
            return limit.get("rate")
    return None


def rule_counter(rule):
''', "SSH: скорость из правила")
menu = replace_once(menu, '''kind in {"api_lock", "api_allow", "trusted", "lan", "scanners", "non_ru", "other"}:''',
                    '''kind in {"api_lock", "api_allow", "trusted", "lan", "scanners", "non_ru", "ssh_rate", "other"}:''',
                    "SSH: объединение правил")
menu = replace_once(menu, '''                       "protocols": rule_protocols(rule.get("expr", []))})
    kinds = [group["kind"] for group in groups]
''', '''                       "protocols": rule_protocols(rule.get("expr", [])),
                       "rate": rule_rate(rule.get("expr", []))})
    kinds = [group["kind"] for group in groups]
''', "SSH: скорость в группе")
menu = replace_once(menu, '''        elif kind == "non_ru":
            steps.append(f"{ports_phrase(group['ports'], 'Порт', 'Порты')} ({protocols_phrase(group['protocols'])}): "
''', '''        elif kind == "ssh_rate":
            steps.append(f"{ports_phrase(group['ports'], 'SSH, порт', 'SSH, порты')}: с одного адреса не больше "
                         f"{group['rate']} новых подключений в минуту, лишние отбрасываются ({dropped_text(group)}). "
                         "Открытые сессии это не затрагивает.")
        elif kind == "non_ru":
            steps.append(f"{ports_phrase(group['ports'], 'Порт', 'Порты')} ({protocols_phrase(group['protocols'])}): "
''', "SSH: пояснение в статусе")
menu = replace_once(menu, '''def command_output(*command):
''', '''SSH_BEGIN, SSH_END = "# BEGIN FW-MENU SSH", "# END FW-MENU SSH"


def ssh_rate_setting():
    """Действующее значение SSH_RATE_LIMIT и SSH_PORTS из fw.conf и local.conf."""
    script = ('source "$1/fw.conf"; [ -f "$1/local.conf" ] && source "$1/local.conf"; '
              'printf "%s\\\\n%s\\\\n" "${SSH_RATE_LIMIT:-0}" "${SSH_PORTS-}"')
    values = (probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).splitlines() + ["0", ""])[:2]
    return (int(values[0]) if values[0].isdigit() else 0), values[1].split()


def configure_ssh_limit():
    rate, ports = ssh_rate_setting()
    shown = ports or ssh_ports()
    print(textwrap.fill("Ограничение частоты новых подключений к SSH: с одного адреса пропускается не больше "
                        "заданного числа новых подключений в минуту, лишние отбрасываются. Уже открытые сессии, "
                        "доверенные адреса и LAN_ALLOW не затрагиваются; через минуту доступ возвращается сам.", width=92))
    print(f"Порты SSH: {', '.join(str(port) for port in shown)}" + (" (заданы в SSH_PORTS)" if ports else " (определены автоматически)"))
    print(f"Сейчас: {'выключено' if not rate else f'не больше {rate} новых подключений в минуту'}")
    print(textwrap.fill("Осторожно: при плохой связи один вход может отправить несколько пакетов подряд, "
                        "поэтому порог должен быть щедрым. Рекомендуется 20. Если вы часто открываете много сессий "
                        "сразу (rsync, ansible, autossh), поставьте больше.", width=92))
    answer = input("Новых подключений в минуту (число от 5, 0 — выключить, Enter — отмена): ").strip()
    if not answer:
        return
    if not re.fullmatch(r"[0-9]{1,4}", answer) or (0 < int(answer) < 5):
        raise ValueError("укажи число от 5 до 9999 или 0, чтобы выключить")
    value = int(answer)
    if value == rate:
        print("Это уже действующее значение, ничего не меняю.")
        return
    exposure, admin = ask_admin_for_ssh_limit(value) if value else ("ok", "")
    if exposure == "cancel":
        print("Отменено, настройка не изменена.")
        return
    print("Будет сохранено: " + ("ограничение выключено." if not value else f"не больше {value} новых подключений в минуту."))
    if not confirm_yes("Сохранить настройку на этой ноде? Введи «да» или «нет»: "):
        print("Отменено, настройка не изменена.")
        return
    managed_block = re.compile(r"(?ms)^" + re.escape(SSH_BEGIN) + r"\\n.*?^" + re.escape(SSH_END) + r"(?:\\n|$)")
    with (DIRECTORY / ".api.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = LOCAL_CONFIG.read_text(encoding="utf-8") if LOCAL_CONFIG.exists() else ""
        current, count = managed_block.subn("", current)
        if count > 1 or SSH_BEGIN in current or SSH_END in current:
            raise ValueError("проверь границы блока FW-MENU SSH в local.conf")
        prefix = current.rstrip("\\n") + "\\n\\n" if current.rstrip("\\n") else ""
        write_text_atomic(LOCAL_CONFIG, f"{prefix}{SSH_BEGIN}\\nSSH_RATE_LIMIT={value}\\n{SSH_END}\\n")
    print("Настройка сохранена. Дальше открой «Проверить и применить правила»: проверка без применения, "
          "затем применение с подтверждением и автооткатом.")
    if exposure == "exposed":
        paint(33, f"Внимание: адрес {admin}, с которого ты работаешь, не в доверенных: лимит может заблокировать тебя на 5 минут.")
    elif exposure == "unknown":
        paint(33, "Внимание: адрес этого подключения не определён: проверь, что он есть в доверенных.")


def command_output(*command):
''', "SSH: настройка в меню")
ADMIN_HELPERS = r"""def admin_address():
    # Адрес, с которого ты сейчас подключён по SSH ("" — определить не удалось: консоль хостера и т. п.).
    def valid(value):
        try:
            return str(ipaddress.ip_address(value.strip("[]")))
        except ValueError:
            return ""
    client = valid(os.environ.get("SSH_CONNECTION", "").partition(" ")[0])
    if client:
        return client
    for command in (("who", "-m", "--ips"), ("who", "am", "i")):
        match = re.search(r"\(([^()]*)\)\s*$", probe_output(*command).strip())
        client = valid(match.group(1)) if match else ""
        if client:
            return client
    # sudo сбрасывает SSH_CONNECTION: ищем сокет sshd по цепочке родительских процессов.
    pids, pid = set(), os.getppid()
    for _ in range(32):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    for line in probe_output("ss", "-Htnp", "state", "established").splitlines():
        fields = line.split()
        if len(fields) > 4 and "sshd" in line and any(f"pid={item}," in line for item in pids):
            client = valid(fields[-2].rsplit(":", 1)[0])
            if client:
                return client
    return ""


def lan_allow_networks():
    script = ('source "$1/fw.conf"; [ -f "$1/local.conf" ] && source "$1/local.conf"; '
              'printf "%s" "${LAN_ALLOW-}"')
    result = []
    for item in probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).replace(",", " ").split():
        try:
            result.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return result


def admin_protection(address):
    # Где адрес уже пропускается раньше SSH-правила: "в доверенных", "в LAN_ALLOW" или "" (лимит его заденет).
    ip = ipaddress.ip_address(address)
    if any(ip in network for network in trusted_networks() if network.version == ip.version):
        return "в доверенных"
    if any(ip in network for network in lan_allow_networks() if network.version == ip.version):
        return "в LAN_ALLOW"
    return ""


LAYOUT_FIX = str.maketrans("qwertyuiop[]asdfghjkl;'zxcvbnm,.", "йцукенгшщзхъфывапролджэячсмитьбю")


def explain_unclear_answer(raw, options):
    # Почему вопрос задан ещё раз. Ответ, набранный в английской раскладке («lf» вместо «да»), узнаём и говорим об этом прямо.
    if not raw:
        return textwrap.fill(f"Ответ пустой, поэтому вопрос задан ещё раз. Нужно: {options}.", width=92)
    text = f"Ответ «{raw}» не понят, поэтому вопрос задан ещё раз. Нужно: {options}."
    fixed = raw.casefold().translate(LAYOUT_FIX)
    if fixed != raw.casefold() and fixed in ("да", "нет", "отмена"):
        text += (f" Похоже, включена английская раскладка: «{raw}» в ней то же, что «{fixed}». "
                 "Переключи раскладку и введи слово заново.")
    return textwrap.fill(text, width=92)


def confirm_yes(prompt):
    # «да» — продолжить; «нет» или Enter — отмена; всё остальное не угадываем, а спрашиваем снова и объясняем почему.
    while True:
        raw = input(prompt).strip()
        answer = raw.casefold()
        if answer == "да":
            return True
        if answer in ("", "нет", "отмена"):
            return False
        print(explain_unclear_answer(raw, "«да» (сохранить) или «нет» / Enter (отмена)"))


def ask_admin_for_ssh_limit(rate):
    # Возвращает ("ok" | "exposed" | "unknown" | "cancel", адрес). Ответ «да» или «нет» обязателен: Enter ничего не решает.
    address = admin_address()
    if not address:
        paint(33, "Внимание: адрес, с которого ты подключён, определить не удалось (консоль хостера или нестандартный вход).")
        print(textwrap.fill("Убедись, что твой адрес есть в доверенных (пункт «Показать доверенные адреса»): иначе при быстрых "
                            "подключениях можно заблокировать самого себя на 5 минут.", width=92))
        return "unknown", ""
    where = admin_protection(address)
    if where:
        print(f"Ты подключён с {address}: он {where}, лимит тебе не помешает.")
        return "ok", address
    paint(33, f"Внимание: ты сейчас подключён с адреса {address}, его нет в списке доверенных.")
    print(textwrap.fill(f"Если ты сам сделаешь больше {rate} новых подключений за минуту (scp, rsync, несколько окон), "
                        "этот адрес будет заблокирован на 5 минут. Доверенные адреса лимитом не затрагиваются.", width=92))
    if ":" in address:
        print(textwrap.fill("IPv6: лимит считается по всей подсети /64, а в доверенные попадёт только этот адрес. "
                            "Если провайдер часто меняет адрес, добавь подсеть вручную.", width=92))
    while True:
        raw = input(f"Добавить {address} в доверенные? Введи «да», «нет» или «отмена»: ").strip()
        answer = raw.casefold()
        if answer == "да":
            with (DIRECTORY / ".trusted.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                network = parse_network(address)
                if network not in set(trusted_networks()):
                    append_entries(TRUSTED, [network_text(network)])
            print(f"Адрес {address} добавлен в trusted.txt. Он начнёт действовать при применении правил.")
            return "ok", address
        if answer == "нет":
            return "exposed", address
        if answer == "отмена":
            return "cancel", address
        print(explain_unclear_answer(raw, "«да», «нет» или «отмена»; Enter здесь ничего не решает"))


def ssh_admin_warning(rate):
    # Строка для «Статуса файрвола»: лимит включён, а адрес этого подключения не в доверенных.
    address = admin_address()
    if not address or admin_protection(address):
        return ""
    return textwrap.fill(f"Внимание: ты подключён с {address}, его нет в доверенных: лимит SSH ({rate} в минуту) может "
                         "заблокировать тебя при быстрых подключениях. Добавь адрес: «Настроить доступ» → "
                         "«Добавить доверенные IP или подсети» → ssh.", width=92)


"""
menu = replace_once(menu, "SSH_BEGIN, SSH_END = \"# BEGIN FW-MENU SSH\", \"# END FW-MENU SSH\"\n",
                    "SSH_BEGIN, SSH_END = \"# BEGIN FW-MENU SSH\", \"# END FW-MENU SSH\"\n\n\n" + ADMIN_HELPERS, "SSH: адрес администратора")
menu = replace_once(menu, 'def ssh_client():\n    return os.environ.get("SSH_CONNECTION", "").partition(" ")[0]\n',
                    'def ssh_client():\n    # sudo сбрасывает SSH_CONNECTION, поэтому адрес ищется и другими способами.\n    return admin_address()\n',
                    "SSH: ssh_client через admin_address")
menu = replace_once(menu, '''              'printf "%s\\n%s\\n%s\\n%s\\n%s\\n" "${LAN_ALLOW-}" "${CONFIRM_TIMEOUT:-60}" "${MAX_GROWTH:-3}" "${MIN_KEEP:-0.2}" "${MAX_CACHE_DAYS:-14}"')
    values = probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).splitlines()
    if len(values) == 5:
        print("Настройки: LAN_ALLOW=" + (values[0] or "выключено") +
              f", CONFIRM_TIMEOUT={values[1]}, MAX_GROWTH={values[2]}, MIN_KEEP={values[3]}, MAX_CACHE_DAYS={values[4]}")
''', '''              'printf "%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n" "${LAN_ALLOW-}" "${CONFIRM_TIMEOUT:-60}" "${MAX_GROWTH:-3}" "${MIN_KEEP:-0.2}" "${MAX_CACHE_DAYS:-14}" "${SSH_RATE_LIMIT:-0}"')
    values = probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).splitlines()
    if len(values) == 6:
        ssh_rate = int(values[5]) if values[5].isdigit() else 0
        print("Настройки: LAN_ALLOW=" + (values[0] or "выключено") +
              f", CONFIRM_TIMEOUT={values[1]}, MAX_GROWTH={values[2]}, MIN_KEEP={values[3]}, MAX_CACHE_DAYS={values[4]}, "
              + ("SSH_RATE_LIMIT=выключено" if not ssh_rate else f"SSH_RATE_LIMIT={ssh_rate} в минуту"))
        if ssh_rate and "ssh-rate" not in probe_output("nft", "list", "chain", "inet", "fw", "input"):
            print(textwrap.fill("Ограничение частоты SSH настроено, но правило ещё не загружено. "
                                "Примени правила: «Проверить и применить правила».", width=92))
        admin_warning = ssh_admin_warning(ssh_rate) if ssh_rate else ""
        if admin_warning:
            paint(31, admin_warning)
''', "SSH: настройки в статусе")
menu = replace_once(menu, '''            ("Настроить порт управления и разрешённые IP", configure_api),
''', '''            ("Настроить порт управления и разрешённые IP", configure_api),
            ("Ограничить частоту новых SSH-подключений", configure_ssh_limit),
''', "SSH: пункт меню")

# ---------------------------------------------------------------- статистика превышений SSH-лимита (1.7.0)
upd = replace_once(upd, '''    stat_set ss4 ipv4_addr; stat_set ss6 ipv6_addr; stat_set sn4 ipv4_addr; stat_set sn6 ipv6_addr
  fi
''', '''    stat_set ss4 ipv4_addr; stat_set ss6 ipv6_addr; stat_set sn4 ipv4_addr; stat_set sn6 ipv6_addr
    # Адреса, превысившие лимит SSH: счёт по паре «адрес + порт», как у сканеров (экран «Кто стучится»).
    if [ "$SSH_RULES" = 1 ]; then stat_set sr4 ipv4_addr; stat_set sr6 ipv6_addr; fi
  fi
''', "SSH-статистика: наборы")
upd = replace_once(upd, '''    echo "    tcp dport $SP ct state new meter ssh4 { ip saddr timeout 5m limit rate over $SSH_RATE_LIMIT/minute burst $SSH_BURST packets } counter drop comment \\"ssh-rate\\""
    echo "    tcp dport $SP ct state new meter ssh6 { ip6 saddr & ffff:ffff:ffff:ffff:: timeout 5m limit rate over $SSH_RATE_LIMIT/minute burst $SSH_BURST packets } counter drop comment \\"ssh-rate\\""
''', '''    # Запись в набор статистики стоит после meter, поэтому выполняется только для превысивших лимит.
    SSH_ADD4=""; SSH_ADD6=""
    if [ "$STATS" = 1 ]; then
      SSH_ADD4=" add @sr4 { ip saddr . tcp dport }"; SSH_ADD6=" add @sr6 { ip6 saddr . tcp dport }"
    fi
    echo "    tcp dport $SP ct state new meter ssh4 { ip saddr timeout 5m limit rate over $SSH_RATE_LIMIT/minute burst $SSH_BURST packets }$SSH_ADD4 counter drop comment \\"ssh-rate\\""
    echo "    tcp dport $SP ct state new meter ssh6 { ip6 saddr & ffff:ffff:ffff:ffff:: timeout 5m limit rate over $SSH_RATE_LIMIT/minute burst $SSH_BURST packets }$SSH_ADD6 counter drop comment \\"ssh-rate\\""
''', "SSH-статистика: правила")
upd = replace_once(upd, 'for stat in ss4 ss6 sn4 sn6; do\n  : >', 'for stat in ss4 ss6 sn4 sn6 sr4 sr6; do\n  : >', "SSH-статистика: перенос")
upd = replace_once(upd, '  for stat in ss4 ss6 sn4 sn6; do : >', '  for stat in ss4 ss6 sn4 sn6 sr4 sr6; do : >', "SSH-статистика: сброс переноса")

# экран «Кто стучится»: четвёртая группа
menu = replace_once(menu, 'HIT_SETS = (("scanners", "ss4"), ("scanners", "ss6"), ("non_ru", "sn4"), ("non_ru", "sn6"))\n',
                    'HIT_SETS = (("scanners", "ss4"), ("scanners", "ss6"), ("non_ru", "sn4"), ("non_ru", "sn6"),\n'
                    '            ("ssh_rate", "sr4"), ("ssh_rate", "sr6"))\n', "SSH-статистика: наборы в меню")
menu = replace_once(menu, 'HITS_HEADINGS = ("Сканеры и сети ведомств", "Из других стран", "Чего здесь нет")\n',
                    'HITS_HEADINGS = ("Сканеры и сети ведомств", "Из других стран", "Превысили лимит SSH", "Чего здесь нет")\n',
                    "SSH-статистика: заголовки")
menu = replace_once(menu, '''    groups = (("scanners", "Сканеры и сети ведомств (любые порты)"),
              ("non_ru", "Из других стран (на защищённых портах)"))
    collected = {"scanners": [], "non_ru": []}
    ages = {"scanners": {}, "non_ru": {}}
''', '''    groups = (("scanners", "Сканеры и сети ведомств (любые порты)"),
              ("non_ru", "Из других стран (на защищённых портах)"),
              ("ssh_rate", "Превысили лимит SSH (слишком частые новые подключения)"))
    collected = {"scanners": [], "non_ru": [], "ssh_rate": []}
    ages = {"scanners": {}, "non_ru": {}, "ssh_rate": {}}
    ssh_stats = False
''', "SSH-статистика: группы")
menu = replace_once(menu, '''        elif text:
            seen = True
            collected[kind].extend(parse_hits(text))
''', '''        elif text:
            seen = True
            ssh_stats = ssh_stats or kind == "ssh_rate"
            collected[kind].extend(parse_hits(text))
''', "SSH-статистика: набор есть")
menu = replace_once(menu, '''    say("Попытки входа по SSH сюда не попадают: порт SSH файрвол не фильтрует (смотрите журнал sshd).")
''', '''    if ssh_stats:
        say("В блоке «Превысили лимит SSH» только адреса, которые превысили порог новых подключений. "
            "Медленный подбор паролей ниже порога сюда не попадает: смотрите журнал sshd.")
    else:
        say("Попытки входа по SSH сюда не попадают: порт SSH файрвол не фильтрует (смотрите журнал sshd). "
            "Если включить ограничение частоты SSH, адреса, превысившие порог, появятся здесь.")
''', "SSH-статистика: пояснение")
menu = replace_once(menu, '''        if any("set" in expression for expression in rule.get("expr", [])):
            continue  # служебная строка статистики «Кто стучится», не правило отбора
''', '''        if any("set" in expression for expression in rule.get("expr", [])) and rule.get("comment") != "ssh-rate":
            continue  # служебная строка статистики «Кто стучится», не правило отбора (правило SSH-лимита тоже пишет в набор)
''', "SSH-статистика: правило лимита не служебное")
menu = replace_once(menu, '''        rows = collected[kind]
        if not rows:
            continue
        summary = summarize_hits(rows)
''', '''        rows = collected[kind]
        if not rows:
            if kind == "ssh_rate" and ssh_stats:
                print(f"\\n{title.split(' (')[0]}")
                say("Пока никто не превысил порог новых подключений.")
            continue
        summary = summarize_hits(rows)
''', "SSH-статистика: пустая группа")

# ---------------------------------------------------------------- список blocklist.de для SSH (1.10.0)
# По умолчанию включено (BLOCKLIST_SSH=0 выключает). Адреса из ssh.txt (замеченные в переборе SSH
# за последние 48 часов) отбрасываются только на портах SSH; доверенные и LAN_ALLOW пропускаются раньше.
upd = replace_once(upd, 'SSH_RATE_LIMIT="${SSH_RATE_LIMIT:-0}"; SSH_PORTS="${SSH_PORTS-}"\n',
                   'SSH_RATE_LIMIT="${SSH_RATE_LIMIT:-0}"; SSH_PORTS="${SSH_PORTS-}"\n'
                   'BLOCKLIST_SSH="${BLOCKLIST_SSH:-1}"; BLOCKLIST_URL="https://lists.blocklist.de/lists/ssh.txt"\n'
                   'BLOCKLIST_REFRESH_MIN=360; BLOCKLIST_MAX_AGE_MIN=4320\n', "blocklist: настройки")
upd = replace_once(upd, 'for ssh_port in $SSH_PORTS; do\n',
                   '[[ "$BLOCKLIST_SSH" =~ ^[01]$ ]] || fail_keep "BLOCKLIST_SSH: 1 (включено) или 0 (выключено)"\n'
                   'for ssh_port in $SSH_PORTS; do\n', "blocklist: проверка настройки")
upd = replace_once(upd, 'SSH_RULES=0\nif [ "$SSH_RATE_LIMIT" -gt 0 ]; then\n', r'''# blocklist.de: скачивание и проверка. В список попадают только отдельные публичные адреса.
fetch_blocklist() {
  dl "$MAX_LIST_BYTES" "$TMP/bl_raw" --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 15 "$BLOCKLIST_URL" || return 1
  python3 - "$TMP/bl_raw" "$1" <<'PY'
import ipaddress
from pathlib import Path
import sys
SKIP = [ipaddress.ip_network(net) for net in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
    "224.0.0.0/4", "240.0.0.0/4", "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8")]
found = set()
for line in Path(sys.argv[1]).read_text(encoding="utf-8", errors="strict").splitlines():
    line = line.partition("#")[0].strip()
    if not line:
        continue
    try:
        address = ipaddress.ip_address(line)
    except ValueError:
        sys.exit("в списке не адрес: " + line[:40])
    if any(address.version == net.version and address in net for net in SKIP):
        continue
    found.add(address)
if not found or len(found) > 200000:
    sys.exit("в списке %d адресов" % len(found))
Path(sys.argv[2]).write_text("".join(f"{a}\n" for a in sorted(found, key=lambda a: (a.version, int(a)))), encoding="utf-8")
PY
}
# Файл не старше N минут (кэш списка живёт недолго: в нём адреса за двое суток).
bl_younger_than() { [ -s "$1" ] && [ -n "$(find "$1" -maxdepth 0 -mmin "-$2" 2>/dev/null)" ]; }
BL_RULES=0; BL_FRESH=0
if [ "$BLOCKLIST_SSH" = 1 ]; then
  BL_CACHE=$SCAN_CACHE/blocklist_ssh
  BL_PORTS="{ $(echo ${SSH_PORTS:-$(ssh_ports_detect)} | tr ' ' ',') }"
  : > "$TMP/bl"
  if bl_younger_than "$BL_CACHE" "$BLOCKLIST_REFRESH_MIN"; then
    cat "$BL_CACHE" > "$TMP/bl"
  elif fetch_blocklist "$TMP/bl_new" && growth_ok "$TMP/bl_new" "$BL_CACHE" "blocklist.de SSH"; then
    cat "$TMP/bl_new" > "$TMP/bl"; BL_FRESH=1
  elif bl_younger_than "$BL_CACHE" "$BLOCKLIST_MAX_AGE_MIN"; then
    cat "$BL_CACHE" > "$TMP/bl"
    note "WARN: список blocklist.de недоступен или подозрителен, использую кэш"
  else
    note "WARN: список blocklist.de недоступен и свежего кэша нет — в этот раз без него"
  fi
  if [ -s "$TMP/bl" ]; then
    BL_RULES=1
    split "$TMP/bl" "$TMP/bl4" "$TMP/bl6"
  fi
fi
SSH_RULES=0
if [ "$SSH_RATE_LIMIT" -gt 0 ]; then
''', "blocklist: загрузка")
upd = replace_once(upd, '''  set_def ap4 ipv4_addr "$TMP/ap4"; set_def ap6 ipv6_addr "$TMP/ap6"
''', '''  set_def ap4 ipv4_addr "$TMP/ap4"; set_def ap6 ipv6_addr "$TMP/ap6"
  if [ "$BL_RULES" = 1 ]; then set_def bl4 ipv4_addr "$TMP/bl4"; set_def bl6 ipv6_addr "$TMP/bl6"; fi
''', "blocklist: наборы")
upd = replace_once(upd, '''    if [ "$SSH_RULES" = 1 ]; then stat_set sr4 ipv4_addr; stat_set sr6 ipv6_addr; fi
''', '''    if [ "$SSH_RULES" = 1 ]; then stat_set sr4 ipv4_addr; stat_set sr6 ipv6_addr; fi
    if [ "$BL_RULES" = 1 ]; then stat_set bs4 ipv4_addr; stat_set bs6 ipv6_addr; fi
''', "blocklist: наборы статистики")
upd = replace_once(upd, '''  if [ "$SSH_RULES" = 1 ]; then
    # Отбрасывается только избыток''', r'''  if [ "$BL_RULES" = 1 ]; then
    # Только порты SSH. Запись в набор статистики стоит до drop и считает пару «адрес + порт».
    BL_ADD4=""; BL_ADD6=""
    if [ "$STATS" = 1 ]; then
      BL_ADD4=" add @bs4 { ip saddr . tcp dport }"; BL_ADD6=" add @bs6 { ip6 saddr . tcp dport }"
    fi
    echo "    tcp dport $BL_PORTS ip saddr @bl4$BL_ADD4 counter drop comment \"blocklist-ssh\""
    echo "    tcp dport $BL_PORTS ip6 saddr @bl6$BL_ADD6 counter drop comment \"blocklist-ssh\""
  fi
  if [ "$SSH_RULES" = 1 ]; then
    # Отбрасывается только избыток''', "blocklist: правила")
upd = replace_once(upd, 'for stat in ss4 ss6 sn4 sn6 sr4 sr6; do\n  : >', 'for stat in ss4 ss6 sn4 sn6 sr4 sr6 bs4 bs6; do\n  : >', "blocklist: перенос статистики")
upd = replace_once(upd, '  for stat in ss4 ss6 sn4 sn6 sr4 sr6; do : >', '  for stat in ss4 ss6 sn4 sn6 sr4 sr6 bs4 bs6; do : >', "blocklist: сброс переноса")
upd = replace_once(upd, '''wide_finish
if [ "$DRY" = 1 ]''', '''[ "$BL_RULES" = 0 ] || echo "blocklist.de SSH: $(cat "$TMP/bl4" "$TMP/bl6" | wc -l) адр. на портах $BL_PORTS"
wide_finish
if [ "$DRY" = 1 ]''', "blocklist: строка итога")
upd = replace_once(upd, '# Кэш своих списков, убранных из sources.txt, больше не нужен.\n', r'''if [ "$BL_FRESH" = 1 ]; then
  mkdir -p "$SCAN_CACHE" || fail_save "не создался каталог кэша списков"
  staged_cache=$(mktemp "$SCAN_CACHE/.new.XXXXXXXX") || fail_save "не создался временный файл для blocklist.de"
  if ! cat "$TMP/bl" > "$staged_cache" || ! mv -fT "$staged_cache" "$SCAN_CACHE/blocklist_ssh"; then
    rm -f -- "$staged_cache"
    fail_save "не сохранился кэш blocklist.de"
  fi
fi
# Кэш своих списков, убранных из sources.txt, больше не нужен.
''', "blocklist: сохранение кэша")

# ---------------------------------------------------------------- «Статус файрвола»: читаемость (1.7.0)
menu = replace_once(menu, "import concurrent.futures\n", "import concurrent.futures\nimport contextlib\nimport io\n", "статус: импорты")
menu = replace_once(menu, '    "Профиль сервера",\n    "Служба и таймер",\n',
                    '    "Профиль сервера",\n    "Настройки",\n    "Служба и таймер",\n', "статус: заголовок «Настройки»")
menu = replace_once(menu, "def dropped_text(group):\n", r"""class CountedStep(str):
    # Пункт проверки со счётчиком: текст и группа правил, для которой под пунктом печатается строка «Отброшено …».
    def __new__(cls, text, group):
        item = super().__new__(cls, text)
        item.group = group
        return item


def dropped_line(group):
    text = dropped_text(group)
    return "     " + text[0].upper() + text[1:]


def dropped_text(group):
""", "статус: пункт со счётчиком")
menu = replace_once(menu, """            steps.append(f"{ports_phrase(group['ports'], 'Порт управления', 'Порты управления')} "
                         f"({protocols_phrase(group['protocols']) or 'TCP'}): адреса не из списка допущенных "
                         f"отбрасываются ({dropped_text(group)}), допущенные — пропускаются.")
""", """            steps.append(CountedStep(f"{ports_phrase(group['ports'], 'Порт управления', 'Порты управления')} "
                                     f"({protocols_phrase(group['protocols']) or 'TCP'}): адреса не из списка допущенных "
                                     "отбрасываются, допущенные — пропускаются.", group))
""", "статус: счётчик порта управления")
menu = replace_once(menu, """            steps.append(f"Сканеры и сети ведомств — отбрасываются на всех портах ({dropped_text(group)}).")
""", """            steps.append(CountedStep("Сканеры и сети ведомств — отбрасываются на всех портах.", group))
""", "статус: счётчик сканеров")
menu = replace_once(menu, """            steps.append(f"{ports_phrase(group['ports'], 'SSH, порт', 'SSH, порты')}: с одного адреса не больше "
                         f"{group['rate']} новых подключений в минуту, лишние отбрасываются ({dropped_text(group)}). "
                         "Открытые сессии это не затрагивает.")
""", """            steps.append(CountedStep(f"{ports_phrase(group['ports'], 'SSH, порт', 'SSH, порты')}: с одного адреса не больше "
                                     f"{group['rate']} новых подключений в минуту, лишние отбрасываются. "
                                     "Открытые сессии это не затрагивает.", group))
""", "статус: счётчик SSH")
menu = replace_once(menu, """            steps.append(f"{ports_phrase(group['ports'], 'Порт', 'Порты')} ({protocols_phrase(group['protocols'])}): "
                         f"адреса не из России — отбрасываются ({dropped_text(group)}).")
""", """            steps.append(CountedStep(f"{ports_phrase(group['ports'], 'Порт', 'Порты')} ({protocols_phrase(group['protocols'])}): "
                                     "адреса не из России — отбрасываются.", group))
""", "статус: счётчик геофильтра")
menu = replace_once(menu, """        print(textwrap.fill(f"{number}. {text}", width=92, initial_indent="  ", subsequent_indent="     "))
""", """        print(textwrap.fill(f"{number}. {text}", width=92, initial_indent="  ", subsequent_indent="     "))
        if isinstance(text, CountedStep):
            # Счётчик отдельной строкой под пунктом: ненулевой выделен, чтобы было видно, где что-то отбрасывается.
            if text.group["packets"]:
                paint(33, dropped_line(text.group))
            else:
                print(dropped_line(text.group))
""", "статус: строка счётчика")

# show_profile и расписание возвращают то, что нужно итоговой строке
menu = replace_once(menu, """    print("Расхождения с настройками:" if findings else "Расхождений с настройками нет.")
    for finding in findings:
        print(f"  ! {finding}")
""", """    print("Расхождения с настройками:" if findings else "Расхождений с настройками нет.")
    for finding in findings:
        print(f"  ! {finding}")
    return findings
""", "статус: show_profile возвращает расхождения")
menu = replace_once(menu, """    print(f"Следующее обновление — {next_run}")
""", """    print(f"Следующее обновление — {next_run}")
    return properties
""", "статус: расписание возвращает свойства")

# итоговая строка сверху: тело статуса печатается в буфер, затем выводится ИТОГ и сам текст
menu = replace_once(menu, '''def show_status():
    print("Проверка файлов и команд")
''', r"""def show_status():
    # Сначала собирается весь экран, потом сверху печатается итог: одна строка, по которой видно, всё ли в порядке.
    problems = []
    real_tty = sys.stdout.isatty()

    class Buffer(io.StringIO):
        def isatty(self):
            return real_tty

    buffer = Buffer()
    completed = False
    try:
        with contextlib.redirect_stdout(buffer):
            status_body(problems)
        completed = True
    finally:
        if completed:
            if problems:
                paint(min(code for code, _ in problems), f"ИТОГ: ЕСТЬ ЗАМЕЧАНИЯ ({len(problems)})")
                for code, message in problems:
                    paint(code, f"  ! {message}")
            else:
                paint(32, "ИТОГ: ВСЁ В ПОРЯДКЕ — файлы на месте, правила загружены, таймер активен, расхождений нет")
            print()
        sys.stdout.write(buffer.getvalue())


def status_body(problems):
    print("Проверка файлов и команд")
""", "статус: итог сверху")
menu = replace_once(menu, """        print(f"  {'OK' if present else 'ПРОВЕРЬ'} {path}")
""", """        print(f"  {'OK' if present else 'ПРОВЕРЬ'} {path}")
        if not present:
            problems.append((31, f"нет файла {path}"))
""", "статус: проблема с файлом")
menu = replace_once(menu, """        print(f"  {'OK' if shutil.which(name) else 'ПРОВЕРЬ'} {name}")
""", """        print(f"  {'OK' if shutil.which(name) else 'ПРОВЕРЬ'} {name}")
        if not shutil.which(name):
            problems.append((31, f"нет команды {name}"))
""", "статус: проблема с командой")
menu = replace_once(menu, """    warn_reverse_proxy()
    show_profile()
    script = (""", """    warn_reverse_proxy()
    findings = show_profile() or []
    if findings:
        problems.append((33, f"расхождения с настройками: {len(findings)} (подробности в блоке «Профиль сервера»)"))
    script = (""", "статус: расхождения")
menu = replace_once(menu, """        print("Настройки: LAN_ALLOW=" + (values[0] or "выключено") +
              f", CONFIRM_TIMEOUT={values[1]}, MAX_GROWTH={values[2]}, MIN_KEEP={values[3]}, MAX_CACHE_DAYS={values[4]}, "
              + ("SSH_RATE_LIMIT=выключено" if not ssh_rate else f"SSH_RATE_LIMIT={ssh_rate} в минуту"))
        if ssh_rate and "ssh-rate" not in probe_output("nft", "list", "chain", "inet", "fw", "input"):
            print(textwrap.fill("Ограничение частоты SSH настроено, но правило ещё не загружено. "
                                "Примени правила: «Проверить и применить правила».", width=92))
        admin_warning = ssh_admin_warning(ssh_rate) if ssh_rate else ""
        if admin_warning:
            paint(31, admin_warning)
""", """        print("\\nНастройки")
        not_default = "не по умолчанию"
        for name, shown, note in (
                ("LAN_ALLOW", values[0] or "выключено", ""),
                ("SSH_RATE_LIMIT", f"{ssh_rate} в минуту" if ssh_rate else "выключено", "включено" if ssh_rate else ""),
                ("CONFIRM_TIMEOUT", f"{values[1]} с", not_default if values[1] != "60" else ""),
                ("MAX_GROWTH", values[2], not_default if values[2] != "3" else ""),
                ("MIN_KEEP", values[3], not_default if values[3] != "0.2" else ""),
                ("MAX_CACHE_DAYS", values[4], not_default if values[4] != "14" else "")):
            line = f"  {name} ".ljust(24, ".") + f" {shown}" + (f"   ← {note}" if note else "")
            if note == not_default:
                paint(33, line)
            else:
                print(line)
        if ssh_rate and "ssh-rate" not in probe_output("nft", "list", "chain", "inet", "fw", "input"):
            print(textwrap.fill("Ограничение частоты SSH настроено, но правило ещё не загружено. "
                                "Примени правила: «Проверить и применить правила».", width=92))
            problems.append((33, "лимит SSH настроен, но правило ещё не загружено: примени правила"))
        admin_warning = ssh_admin_warning(ssh_rate) if ssh_rate else ""
        if admin_warning:
            paint(31, admin_warning)
            problems.append((31, "адрес твоего подключения не в доверенных: лимит SSH может заблокировать тебя"))
""", "статус: настройки по строке")
menu = replace_once(menu, """    print(f"Последнее обновление — {last_update}")
""", """    print(f"Последнее обновление — {last_update}")
    if result and result != "success":
        problems.append((31, "последнее обновление завершилось с ошибкой: смотри «Журнал ошибок»"))
""", "статус: проблема обновления")
menu = replace_once(menu, """    show_schedule_status()
    print("\\nЗагруженные правила""", """    timer = show_schedule_status()
    if timer.get("ActiveState") in ("inactive", "failed", "deactivating"):
        problems.append((33, "таймер обновления не активен: списки не обновляются"))
    print("\\nЗагруженные правила""", "статус: проблема таймера")
menu = replace_once(menu, """        print(table.stderr.strip() or "Таблица inet fw пока не загружена.")
        return
""", """        print(table.stderr.strip() or "Таблица inet fw пока не загружена.")
        problems.append((33, "правила ещё не загружены: «Проверить и применить правила»"))
        return
""", "статус: правила не загружены")
menu = replace_once(menu, """        print("Не удалось разобрать правила, ниже технический вид.", flush=True)
        run("nft", "-t", "list", "chain", "inet", "fw", "input")
        return
""", """        print("Не удалось разобрать правила, ниже технический вид.", flush=True)
        print(probe_output("nft", "-t", "list", "chain", "inet", "fw", "input"))
        problems.append((33, "правила не удалось разобрать: смотри технический вид ниже"))
        return
""", "статус: разбор правил")

# После правки доверенных адресов и списков меню само предлагает применить правила: те же проверка,
# подтверждение и автооткат, что в «Проверить и применить правила» → 2, но без поиска пункта.
OFFER_APPLY_CODE = r"""def offer_apply():
    try:
        apply_rules()
    except EOFError:
        print()
        print("Применить позже: fw-menu → 4 → 2.")


"""
menu = replace_once(menu, 'def apply_rules():\n', OFFER_APPLY_CODE + 'def apply_rules():\n', "предложение применить правила")
menu = replace_once(menu, """    print(f"Добавлено: {', '.join(addresses)}. Чтобы обновить правила, открой «Проверить и применить правила».")
    if any(isinstance(item, str) for item in items):
        print("DDNS-адрес превращается в IP при каждом плановом обновлении. Чтобы смену IP подхватывало быстрее,")
        print("задай частоту: «Проверить и применить правила» → «Настроить расписание обновления» → 3 (раз в 3 часа).")
""", """    print(f"Добавлено: {', '.join(addresses)}.")
    if any(isinstance(item, str) for item in items):
        print("DDNS-адрес превращается в IP при каждом плановом обновлении. Чтобы смену IP подхватывало быстрее,")
        print("задай частоту: «Проверить и применить правила» → «Настроить расписание обновления» → 3 (раз в 3 часа).")
    offer_apply()
""", "предложение после добавления доверенных")
menu = replace_once(menu, """    print("Чтобы правила обновились, открой «Проверить и применить правила» → «Применить правила и включить таймер».")
""", """    offer_apply()
""", "предложение после удаления доверенных")
menu = replace_once(menu, """    print(f"{asn} сохранён. Чтобы обновить правила, открой «Проверить и применить правила».")
""", """    print(f"{asn} сохранён.")
    offer_apply()
""", "предложение после ASN")
menu = replace_once(menu, """    print(f"Добавлено в extra.txt: {address}. Дальше открой «Проверить и применить правила».")
""", """    print(f"Добавлено в extra.txt: {address}.")
    offer_apply()
""", "предложение после extra")
menu = replace_once(menu, """    print("Список сохранён и будет обновляться вместе со штатными. "
          "Чтобы обновить правила, открой «Проверить и применить правила».")
""", """    print("Список сохранён и будет обновляться вместе со штатными.")
    offer_apply()
""", "предложение после своего списка")
menu = replace_once(menu, """    print("Записи удалены. Чтобы обновить правила, открой «Проверить и применить правила».")
    print("Адрес может оставаться заблокированным через другие ASN, подсети, списки или пресеты.")
""", """    print("Записи удалены. Адрес может оставаться заблокированным через другие ASN, подсети, списки или пресеты.")
    offer_apply()
""", "предложение после удаления записей")

# Подтверждение после применения: лишний Enter или опечатка не должны молча откатывать правила, пока не вышло время.
menu = replace_once(menu, """def ask_confirmation(timeout):
    import select
    print(f"Правила работают? Введите «да» за {timeout} с: ", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        print()
    return bool(ready and sys.stdin.readline().strip().casefold() == "да")
""", """def ask_yes_within(prompt, again, timeout):
    import select
    import time
    # Читаем напрямую из дескриптора: если ввод пришёл пачкой (вставка, лишний Enter), буфер Python не прячет оставшиеся строки от select.
    fd = sys.stdin.fileno()
    pending = b""
    deadline = time.monotonic() + timeout
    print(prompt, end="", flush=True)
    while True:
        while b"\\n" not in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                print()
                return False
            chunk = os.read(fd, 4096)
            if not chunk:
                print()
                return False
            pending += chunk
        line, _, pending = pending.partition(b"\\n")
        raw = line.decode("utf-8", "replace").strip()
        if raw.casefold() == "да":
            return True
        if raw.casefold() in ("нет", "отмена"):
            return False
        print(explain_unclear_answer(raw, "«да», пока не вышло время; «нет» отменит"))
        print(again, end="", flush=True)


def ask_confirmation(timeout):
    return ask_yes_within(f"Правила работают? Введите «да» за {timeout} с: ", "Правила работают? Введите «да»: ", timeout)
""", "подтверждение применения: повторный вопрос")

# ---------------------------------------------------------------- SSH-ключ для входа (1.9.0)
SSH_KEY_CODE = r'''SSH_KEYS_FILE = DIRECTORY / "ssh-keys.json"
SSH_KEY_TTL = 24 * 3600
SSH_SWEEP_UNIT = "fw-ssh-key-sweep"


@contextlib.contextmanager
def ssh_keys_locked():
    with (DIRECTORY / ".sshkey.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load_ssh_keys():
    try:
        data = json.loads(SSH_KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def save_ssh_keys(items):
    write_text_atomic(SSH_KEYS_FILE, json.dumps(items, ensure_ascii=False, indent=1) + "\n")


def ssh_key_user():
    # Для кого создаётся ключ: пользователь, под которым ты вошёл (при sudo — тот, кто его вызвал), иначе root.
    import pwd
    names = [os.environ.get("SUDO_USER", "")]
    who = probe_output("who", "-m").split()
    names.append(who[0] if who else "")
    names.append(probe_output("logname").strip())
    for name in names:
        if name:
            try:
                pwd.getpwnam(name)
            except KeyError:
                continue
            return name
    return "root"


def ssh_session_sockets():
    # ((адрес_сервера, порт_сервера), (адрес_клиента, порт_клиента)) текущего SSH-подключения; None — определить не удалось.
    def split(value):
        host, _, port = value.rpartition(":")
        return host.strip("[]").partition("%")[0], int(port) if port.isdigit() else 0

    parts = os.environ.get("SSH_CONNECTION", "").split()
    if len(parts) == 4 and parts[1].isdigit() and parts[3].isdigit():
        return (parts[2], int(parts[3])), (parts[0], int(parts[1]))
    pids, pid = set(), os.getppid()
    for _ in range(32):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    for line in probe_output("ss", "-Htnp", "state", "established").splitlines():
        fields = line.split()
        if len(fields) > 4 and "sshd" in line and any(f"pid={item}," in line for item in pids):
            return split(fields[-3]), split(fields[-2])
    return None


def ssh_session_endpoint():
    # (адрес, порт), к которым ты подключился по SSH. Адрес "" — не определён или не публичный (тогда его вводит человек).
    sockets = ssh_session_sockets()
    if not sockets:
        return "", 0
    (host, port), _ = sockets
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "", port
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return "", port
    return str(ip), port


def authorized_keys_path(user):
    import pwd
    home = pwd.getpwnam(user).pw_dir
    spec = ".ssh/authorized_keys"
    for line in probe_output("sshd", "-T").splitlines():
        key, _, value = line.partition(" ")
        if key == "authorizedkeysfile" and value.split():
            spec = value.split()[0]
            break
    path = spec.replace("%h", home).replace("%u", user).replace("%%", "%")
    return Path(path if path.startswith("/") else os.path.join(home, path))


def pubkey_auth_enabled():
    for line in probe_output("sshd", "-T").splitlines():
        key, _, value = line.partition(" ")
        if key == "pubkeyauthentication":
            return value.strip().casefold() == "yes"
    return True


def public_key_blob(line):
    fields = line.split()
    for index, field in enumerate(fields[:-1]):
        if field.startswith(("ssh-", "ecdsa-", "sk-")):
            return fields[index + 1]
    return ""


def ssh_fingerprint(public_line):
    import base64
    import hashlib
    try:
        blob = base64.b64decode(public_key_blob(public_line) + "==")
    except ValueError:
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def generate_ssh_key(path, comment, with_passphrase):
    if not shutil.which("ssh-keygen"):
        raise ValueError("не найден ssh-keygen. Установи пакет openssh-client")
    command = ["ssh-keygen", "-q", "-t", "ed25519", "-C", comment, "-f", str(path)]
    if not with_passphrase:
        command += ["-N", ""]
    if subprocess.run(command, check=False).returncode or not path.exists():
        raise ValueError("ssh-keygen не создал ключ")


def add_authorized_key(user, public_line):
    import pwd
    path = authorized_keys_path(user)
    entry = pwd.getpwnam(user)
    blob = public_key_blob(public_line)
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
        os.chown(path.parent, entry.pw_uid, entry.pw_gid)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if any(public_key_blob(line) == blob for line in current.splitlines()):
        return path
    created = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        if current and not current.endswith("\n"):
            handle.write("\n")
        handle.write(public_line.strip() + "\n")
    if created:
        os.chmod(path, 0o600)
        os.chown(path, entry.pw_uid, entry.pw_gid)
    return path


def remove_authorized_key(user, public_line):
    try:
        path = authorized_keys_path(user)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, KeyError):
        return
    blob = public_key_blob(public_line)
    kept = [line for line in lines if public_key_blob(line) != blob]
    if len(kept) != len(lines):
        with path.open("w", encoding="utf-8") as handle:  # на месте: владелец и права файла не меняются
            handle.write("".join(kept))


def wipe_ssh_private(item):
    path = Path(item.get("file", ""))
    if path.is_file():
        if shutil.which("shred"):
            subprocess.run(["shred", "-u", str(path)], check=False, capture_output=True)
        path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        path.parent.rmdir()


def ssh_login_log(since):
    out = probe_output("journalctl", "--no-pager", "-o", "cat", "--since", f"@{int(since)}",
                       "-t", "sshd", "-t", "sshd-session", "-t", "ssh", timeout=15)
    if out:
        return out
    for name in ("/var/log/auth.log", "/var/log/secure"):
        try:
            return Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return ""


def ssh_key_used(fingerprint, since):
    if not fingerprint:  # пустая строка «входит» в любую строку журнала и засчитала бы чужой вход
        return False
    return any("Accepted publickey" in line and fingerprint in line for line in ssh_login_log(since).splitlines())


def schedule_ssh_sweep():
    subprocess.run(["systemd-run", "--quiet", "--collect", "--unit=" + SSH_SWEEP_UNIT, "--on-active=2min",
                    "--on-unit-active=2min", str(MENU_FILE), "--ssh-key-sweep"],
                   check=False, capture_output=True)


def sweep_ssh_keys():
    # Приватная часть удаляется после первого входа этим ключом или через сутки, если вход не состоялся.
    import time
    if not SSH_KEYS_FILE.exists():
        return
    now = time.time()
    with ssh_keys_locked():
        items, kept, changed = load_ssh_keys(), [], False
        for item in items:
            if item.get("state") == "pending":
                if ssh_key_used(item.get("fingerprint", ""), item.get("created", now)):
                    wipe_ssh_private(item)
                    item["state"], item["used"] = "used", now
                    changed = True
                elif now - item.get("created", now) > SSH_KEY_TTL:
                    wipe_ssh_private(item)
                    remove_authorized_key(item.get("user", "root"), item.get("pub", ""))
                    changed = True
                    continue
            kept.append(item)
        if changed:
            save_ssh_keys(kept)
    if not any(item.get("state") == "pending" for item in kept):
        subprocess.run(["systemctl", "stop", SSH_SWEEP_UNIT + ".timer"], check=False, capture_output=True)


def scp_hint(item):
    host, port = ssh_session_endpoint()
    if not host:
        host = "IP_ВАШЕГО_СЕРВЕРА"
    elif ":" in host:
        host = f"[{host}]"
    option = f"-P {port} " if port and port != 22 else ""
    return f"scp {option}{item['user']}@{host}:{item['file']} ."


def show_ssh_key(item):
    try:
        private = Path(item["file"]).read_text(encoding="utf-8").strip()
    except OSError:
        print("Приватная часть этого ключа уже удалена с сервера (вход им выполнен или прошли сутки).")
        return
    host, _ = ssh_session_endpoint()
    print_box("SSH-ключ создан", "\n".join([
        "Сохраните ключ на своём устройстве. Он нужен только вам.",
        f"Пользователь: {item['user']}",
        f"Отпечаток: {item['fingerprint']}",
        f"Название: {item['comment']}"]))
    print("\nСпособ 1 (надёжнее). Скопировать файлом")
    print("  Выполните на своём компьютере (не на сервере):")
    print("    " + scp_hint(item))
    if not host:
        print("  IP_ВАШЕГО_СЕРВЕРА замените на публичный адрес сервера (он есть в панели хостера).")
    print(f"  В Termius: откройте хост → SFTP → папка {Path(item['file']).parent} → скачайте файл {Path(item['file']).name}.")
    print("\nСпособ 2. Скопировать текстом")
    print("  Копируйте всё, что между линиями, включая строки BEGIN и END:")
    print("  " + "═" * 58)
    print(private)
    print("  " + "═" * 58)
    print("  Termius: Keychain → Новый ключ → вставить → сохранить.")
    print("  Компьютер: сохраните в файл с названием ключа (подойдёт Блокнот), на Linux и macOS выдайте права: chmod 600 файл.")
    print("\nПроверка: подключитесь этим ключом. Как только вход пройдёт,")
    print("скрипт сам удалит приватную часть с сервера. Если вход не состоится,")
    print("ключ останется здесь ещё сутки, его можно показать снова.")
    print("Вход по паролю остаётся как был: ключ добавляет ещё один способ входа.")


def create_ssh_key():
    import datetime as dt
    import pwd
    import time
    if not pubkey_auth_enabled():
        paint(33, "Внимание: в sshd выключен вход по ключам (PubkeyAuthentication no). Ключ создастся, но войти им не получится, пока это не изменить.")
    default = ssh_key_user()
    user = input(f"Для какого пользователя создать ключ? Enter — {default}: ").strip() or default
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        print(f"Пользователя «{user}» на сервере нет.")
        return
    print("Ключ можно защитить парольной фразой: тогда украденный файл без неё бесполезен, но фразу придётся вводить при каждом входе.")
    with_passphrase = input("Защитить ключ парольной фразой? Введи «да» (Enter — без фразы): ").strip().casefold() == "да"
    now = dt.datetime.now()
    directory = Path(entry.pw_dir) / ".fw-key"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chown(directory, entry.pw_uid, entry.pw_gid)
    stamp = now.strftime("%Y%m%d-%H%M")
    path, number = directory / f"fw-key-{stamp}.pem", 1
    while path.exists() or Path(str(path) + ".pub").exists():
        number += 1
        path = directory / f"fw-key-{stamp}-{number}.pem"
    comment = "fw-menu " + now.strftime("%Y-%m-%d")
    generate_ssh_key(path, comment, with_passphrase)
    public_path = Path(str(path) + ".pub")
    public_line = public_path.read_text(encoding="utf-8").strip()
    public_path.unlink(missing_ok=True)
    os.chmod(path, 0o600)
    os.chown(path, entry.pw_uid, entry.pw_gid)
    add_authorized_key(user, public_line)
    item = {"id": stamp if number == 1 else f"{stamp}-{number}", "user": user, "file": str(path), "pub": public_line,
            "fingerprint": ssh_fingerprint(public_line), "comment": comment, "created": time.time(), "state": "pending"}
    with ssh_keys_locked():
        save_ssh_keys(load_ssh_keys() + [item])
    schedule_ssh_sweep()
    show_ssh_key(item)


def pending_ssh_keys():
    sweep_ssh_keys()
    return [item for item in load_ssh_keys() if item.get("state") == "pending" and Path(item.get("file", "")).is_file()]


def show_ssh_key_again():
    items = pending_ssh_keys()
    if not items:
        print("Нет ключей, которые ещё ждут первого входа. Либо ключей нет, либо вход ими уже был (приватная часть удалена).")
        return
    item = items[0]
    if len(items) > 1:
        for number, entry in enumerate(items, 1):
            print(f"  {number}. {entry['comment']}, пользователь {entry['user']}")
        answer = input("Номер ключа (Enter — отмена): ").strip()
        if not answer.isdigit() or not 1 <= int(answer) <= len(items):
            return
        item = items[int(answer) - 1]
    show_ssh_key(item)


def ssh_key_state_text(item):
    import time
    if item.get("state") == "used":
        return "используется, вход этим ключом был"
    left = max(0, int(item.get("created", 0) + SSH_KEY_TTL - time.time()))
    return f"ждёт первого входа, приватная часть на сервере ещё {left // 3600} ч {left % 3600 // 60} мин"


def show_ssh_keys():
    sweep_ssh_keys()
    items = load_ssh_keys()
    if not items:
        print("Ключей, созданных через fw-menu, нет.")
        return
    for number, item in enumerate(items, 1):
        print(f"{number}. {item['comment']}, пользователь {item['user']}")
        print(f"   {item['fingerprint']}")
        print(f"   {ssh_key_state_text(item)}")
        for text in key_presence_lines(item.get("fingerprint", ""), key_log, clients):
            print(f"   {text}")


def remove_ssh_key():
    sweep_ssh_keys()
    items = load_ssh_keys()
    if not items:
        print("Ключей, созданных через fw-menu, нет.")
        return
    for number, item in enumerate(items, 1):
        print(f"  {number}. {item['comment']}, пользователь {item['user']}, {item['fingerprint']}")
    answer = input("Номер ключа для удаления (Enter — отмена): ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(items):
        return
    item = items[int(answer) - 1]
    if not confirm_yes(f"Удалить ключ «{item['comment']}» пользователя {item['user']}? Устройство с этим ключом больше не войдёт. Введи «да»: "):
        print("Отменено.")
        return
    with ssh_keys_locked():
        current = [entry for entry in load_ssh_keys() if entry.get("fingerprint") != item["fingerprint"]]
        remove_authorized_key(item["user"], item["pub"])
        wipe_ssh_private(item)
        save_ssh_keys(current)
    print("Ключ удалён с сервера. Вход по паролю и другими ключами не затронут.")


'''
menu = replace_once(menu, 'def apply_rules():\n', SSH_KEY_CODE + 'def apply_rules():\n', "SSH-ключ: код")
menu = replace_once(menu, '''            ("Ограничить частоту новых SSH-подключений", configure_ssh_limit),
''', '''            ("Ограничить частоту новых SSH-подключений", configure_ssh_limit),
            ("SSH-ключ для входа (создать, показать, удалить)", (
                ("Создать SSH-ключ для входа", create_ssh_key),
                ("Показать созданный ключ ещё раз", show_ssh_key_again),
                ("Показать ключи, созданные через fw-menu", boxed("SSH-ключи", show_ssh_keys)),
                ("Удалить ключ", remove_ssh_key),
            )),
''', "SSH-ключ: пункт меню")
menu = replace_once(menu, '    if sys.argv[1:] == ["--check-proxy"]:\n',
    '    if sys.argv[1:] == ["--ssh-key-sweep"]:\n        sweep_ssh_keys()\n        return\n'
    '    if sys.argv[1:] == ["--check-proxy"]:\n', "SSH-ключ: команда проверки")
menu = replace_once(menu, '    entries = (\n        ("Статус и наблюдение", (\n',
    '    sweep_ssh_keys()\n    entries = (\n        ("Статус и наблюдение", (\n', "SSH-ключ: проверка при запуске меню")

# ---------------------------------------------------------------- вход по паролю: выключить и вернуть, перебор на SSH (1.9.0)
SSH_PASSWORD_CODE = r'''SSH_NOPASS_FILE = Path("/etc/ssh/sshd_config.d/10-fw-menu-keys-only.conf")
SSH_NOPASS_UNIT = "fw-ssh-password-rollback"
SSH_NOPASS_WINDOW = 120
SSH_CONFIG_MAIN = Path("/etc/ssh/sshd_config")
SSH_ATTEMPT_PATTERNS = (
    re.compile(r"Invalid user (\S+) from (\S+) port (\d+)"),
    re.compile(r"Failed \S+ for (?:invalid user )?(\S+) from (\S+) port (\d+)"),
    re.compile(r"(?:Connection closed by|Connection reset by|Disconnected from) (?:invalid|authenticating) user (\S+) (\S+) port (\d+)"),
    re.compile(r"maximum authentication attempts exceeded for (?:invalid user )?(\S+) from (\S+) port (\d+)"),
)


def sshd_settings():
    result = {}
    for line in probe_output("sshd", "-T").splitlines():
        key, _, value = line.partition(" ")
        result.setdefault(key.casefold(), value.strip().casefold())
    return result


def password_login_open():
    # Вход по паролю возможен, если включён любой из двух способов: обычный пароль или ввод через PAM.
    settings = sshd_settings()
    return settings.get("passwordauthentication", "yes") != "no" or settings.get("kbdinteractiveauthentication", "yes") != "no"


def ssh_config_files():
    files = [SSH_CONFIG_MAIN]
    drop_in = SSH_CONFIG_MAIN.parent / "sshd_config.d"
    if drop_in.is_dir():
        files += sorted(drop_in.glob("*.conf"))
    return files


def ssh_config_hits(pattern):
    found = []
    for path in ssh_config_files():
        for line in read_lines(path):
            text = line.strip()
            if text and not text.startswith("#") and re.match(pattern, text, re.I):
                found.append(f"{path}: {text}")
    return found


def current_session_used_key():
    # True — это подключение прошло по ключу; False — иначе; None — подключение определить не удалось.
    import time
    sockets = ssh_session_sockets()
    if not sockets:
        return None
    _, (address, port) = sockets
    needle = f"from {address} port {port} "
    log = ssh_login_log(time.time() - 30 * 24 * 3600)
    return any("Accepted publickey" in line and needle in line for line in log.splitlines())


def reload_sshd():
    for unit in ("ssh", "sshd"):
        result = subprocess.run(["systemctl", "reload", unit], check=False, capture_output=True)
        if result.returncode == 0:
            return True
    return False


def sshd_config_valid():
    return subprocess.run(["sshd", "-t"], check=False, capture_output=True).returncode == 0


def write_nopass_file(keyword):
    SSH_NOPASS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SSH_NOPASS_FILE.write_text("# Создан fw-menu: вход только по ключам. Удаляется пунктом «Вернуть вход по паролю».\n"
                               f"PasswordAuthentication no\n{keyword} no\n", encoding="utf-8")
    os.chmod(SSH_NOPASS_FILE, 0o644)


def stop_nopass_timer():
    subprocess.run(["systemctl", "stop", SSH_NOPASS_UNIT + ".timer"], check=False, capture_output=True)


def restore_password_login(announce=True):
    existed = SSH_NOPASS_FILE.exists()
    SSH_NOPASS_FILE.unlink(missing_ok=True)
    reloaded = True
    if existed:
        reloaded = sshd_config_valid() and reload_sshd()
    stop_nopass_timer()
    if announce:
        if not existed:
            print("Отдельной настройки fw-menu нет: вход по паролю fw-menu не менял.")
        elif reloaded:
            print("Вход по паролю возвращён.")
        else:
            print("Файл настройки удалён, но sshd перезагрузить не удалось: выполните systemctl reload ssh.")
    return reloaded


def disable_password_login():
    if not password_login_open():
        print("Вход по паролю уже выключен.")
        sockets = ssh_session_sockets()
        if sockets:
            print(f"Текущее подключение: {sockets[1][0]}, порт {sockets[1][1]}.")
        return
    used = current_session_used_key()
    if used is None:
        print(textwrap.fill("Не удалось определить текущее SSH-подключение (возможно, вы в консоли хостера). "
                            "Зайдите на сервер по SSH по ключу и повторите.", width=92))
        return
    sockets = ssh_session_sockets()
    client = f"{sockets[1][0]}, порт {sockets[1][1]}" if sockets else ""
    if not used:
        print(f"Текущее подключение: {client}, вход был не по ключу.")
        print(textwrap.fill("Это подключение прошло не по ключу. Если отключить пароль сейчас, вход потеряется, "
                            "если ключ не работает. Зайдите на сервер по ключу (например, с -i файл_ключа) и повторите отсюда.", width=92))
        return
    if not any(re.match(r"include\s+.*sshd_config\.d", line.strip(), re.I) for line in read_lines(SSH_CONFIG_MAIN)):
        print(textwrap.fill("В /etc/ssh/sshd_config нет строки Include для sshd_config.d, поэтому отдельный файл настройки не подхватится. "
                            "Отключите вход по паролю вручную (PasswordAuthentication no в sshd_config).", width=92))
        return
    matches = ssh_config_hits(r"match\s")
    if matches:
        paint(33, "Внимание: в настройках sshd есть блоки Match (для отдельных пользователей или адресов). Они могут вести себя иначе:")
        for line in matches:
            print("  " + line)
    print(f"Текущее подключение: {client}, вход по ключу.")
    print(textwrap.fill("Вход по паролю закроется для всех пользователей этого сервера и для всех устройств и скриптов, "
                        "которые заходят паролем. Консоль хостера продолжит работать. Сейчас вы вошли по ключу, он останется рабочим.", width=92))
    if not confirm_yes("Отключить вход по паролю? Введи «да»: "):
        print("Отменено.")
        return
    for keyword in ("KbdInteractiveAuthentication", "ChallengeResponseAuthentication"):
        write_nopass_file(keyword)
        if sshd_config_valid():
            break
    else:
        SSH_NOPASS_FILE.unlink(missing_ok=True)
        print("sshd не принял новую настройку, ничего не изменено.")
        return
    subprocess.run(["systemd-run", "--quiet", "--collect", "--unit=" + SSH_NOPASS_UNIT,
                    f"--on-active={SSH_NOPASS_WINDOW + 30}s", str(MENU_FILE), "--ssh-password-restore"],
                   check=False, capture_output=True)
    if not reload_sshd():
        restore_password_login(announce=False)
        print("Не удалось перезагрузить sshd, настройка отменена.")
        return
    if password_login_open():
        restore_password_login(announce=False)
        print("Вход по паролю остаётся включённым: настройку перебивает другой файл. Настройка отменена. Найдено:")
        for line in ssh_config_hits(r"(passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication)\s+yes"):
            print("  " + line)
        return
    print()
    print(textwrap.fill("Теперь откройте НОВОЕ окно терминала и зайдите на сервер по ключу. "
                        "Если не получится, ничего не вводите: настройка сама вернётся.", width=92))
    if ask_yes_within(f"Зашли по ключу в новом окне? Введите «да» за {SSH_NOPASS_WINDOW} с: ",
                      "Зашли по ключу в новом окне? Введите «да»: ", SSH_NOPASS_WINDOW):
        stop_nopass_timer()
        print("Вход по паролю выключен. Вернуть: fw-menu → 2 → 6 → «Вернуть вход по паролю».")
    else:
        print("Подтверждения нет. Возвращаю вход по паролю.")
        restore_password_login()


def enable_password_login():
    restore_password_login()


def ssh_attempts(log):
    # По журналу sshd: адрес -> {"conns": множество (адрес, порт), "users": {логин: сколько раз}}. Подключение считается один раз.
    found = {}
    for line in log.splitlines():
        for pattern in SSH_ATTEMPT_PATTERNS:
            match = pattern.search(line)
            if match:
                user, address, port = match.groups()
                entry = found.setdefault(address, {"conns": set(), "users": {}})
                if port not in entry["conns"]:
                    entry["conns"].add(port)
                    entry["users"][user] = entry["users"].get(user, 0) + 1
                break
    return found


def short_owner(owner, width):
    """Сокращает только название («ИМЯ, СТРАНА, AS123»): страна и номер AS остаются целыми."""
    if len(owner) <= width:
        return owner
    name, sep, tail = owner.partition(", ")
    if not sep:
        return owner[:width - 1] + "…"
    room = width - len(tail) - 2
    return (name[:room - 1] + "…" if room > 1 else name[:1] + "…") + ", " + tail


SSH_ACCEPTED = re.compile(r"Accepted (\S+) for (\S+) from (\S+) port \d+")


def ssh_logins(log):
    """{(способ, пользователь, адрес): сколько раз} по строкам «Accepted ... for ... from ...» журнала sshd."""
    found = {}
    for line in log.splitlines():
        match = SSH_ACCEPTED.search(line)
        if match:
            key = match.groups()
            found[key] = found.get(key, 0) + 1
    return found


SSH_ACCEPTED_KEY = re.compile(r"Accepted publickey for (\S+) from (\S+) port (\d+) .*?(SHA256:\S+)")


def ssh_established_clients():
    """Множество (адрес, порт) клиентов, у которых сейчас открыто TCP-соединение с этим сервером."""
    clients = set()
    for line in probe_output("ss", "-Htn", "state", "established").splitlines():
        fields = line.split()
        if len(fields) >= 4:
            host, _, port = fields[-1].rpartition(":")
            if port.isdigit():
                clients.add((host.strip("[]").partition("%")[0], port))
    return clients


def key_logins(fingerprint, log):
    """[(пользователь, адрес, порт)] входов по ключу с этим отпечатком, по порядку журнала."""
    if not fingerprint:
        return []
    result = []
    for line in log.splitlines():
        match = SSH_ACCEPTED_KEY.search(line)
        if match and match.group(4) == fingerprint:
            result.append((match.group(1), match.group(2), match.group(3)))
    return result


def key_presence_lines(fingerprint, log, clients):
    """Строки для списка ключей: с каких адресов входили и в сети ли ключ сейчас."""
    logins = key_logins(fingerprint, log)
    if not logins:
        return []
    counts = {}
    for _user, address, _port in logins:
        counts[address] = counts.get(address, 0) + 1
    online = sorted({address for _user, address, port in logins if (address, port) in clients})
    lines = ["входили с адресов: " + ", ".join(f"{address} ({count})" for address, count in sorted(counts.items(), key=lambda item: -item[1]))]
    lines.append("сейчас в сети, адрес: " + ", ".join(online) if online else "сейчас не в сети")
    return lines


def show_ssh_logins():
    # Кто заходил на сервер за сутки: способ входа, пользователь, адрес. Видно и вход по паролю, и по ключу.
    import time
    log = ssh_login_log(time.time() - DAY)
    found = ssh_logins(log)
    print("Успешные входы по SSH за сутки (способ, пользователь, адрес, раз):")
    if not found:
        print("  нет или журнал sshd прочитать не удалось")
        return
    names = {"publickey": "по ключу", "password": "по паролю", "keyboard-interactive/pam": "по паролю (PAM)"}
    for (method, user, address), count in sorted(found.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {names.get(method, method):<16} {user:<12} {address:<39} {count}")


def show_ssh_attempts(say):
    # Нужен, когда вход только по ключам: пароли подобрать нельзя, но видно, кто пробует. True — блок показан.
    import time
    if password_login_open():
        return False
    print("\nПеребор на SSH")
    log = ssh_login_log(time.time() - DAY)
    if not log:
        say("Журнал sshd прочитать не удалось (нет journalctl и /var/log/auth.log).")
        return True
    found = ssh_attempts(log)
    if not found:
        say("Вход по паролю выключен. За последние сутки попыток входа с неверными данными в журнале sshd нет.")
        return True
    total = sum(len(entry["conns"]) for entry in found.values())
    say(f"Вход по паролю выключен, подобрать пароль нельзя. По журналу sshd за сутки: подключений с попыткой входа: {total}, "
        f"с разных адресов: {len(found)}. Самые активные (адрес, подключений, чей адрес, какие логины пробовали):")
    top = sorted(found.items(), key=lambda item: (-len(item[1]["conns"]), item[0]))[:10]
    owners = resolve_owners([address for address, _entry in top], "ssh", {})
    for address, entry in top:
        users = ", ".join(user for user, _count in sorted(entry["users"].items(), key=lambda item: (-item[1], item[0]))[:3])
        print(f"  {address:<39} {len(entry['conns']):>5}  {short_owner(owners.get(address, 'неизвестно'), 40):<40}  {users}")
    return True


'''
menu = replace_once(menu, 'def apply_rules():\n', SSH_PASSWORD_CODE + 'def apply_rules():\n', "вход по паролю: код")
menu = replace_once(menu, '''            ("SSH-ключ для входа (создать, показать, удалить)", (
                ("Создать SSH-ключ для входа", create_ssh_key),
                ("Показать созданный ключ ещё раз", show_ssh_key_again),
                ("Показать ключи, созданные через fw-menu", boxed("SSH-ключи", show_ssh_keys)),
                ("Удалить ключ", remove_ssh_key),
            )),
''', '''            ("SSH-ключ и вход по паролю", (
                ("Создать SSH-ключ для входа", create_ssh_key),
                ("Показать созданный ключ ещё раз", show_ssh_key_again),
                ("Показать ключи и состояние входа по паролю", boxed("SSH-ключи", show_ssh_keys)),
                ("Удалить ключ", remove_ssh_key),
                ("Отключить вход по паролю (только по ключу)", disable_password_login),
                ("Вернуть вход по паролю", enable_password_login),
            )),
''', "вход по паролю: меню")
menu = replace_once(menu, '''def show_ssh_keys():
    sweep_ssh_keys()
    items = load_ssh_keys()
    if not items:
''', '''def show_ssh_keys():
    sweep_ssh_keys()
    print("Вход по паролю: " + ("включён" if password_login_open() else "выключен, вход только по ключам"))
    show_ssh_logins()
    print()
    import time
    key_log = ssh_login_log(time.time() - 30 * 24 * 3600)
    clients = ssh_established_clients()
    items = load_ssh_keys()
    if not items:
''', "вход по паролю: статус")
menu = replace_once(menu, '    if sys.argv[1:] == ["--ssh-key-sweep"]:\n',
    '    if sys.argv[1:] == ["--ssh-password-restore"]:\n        restore_password_login(announce=False)\n        return\n'
    '    if sys.argv[1:] == ["--ssh-key-sweep"]:\n', "вход по паролю: команда отката")
# «Кто стучится»: блок про перебор на SSH
menu = replace_once(menu, '''    if not seen and slow:
        return
''', '''    if not seen and slow:
        show_ssh_attempts(say)
        return
''', "кто стучится: ssh при медленном nft")
menu = replace_once(menu, '''        say("Примените правила заново: «Проверить и применить правила» → «Применить правила и включить таймер».")
        return
''', '''        say("Примените правила заново: «Проверить и применить правила» → «Применить правила и включить таймер».")
        show_ssh_attempts(say)
        return
''', "кто стучится: ssh без статистики")
menu = replace_once(menu, '''    print("\\nЧего здесь нет")
    if ssh_stats:
''', '''    attempts_shown = show_ssh_attempts(say)
    print("\\nЧего здесь нет")
    if attempts_shown:
        say("Попытки входа по SSH взяты из журнала sshd (блок «Перебор на SSH»), а не из файрвола: порт SSH он не фильтрует по странам.")
    if ssh_stats:
''', "кто стучится: блок SSH")
menu = replace_once(menu, '''    else:
        say("Попытки входа по SSH сюда не попадают: порт SSH файрвол не фильтрует (смотрите журнал sshd). "''',
'''    elif not attempts_shown:
        say("Попытки входа по SSH сюда не попадают: порт SSH файрвол не фильтрует (смотрите журнал sshd). "''', "кто стучится: подпись")
menu = replace_once(menu, 'HITS_HEADINGS = ("Сканеры и сети ведомств", "Из других стран", "Превысили лимит SSH", "Чего здесь нет")',
                    'HITS_HEADINGS = ("Сканеры и сети ведомств", "Из других стран", "Превысили лимит SSH", "Перебор на SSH", "Чего здесь нет")', "кто стучится: заголовок")

# ---------------------------------------------------------------- уведомление о новой версии при входе по SSH (1.9.0)
MOTD_CODE = r"""MOTD_STATE = DIRECTORY / "motd-notice"
MOTD_CACHE = DIRECTORY / "latest-version.json"
MOTD_DIR = Path("/etc/update-motd.d")
MOTD_HOOK = MOTD_DIR / "99-fw-update"
MOTD_PROFILE_HOOK = Path("/etc/profile.d/fw-update-notice.sh")
MOTD_TTL = 24 * 3600


def motd_hook_path():
    return MOTD_HOOK if MOTD_DIR.is_dir() else MOTD_PROFILE_HOOK


def motd_hook_text(path):
    head = "# Создан fw-menu: сообщает при входе, что вышла новая версия. Выключается в fw-menu → 5 → 4.\n"
    if path == MOTD_HOOK:
        return f"#!/bin/sh\n{head}[ -x {MENU_FILE} ] && {MENU_FILE} --motd 2>/dev/null\nexit 0\n"
    return head + f'case $- in *i*) [ "$(id -u)" = 0 ] && [ -x {MENU_FILE} ] && {MENU_FILE} --motd 2>/dev/null;; esac\n'


def write_motd_state(value):
    write_text_atomic(MOTD_STATE, value + "\n")


def enable_motd_notice():
    path = motd_hook_path()
    write_text_atomic(path, motd_hook_text(path))
    os.chmod(path, 0o755 if path == MOTD_HOOK else 0o644)
    write_motd_state("on")


def disable_motd_notice():
    for path in (MOTD_HOOK, MOTD_PROFILE_HOOK):
        path.unlink(missing_ok=True)
    write_motd_state("off")


def remove_motd_notice():
    for path in (MOTD_HOOK, MOTD_PROFILE_HOOK):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def load_latest_cache():
    try:
        data = json.loads(MOTD_CACHE.read_text(encoding="utf-8"))
        return str(data.get("version", "")), float(data.get("checked", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return "", 0.0


def save_latest_cache(version, checked):
    write_text_atomic(MOTD_CACHE, json.dumps({"version": version, "checked": checked}) + "\n")


def refresh_latest_version():
    import time
    version, _ = load_latest_cache()
    try:
        releases = parse_changelog(fetch_ref("main", "CHANGELOG.md"))
        if releases:
            version = releases[0]["version"]
    except OSError:
        pass
    save_latest_cache(version, time.time())


def motd_notice():
    # Вызывается при входе по SSH: только читает кэш; устаревший кэш обновляется в фоне, вход не задерживается.
    import time
    version, checked = load_latest_cache()
    if time.time() - checked > MOTD_TTL:
        try:
            save_latest_cache(version, time.time())  # чтобы параллельные входы не запускали проверку одновременно
            subprocess.Popen([str(MENU_FILE), "--refresh-latest"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass
    if version and VERSION_RE.fullmatch(version) and version_key(version) > version_key(FW_VERSION):
        print(f"\n\033[1;33m▶ Файрвол: вышла версия {version} (у вас {FW_VERSION}). Обновить: fw-menu → 5 → 2\033[0m\n")


def motd_state():
    try:
        return MOTD_STATE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_motd_notice():
    # При первом запуске меню уведомление включается само (один раз); дальше решает состояние «on» или «off».
    if motd_state():
        return
    try:
        enable_motd_notice()
    except OSError:
        return
    print(textwrap.fill("Включено уведомление о новой версии при входе по SSH. Отключить: fw-menu → 5 → 4.", width=92))


def toggle_motd_notice():
    if motd_state() == "off":
        enable_motd_notice()
        print("Уведомление о новой версии при входе по SSH включено.")
    else:
        disable_motd_notice()
        print("Уведомление о новой версии при входе по SSH выключено.")


"""
menu = replace_once(menu, 'def apply_rules():\n', MOTD_CODE + 'def apply_rules():\n', "MOTD: код")
menu = replace_once(menu, '            ("Откатить скрипт к другой версии", rollback_script),\n        )),\n        ("Отключить или удалить"',
    '            ("Откатить скрипт к другой версии", rollback_script),\n'
    '            ("Уведомление о новой версии при входе по SSH (включить или выключить)", toggle_motd_notice),\n'
    '        )),\n        ("Отключить или удалить"', "MOTD: меню")
menu = replace_once(menu, '    if sys.argv[1:] == ["--check-proxy"]:\n',
    '    if sys.argv[1:] == ["--motd"]:\n        motd_notice()\n        return\n'
    '    if sys.argv[1:] == ["--refresh-latest"]:\n        refresh_latest_version()\n        return\n'
    '    if sys.argv[1:] == ["--check-proxy"]:\n', "MOTD: команды")
menu = replace_once(menu, '    sweep_ssh_keys()\n    entries = (\n', '    sweep_ssh_keys()\n    ensure_motd_notice()\n    entries = (\n', "MOTD: при запуске меню")
menu = replace_once(menu, '            for path in installed_files:\n                path.unlink()\n',
    '            for path in installed_files:\n                path.unlink()\n            remove_motd_notice()\n', "MOTD: удаление")

# ---------------------------------------------------------------- сборка
# ---------------------------------------------------------------- список blocklist.de в fw-menu (1.10.0)
menu = replace_once(menu, '"non-RU": "non_ru", "ssh-rate": "ssh_rate"}\n',
                    '"non-RU": "non_ru", "ssh-rate": "ssh_rate", "blocklist-ssh": "blocklist"}\n', "blocklist: вид правила")
menu = replace_once(menu, 'and rule.get("comment") != "ssh-rate":\n',
                    'and rule.get("comment") not in ("ssh-rate", "blocklist-ssh"):\n', "blocklist: правило не служебное")
menu = replace_once(menu, '"scanners", "non_ru", "ssh_rate", "other"}:',
                    '"scanners", "non_ru", "ssh_rate", "blocklist", "other"}:', "blocklist: объединение правил")
menu = replace_once(menu, '        elif kind == "ssh_rate":\n', '''        elif kind == "blocklist":
            updated = blocklist_updated()
            steps.append(CountedStep(f"{ports_phrase(group['ports'], 'SSH, порт', 'SSH, порты')}: адреса из списка blocklist.de "
                                     "(перебирали SSH за последние 48 часов) отбрасываются. "
                                     "Список обновляется вместе с правилами" + (f", обновлён {updated}." if updated else "."), group))
        elif kind == "ssh_rate":
''', "blocklist: пояснение в статусе")
menu = replace_once(menu, '("ssh_rate", "sr4"), ("ssh_rate", "sr6"))\n',
                    '("ssh_rate", "sr4"), ("ssh_rate", "sr6"), ("blocklist", "bs4"), ("blocklist", "bs6"))\n', "blocklist: наборы в меню")
menu = replace_once(menu, '"Превысили лимит SSH", "Перебор на SSH"',
                    '"Превысили лимит SSH", "Из списка blocklist.de", "Перебор на SSH"', "blocklist: заголовки")
menu = replace_once(menu, 'collected = {"scanners": [], "non_ru": [], "ssh_rate": []}\n    ages = {"scanners": {}, "non_ru": {}, "ssh_rate": {}}\n    ssh_stats = False\n',
                    'collected = {"scanners": [], "non_ru": [], "ssh_rate": [], "blocklist": []}\n'
                    '    ages = {"scanners": {}, "non_ru": {}, "ssh_rate": {}, "blocklist": {}}\n    ssh_stats = False\n    bl_stats = False\n',
                    "blocklist: сбор статистики")
menu = replace_once(menu, '            ssh_stats = ssh_stats or kind == "ssh_rate"\n',
                    '            ssh_stats = ssh_stats or kind == "ssh_rate"\n            bl_stats = bl_stats or kind == "blocklist"\n', "blocklist: набор есть")
menu = replace_once(menu, '    attempts_shown = show_ssh_attempts(say)\n', r'''    if bl_stats:
        print("\nИз списка blocklist.de")
        if collected["blocklist"]:
            summary = summarize_hits(collected["blocklist"])
            say(f"Из списка blocklist.de попыток на SSH: {summary['packets']}, адресов: {summary['sources']}")
        else:
            say("Список blocklist.de включён, пока никто из него не стучался на порты SSH.")
    attempts_shown = show_ssh_attempts(say)
''', "blocklist: строка в «Кто стучится»")
menu = replace_once(menu, 'def show_ssh_attempts(say):\n', r'''def blocklist_updated():
    # Как давно скачан список blocklist.de (по времени файла кэша); пусто, если файла нет.
    import time
    try:
        age = time.time() - (DIRECTORY / "scanner-cache" / "blocklist_ssh").stat().st_mtime
    except OSError:
        return ""
    if age < 3600:
        return "меньше часа назад"
    if age < 48 * 3600:
        return f"{int(age // 3600)} ч назад"
    return f"{int(age // 86400)} дн. назад"


def blocklist_listed(addresses):
    # Какие из адресов сейчас в наборе blocklist.de (nft get element сам ищет и среди диапазонов).
    listed = set()
    for address in addresses:
        try:
            version = ipaddress.ip_address(address).version
        except ValueError:
            continue
        answer = probe_output("nft", "-n", "get", "element", "inet", "fw", "bl4" if version == 4 else "bl6",
                              "{ " + address + " }", timeout=3)
        if "elements" in answer:
            listed.add(address)
    return listed


def show_ssh_attempts(say):
''', "blocklist: поиск адресов")
menu = replace_once(menu, '''    owners = resolve_owners([address for address, _entry in top], "ssh", {})
''', '''    owners = resolve_owners([address for address, _entry in top], "ssh", {})
    listed = blocklist_listed([address for address, _entry in top])
''', "blocklist: адреса в списке")
menu = replace_once(menu, '''{short_owner(owners.get(address, 'неизвестно'), 40):<40}  {users}")
''', '''{short_owner(owners.get(address, 'неизвестно'), 40):<40}  {users}"
              + ("  · в blocklist.de" if address in listed else ""))
    if listed:
        say("Пометка «в blocklist.de»: адрес уже в списке и блокируется на портах SSH, пока список включён и применён.")
''', "blocklist: пометка")
# Пункт стоит после подменю SSH-ключей, чтобы не сдвинуть номера, на которые ссылаются подсказки (2 → 6).
menu = replace_once(menu, '''                ("Вернуть вход по паролю", enable_password_login),
            )),
''', '''                ("Вернуть вход по паролю", enable_password_login),
            )),
            ("Блокировать на SSH адреса из списка blocklist.de", configure_blocklist_ssh),
''', "blocklist: пункт меню")
menu = replace_once(menu, '''    if "api_lock" in kinds:
        rows.append(("Допущенные к порту управления", "ap4", "ap6"))
''', '''    if "blocklist" in kinds:
        rows.append(("Адреса blocklist.de, блокируются на SSH", "bl4", "bl6"))
    if "api_lock" in kinds:
        rows.append(("Допущенные к порту управления", "ap4", "ap6"))
''', "blocklist: строка в списках статуса")
menu = replace_once(menu, 'def command_output(*command):\n', r'''BLOCKLIST_BEGIN, BLOCKLIST_END = "# BEGIN FW-MENU BLOCKLIST", "# END FW-MENU BLOCKLIST"


def blocklist_setting():
    # Действующее BLOCKLIST_SSH из fw.conf и local.conf.
    script = 'source "$1/fw.conf"; [ -f "$1/local.conf" ] && source "$1/local.conf"; printf "%s" "${BLOCKLIST_SSH:-1}"'
    return probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).strip() == "1"


def configure_blocklist_ssh():
    enabled = blocklist_setting()
    print(textwrap.fill("Список blocklist.de: адреса, которых за последние 48 часов заметили в переборе SSH на чужих серверах "
                        "(отчёты присылают администраторы по всему миру). Если включить, такие адреса не смогут подключаться "
                        "к портам SSH этой ноды. Другие порты не затрагиваются; доверенные адреса и LAN_ALLOW пропускаются раньше. "
                        "Список обновляется вместе с правилами, по таймеру (по умолчанию раз в сутки); чаще чем раз в 6 часов он не скачивается.", width=92))
    print(textwrap.fill("Осторожно: под одним адресом бывает много людей (мобильный интернет, общий NAT), и если на нём кто-то "
                        "перебирал пароли, адрес попадёт в список вместе со всеми. Свой адрес для входа добавьте в доверенные.", width=92))
    print(f"Сейчас: {'включено' if enabled else 'выключено'}")
    value = 0 if enabled else 1
    if not confirm_yes(("Выключить" if enabled else "Включить") + " блокировку адресов из blocklist.de на SSH? Введи «да» или «нет»: "):
        print("Отменено, настройка не изменена.")
        return
    managed_block = re.compile(r"(?ms)^" + re.escape(BLOCKLIST_BEGIN) + r"\n.*?^" + re.escape(BLOCKLIST_END) + r"(?:\n|$)")
    with (DIRECTORY / ".api.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = LOCAL_CONFIG.read_text(encoding="utf-8") if LOCAL_CONFIG.exists() else ""
        current, count = managed_block.subn("", current)
        if count > 1 or BLOCKLIST_BEGIN in current or BLOCKLIST_END in current:
            raise ValueError("проверь границы блока FW-MENU BLOCKLIST в local.conf")
        prefix = current.rstrip("\n") + "\n\n" if current.rstrip("\n") else ""
        write_text_atomic(LOCAL_CONFIG, f"{prefix}{BLOCKLIST_BEGIN}\nBLOCKLIST_SSH={value}\n{BLOCKLIST_END}\n")
    print("Настройка сохранена. Дальше открой «Проверить и применить правила»: проверка без применения, "
          "затем применение с подтверждением и автооткатом.")


def command_output(*command):
''', "blocklist: настройка в меню")

# ------------------------------------------------ «Статус файрвола»: адреса вместо одних пакетов (1.10.0)
menu = replace_once(menu, 'def explain_rules(items):\n', r'''STAT_SETS = {"scanners": ("ss4", "ss6"), "non_ru": ("sn4", "sn6"), "ssh_rate": ("sr4", "sr6"), "blocklist": ("bs4", "bs6")}


def stat_address_counts(items):
    # Сколько разных адресов записано в наборах статистики (пара «адрес + порт» считается один раз на адрес).
    # Группа без наборов статистики в результат не попадает.
    found = {}
    for item in items:
        data = item.get("set")
        if not data:
            continue
        for kind, names in STAT_SETS.items():
            if data.get("name") in names:
                addresses = found.setdefault(kind, set())
                for entry in data.get("elem", []):
                    value = (entry.get("elem", entry) if isinstance(entry, dict) else entry)
                    value = value.get("val", value) if isinstance(value, dict) else value
                    if isinstance(value, dict) and value.get("concat"):
                        value = value["concat"][0]
                    addresses.add(str(value))
    return {kind: len(addresses) for kind, addresses in found.items()}


def explain_rules(items):
''', "статус: подсчёт адресов")
menu = replace_once(menu, '    kinds = [group["kind"] for group in groups]\n',
                    '    kinds = [group["kind"] for group in groups]\n'
                    '    address_counts = stat_address_counts(items)\n'
                    '    for group in groups:\n'
                    '        group["addresses"] = address_counts.get(group["kind"])\n', "статус: адреса в группах")
menu = replace_once(menu, '''def dropped_line(group):
    text = dropped_text(group)
    return "     " + text[0].upper() + text[1:]
''', '''def dropped_line(group):
    text = dropped_text(group)
    return "     " + text[0].upper() + text[1:]


def addresses_word(count):
    tail = count % 100
    if 11 <= tail <= 14 or count % 10 == 0 or count % 10 >= 5:
        return "адресов"
    return "адрес" if count % 10 == 1 else "адреса"


def stats_line(group):
    # Статистика «Кто стучится» живёт 7 дней и не сбрасывается при применении правил, в отличие от счётчика пакетов.
    count = group.get("addresses")
    if count is None:
        return ""
    return f"     В статистике «Кто стучится»: {count} {addresses_word(count)} за 7 дней"
''', "статус: строка с адресами")
menu = replace_once(menu, '''            else:
                print(dropped_line(text.group))
''', '''            else:
                print(dropped_line(text.group))
            if stats_line(text.group):
                print(stats_line(text.group))
    if any(isinstance(text, CountedStep) for text in steps):
        print(textwrap.fill("Пакет — одно сетевое сообщение: одна попытка подключения состоит из нескольких пакетов. "
                            "Пакеты считаются с последнего применения правил, а статистика адресов хранится 7 дней и "
                            "при применении не сбрасывается.", width=92,
                            initial_indent="  ", subsequent_indent="  "))
''', "статус: пояснение про пакеты")
menu = replace_once(menu, '''                ("SSH_RATE_LIMIT", f"{ssh_rate} в минуту" if ssh_rate else "выключено", "включено" if ssh_rate else ""),
''', '''                ("SSH_RATE_LIMIT", f"{ssh_rate} в минуту" if ssh_rate else "выключено", "включено" if ssh_rate else ""),
                ("BLOCKLIST_SSH", "включено" if blocklist_on else "выключено", "" if blocklist_on else not_default),
''', "статус: строка настроек blocklist")
menu = replace_once(menu, '''        print("\\nНастройки")
        not_default = "не по умолчанию"
''', '''        print("\\nНастройки")
        not_default = "не по умолчанию"
        blocklist_on = blocklist_setting()
''', "статус: значение blocklist")

# ---------------------------------------------------------------- короткий «Статус файрвола» (1.10.0)
# 1 → 1 показывает коротко: итог, что включено, когда обновление, что делает каждое правило.
# 1 → 5 — прежний подробный экран (проверка файлов, профиль сервера, списки адресов, настройки).
menu = replace_once(menu, 'def show_status():\n', r'''STATUS_COMPACT = False
STATUS_SECTIONS = ("Проверка файлов и команд", "Проверка reverse proxy", "Профиль сервера", "Настройки", "Служба и таймер",
                   "Загруженные правила", "Списки адресов", "Как проверяется каждое новое входящее подключение",
                   "Технический вид правил")


def compact_status(text):
    # Из подробного текста статуса оставляет то, что важно сразу: замечания, включённое, ближайшее обновление, правила.
    blocks = []
    kept = []
    section = None
    heading = ""
    good = {"загружена", "включён", "активен", "успешно"}

    def flush():
        if section and (kept or section == "Как проверяется каждое новое входящее подключение"):
            blocks.append([heading] + kept)

    for raw in text.splitlines():
        plain = BOX_ANSI.sub("", raw).strip()
        name = next((item for item in STATUS_SECTIONS if plain.startswith(item)), None)
        if name:
            flush()
            section, heading, kept = name, raw, []
            continue
        if not plain or section is None:
            continue
        if section == "Проверка файлов и команд":
            if plain.startswith(("ПРОВЕРЬ", "Внимание")):
                kept.append(raw)
        elif section == "Проверка reverse proxy":
            if plain.startswith("Внимание"):
                kept.append(raw)
        elif section == "Профиль сервера":
            if kept or plain.startswith("Расхождения с настройками:"):
                kept.append(raw)
        elif section == "Настройки":
            if "←" in plain or not re.match(r"\w+ \.{2,}", plain):
                kept.append(raw)
        elif section == "Служба и таймер":
            label, _, value = plain.partition(" — ")
            if label.startswith(("Последнее обновление", "Следующее обновление")) or not value or value not in good:
                kept.append(raw)
        elif section in ("Загруженные правила", "Как проверяется каждое новое входящее подключение"):
            kept.append(raw)
    flush()
    lines = []
    for block in blocks:
        lines.extend(["", *block])
    lines.extend(["", "Подробно (проверка файлов, списки адресов, профиль сервера): fw-menu → 1 → 5"])
    return "\n".join(lines).strip("\n") + "\n"


def short_step(group):
    # Короткая формулировка правила для компактного статуса (None — оставить подробный текст).
    ports = ", ".join(group.get("ports") or [])
    kind = group["kind"]
    if kind == "api_lock":
        return f"Порт управления {ports}: только допущенные адреса"
    if kind == "scanners":
        return "Сканеры и сети ведомств: блокируются на всех портах"
    if kind == "blocklist":
        return f"SSH, порт {ports}: адреса из blocklist.de блокируются"
    if kind == "ssh_rate":
        return f"SSH, порт {ports}: не больше {group['rate']} новых подключений в минуту с одного адреса"
    if kind == "non_ru":
        return f"Порт {ports}: адреса не из России блокируются"
    return None


def short_counter(group):
    count = group.get("addresses")
    if count is None:
        return f"     отброшено пакетов: {group['packets']}"
    return f"     отброшено за 7 дней: {count} {addresses_word(count)}"


def show_status(compact=False):
''', "статус: короткий вид, функции")
menu = replace_once(menu, '''    completed = False
    try:
        with contextlib.redirect_stdout(buffer):
            status_body(problems)
        completed = True
    finally:
''', '''    completed = False
    global STATUS_COMPACT
    try:
        STATUS_COMPACT = compact
        with contextlib.redirect_stdout(buffer):
            status_body(problems)
        completed = True
    finally:
        STATUS_COMPACT = False
''', "статус: флаг короткого вида")
menu = replace_once(menu, '        sys.stdout.write(buffer.getvalue())\n',
                    '        sys.stdout.write(compact_status(buffer.getvalue()) if compact and completed else buffer.getvalue())\n',
                    "статус: вывод")
menu = replace_once(menu, '''        print(textwrap.fill(f"{number}. {text}", width=92, initial_indent="  ", subsequent_indent="     "))
        if isinstance(text, CountedStep):
''', '''        shown = short_step(text.group) if STATUS_COMPACT and isinstance(text, CountedStep) else None
        print(textwrap.fill(f"{number}. {shown or text}", width=92, initial_indent="  ", subsequent_indent="     "))
        if shown:
            if text.group["packets"]:
                paint(33, short_counter(text.group))
            else:
                print(short_counter(text.group))
        elif isinstance(text, CountedStep):
''', "статус: короткий шаг")
menu = replace_once(menu, '''    if any(isinstance(text, CountedStep) for text in steps):
        print(textwrap.fill("Пакет''', '''    if not STATUS_COMPACT and any(isinstance(text, CountedStep) for text in steps):
        print(textwrap.fill("Пакет''', "статус: пояснение про пакеты только в подробном")
menu = replace_once(menu, '''def settings_paths():''', '''def show_status_short():
    show_status(compact=True)


def settings_paths():''', "статус: функция для меню")
menu = replace_once(menu, '("Статус файрвола", boxed("Статус файрвола", show_status, BOX_HEADINGS)),\n',
                    '("Статус файрвола", boxed("Статус файрвола", show_status_short, BOX_HEADINGS)),\n', "статус: пункт 1 короткий")
menu = replace_once(menu, '''            ("Что ещё есть на сервере", boxed("Что ещё есть на сервере", show_server_overview)),
''', '''            ("Что ещё есть на сервере", boxed("Что ещё есть на сервере", show_server_overview)),
            ("Статус файрвола подробно", boxed("Статус файрвола подробно", show_status, BOX_HEADINGS)),
''', "статус: подробный пункт")

new_text = text
for key, replacement in (("menu", menu), ("update", upd)):
    a, b, _ = parts[key]
    # заменяем с конца, чтобы смещения не сбивались: menu идёт после update
    new_text = new_text[:a] + replacement + new_text[b:]
    # пересчитать смещения оставшихся частей после правки
    a2, b2 = heredoc(new_text, {"menu": "FW_MENU_FILE", "update": "FW_UPDATER_FILE"}[key])
    parts[key] = (a2, b2, new_text[a2:b2])

# Установщик вне heredoc также патчится при сборке.
# Установка: порт SSH под геофильтром — предупреждаем сразу, до записи файлов.
new_text = replace_once(new_text, """  *" 80 "*) echo 'Внимание: порт 80 под геофильтром сломает продление сертификата Let'\\''s Encrypt.' ;;
esac
""", """  *" 80 "*) echo 'Внимание: порт 80 под геофильтром сломает продление сертификата Let'\\''s Encrypt.' ;;
esac
# SSH-порт и геофильтр (установка)
ssh_found=$(timeout 5 sshd -T 2>/dev/null | awk '$1=="port" {print $2}')
ssh_conn=${SSH_CONNECTION:-}
ssh_conn=${ssh_conn##* }
case "$ssh_conn" in ''|*[!0-9]*) ssh_conn='' ;; esac
for ssh_port in $(printf '%s\n' $ssh_found $ssh_conn | sort -u); do
  case " $geo_ports " in
    *" $ssh_port "*) echo "Внимание: порт SSH $ssh_port входит в геофильтр — вход по SSH будет только из России и с доверенных адресов (адрес этого подключения добавится в доверенные). Если так не задумано, укажите порты без SSH." ;;
  esac
done
# конец проверки SSH-порта
""", "предупреждение про SSH-порт в установщике")

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
    '  if [ -n "$ssh_client" ] && ! sed "s/#.*//; s/[[:space:]]//g" "$FW/trusted.txt" | grep -Fxq "$ssh_client"; then\n'
    '    printf "%s  # авто, добавлен %s\\n" "$ssh_client" "$(date +%F)" >> "$FW/trusted.txt"\n'
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
    '  printf "%s  # авто, добавлен %s\\n" "$ssh_client" "$(date +%F)" > "$FW/trusted.txt"\n'
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
