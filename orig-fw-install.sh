bash <<'INSTALL_FW'
# Файрвол /opt/fw: геофильтр RU, блокировка сканеров, защита порта управления.
# Один блок и для новой установки, и для обновления существующей.
# Без диалога: FW_GEO_PORTS="443 8443" FW_PANEL_IP="203.0.113.10" FW_DEFAULT_ASN=да|нет
set -euo pipefail
umask 022

if [ "$(id -u)" -ne 0 ]; then
  echo 'Запусти блок от root.' >&2
  exit 1
fi
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *" debian "*|*" ubuntu "*) ;;
  *) echo "Нужен Debian или Ubuntu (есть: ${PRETTY_NAME:-неизвестно})." >&2; exit 1 ;;
esac

FW=/opt/fw
MENU=/usr/local/sbin/fw-menu
SERVICE=/etc/systemd/system/fw-update.service
TIMER=/etc/systemd/system/fw-update.timer

if [ -f "$FW/fw.conf" ] && [ -x "$FW/update-fw.sh" ]; then
  mode=upgrade
else
  mode=install
  for path in "$FW" "$SERVICE" "$TIMER" "$MENU"; do
    if [ -e "$path" ] || [ -L "$path" ]; then
      printf 'Найден %s, но целой установки нет. Разберись с остатками и запусти блок снова.\n' "$path" >&2
      exit 1
    fi
  done
  if command -v nft >/dev/null && nft list table inet fw >/dev/null 2>&1; then
    echo 'Таблица inet fw уже существует, а /opt/fw нет. Установка остановлена.' >&2
    exit 1
  fi
fi

has_tty() { ( : </dev/tty ) 2>/dev/null; }
# ask ПЕРЕМЕННАЯ "вопрос" "значение по умолчанию"
ask() {
  local __name=$1 __prompt=$2 __default=$3 __answer=""
  if has_tty; then
    if [ -n "$__default" ]; then __prompt="$__prompt [$__default]"; fi
    read -r -p "$__prompt: " __answer </dev/tty || __answer=""
  fi
  printf -v "$__name" '%s' "${__answer:-$__default}"
}
valid_ports() {
  local port
  [ "$#" -gt 0 ] || return 1
  for port in "$@"; do
    [[ "$port" =~ ^[1-9][0-9]{0,4}$ ]] && (( port <= 65535 )) || return 1
  done
}

echo 'Ставлю пакеты: nftables, python3, curl.'
apt-get update </dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates curl nftables python3 util-linux iproute2 </dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || {
  echo 'Нужен Python 3.9 или новее.' >&2
  exit 1
}

stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT

cat > "$stage/update-fw.sh" <<'FW_UPDATER_FILE'
#!/bin/bash
set -u
DIR=/opt/fw
CACHE=$DIR/fw.nft
SCAN_CACHE=$DIR/scanner-cache
DRY=0; [ "${1:-}" = "--dry" ] && DRY=1
fail_keep() { echo "ERR: $1 — правила не тронуты" >&2; exit 1; }
fail_save() { echo "ERR: правила применены, но $1" >&2; exit 1; }

exec 9<"$DIR" || fail_keep "не открывается $DIR"
flock -x 9 || fail_keep "не удалось заблокировать обновление"
source "$DIR/fw.conf"
[ -f "$DIR/local.conf" ] && source "$DIR/local.conf"
API_PORTS="${API_PORTS:-}"; API_ALLOW="${API_ALLOW:-}"
# Самая широкая сеть, которую можно принять из чужого списка или анонса ASN.
# Записи из extra.txt этим порогом не ограничены: их вносит администратор.
MIN_PREFIX4="${MIN_PREFIX4:-8}"; MIN_PREFIX6="${MIN_PREFIX6:-16}"
HOOK="${HOOK:-input}"
ZKEEN_MIRRORS="${ZKEEN_MIRRORS-https://raw.githubusercontent.com/jameszeroX/zkeen-ip/master/release/zkeenip.dat https://cdn.jsdelivr.net/gh/jameszeroX/zkeen-ip@master/release/zkeenip.dat https://fastly.jsdelivr.net/gh/jameszeroX/zkeen-ip@master/release/zkeenip.dat}"
[ "$HOOK" = input ] || [ "$HOOK" = prerouting ] || fail_keep "HOOK должен быть input или prerouting"
[[ "$MIN_PREFIX4" =~ ^([1-9]|[12][0-9]|3[0-2])$ ]] || fail_keep "MIN_PREFIX4 должен быть от 1 до 32"
[[ "$MIN_PREFIX6" =~ ^([1-9][0-9]?|1[01][0-9]|12[0-8])$ ]] || fail_keep "MIN_PREFIX6 должен быть от 1 до 128"
TMP=$(mktemp -d) || fail_keep "не удалось создать временный каталог"
trap 'rm -rf "$TMP"' EXIT

OPENFILTERS="OpenFilters/internet-scanners@main/cidr"
SCAN_FILES=(
  "tread-lightly/CyberOK_Skipa_ips@main/lists/skipa_cidr.txt"
  "shadow-netlab/traffic-guard-lists@main/public/skipa.list"
  "shadow-netlab/traffic-guard-lists@main/public/antiscanner.list"
  "$OPENFILTERS/censys_v4.txt"
  "$OPENFILTERS/shodan_v4.txt"
  "$OPENFILTERS/paloaltonetworks_v4.txt"
  "$OPENFILTERS/shadowserver_v4.txt"
  "$OPENFILTERS/driftnet_v4.txt"
  "$OPENFILTERS/onyphe_v4.txt"
  "$OPENFILTERS/zoomeye_v4.txt"
  "$OPENFILTERS/leakix_v4.txt"
  "$OPENFILTERS/rapid7_v4.txt"
  "$OPENFILTERS/fofa_v4.txt"
  "$OPENFILTERS/quake_v4.txt"
)
ENABLED_PRESETS="skipa antiscanner classic fofa"
if [ -f "$DIR/presets.conf" ]; then
  ENABLED_PRESETS=$(sed 's/#.*//' "$DIR/presets.conf")
fi
selected_presets=""
for preset in $ENABLED_PRESETS; do
  case "$preset" in
    skipa|antiscanner|classic|fofa) ;;
    *) fail_keep "неизвестный пресет $preset" ;;
  esac
  selected_presets+=" $preset"
done
ALL_SCAN_FILES=("${SCAN_FILES[@]}")
SCAN_FILES=()
for spec in "${ALL_SCAN_FILES[@]}"; do
  case "${spec##*/}" in
    skipa_cidr.txt|skipa.list) preset=skipa ;;
    antiscanner.list) preset=antiscanner ;;
    fofa_v4.txt|quake_v4.txt) preset=fofa ;;
    *) preset=classic ;;
  esac
  if [[ "$selected_presets " == *" $preset "* ]]; then
    SCAN_FILES+=("$spec")
  fi
done

V4='^([0-9]{1,3}\.){3}[0-9]{1,3}(/([1-9]|[12][0-9]|3[0-2]))?$'
V6='^[0-9a-fA-F]*:[0-9a-fA-F:]*(/([1-9][0-9]?|1[01][0-9]|12[0-8]))?$'
clean() { sed 's/#.*//; s/[[:space:]]//g' "$@" 2>/dev/null | grep -v '^$'; }
split() { clean "$1" | grep -E "$V4" | sort -u > "$2"; clean "$1" | grep -E "$V6" | sort -u > "$3"; }

# Свои списки с GitHub: /opt/fw/sources.txt, на строку одна запись
# «владелец/репозиторий@ветка/путь». Они проходят ту же проверку, зеркала и кэш,
# что и штатные; пресеты на них не влияют. Имя файла кэша содержит отпечаток
# записи, чтобы одноимённые файлы из разных репозиториев не затирали друг друга.
SCAN_NAMES=()
for spec in "${SCAN_FILES[@]}"; do SCAN_NAMES+=("${spec##*/}"); done
SOURCE_SPEC='^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+@[A-Za-z0-9._-]+/[A-Za-z0-9._~+/-]+$'
custom_total=0
if [ -f "$DIR/sources.txt" ]; then
  while IFS= read -r spec; do
    if ! [[ "$spec" =~ $SOURCE_SPEC ]] || [[ "$spec" == *..* || "$spec" == *//* || "$spec" == */ ]]; then
      fail_keep "некорректная запись в sources.txt: $spec"
    fi
    digest=$(printf '%s' "$spec" | sha256sum) || fail_keep "не посчитался отпечаток записи $spec"
    SCAN_FILES+=("$spec")
    SCAN_NAMES+=("custom_${digest:0:8}_${spec##*/}")
    custom_total=$((custom_total+1))
  done < <(clean "$DIR/sources.txt")
fi

# normalize_scanners ИСТОЧНИК НАЗНАЧЕНИЕ [МИН_ПРЕФИКС_V4 МИН_ПРЕФИКС_V6]
# Без порогов отклоняется только сеть, покрывающая всё семейство адресов.
normalize_scanners() {
  python3 - "$1" "$2" "${3:-1}" "${4:-1}" <<'PY'
import ipaddress
from pathlib import Path
import sys

source, destination = map(Path, sys.argv[1:3])
minimum = {4: int(sys.argv[3]), 6: int(sys.argv[4])}
by_version = {4: [], 6: []}
try:
    for raw in source.read_text(encoding="utf-8-sig").splitlines():
        value = raw.partition("#")[0].strip()
        if not value:
            continue
        network = ipaddress.ip_network(value)
        if network.prefixlen < minimum[network.version]:
            raise ValueError(f"слишком широкая сеть {network} (порог /{minimum[network.version]})")
        by_version[network.version].append(network)
    lines = []
    for networks in by_version.values():
        for network in ipaddress.collapse_addresses(networks):
            if network.prefixlen == 0:
                raise ValueError("scanner list covers an entire IP family")
            lines.append(str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network))
    destination.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
except (OSError, ValueError) as error:
    sys.exit(str(error))
PY
}

# asn_prefixes ASN ОТВЕТ_RIPESTAT НАЗНАЧЕНИЕ
# Проверяет ответ и пишет анонсированные сети. Пустой файл — у ASN нет анонсов.
asn_prefixes() {
  python3 - "$1" "$2" "$3" "$MIN_PREFIX4" "$MIN_PREFIX6" <<'PY'
import ipaddress
import json
from pathlib import Path
import sys

asn, source, destination = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
minimum = {4: int(sys.argv[4]), 6: int(sys.argv[5])}
try:
    payload = json.loads(source.read_text(encoding="utf-8"))
    resource = str(payload["data"]["resource"]).upper().removeprefix("AS")
    if payload["status"] != "ok" or resource != asn[2:]:
        raise ValueError("неожиданный ответ RIPEstat")
    lines = []
    for item in payload["data"]["prefixes"]:
        network = ipaddress.ip_network(item["prefix"])
        if network.prefixlen < minimum[network.version]:
            raise ValueError(f"слишком широкая сеть {network} (порог /{minimum[network.version]})")
        lines.append(str(network))
    destination.write_text("".join(line + "\n" for line in sorted(set(lines))), encoding="utf-8")
except (OSError, AttributeError, KeyError, TypeError, ValueError) as error:
    sys.exit(str(error) or "некорректный ответ RIPEstat")
PY
}

# fetch_asn ASN НАЗНАЧЕНИЕ
# Сначала RIPEstat, затем ежедневный снимок ipverse (обычные файлы на GitHub).
# Код 0 — ответ получен (пустой файл значит, что у ASN нет анонсов),
# код 1 — не ответил ни один источник.
fetch_asn() {
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
  for base in \
    "https://raw.githubusercontent.com/ipverse/as-ip-blocks/master/as/$number" \
    "https://cdn.jsdelivr.net/gh/ipverse/as-ip-blocks@master/as/$number"; do
    curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$base/ipv4-aggregated.txt" > "$TMP/asn_alt" || continue
    curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$base/ipv6-aggregated.txt" >> "$TMP/asn_alt" || continue
    if reason=$(normalize_scanners "$TMP/asn_alt" "$destination" "$MIN_PREFIX4" "$MIN_PREFIX6" 2>&1) && [ -s "$destination" ]; then
      echo "Запасной источник для $asn: ipverse"
      return 0
    fi
    rm -f -- "$destination"
    echo "WARN: данные ipverse по $asn отклонены: ${reason:-пусто}" >&2
  done
  return 1
}

# Если часть данных не получена, обновление повторяется через 15 минут,
# но не больше четырёх раз за сутки, чтобы мёртвый источник не крутил его вечно.
schedule_retry() {
  local today count=0 stamp
  [ "$DRY" = 0 ] || return 0
  command -v systemd-run >/dev/null || return 0
  today=$(date +%F)
  if read -r stamp count 2>/dev/null < "$DIR/.retry" && [ "$stamp" = "$today" ] && [[ "$count" =~ ^[0-9]+$ ]]; then :; else count=0; fi
  [ "$count" -lt 4 ] || { echo "WARN: повторы на сегодня исчерпаны, следующий запуск по таймеру" >&2; return 0; }
  if systemd-run --quiet --collect --unit=fw-update-retry --on-active=15min /bin/systemctl start fw-update.service 2>/dev/null; then
    printf '%s %s\n' "$today" "$((count+1))" > "$DIR/.retry"
    echo "Повтор обновления назначен через 15 минут."
  fi
}

# unpack_geoip ФАЙЛ_DAT КАТЕГОРИЯ НАЗНАЧЕНИЕ
# Разбирает geoip.dat (protobuf GeoIPList) без сторонних утилит.
unpack_geoip() {
  python3 - "$1" "$2" "$3" <<'PY'
import ipaddress
from pathlib import Path
import sys


def varint(data, position):
    value = shift = 0
    while True:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, position


def fields(data):
    position, size = 0, len(data)
    while position < size:
        key, position = varint(data, position)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, position = varint(data, position)
        elif kind == 2:
            length, position = varint(data, position)
            value = data[position:position + length]
            position += length
            if len(value) != length:
                raise ValueError("файл обрезан")
        elif kind in (1, 5):
            width = 8 if kind == 1 else 4
            value = data[position:position + width]
            position += width
        else:
            raise ValueError("это не geoip.dat")
        yield number, kind, value
    if position != size:
        raise ValueError("файл обрезан")


def unpack(path, tag):
    networks, found = [], False
    for number, kind, entry in fields(memoryview(Path(path).read_bytes())):
        if number != 1 or kind != 2:
            continue
        code, cidrs = "", []
        for inner, inner_kind, value in fields(entry):
            if inner == 1 and inner_kind == 2:
                code = bytes(value).decode("utf-8")
            elif inner == 2 and inner_kind == 2:
                cidrs.append(value)
        if code.lower() != tag.lower():
            continue
        found = True
        for cidr in cidrs:
            address, prefix = b"", 0
            for inner, inner_kind, value in fields(cidr):
                if inner == 1 and inner_kind == 2:
                    address = bytes(value)
                elif inner == 2 and inner_kind == 0:
                    prefix = value
            if len(address) not in (4, 16):
                raise ValueError("адрес неожиданной длины")
            networks.append(ipaddress.ip_network((address, prefix), strict=False))
    if not found:
        raise ValueError(f"в файле нет категории {tag}")
    return networks


try:
    result = unpack(sys.argv[1], sys.argv[2])
    Path(sys.argv[3]).write_text("".join(f"{network}\n" for network in result), encoding="utf-8")
except (OSError, IndexError, ValueError, UnicodeDecodeError) as error:
    sys.exit(str(error) or "не удалось разобрать геобазу")
PY
}

fetch_scanner_file() {
  local spec=$1 destination=$2 url reason
  for url in \
    "https://raw.githubusercontent.com/${spec%%@*}/${spec#*@}" \
    "https://cdn.jsdelivr.net/gh/$spec" \
    "https://fastly.jsdelivr.net/gh/$spec"; do
    curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 4 --max-time 10 "$url" > "$TMP/one" || continue
    if ! reason=$(normalize_scanners "$TMP/one" "$destination" "$MIN_PREFIX4" "$MIN_PREFIX6" 2>&1); then
      echo "WARN: ${spec##*/} с ${url#https://} отклонён: $reason" >&2
      continue
    fi
    [ -s "$destination" ] || continue
    case "$url" in
      https://raw.githubusercontent.com/*) ;;
      *) printf 'Зеркало для %s: %s\n' "${spec##*/}" "${url#https://}" ;;
    esac
    return 0
  done
  return 1
}

# 1. RU из zkeen-ip
# Основной адрес и зеркала: тот же файл лежит в репозитории zkeen-ip.
# Адрес подходит, только если файл скачался, разобрался и список не подозрительно мал.
geo_ok=0
for url in "$ZKEEN_URL" $ZKEEN_MIRRORS; do
  if ! curl -fsSL --proto '=https' --proto-redir '=https' --connect-timeout 8 --max-time 60 -o "$TMP/geo.dat" "$url"; then
    echo "WARN: геобаза не скачалась с ${url#https://}" >&2
    continue
  fi
  if ! reason=$(unpack_geoip "$TMP/geo.dat" "$GEO_TAG" "$TMP/ru_raw" 2>&1); then
    echo "WARN: геобаза с ${url#https://} не разобрана: $reason" >&2
    continue
  fi
  split "$TMP/ru_raw" "$TMP/ru4" "$TMP/ru6"
  n=$(wc -l < "$TMP/ru4")
  if [ "$n" -lt "$MIN_RU" ] || [ ! -s "$TMP/ru6" ]; then
    echo "WARN: геобаза с ${url#https://} неполная: IPv4 $n (нужно от $MIN_RU), IPv6 $(wc -l < "$TMP/ru6")" >&2
    continue
  fi
  [ "$url" = "$ZKEEN_URL" ] || echo "Зеркало для геобазы: ${url#https://}"
  geo_ok=1
  break
done
if [ "$geo_ok" = 0 ]; then
  schedule_retry
  fail_keep "геобаза недоступна ни по одному адресу"
fi

# 2. Сканеры: списки + ASN + extra
: > "$TMP/scan_raw"; ok=0; cached=0; missing=0
# Источник без свежих данных и без кэша пропускается с предупреждением:
# геофильтр и остальные списки важнее, чем одна недоступная ссылка.
mkdir "$TMP/scanner" "$TMP/asn" || fail_keep "не удалось создать временный каталог списков"
for index in "${!SCAN_FILES[@]}"; do
  spec=${SCAN_FILES[$index]}
  name=${SCAN_NAMES[$index]}
  label=$name
  case "$name" in custom_*) label=$spec ;; esac
  fresh="$TMP/scanner/$name"
  if fetch_scanner_file "$spec" "$fresh"; then
    source_list="$fresh"
    ok=$((ok+1))
  elif [ -s "$SCAN_CACHE/$name" ]; then
    source_list="$SCAN_CACHE/$name"
    cached=$((cached+1))
    echo "WARN: источник $label недоступен или пуст, использую кэш" >&2
  else
    missing=$((missing+1))
    echo "WARN: источник $label недоступен или пуст и кэша нет — в этот раз без него" >&2
    continue
  fi
  cat "$source_list" >> "$TMP/scan_raw" || fail_keep "не прочитался список $name"
  printf '\n' >> "$TMP/scan_raw"
done
asn_total=0; asn_cached=0
for asn in $(clean "$DIR/asn.txt"); do
  asn=${asn^^}
  [[ "$asn" =~ ^(AS)?[0-9]{1,10}$ ]] || fail_keep "некорректная запись в asn.txt: $asn"
  asn="AS${asn#AS}"
  asn_total=$((asn_total+1))
  asn_cache="$DIR/asn_$asn.cache"
  fresh="$TMP/asn/$asn"
  answered=0
  fetch_asn "$asn" "$fresh" && answered=1
  if [ -s "$fresh" ]; then
    asn_source="$fresh"
  elif [ -s "$asn_cache" ]; then
    asn_source="$asn_cache"
    asn_cached=$((asn_cached+1))
    if [ "$answered" = 1 ]; then
      echo "WARN: у $asn сейчас нет анонсов, использую кэш" >&2
    else
      echo "WARN: для $asn использую кэш" >&2
    fi
  elif [ "$answered" = 1 ]; then
    echo "WARN: у $asn нет анонсов и кэша нет — по нему ничего не блокируется" >&2
    continue
  else
    missing=$((missing+1))
    echo "WARN: нет данных по $asn и кэша нет — в этот раз без него" >&2
    continue
  fi
  cat "$asn_source" >> "$TMP/scan_raw" || fail_keep "не прочитался список $asn"
  printf '\n' >> "$TMP/scan_raw"
done
cat "$DIR/extra.txt" >> "$TMP/scan_raw" 2>/dev/null
normalize_scanners "$TMP/scan_raw" "$TMP/scan_normalized" || fail_keep "список сканеров некорректен"
split "$TMP/scan_normalized" "$TMP/sc4" "$TMP/sc6"

# 3. Trusted: общий + локальный
cat "$DIR/trusted.txt" "$DIR/trusted.local.txt" 2>/dev/null > "$TMP/tr_raw"
split "$TMP/tr_raw" "$TMP/tr4" "$TMP/tr6"

# 4. API-доступ
echo "$API_ALLOW" | tr ' ,' '\n\n' > "$TMP/api_raw"
split "$TMP/api_raw" "$TMP/ap4" "$TMP/ap6"
if [ -n "$API_PORTS" ] && [ ! -s "$TMP/ap4" ] && [ ! -s "$TMP/ap6" ]; then
  fail_keep "API_PORTS задан, а API_ALLOW пустой — так закрылся бы доступ панели"
fi

# 5. Сборка nft
set_def() { echo "  set $1 { type $2; flags interval; auto-merge;"; [ -s "$3" ] && echo "    elements = { $(paste -sd, "$3") }"; echo "  }"; }
P="{ $(echo $GEO_PORTS | tr ' ' ',') }"
{
  echo "add table inet fw"
  echo "delete table inet fw"
  echo "table inet fw {"
  set_def tr4 ipv4_addr "$TMP/tr4"; set_def tr6 ipv6_addr "$TMP/tr6"
  set_def sc4 ipv4_addr "$TMP/sc4"; set_def sc6 ipv6_addr "$TMP/sc6"
  set_def ru4 ipv4_addr "$TMP/ru4"; set_def ru6 ipv6_addr "$TMP/ru6"
  set_def ap4 ipv4_addr "$TMP/ap4"; set_def ap6 ipv6_addr "$TMP/ap6"
  echo "  chain input {"
  if [ "$HOOK" = prerouting ]; then
    # Для портов, опубликованных из Docker-контейнера в bridge-сети: такой
    # трафик идёт через forward и на хуке input не виден. Здесь порт ещё
    # внешний (до DNAT). Всё, что адресовано не этому серверу, пропускается.
    echo "    type filter hook prerouting priority -150; policy accept;"
    echo "    iif \"lo\" accept"
    echo "    fib daddr type != local accept"
  else
    echo "    type filter hook input priority -10; policy accept;"
    echo "    iif \"lo\" accept"
  fi
  # Ответы на соединения, которые открыл сам сервер (в том числе xray за
  # клиентов), проходят всегда: блокировки ниже касаются только новых входящих.
  echo "    ct state established,related accept"
  if [ -n "$API_PORTS" ]; then
    AP="{ $(echo $API_PORTS | tr ' ' ',') }"
    echo "    tcp dport $AP ip saddr != @ap4 counter drop comment \"api-lock\""
    echo "    tcp dport $AP ip6 saddr != @ap6 counter drop comment \"api-lock\""
    # Сюда доходят только адреса из API_ALLOW: списки сканеров и геофильтр
    # не должны отрезать панель или релей от порта управления.
    echo "    tcp dport $AP accept comment \"api-allow\""
  fi
  echo "    ip saddr @tr4 accept"
  echo "    ip6 saddr @tr6 accept"
  echo "    ip saddr @sc4 counter drop comment \"scanners\""
  echo "    ip6 saddr @sc6 counter drop comment \"scanners\""
  echo "    meta l4proto { tcp, udp } th dport $P ip saddr != @ru4 counter drop comment \"non-RU\""
  echo "    meta l4proto { tcp, udp } th dport $P ip6 saddr != @ru6 counter drop comment \"non-RU\""
  echo "  }"
  echo "}"
} > "$TMP/new.nft"

nft -c -f "$TMP/new.nft" || fail_keep "nft -c не прошёл"
echo "RU v4/v6: $(wc -l < "$TMP/ru4")/$(wc -l < "$TMP/ru6") | сканеры v4/v6: $(wc -l < "$TMP/sc4")/$(wc -l < "$TMP/sc6") | trusted: $(cat "$TMP/tr4" "$TMP/tr6" | wc -l) | API: ${API_PORTS:-off} <- $(cat "$TMP/ap4" "$TMP/ap6" | wc -l) адр. | списков $ok/${#SCAN_FILES[@]} (своих $custom_total) | из кэша $cached | ASN $asn_total, из кэша $asn_cached | без данных $missing"
if [ "$DRY" = 1 ]; then echo "DRY: синтаксис ок, ничего не применено"; exit 0; fi
nft -f "$TMP/new.nft" || fail_keep "nft не применил новые правила"
cp "$TMP/new.nft" "$CACHE" || fail_save "не сохранилась копия правил"
if [ "$ok" -gt 0 ]; then
  mkdir -p "$SCAN_CACHE" || fail_save "не создался каталог кэша списков"
  for fresh in "$TMP/scanner/"*; do
    [ -s "$fresh" ] || continue
    name=${fresh##*/}
    staged_cache=$(mktemp "$SCAN_CACHE/.new.XXXXXXXX") || fail_save "не создался временный файл для $name"
    if ! cat "$fresh" > "$staged_cache" || ! mv -fT "$staged_cache" "$SCAN_CACHE/$name"; then
      rm -f -- "$staged_cache"
      fail_save "не сохранился кэш $name"
    fi
  done
fi
# Кэш своих списков, убранных из sources.txt, больше не нужен.
for stale in "$SCAN_CACHE"/custom_*; do
  [ -f "$stale" ] || continue
  keep=0
  for name in "${SCAN_NAMES[@]}"; do
    if [ "${stale##*/}" = "$name" ]; then keep=1; break; fi
  done
  [ "$keep" = 1 ] || rm -f -- "$stale"
done
for fresh in "$TMP/asn/"*; do
  [ -s "$fresh" ] || continue
  name=${fresh##*/}
  staged_cache=$(mktemp "$DIR/.asn-new.XXXXXXXX") || fail_save "не создался временный файл для $name"
  if ! cat "$fresh" > "$staged_cache" || ! mv -fT "$staged_cache" "$DIR/asn_$name.cache"; then
    rm -f -- "$staged_cache"
    fail_save "не сохранился кэш $name"
  fi
done
if [ "$missing" -gt 0 ]; then schedule_retry; else rm -f -- "$DIR/.retry"; fi
echo "OK: применено"
FW_UPDATER_FILE

cat > "$stage/fw-menu" <<'FW_MENU_FILE'
#!/usr/bin/python3
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap


DIRECTORY = Path("/opt/fw")
TRUSTED = DIRECTORY / "trusted.txt"
UPDATER = DIRECTORY / "update-fw.sh"
ASN_FILE = DIRECTORY / "asn.txt"
EXTRA = DIRECTORY / "extra.txt"
PRESETS_FILE = DIRECTORY / "presets.conf"
SOURCES = DIRECTORY / "sources.txt"
SOURCE_NAME = r"[A-Za-z0-9._-]+"
SOURCE_PATH = r"[A-Za-z0-9._~+-]+(?:/[A-Za-z0-9._~+-]+)*"
PRESETS = {
    "skipa": "RU-сканеры (Skipa)",
    "antiscanner": "Дополнительные сканеры (AntiScanner)",
    "classic": "Классические сканеры",
    "fofa": "FOFA + Quake",
}
LOCAL_CONFIG = DIRECTORY / "local.conf"
MENU_FILE = Path("/usr/local/sbin/fw-menu")
SERVICE_FILE = Path("/etc/systemd/system/fw-update.service")
TIMER_FILE = Path("/etc/systemd/system/fw-update.timer")
SCHEDULE_FILE = TIMER_FILE.with_name(TIMER_FILE.name + ".d") / "10-fw-menu-schedule.conf"
REINSTALL_GUARD = "fw_reinstall_guard"
PROXY_NAMES = {
    "nginx", "nginx-proxy", "nginx-proxy-manager", "openresty", "swag",
    "caddy", "traefik", "haproxy", "envoy", "apache2", "httpd",
}
INBOUND_NAMES = {"xray", "rw-core"}
PANEL_NAMES = {"x-ui", "3x-ui"}
NETWORK_PROCESSES = PROXY_NAMES | INBOUND_NAMES | PANEL_NAMES | {"rw-node"}


def owner_name(raw):
    # 3x-ui запускает ядро как xray-linux-<arch>; в ss имя обрезано до 15 символов.
    return "xray" if raw.startswith("xray-linux") else raw


def process_role(name):
    if name in INBOUND_NAMES:
        return "inbound"
    if name == "rw-node":
        return "node"
    if name in PANEL_NAMES:
        return "panel"
    if name in PROXY_NAMES:
        return "proxy"
    return None


def parse_network(value):
    if "%" in value:
        raise ValueError("укажи адрес без зоны интерфейса")
    network = ipaddress.ip_network(value)
    if network.prefixlen == 0:
        raise ValueError("сеть /0 открыла бы доступ всем адресам")
    return network


def ssh_client():
    return os.environ.get("SSH_CONNECTION", "").partition(" ")[0]


def entries(path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            value = "".join(line.partition("#")[0].split())
            if value:
                yield value


def networks_from_file(path):
    for value in entries(path):
        try:
            yield parse_network(value)
        except ValueError:
            continue


def trusted_networks():
    for path in (TRUSTED, DIRECTORY / "trusted.local.txt"):
        yield from networks_from_file(path)


def network_text(network):
    if network.prefixlen == network.max_prefixlen:
        return str(network.network_address)
    return str(network)


def write_text_atomic(path, text):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as staged:
        staged_path = Path(staged.name)
        try:
            staged.write(text)
            staged.close()
            staged_path.replace(path)
        finally:
            staged_path.unlink(missing_ok=True)


def append_entries(path, values):
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current and not current.endswith("\n"):
        current += "\n"
    write_text_atomic(path, current + "\n".join(values) + "\n")


def add_trusted():
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


def show_trusted():
    for path in (TRUSTED, DIRECTORY / "trusted.local.txt"):
        if path.exists():
            print(f"{path}:")
            print("\n".join(entries(path)) or "(пусто)")


def parse_asn(value):
    number = r"(?:AS)?([0-9]{1,10})"
    match = re.fullmatch(number, value, re.IGNORECASE)
    if not match:
        site = (
            r"(?:https?://)?(?:www\.)?"
            r"(?:bgp\.tools/as/|stat\.ripe\.net/(?:resource/)?|bgp\.he\.net/|ipinfo\.io/)"
        )
        match = re.fullmatch(site + number + r"/?(?:[?#]\S*)?", value, re.IGNORECASE)
    if not match or not 1 <= int(match[1]) <= 4294967295:
        raise ValueError("введи номер или прямую ссылку на ASN в bgp.tools, RIPEstat, Hurricane Electric или IPinfo")
    return f"AS{int(match[1])}"


def announced_prefixes(asn):
    url = f"https://stat.ripe.net/data/announced-prefixes/data.json?resource={asn}"
    result = subprocess.run(["curl", "-fsS", "--max-time", "30", url],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"RIPEstat не ответил для {asn}: {result.stderr.strip() or 'запрос не прошёл'}")
    try:
        payload = json.loads(result.stdout)
        resource = str(payload["data"]["resource"]).upper().removeprefix("AS")
        if payload["status"] != "ok" or resource != asn[2:]:
            raise ValueError("неожиданный результат")
        networks = {parse_network(item["prefix"]) for item in payload["data"]["prefixes"]}
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"RIPEstat вернул некорректный ответ для {asn}") from error
    return sorted(networks, key=lambda network: (network.version, network))


def lookup_asn():
    value = input("Номер ASN или прямая ссылка на его страницу: ").strip()
    if not value:
        return
    asn = parse_asn(value)
    networks = announced_prefixes(asn)
    if not networks:
        print(f"RIPEstat не вернул подсети для {asn}.")
        return
    for version in (4, 6):
        prefixes = [str(network) for network in networks if network.version == version]
        print(f"{asn} IPv{version}: {len(prefixes)}")
        for prefix in prefixes:
            print(f"  {prefix}")

    if asn in {entry.upper() for entry in entries(ASN_FILE)}:
        print(f"{asn} уже есть в {ASN_FILE}.")
        return
    if input(f"Добавить {asn} в список блокировки? Введи «да»: ").strip().casefold() != "да":
        return
    with (DIRECTORY / ".asn.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if asn in {entry.upper() for entry in entries(ASN_FILE)}:
            print(f"{asn} уже есть в {ASN_FILE}.")
            return
        append_entries(ASN_FILE, [asn])
    print(f"{asn} сохранён. Чтобы обновить правила, открой «Проверить и применить правила».")


def extra_covers(network):
    for existing in networks_from_file(EXTRA):
        if existing.version == network.version and network.subnet_of(existing):
            print(f"Запись {network_text(existing)} в extra.txt уже покрывает {network_text(network)}.")
            return True
    return False


def add_blocked_address():
    value = input("IPv4, IPv6 или подсеть для блокировки: ").strip()
    if not value:
        return
    network = parse_network(value)
    address = network_text(network)
    if extra_covers(network):
        return
    if input(f"Добавить в extra.txt запись {address}? Введи «да»: ").strip().casefold() != "да":
        return
    with (DIRECTORY / ".extra.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if extra_covers(network):
            return
        append_entries(EXTRA, [address])
    print(f"Добавлено в extra.txt: {address}. Дальше открой «Проверить и применить правила».")


def parse_source(value):
    """Ссылка на файл в репозитории GitHub → запись «владелец/репозиторий@ветка/путь»."""
    value = re.sub(r"[?#].*$", "", value.strip())
    repo = rf"({SOURCE_NAME}/{SOURCE_NAME})"
    branch = rf"(?:refs/heads/)?({SOURCE_NAME})"
    path = rf"({SOURCE_PATH})"
    patterns = (
        rf"{repo}@({SOURCE_NAME})/{path}",
        rf"https://raw\.githubusercontent\.com/{repo}/{branch}/{path}",
        rf"https://(?:www\.)?github\.com/{repo}/(?:blob|raw)/{branch}/{path}",
        rf"https://(?:cdn|fastly)\.jsdelivr\.net/gh/{repo}@({SOURCE_NAME})/{path}",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match:
            spec = f"{match[1]}@{match[2]}/{match[3]}"
            if ".." not in spec:
                return spec
    raise ValueError("нужна ссылка на файл в репозитории GitHub: адрес страницы файла или кнопки Raw. "
                     "Ветки с «/» в имени и файлы из релизов не поддерживаются")


def fetch_source(spec):
    repo, _, rest = spec.partition("@")
    for url in (f"https://raw.githubusercontent.com/{repo}/{rest}",
                f"https://cdn.jsdelivr.net/gh/{spec}",
                f"https://fastly.jsdelivr.net/gh/{spec}"):
        result = subprocess.run(
            ["curl", "-fsSL", "--proto", "=https", "--proto-redir", "=https",
             "--connect-timeout", "4", "--max-time", "15", url],
            capture_output=True, check=False)
        if result.returncode == 0:
            return result.stdout.decode("utf-8-sig")
    raise ValueError("список не скачался ни с GitHub, ни с зеркал jsDelivr. Проверь ссылку и ветку")


def prefix_limits():
    script = (
        'source "$1/fw.conf"; [ -f "$1/local.conf" ] && source "$1/local.conf"; '
        'printf "%s %s\\n" "${MIN_PREFIX4:-8}" "${MIN_PREFIX6:-16}"'
    )
    values = probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).split()
    if len(values) == 2 and all(value.isdigit() for value in values):
        return {4: int(values[0]), 6: int(values[1])}
    return {4: 8, 6: 16}


def source_networks(text, limits):
    """Проверяет список так же, как скрипт обновления: строка — IP или подсеть."""
    networks = []
    for number, raw in enumerate(text.splitlines(), 1):
        value = raw.partition("#")[0].strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value)
        except ValueError:
            raise ValueError(f"строка {number} не IP и не подсеть: {value[:60]}") from None
        if network.prefixlen < limits[network.version]:
            raise ValueError(f"слишком широкая сеть {network} (порог /{limits[network.version]})")
        networks.append(network)
    if not networks:
        raise ValueError("в списке нет ни одного адреса")
    return networks


def add_source():
    current = list(entries(SOURCES))
    if current:
        print("Свои списки:")
        for value in current:
            print(f"  {value}")
    value = input("Ссылка на файл списка в репозитории GitHub (Enter — отмена): ").strip()
    if not value:
        return
    spec = parse_source(value)
    if spec in current:
        print(f"Список {spec} уже добавлен.")
        return
    networks = source_networks(fetch_source(spec), prefix_limits())
    print(f"Список {spec}")
    for version in (4, 6):
        merged = list(ipaddress.collapse_addresses(n for n in networks if n.version == version))
        note = f", адресов {sum(n.num_addresses for n in merged)}" if version == 4 and merged else ""
        print(f"  IPv{version}: записей {len(merged)}{note}")
    client = ssh_client()
    if client:
        address = ipaddress.ip_address(client)
        if any(address in network for network in networks) and not any(
                address in network for network in trusted_networks()):
            print("Адрес этого SSH-подключения есть в списке и не внесён в доверенные: после применения "
                  "правил связь оборвётся. Сначала добавь его в «Настроить доступ».")
            return
    print("Адреса из списка блокируются на всех портах. Доверенные адреса и API_ALLOW проходят раньше.")
    if input("Добавить список в блокировку? Введи «да»: ").strip().casefold() != "да":
        return
    with (DIRECTORY / ".sources.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if spec in set(entries(SOURCES)):
            print(f"Список {spec} уже добавлен.")
            return
        append_entries(SOURCES, [spec])
    print("Список сохранён и будет обновляться вместе со штатными. "
          "Чтобы обновить правила, открой «Проверить и применить правила».")


def manage_presets():
    while True:
        enabled = set(entries(PRESETS_FILE)) if PRESETS_FILE.exists() else set(PRESETS)
        unknown = enabled - PRESETS.keys()
        if unknown:
            raise ValueError(f"неизвестные пресеты в {PRESETS_FILE}: {', '.join(sorted(unknown))}")
        names = list(PRESETS)
        print("\nПресеты блокировки")
        for number, name in enumerate(names, 1):
            print(f"{number}. {PRESETS[name]} — {'включён' if name in enabled else 'выключен'}")
        print("0. Назад")
        choice = input("Выбери пресет для переключения: ").strip()
        if choice == "0":
            return
        if choice not in {str(number) for number in range(1, len(names) + 1)}:
            continue
        name = names[int(choice) - 1]
        action = "Выключить" if name in enabled else "Включить"
        if input(f"{action} {PRESETS[name]}? Введи «да»: ").strip().casefold() != "да":
            continue
        with (DIRECTORY / ".presets.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            latest = set(entries(PRESETS_FILE)) if PRESETS_FILE.exists() else set(PRESETS)
            if name in enabled:
                latest.discard(name)
            else:
                latest.add(name)
            write_text_atomic(PRESETS_FILE, "".join(key + "\n" for key in PRESETS if key in latest))
        print("Выбор сохранён. Чтобы обновить правила, открой «Проверить и применить правила».")


def remove_manual_entries():
    print("1. Удалить ручные IP или подсети\n2. Удалить ASN\n3. Удалить свой список с GitHub\n0. Назад")
    choice = input("Выбор: ").strip()
    kinds = {
        "1": (EXTRA, parse_network, ".extra.lock"),
        "2": (ASN_FILE, parse_asn, ".asn.lock"),
        "3": (SOURCES, parse_source, ".sources.lock"),
    }
    if choice not in kinds:
        return
    path, normalize, lock_name = kinds[choice]
    values = list(entries(path))
    if not values:
        print("Список пуст.")
        return
    for number, value in enumerate(values, 1):
        print(f"{number}. {value}")
    selected = input("Номера записей через пробел (Enter — отмена): ").split()
    if not selected:
        return
    if any(not item.isascii() or not item.isdigit() or not 1 <= int(item) <= len(values) for item in selected):
        raise ValueError("укажи номера из списка")
    targets = {normalize(values[int(item) - 1]) for item in selected}
    if input("Удалить выбранные записи? Введи «да»: ").strip().casefold() != "да":
        return
    lock_path = DIRECTORY / lock_name
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        kept = []
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            value = "".join(line.partition("#")[0].split())
            if not value or normalize(value) not in targets:
                kept.append(line)
        write_text_atomic(path, "".join(kept))
    print("Записи удалены. Чтобы обновить правила, открой «Проверить и применить правила».")
    print("Адрес может оставаться заблокированным через другие ASN, подсети, списки или пресеты.")


def run(*command):
    return subprocess.run(command, check=False).returncode == 0


def probe_output(*command):
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=4, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def socket_listeners(output, unix=False):
    listeners = set()
    for line in output.splitlines():
        prefix, _, _ = line.partition("users:")
        owners = [owner_name(raw) for raw in re.findall(r'\("([^"]+)",pid=', line)]
        fields = prefix.split()
        if unix:
            endpoints = re.findall(r"(?<!\S)(?:/|@)\S+", prefix)
        else:
            endpoints = fields[-2:-1] if len(fields) >= 5 else []
        for owner in owners:
            listeners.update((owner, endpoint) for endpoint in endpoints)
    return listeners


def report_socket_roles(tcp, unix):
    for name, endpoint in sorted(tcp):
        if name in INBOUND_NAMES:
            print(f"Inbound — TCP {endpoint} ({name})")
        elif name == "rw-node":
            print(f"Управление нодой — TCP {endpoint} ({name})")
        elif name in PANEL_NAMES:
            print(f"Панель x-ui — TCP {endpoint} ({name})")
        elif name in PROXY_NAMES:
            print(f"{name} — TCP {endpoint}")
    for name, endpoint in sorted(unix):
        if name in PROXY_NAMES:
            print(f"{name} — Unix-сокет {endpoint}")
    tcp_proxies = {name for name, _ in tcp if name in PROXY_NAMES}
    if tcp_proxies:
        print(f"Внимание: возможный reverse proxy на сервере ({', '.join(sorted(tcp_proxies))}).")
        print("Если панель и Inbound на общем внешнем порту, геофильтр затронет оба.", flush=True)
        print("Связь reverse proxy с Inbound по конфигурации не подтверждена.")


def local_docker_command():
    endpoint = os.environ.get("DOCKER_HOST", "")
    if not endpoint.startswith("unix://"):
        endpoint = "unix:///var/run/docker.sock"
    return ["docker", "--host", endpoint]


def docker_network_inventory(command):
    identifiers = [value for value in probe_output(*command, "ps", "-q").splitlines()
                   if re.fullmatch(r"[0-9a-f]{4,64}", value)]
    if not identifiers:
        return []
    template = (
        '{"name":{{json .Name}},"image":{{json .Config.Image}},"pid":{{.State.Pid}},'
        '"network":{{json .HostConfig.NetworkMode}},"ports":{{json .NetworkSettings.Ports}}}'
    )
    output = probe_output(*command, "inspect", "--format", template, *identifiers)
    containers = []
    for line in output.splitlines():
        try:
            container = json.loads(line)
            if isinstance(container, dict) and isinstance(container.get("pid"), int) and container["pid"] > 0:
                containers.append(container)
        except ValueError:
            continue
    return containers


def warn_reverse_proxy():
    tcp = socket_listeners(probe_output("ss", "-H", "-lntp"))
    unix = socket_listeners(probe_output("ss", "-H", "-lxnp"), unix=True)
    known_host = any(name in NETWORK_PROCESSES for name, _ in tcp | unix)
    command = local_docker_command()
    containers = docker_network_inventory(command)
    if known_host:
        print("Проверка reverse proxy и Inbound на хосте")
        report_socket_roles(tcp, unix)
    observed = {name for name, _ in tcp | unix}
    checked_namespaces = set()
    for container in containers:
        if container.get("network") == "host":
            continue
        pid = container["pid"]
        try:
            namespace = os.stat(f"/proc/{pid}/ns/net").st_ino
        except OSError:
            namespace = pid
        if namespace in checked_namespaces:
            continue
        checked_namespaces.add(namespace)
        prefix = ["nsenter", "-t", str(pid), "-n", "--", "ss", "-H"]
        inner_tcp = socket_listeners(probe_output(*prefix, "-lntp"))
        inner_unix = socket_listeners(probe_output(*prefix, "-lxnp"), unix=True)
        detected = {name for name, _ in inner_tcp | inner_unix} & NETWORK_PROCESSES
        if not detected:
            continue
        observed.update(detected)
        name = str(container.get("name", "")).lstrip("/") or str(pid)
        print(f"Контейнер {name} — режим сети {container.get('network', 'не определён')}")
        report_socket_roles(inner_tcp, inner_unix)
        for target, bindings in (container.get("ports") or {}).items():
            for binding in bindings or []:
                print(f"  Опубликован порт {binding['HostIp']}:{binding['HostPort']} → {target}")
        print("  Указанные слушающие сокеты находятся внутри контейнера.")
    images = [container.get("image", "") for container in containers]
    if not containers:
        images = probe_output(*command, "ps", "--format", "{{.Image}}").splitlines()
    image_names = {image.rsplit("/", 1)[-1].split("@", 1)[0].split(":", 1)[0] for image in images}
    for name in sorted((image_names & PROXY_NAMES) - observed):
        print(f"Контейнер {name} обнаружен. Его слушающий сокет пока не определён.")


def parse_listeners(output):
    """Строки `ss -H -lntup` → множество (протокол, порт, процесс, только_loopback)."""
    found = set()
    for line in output.splitlines():
        prefix, _, users = line.partition("users:")
        fields = prefix.split()
        if len(fields) < 5 or fields[0] not in {"tcp", "udp"}:
            continue
        host, _, port = fields[4].rpartition(":")
        if not port.isdigit():
            continue
        address = host.strip("[]").partition("%")[0]
        loopback = "%lo" in host or address.startswith("127.") or address == "::1"
        names = [owner_name(raw) for raw in re.findall(r'\("([^"]+)",pid=', users)] or [""]
        found.update((fields[0], int(port), name, loopback) for name in names)
    return found


def ufw_policy(output):
    """Разбор `ufw status verbose`. None — ufw выключен или его нет."""
    if not re.search(r"^Status: active", output, re.MULTILINE):
        return None
    default = re.search(r"^Default: (\w+) \(incoming\)", output, re.MULTILINE)
    rules = []
    pattern = re.compile(
        r"^(\d+(?::\d+)?(?:,\d+(?::\d+)?)*)(?:/(tcp|udp))?(?: \(v6\))?\s+"
        r"(?:ALLOW|LIMIT)(?: IN)?\s+(.+?)\s*(?:#.*)?$"
    )
    for line in output.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        ranges = []
        for part in match[1].split(","):
            low, _, high = part.partition(":")
            ranges.append((int(low), int(high or low)))
        source = match[3].strip()
        rules.append((ranges, match[2], None if source.startswith("Anywhere") else source))
    return {"allow": bool(default) and default[1] == "allow", "rules": rules}


def exposure(policy, protocol, port):
    """Доступность порта снаружи по ufw: (open|limited|closed, источники)."""
    if policy is None or policy["allow"]:
        return "open", []
    sources = []
    for ranges, rule_protocol, source in policy["rules"]:
        if rule_protocol not in (None, protocol):
            continue
        if not any(low <= port <= high for low, high in ranges):
            continue
        if source is None:
            return "open", []
        sources.append(source)
    return ("limited", sorted(set(sources))) if sources else ("closed", [])


def classify(listeners, policy, containers, inner, x_ui_installed):
    """Профиль сервера и точки входа по собранным фактам. Ничего не запускает."""
    entries = []
    for protocol, port, name, loopback in sorted(listeners):
        role = process_role(name)
        if role and not loopback:
            state, sources = exposure(policy, protocol, port)
            entries.append({"protocol": protocol, "port": port, "name": name, "role": role,
                            "state": state, "sources": sources, "container": None})
    for container in containers:
        if container.get("network") == "host":
            continue
        title = str(container.get("name", "")).lstrip("/")
        inside = inner.get(title, set())
        for target, bindings in (container.get("ports") or {}).items():
            inner_port, _, protocol = target.partition("/")
            owners = sorted({name for proto, port, name, _ in inside
                             if proto == protocol and str(port) == inner_port and process_role(name)})
            for binding in bindings or []:
                host_ip = binding.get("HostIp", "")
                if host_ip.startswith("127.") or host_ip == "::1" or not str(binding.get("HostPort", "")).isdigit():
                    continue
                for name in owners:
                    entries.append({"protocol": protocol, "port": int(binding["HostPort"]), "name": name,
                                    "role": process_role(name), "state": "published",
                                    "sources": [], "container": title})
    unique = {(e["protocol"], e["port"], e["name"], e["state"]): e for e in entries}
    entries = sorted(unique.values(), key=lambda e: (e["role"], e["port"], e["protocol"]))

    images = [str(container.get("image", "")) for container in containers]
    names = {name for _, _, name, _ in listeners}
    has_node = "rw-node" in names or any(image.startswith("remnawave/node") for image in images)
    has_panel = any(image.startswith("remnawave/backend") for image in images)
    has_xui = x_ui_installed or bool(names & PANEL_NAMES)
    reachable = [e for e in entries if e["state"] != "closed"]
    published = [e for e in reachable if e["state"] == "published" and e["role"] == "inbound"]
    direct_tcp = [e for e in reachable if e["role"] == "inbound" and e["protocol"] == "tcp"]
    proxies = [e for e in reachable if e["role"] == "proxy" and e["state"] != "published"]
    behind_proxy = bool(proxies) and not direct_tcp and not has_node

    if has_node and has_panel:
        profile = "панель и нода RemnaWave на одном сервере"
    elif has_node:
        profile = "нода RemnaWave"
    elif published:
        profile = "Inbound в Docker-контейнере с опубликованными портами"
    elif has_xui and behind_proxy:
        profile = "3x-ui за reverse proxy"
    elif has_xui:
        profile = "3x-ui, Inbound слушает напрямую"
    elif behind_proxy and "xray" in names:
        profile = "Xray за reverse proxy"
    else:
        profile = "не определён"

    suggested = {e["port"] for e in reachable if e["role"] == "inbound"}
    if behind_proxy:
        suggested |= {e["port"] for e in proxies if e["port"] != 80}
    return {"profile": profile, "entries": entries, "suggested": suggested,
            "published": sorted({e["port"] for e in published}),
            "listening": {port for _, port, _, loopback in listeners if not loopback}}


def profile_findings(result, geo_ports, api_ports, hook="input"):
    """Расхождения между тем, что слушает сервер, и настройками файрвола."""
    findings = []
    for port in sorted(result["suggested"] - geo_ports):
        owners = sorted({e["name"] for e in result["entries"] if e["port"] == port})
        findings.append(f"Порт {port} ({', '.join(owners)}) принимает подключения снаружи, но в геофильтре его нет.")
    for port in sorted(geo_ports - result["listening"] - set(result["published"])):
        findings.append(f"Порт {port} есть в геофильтре, но его никто не слушает.")
    if 80 in geo_ports:
        findings.append("Порт 80 в геофильтре: проверка Let's Encrypt приходит из-за границы, продление сертификата сломается.")
    if result["published"] and hook != "prerouting":
        ports = ", ".join(map(str, result["published"]))
        findings.append(f"Порты {ports} опубликованы из контейнера: правила на хуке input на них не действуют. "
                        "Добавь строку HOOK=prerouting в /opt/fw/local.conf и примени правила.")
    for entry in result["entries"]:
        if entry["role"] == "node" and entry["state"] == "open" and entry["port"] not in api_ports:
            findings.append(f"Порт управления нодой {entry['port']} открыт для всех. "
                            "Закрой его на IP панели в «Настроить доступ».")
        if entry["role"] == "panel" and entry["state"] != "closed" and entry["port"] in geo_ports:
            findings.append(f"Веб-порт панели {entry['port']} попадает под геофильтр: из-за границы панель не откроется.")
    if result["profile"] == "панель и нода RemnaWave на одном сервере":
        findings.append("Панель и нода делят сервер: геофильтр на общем порту закроет панель из-за границы.")
    return findings


def quiet(*command):
    """Команда выполнилась успешно; её вывод не показывается."""
    try:
        return subprocess.run(command, capture_output=True, timeout=8, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def configured_ports():
    script = (
        'source "$1/fw.conf"; [ -f "$1/local.conf" ] && source "$1/local.conf"; '
        'printf "%s\\n%s\\n%s\\n" "${GEO_PORTS:-}" "${API_PORTS:-}" "${HOOK:-input}"'
    )
    lines = probe_output("bash", "-c", script, "fw-menu", str(DIRECTORY)).splitlines()
    lines += ["", "", "input"]
    geo_ports, api_ports = ({int(value) for value in line.split() if value.isdigit()} for line in lines[:2])
    return geo_ports, api_ports, lines[2].strip() or "input"


def collect_profile():
    try:
        ufw = subprocess.run(["ufw", "status", "verbose"], text=True, capture_output=True, timeout=8,
                             check=False, env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        ufw = ""
    containers = docker_network_inventory(local_docker_command())
    inner = {}
    for container in containers:
        if container.get("network") != "host":
            title = str(container.get("name", "")).lstrip("/")
            inner[title] = parse_listeners(
                probe_output("nsenter", "-t", str(container["pid"]), "-n", "--", "ss", "-H", "-lntup"))
    installed = Path("/etc/systemd/system/x-ui.service").exists() or Path("/usr/local/x-ui").is_dir()
    policy = ufw_policy(ufw)
    result = classify(parse_listeners(probe_output("ss", "-H", "-lntup")), policy, containers, inner, installed)
    result["ufw"] = policy is not None
    return result


def show_profile():
    result = collect_profile()
    geo_ports, api_ports, hook = configured_ports()
    print(f"\nПрофиль сервера — {result['profile']}")
    if hook == "prerouting":
        print("Хук фильтра — prerouting (действует и на порты из контейнеров)")
    print(f"ufw — {'активен' if result['ufw'] else 'не активен или не установлен'}")
    roles = {"inbound": "Inbound", "node": "управление нодой", "panel": "панель", "proxy": "reverse proxy"}
    for entry in result["entries"]:
        state = {"open": "открыт", "closed": "слушает, снаружи закрыт ufw",
                 "limited": "ufw пускает только " + ", ".join(entry["sources"]),
                 "published": f"опубликован из контейнера {entry['container']}"}[entry["state"]]
        notes = []
        if entry["port"] in geo_ports:
            notes.append("в геофильтре")
        if entry["protocol"] == "tcp" and entry["port"] in api_ports:
            notes.append("доступ по списку API_ALLOW")
        tail = f" — {', '.join(notes)}" if notes else ""
        print(f"  {entry['protocol'].upper()} {entry['port']} — {roles[entry['role']]} ({entry['name']}), {state}{tail}")
    if not result["entries"]:
        print("  Известных сервисов на внешних портах нет.")
    findings = profile_findings(result, geo_ports, api_ports, hook)
    if quiet("nft", "-t", "list", "table", "inet", "fw") or (DIRECTORY / "fw.nft").exists():
        disabled = [unit for unit in ("fw-update.service", "fw-update.timer")
                    if not quiet("systemctl", "is-enabled", "--quiet", unit)]
        if disabled:
            findings.append("Автозапуск выключен (" + ", ".join(disabled) + "): списки не обновляются, а после "
                            "перезагрузки правила не поднимутся. Включи: «Проверить и применить правила» → "
                            "«Применить правила и включить таймер».")
    print("Расхождения с настройками:" if findings else "Расхождений с настройками нет.")
    for finding in findings:
        print(f"  ! {finding}")


def show_suggestion():
    """Для установщика: строки КЛЮЧ=значение без кавычек и пробелов вокруг «=»."""
    result = collect_profile()
    print(f"PROFILE={result['profile']}")
    print("PORTS=" + " ".join(map(str, sorted(result["suggested"]))))
    print("NODE_PORTS=" + " ".join(str(entry["port"]) for entry in result["entries"]
                                   if entry["role"] == "node" and entry["state"] == "open"))
    print("HOOK=" + ("prerouting" if result["published"] else "input"))


def show_status():
    print("Проверка файлов и команд")
    for name in ("fw.conf", "asn.txt", "extra.txt", "trusted.txt", "update-fw.sh"):
        path = DIRECTORY / name
        present = path.is_file()
        if name == "update-fw.sh":
            present = present and os.access(path, os.X_OK)
        print(f"  {'OK' if present else 'ПРОВЕРЬ'} {path}")
    for name in ("nft", "curl", "systemctl"):
        print(f"  {'OK' if shutil.which(name) else 'ПРОВЕРЬ'} {name}")
    print(f"  Доверенных адресов и подсетей — {len(set(trusted_networks()))}")
    warn_reverse_proxy()
    show_profile()

    print("\nСлужба и таймер", flush=True)
    service = systemd_properties("fw-update.service", "LoadState", "UnitFileState", "Result")
    loaded = {"loaded": "загружена", "not-found": "отсутствует",
              "masked": "заблокирована", "error": "ошибка загрузки"}.get(service.get("LoadState"), "нет данных")
    result = service.get("Result")
    last_update = "успешно" if result == "success" else "завершилось с ошибкой" if result else "нет данных"
    print(f"Служба обновления — {loaded}")
    print(f"Последнее обновление — {last_update}")
    print(f"Запуск службы при загрузке сервера — {unit_startup_status(service.get('UnitFileState'))}")
    show_schedule_status()
    print("\nЗагруженные правила", flush=True)
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


def apply_rules():
    client = ssh_client()
    if client and not any(ipaddress.ip_address(client) in network for network in trusted_networks()):
        print("Сначала открой «Настроить доступ», выбери добавление доверенных IP и введи ssh.")
        return
    warn_reverse_proxy()
    if input("Применить правила и включить ежедневное обновление? Введи «да»: ").strip().casefold() != "да":
        return
    if (run("systemctl", "start", "fw-update.service")
            and run("systemctl", "enable", "fw-update.service")
            and run("systemctl", "enable", "--now", "fw-update.timer")):
        print("Правила применены. Таймер включён.")
        reconcile_reinstall_guard()
    else:
        print("Одна из команд завершилась с ошибкой. Проверь «Статус файрвола» и журнал fw-update.service.")


def remove_rules():
    print("Будут удалены геофильтр, блокировки сканеров и ограничения доступа из таблицы inet fw.")
    if input("Удалить правила и автообновление? Введи «да»: ").strip().casefold() != "да":
        return
    if not run("systemctl", "disable", "--now", "fw-update.timer", "fw-update.service"):
        print("Проверь состояние службы и таймера. Команда удаления правил не запускалась.")
        return

    directory_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        result = subprocess.run(["nft", "-j", "list", "tables"],
                                text=True, capture_output=True, check=False)
        if result.returncode:
            print(f"Автообновление отключено. Не удалось прочитать таблицы nft: {result.stderr.strip()}")
            return
        tables = [entry["table"] for entry in json.loads(result.stdout)["nftables"] if "table" in entry]
        if any(table["family"] == "inet" and table["name"] == "fw" for table in tables):
            if not run("nft", "delete", "table", "inet", "fw"):
                print("Автообновление отключено. Проверь ошибку nft перед повторной попыткой.")
                return
            print("Таблица inet fw удалена. Автообновление отключено.")
        else:
            print("Таблица inet fw уже отсутствует. Автообновление отключено.")
    finally:
        os.close(directory_fd)
    print("Чтобы снова загрузить правила и включить таймер, открой «Проверить и применить правила».")


def configure_api():
    values = input("TCP-порты управления через пробел (Enter — отмена): ").split()
    if not values:
        return
    if any(not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535 for value in values):
        raise ValueError("укажи порты от 1 до 65535")
    ports = list(dict.fromkeys(str(int(value)) for value in values))

    values = input("Разрешённые IPv4, IPv6 или подсети через пробел: ").split()
    if not values:
        print("Для ограничения доступа укажи хотя бы один адрес.")
        return
    networks = dict.fromkeys(parse_network(value) for value in values)
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if len(connection) == 4 and connection[3] in ports:
        client = ipaddress.ip_address(connection[0])
        if not any(client in network for network in networks):
            raise ValueError("указан текущий SSH-порт. Добавь адрес текущего SSH-подключения.")

    addresses = [network_text(network) for network in networks]
    port_list, address_list = " ".join(ports), " ".join(addresses)
    print(f"Разрешённые источники для TCP {port_list}: {', '.join(addresses)}")
    if input("Сохранить настройки на этой ноде? Введи «да»: ").strip().casefold() != "да":
        return

    begin, end = "# BEGIN FW-MENU API", "# END FW-MENU API"
    managed_block = re.compile(r"(?ms)^" + re.escape(begin) + r"\n.*?^" + re.escape(end) + r"(?:\n|$)")
    with (DIRECTORY / ".api.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = LOCAL_CONFIG.read_text(encoding="utf-8") if LOCAL_CONFIG.exists() else ""
        current, count = managed_block.subn("", current)
        if count > 1 or begin in current or end in current:
            raise ValueError("проверь границы блока FW-MENU API в local.conf")
        prefix = current.rstrip("\n") + "\n\n" if current.rstrip("\n") else ""
        updated = (
            f"{prefix}{begin}\nAPI_PORTS={shlex.quote(port_list)}\n"
            f"API_ALLOW={shlex.quote(address_list)}\n{end}\n"
        )
        write_text_atomic(LOCAL_CONFIG, updated)
    print("Настройки порта управления сохранены. Дальше открой «Проверить и применить правила».")


def command_output(*command):
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, check=True).stdout


def installed_tables():
    entries = json.loads(command_output("nft", "-j", "list", "tables"))["nftables"]
    return {(entry["table"]["family"], entry["table"]["name"]) for entry in entries if "table" in entry}


def management_rule(line):
    match = re.fullmatch(
        r"tcp dport (\d+|\{\s*\d+(?:\s*,\s*\d+)*\s*\}) (ip6?) saddr != "
        r'(@ap[46]) (?:counter )?drop(?: comment "api-lock")?',
        line,
    )
    if not match:
        return None
    ports = tuple(sorted(map(int, re.findall(r"\d+", match[1]))))
    return ports, match[2], match[3]


def guard_matches_current_rules():
    table = json.loads(command_output("nft", "-j", "-t", "list", "table", "inet", REINSTALL_GUARD))
    chains = [entry["chain"]["name"] for entry in table["nftables"] if "chain" in entry]
    if chains != ["input"]:
        return False
    guard = command_output("nft", "-s", "list", "chain", "inet", REINSTALL_GUARD, "input")
    current = command_output("nft", "-s", "list", "chain", "inet", "fw", "input")
    guard_lines = [line.strip() for line in guard.splitlines()]
    guard_rules = set()
    for line in guard_lines:
        if not line or line == "}" or line.startswith(("table ", "chain ", "type ")):
            continue
        if line in {'iif "lo" accept', 'iifname "lo" accept'}:
            continue
        rule = management_rule(line)
        if not rule:
            return False
        guard_rules.add(rule)
    if {rule[1:] for rule in guard_rules} != {("ip", "@ap4"), ("ip6", "@ap6")}:
        return False
    current_rules = {rule for line in current.splitlines() if (rule := management_rule(line.strip()))}
    if current_rules != guard_rules:
        return False
    for name in ("ap4", "ap6"):
        old = command_output("nft", "-s", "list", "set", "inet", REINSTALL_GUARD, name).splitlines()[1:-1]
        new = command_output("nft", "-s", "list", "set", "inet", "fw", name).splitlines()[1:-1]
        if old != new:
            return False
    return True


def reconcile_reinstall_guard():
    try:
        if ("inet", REINSTALL_GUARD) not in installed_tables():
            return
        fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not guard_matches_current_rules():
                print("Временная защита оставлена: порты или списки доступа отличаются.")
                return
            subprocess.run(["nft", "delete", "table", "inet", REINSTALL_GUARD], check=True)
        finally:
            os.close(fd)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Временная защита оставлена: проверка или удаление не завершились ({error}).")
        return
    print("Временная защита удалена. Ограничения портов управления действуют в inet fw.")


def api_lock_rules():
    chain = command_output("nft", "-s", "list", "chain", "inet", "fw", "input")
    return [line.strip() for line in chain.splitlines() if 'comment "api-lock"' in line]


def nft_batch(batch, check=False):
    command = ["nft", "-c", "-f", "-"] if check else ["nft", "-f", "-"]
    subprocess.run(command, input=batch, text=True, check=True)


def file_size(path):
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def format_size(size):
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if size < 1024 or unit == "ТиБ":
            break
        size /= 1024
    value = f"{size:.1f}".removesuffix(".0").replace(".", ",")
    return f"{value} {unit}"


def file_inventory():
    scanner_cache = DIRECTORY / "scanner-cache"
    local_trusted = DIRECTORY / "trusted.local.txt"
    sections = (
        ("Настройки и списки", (
            ("Основные настройки", DIRECTORY / "fw.conf"),
            ("Локальные настройки ноды", LOCAL_CONFIG),
            ("Доверенные IP", TRUSTED),
            ("Дополнительный список доверенных IP", local_trusted),
            ("ASN", ASN_FILE),
            ("Выбор пресетов", PRESETS_FILE),
            ("Свои списки с GitHub", SOURCES),
            ("Ручные блокировки", EXTRA),
        )),
        ("Скрипты и правила", (
            ("Скрипт обновления", UPDATER),
            ("Меню", MENU_FILE),
            ("Сохранённые правила nftables", DIRECTORY / "fw.nft"),
        )),
        ("Автообновление", (
            ("Служба", SERVICE_FILE),
            ("Таймер", TIMER_FILE),
            ("Своё расписание", SCHEDULE_FILE),
        )),
    )
    cache_groups = []
    for label, folder, pattern in (("Сканеры", scanner_cache, "*"), ("ASN", DIRECTORY, "asn_AS*.cache")):
        paths = sorted(path for path in folder.glob(pattern) if not path.name.startswith("."))
        cache_groups.append((label, folder, paths))
    paths = [path for _, group in sections for _, path in group]
    paths.extend(path for _, _, group in cache_groups for path in group)
    sizes = {path: file_size(path) for path in paths}
    return sections, cache_groups, sizes


def show_file_paths():
    sections, cache_groups, sizes = file_inventory()
    optional = {
        LOCAL_CONFIG: "локальные настройки не заданы",
        DIRECTORY / "trusted.local.txt": "дополнительный список не используется",
        SCHEDULE_FILE: "время через меню не задано",
        PRESETS_FILE: "включены все штатные пресеты",
        SOURCES: "свои списки не добавлены",
    }
    total = sum(size for size in sizes.values() if size is not None)
    for heading, paths in sections:
        print(f"\n{heading}")
        for label, path in paths:
            size = sizes[path]
            if size is None:
                state = optional.get(path, "файл пока отсутствует")
            else:
                state = format_size(size)
            print(f"  {label} — {path} ({state})")
    print("\nКэш загруженных списков")
    for label, folder, paths in cache_groups:
        names = []
        subtotal = 0
        for path in paths:
            size = sizes[path]
            if size is not None:
                names.append(f"{path.name} ({format_size(size)})")
                subtotal += size
        print(f"  {label} — {folder} (файлов {len(names)}, {format_size(subtotal)})")
        if names:
            print(textwrap.fill(", ".join(names), width=76, initial_indent="    ", subsequent_indent="    "))
    print(f"\nВсего по списку — {format_size(total)}")


def reload_timer(was_active):
    return run("systemctl", "daemon-reload") and (
        not was_active or run("systemctl", "restart", "fw-update.timer")
    )


def systemd_properties(unit, *names):
    command = ["systemctl", "--no-pager", "show", unit]
    for name in names:
        command.extend(["-p", name])
    output = command_output(*command)
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def unit_startup_status(state):
    return {"enabled": "включён", "enabled-runtime": "включён до перезагрузки",
            "disabled": "выключен", "masked": "заблокирован",
            "static": "управляется другой службой"}.get(state, "не определён")


def show_schedule_status():
    properties = systemd_properties("fw-update.timer", "ActiveState", "UnitFileState", "NextElapseUSecRealtime")
    print(f"Автозапуск таймера — {unit_startup_status(properties.get('UnitFileState'))}")
    active_state = properties.get("ActiveState")
    status = {
        "active": "активен",
        "inactive": "неактивен",
        "failed": "ошибка",
        "activating": "запускается",
        "deactivating": "останавливается",
    }.get(active_state, "не определён")
    print(f"Статус таймера — {status}")
    stamp = properties.get("NextElapseUSecRealtime", "")
    match = re.search(r"([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}:[0-9]{2}:[0-9]{2})(?: (\S+))?$", stamp)
    next_run = "не запланировано"
    if active_state == "active" and match:
        year, month, day, clock, zone = match.groups()
        next_run = f"{day}.{month}.{year} в {clock}"
        if zone:
            next_run += f" ({'МСК' if zone == 'MSK' else zone})"
    print(f"Следующее обновление — {next_run}")


def configure_schedule():
    if not TIMER_FILE.is_file():
        raise ValueError(f"файл таймера отсутствует: {TIMER_FILE}")
    show_schedule_status()
    value = input("Введи время ЧЧ:ММ по часам VPS, «сброс» для штатного графика или Enter для отмены: ").strip().casefold()
    if not value:
        return
    if value != "сброс" and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("введи время в формате ЧЧ:ММ от 00:00 до 23:59")
    if input("Сохранить расписание? Введи «да»: ").strip().casefold() != "да":
        return

    with (DIRECTORY / ".schedule.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = SCHEDULE_FILE.read_text(encoding="utf-8") if SCHEDULE_FILE.exists() else None
        active = run("systemctl", "is-active", "--quiet", "fw-update.timer")
        if value == "сброс":
            SCHEDULE_FILE.unlink(missing_ok=True)
        else:
            SCHEDULE_FILE.parent.mkdir(mode=0o755, exist_ok=True)
            write_text_atomic(SCHEDULE_FILE, f"[Timer]\nOnCalendar=\nOnCalendar=*-*-* {value}:00\n"
                                             "RandomizedDelaySec=0\nAccuracySec=1s\n")
        try:
            if not reload_timer(active):
                raise RuntimeError("systemd не принял расписание")
        except (OSError, RuntimeError, KeyboardInterrupt) as error:
            if previous is None:
                SCHEDULE_FILE.unlink(missing_ok=True)
            else:
                write_text_atomic(SCHEDULE_FILE, previous)
            print(f"Прежний файл расписания восстановлен ({error}).")
            if not reload_timer(active):
                print("Проверь состояние fw-update.timer: systemd не подтвердил перезагрузку.")
            return
    print("Расписание сохранено." if value != "сброс" else "Штатное расписание восстановлено.")
    show_schedule_status()


def write_uninstall_restore(backup, installed_files, keep_guard, enabled_units, timer_active):
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        'backup=${0%/*}',
        f"fw={shlex.quote(str(DIRECTORY))}",
        'if [ -d "$backup/fw" ]; then',
        '  if [ -e "$fw" ] || [ -L "$fw" ]; then',
        "    echo 'В /opt/fw уже есть установка. Откат остановлен.' >&2",
        "    exit 1",
        "  fi",
        '  mv -T "$backup/fw" "$fw"',
        "fi",
    ]
    for path in installed_files:
        if path == SCHEDULE_FILE:
            lines.append(f"mkdir -p {shlex.quote(str(path.parent))}")
        lines.append(f'cp -p "$backup/{path.name}" {shlex.quote(str(path))}')
    lines.extend([
        "systemctl daemon-reload",
        'if [ -s "$backup/active.nft" ]; then',
        "  {",
        '    cat "$backup/active.nft"',
    ])
    if keep_guard:
        lines.extend([
            f'    if [ -f "$backup/guard-installed" ] && nft list table inet {REINSTALL_GUARD} >/dev/null 2>&1; then',
            f"      printf '\\ndelete table inet {REINSTALL_GUARD}\\n'",
            "    fi",
        ])
    lines.extend(["  } | nft -f -", "fi"])
    if enabled_units:
        lines.append("systemctl enable " + " ".join(enabled_units))
    if timer_active:
        lines.append("systemctl start fw-update.timer")
    lines.append("echo 'Прежняя установка восстановлена.'")
    script = backup / "restore.sh"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o700)
    return script


def uninstall():
    print("Будут удалены файлы установки, правила inet fw и задания автообновления.")
    if input("Удалить установку с резервной копией? Введи «да»: ").strip().casefold() != "да":
        return
    tables = installed_tables()
    keep_guard = False
    if ("inet", "fw") in tables and api_lock_rules():
        answer = input("Оставить временную защиту портов управления? Введи «да» или «нет»: ").strip().casefold()
        if answer not in {"да", "нет"}:
            return
        keep_guard = answer == "да"
    if keep_guard and ("inet", REINSTALL_GUARD) in tables:
        raise ValueError(f"таблица {REINSTALL_GUARD} уже существует")

    backup = Path(tempfile.mkdtemp(prefix=".fw-uninstall.", dir=DIRECTORY.parent))
    installed_files = [MENU_FILE, SERVICE_FILE, TIMER_FILE]
    if SCHEDULE_FILE.is_file():
        installed_files.append(SCHEDULE_FILE)
    for path in installed_files:
        shutil.copy2(path, backup / path.name)
    enabled_units = [unit for unit in ("fw-update.service", "fw-update.timer")
                     if run("systemctl", "is-enabled", "--quiet", unit)]
    timer_active = run("systemctl", "is-active", "--quiet", "fw-update.timer")
    restore = write_uninstall_restore(backup, installed_files, keep_guard, enabled_units, timer_active)
    try:
        subprocess.run(["systemctl", "disable", "--now", "fw-update.timer", "fw-update.service"], check=True)
        fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            transition = ""
            if ("inet", "fw") in installed_tables():
                original = command_output("nft", "-s", "list", "table", "inet", "fw")
                active = "add table inet fw\ndelete table inet fw\n" + original
                nft_batch(active, check=True)
                (backup / "active.nft").write_text(active, encoding="utf-8")
                transition = "delete table inet fw\n"
                if keep_guard:
                    rules = api_lock_rules()
                    if not rules or not original.startswith("table inet fw {"):
                        raise ValueError("не удалось извлечь правила управления")
                    guard = original.replace("table inet fw {", f"table inet {REINSTALL_GUARD} {{", 1)
                    rules = ['iifname "lo" accept', *rules]
                    transition = (
                        f"create table inet {REINSTALL_GUARD}\n" + guard
                        + f"flush chain inet {REINSTALL_GUARD} input\n"
                        + "".join(f"add rule inet {REINSTALL_GUARD} input {rule}\n" for rule in rules)
                        + transition
                    )
                nft_batch(transition, check=True)
            DIRECTORY.rename(backup / "fw")
            for path in installed_files:
                path.unlink()
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            if transition:
                nft_batch(transition)
                if keep_guard:
                    (backup / "guard-installed").touch()
        finally:
            os.close(fd)
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt) as error:
        print(f"Удаление прервано: {error}", flush=True)
        if not run("bash", restore):
            print(f"Ручной откат: bash {restore}")
        return
    print("Установка удалена.")
    if (backup / "guard-installed").exists():
        print(f"Временная защита портов управления действует в {REINSTALL_GUARD}.")
    print(f"Резервная копия: {backup}\nОткат: bash {restore}")
    raise SystemExit(0)


def show_menu(title, entries, exit_label="Назад"):
    choices = {str(number): entry for number, entry in enumerate(entries, start=1)}
    while True:
        print(f"\n{title}")
        for number, (label, _) in choices.items():
            print(f"{number}. {label}")
        print(f"0. {exit_label}")
        try:
            choice = input("Выбор: ").strip()
            if choice == "0":
                return
            if choice in choices:
                label, action = choices[choice]
                if callable(action):
                    action()
                else:
                    show_menu(label, action)
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            print(f"Ошибка: {error}")


def main():
    if os.geteuid() != 0:
        raise SystemExit("Запусти fw-menu от root.")
    if sys.argv[1:] == ["--check-proxy"]:
        warn_reverse_proxy()
        return
    if sys.argv[1:] == ["--profile"]:
        try:
            show_profile()
            sys.stdout.flush()
        except BrokenPipeError:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return
    if sys.argv[1:] == ["--suggest"]:
        show_suggestion()
        return
    entries = (
        ("Статус файрвола", show_status),
        ("Настроить доступ", (
            ("Добавить доверенные IP или подсети через пробел", add_trusted),
            ("Показать доверенные адреса", show_trusted),
            ("Настроить порт управления и разрешённые IP", configure_api),
        )),
        ("Настроить блокировки", (
            ("Посмотреть подсети ASN и добавить ASN в блокировку", lookup_asn),
            ("Добавить IP или подсеть в блокировку", add_blocked_address),
            ("Добавить свой список по ссылке с GitHub", add_source),
            ("Включить или выключить пресеты", manage_presets),
            ("Удалить ручные IP, подсети, ASN или свои списки", remove_manual_entries),
        )),
        ("Проверить и применить правила", (
            ("Проверить обновление без применения (--dry)", lambda: run(UPDATER, "--dry")),
            ("Применить правила и включить таймер", apply_rules),
            ("Настроить расписание обновления", configure_schedule),
            ("Показать пути к файлам на VPS", show_file_paths),
        )),
        ("Отключить или удалить", (
            ("Удалить правила и автообновление", remove_rules),
            ("Удалить скрипт и настройки", uninstall),
        )),
    )
    try:
        try:
            _, _, sizes = file_inventory()
            total = sum(size for size in sizes.values() if size is not None)
            print(f"Размер файлов файрвола — {format_size(total)}")
        except OSError:
            print("Размер файлов файрвола — не удалось определить")
        show_menu("Файрвол IPv4 / IPv6", entries, "Выход")
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
FW_MENU_FILE

# Стартовый кэш списков сканеров: первый запуск не зависит от доступности их источников.
cat > "$stage/seed.txt" <<'FW_SEED_FILE'
### skipa_cidr.txt
# стартовый снимок, источник обновлён 2026-05-26
5.143.224.100/30
5.143.224.104/30
5.159.97.203
5.178.87.167
5.188.159.228
31.131.251.106
31.131.251.235
31.131.255.205
31.131.255.206/31
31.131.255.208/30
31.131.255.212
31.131.255.240
37.9.13.54
37.9.13.84
37.9.13.105
37.9.13.217
45.92.176.94
45.92.176.129
45.92.176.143
45.92.176.144/31
45.92.176.205
45.92.177.113
45.92.177.127
45.92.177.237
45.93.20.45
45.93.20.79
45.93.20.103
45.93.20.104
45.93.20.109
45.93.20.126
45.93.20.148
45.93.20.229
45.141.86.171
45.146.167.56
45.146.167.68
45.146.167.105
45.146.167.237
62.76.98.0/24
62.84.116.11
62.84.116.13
62.84.116.34
62.84.116.219
62.84.116.237
62.113.99.65
77.223.102.84
77.223.102.101
77.223.102.191
77.223.103.45
77.223.103.53
77.223.120.227
80.93.187.17
80.249.131.92
82.148.21.205
85.142.100.0/24
85.175.69.50
85.175.147.234
89.169.28.191
89.169.28.210
89.169.28.214
91.122.177.241
92.38.153.0/24
92.124.109.218
92.223.103.144
94.25.46.114
94.26.228.18
94.26.228.205
95.143.190.169
95.143.190.179
95.143.191.147
95.143.191.223
95.143.191.245
95.167.62.66
95.167.62.82
95.167.82.26
95.167.87.66
95.167.133.10
95.167.148.18
95.167.186.2
95.167.197.242
95.167.198.186
95.167.199.34
95.167.199.90
95.167.200.10
95.189.36.106
176.100.243.247
176.208.65.146
176.208.67.114
176.208.69.226
176.208.70.162
176.208.79.82
176.210.118.218
176.211.46.130
176.211.47.122
176.211.48.242
176.211.51.218
176.211.56.130
176.211.103.178
176.211.103.202
178.185.133.251
178.185.170.42
178.185.202.130
178.185.202.162
178.185.216.114
178.185.228.58
178.185.234.162
178.185.235.58
178.185.235.74
178.185.238.154
178.185.238.178
178.185.239.50
178.185.239.58
178.185.241.98
178.185.241.114
185.224.228.0/24
185.224.230.0/24
188.68.217.207
188.246.224.80
193.168.46.143
194.26.25.137
212.41.10.41
212.41.12.45
212.41.12.46/31
212.41.12.48
212.41.13.23
212.41.13.24/31
212.41.26.138
212.67.10.218
212.67.11.37
212.67.11.128
212.67.11.136
212.67.11.167
212.67.11.227
212.67.11.233
212.67.11.234
212.164.59.250
212.192.158.0/24
213.59.217.242
217.65.82.18
### skipa.list
# стартовый снимок, источник обновлён 2026-09-05
5.143.224.100/30
5.143.224.104/30
5.159.97.203
5.178.87.167
5.188.159.228
31.131.251.106
31.131.251.235
31.131.255.205
31.131.255.206/31
31.131.255.208/30
31.131.255.212
31.131.255.240
37.9.13.54
37.9.13.84
37.9.13.105
37.9.13.217
45.92.176.94
45.92.176.129
45.92.176.143
45.92.176.144/31
45.92.176.205
45.92.177.113
45.92.177.127
45.92.177.237
45.93.20.45
45.93.20.79
45.93.20.103
45.93.20.104
45.93.20.109
45.93.20.126
45.93.20.148
45.93.20.229
45.141.86.171
45.146.167.56
45.146.167.68
45.146.167.105
45.146.167.237
62.76.98.0/24
62.84.116.11
62.84.116.13
62.84.116.34
62.84.116.219
62.84.116.237
62.113.99.65
77.223.102.84
77.223.102.101
77.223.102.191
77.223.103.45
77.223.103.53
77.223.120.227
80.93.187.17
80.249.131.92
82.148.21.205
85.142.100.0/24
85.175.69.50
85.175.147.234
89.169.28.191
89.169.28.210
89.169.28.214
91.122.177.241
92.38.153.0/24
92.124.109.218
92.223.103.144
94.25.46.114
94.26.228.18
94.26.228.205
95.143.190.169
95.143.190.179
95.143.191.147
95.143.191.223
95.143.191.245
95.167.62.66
95.167.62.82
95.167.82.26
95.167.87.66
95.167.133.10
95.167.148.18
95.167.186.2
95.167.197.242
95.167.198.186
95.167.199.34
95.167.199.90
95.167.200.10
95.189.36.106
176.100.243.247
176.208.65.146
176.208.67.114
176.208.69.226
176.208.70.162
176.208.79.82
176.210.118.218
176.211.46.130
176.211.47.122
176.211.48.242
176.211.51.218
176.211.56.130
176.211.103.178
176.211.103.202
178.185.133.251
178.185.170.42
178.185.202.130
178.185.202.162
178.185.216.114
178.185.228.58
178.185.234.162
178.185.235.58
178.185.235.74
178.185.238.154
178.185.238.178
178.185.239.50
178.185.239.58
178.185.241.98
178.185.241.114
185.224.228.0/24
185.224.230.0/24
188.68.217.207
188.246.224.80
193.168.46.143
194.26.25.137
212.41.10.41
212.41.12.45
212.41.12.46/31
212.41.12.48
212.41.13.23
212.41.13.24/31
212.41.26.138
212.67.10.218
212.67.11.37
212.67.11.128
212.67.11.136
212.67.11.167
212.67.11.227
212.67.11.233
212.67.11.234
212.164.59.250
212.192.158.0/24
213.59.217.242
217.65.82.18
### antiscanner.list
# стартовый снимок, источник обновлён 2026-09-05
5.143.224.100/30
5.143.224.104/30
5.159.97.203
5.178.87.167
5.188.159.228
31.131.251.106
31.131.251.235
31.131.255.205
31.131.255.206/31
31.131.255.208/30
31.131.255.212
31.131.255.240
37.9.13.54
37.9.13.84
37.9.13.105
37.9.13.217
45.92.176.94
45.92.176.129
45.92.176.143
45.92.176.144/31
45.92.176.205
45.92.177.113
45.92.177.127
45.92.177.237
45.93.20.45
45.93.20.79
45.93.20.103
45.93.20.104
45.93.20.109
45.93.20.126
45.93.20.148
45.93.20.229
45.141.86.171
45.146.167.56
45.146.167.68
45.146.167.105
45.146.167.237
62.76.98.0/24
62.84.116.11
62.84.116.13
62.84.116.34
62.84.116.219
62.84.116.237
62.113.99.65
77.223.102.84
77.223.102.101
77.223.102.191
77.223.103.45
77.223.103.53
77.223.120.227
80.93.187.17
80.249.131.92
82.148.21.205
85.142.100.0/24
85.175.69.50
85.175.147.234
89.169.28.191
89.169.28.210
89.169.28.214
91.122.177.241
92.38.153.0/24
92.124.109.218
92.223.103.144
94.25.46.114
94.26.228.18
94.26.228.205
95.143.190.169
95.143.190.179
95.143.191.147
95.143.191.223
95.143.191.245
95.167.62.66
95.167.62.82
95.167.82.26
95.167.87.66
95.167.133.10
95.167.148.18
95.167.186.2
95.167.197.242
95.167.198.186
95.167.199.34
95.167.199.90
95.167.200.10
95.189.36.106
176.100.243.247
176.208.65.146
176.208.67.114
176.208.69.226
176.208.70.162
176.208.79.82
176.210.118.218
176.211.46.130
176.211.47.122
176.211.48.242
176.211.51.218
176.211.56.130
176.211.103.178
176.211.103.202
178.185.133.251
178.185.170.42
178.185.202.130
178.185.202.162
178.185.216.114
178.185.228.58
178.185.234.162
178.185.235.58
178.185.235.74
178.185.238.154
178.185.238.178
178.185.239.50
178.185.239.58
178.185.241.98
178.185.241.114
185.224.228.0/22
188.68.217.207
188.246.224.80
193.168.46.143
194.26.25.137
194.67.63.200/30
195.209.120.0/22
212.41.10.41
212.41.12.45
212.41.12.46/31
212.41.12.48
212.41.13.23
212.41.13.24/31
212.41.26.138
212.67.10.218
212.67.11.37
212.67.11.128
212.67.11.136
212.67.11.167
212.67.11.227
212.67.11.233
212.67.11.234
212.164.59.250
212.192.156.0/22
213.59.217.242
217.65.82.18
2a0c:a9c7:156::/47
2a0c:a9c7:158::/48
### censys_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
66.132.148.0/24
66.132.153.0/24
66.132.159.0/24
66.132.172.0/24
66.132.186.0/24
66.132.195.0/24
66.132.198.0/24
66.132.224.0/24
66.132.228.0/24
162.142.125.0/24
167.94.138.0/24
167.94.145.0/24
167.94.146.0/24
167.248.133.0/24
199.45.154.0/23
206.168.32.0/22
### shodan_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
2.59.22.234
64.225.74.178
64.226.86.7
64.227.90.185
66.240.192.138
66.240.205.34
66.240.219.133
66.240.219.146
66.240.236.119
71.6.135.131
71.6.146.130
71.6.146.185
71.6.146.186
71.6.147.198
71.6.147.254
71.6.158.166
71.6.165.200
71.6.167.142
71.6.199.23
80.82.77.33
80.82.77.139
85.25.43.94
85.25.103.50
86.54.31.32/28
89.248.167.131
89.248.172.16
93.120.27.62
93.174.95.106
94.102.49.190
94.102.49.193
134.209.95.237
137.184.13.100
137.184.95.216
137.184.112.103
137.184.112.192
137.184.190.194
137.184.190.205
137.184.190.246
139.59.170.85
143.110.239.2
143.198.68.20
143.198.225.197
143.198.238.87
147.182.202.179
147.182.225.86
147.182.240.244/31
147.182.240.247
147.182.241.81
147.182.247.10
157.245.222.108
159.89.124.112
164.92.106.15
164.92.114.247
165.227.55.4
165.227.62.247
165.227.110.45
165.227.172.206
165.227.188.42
178.128.32.203
185.142.236.34/31
185.142.236.36
185.142.236.38
185.142.236.40/31
185.142.236.43
185.142.239.16
185.165.190.17
185.165.190.34
185.165.191.26/31
188.138.9.50
195.144.21.56
198.20.69.74
198.20.69.98
198.20.70.114
198.20.99.130
207.90.244.0/24
216.117.2.176/28
### paloaltonetworks_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
35.203.210.0/23
144.86.173.0/24
147.185.132.0/22
162.216.148.0/22
172.105.147.0/24
198.235.24.0/24
205.210.31.0/24
216.25.88.0/21
### shadowserver_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
64.62.156.0/24
64.62.195.152/29
64.62.197.0/24
65.49.1.0/24
65.49.20.64/26
74.82.47.0/26
184.105.139.64/26
184.105.247.192/26
199.165.159.0/24
216.218.206.64/26
### driftnet_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
45.55.151.3
45.55.153.86
45.55.158.168
45.55.185.224
45.55.186.92
64.227.99.138
64.227.108.146
64.227.109.89
64.227.110.161
87.236.176.0/24
107.170.65.169
128.199.8.140
157.245.243.118
157.245.245.246
159.65.216.50
159.65.219.252
162.243.114.171
162.243.116.182
162.243.208.127
167.99.234.119
185.247.137.0/24
192.241.179.235
193.163.125.0/24
195.96.139.0/24
### onyphe_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
15.204.109.96/31
15.204.109.103
15.204.109.224
51.254.49.96/28
91.196.152.0/24
91.230.168.0/24
91.231.89.0/24
94.231.206.0/24
195.184.76.0/24
### zoomeye_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
36.156.22.4
148.153.56.170
148.153.56.174
148.153.188.246
148.153.188.254
148.153.189.62
150.107.36.236
150.107.38.251
152.32.142.86
154.197.56.163
165.154.23.177
165.154.163.10
222.186.13.130
### leakix_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
23.239.4.252
23.239.21.238
46.101.1.225
46.101.111.185
64.23.159.209
64.23.218.208
64.225.75.246
64.226.65.160
64.226.78.121
64.227.32.66
64.227.70.2
68.183.9.16
68.183.180.73
96.126.110.54
104.237.130.38
128.199.182.55
128.199.182.77
128.199.182.152
134.122.28.88
134.209.25.199
138.68.82.23
138.68.86.32
138.68.144.227
138.197.191.87
139.59.132.8
139.59.136.184
139.59.143.102
139.59.231.238
139.162.96.14
139.162.96.81
139.162.141.82
139.162.155.225
142.93.0.66
142.93.129.190
142.93.143.8
143.110.213.72
143.110.217.244
143.244.168.161
146.190.63.48
146.190.63.248
146.190.103.103
146.190.242.161
147.182.149.75
147.182.200.94
157.230.19.140
157.245.36.108
157.245.105.107
157.245.113.227
157.245.204.205
159.65.18.197
159.65.144.72
159.89.12.166
159.89.127.165
159.89.174.87
159.203.96.42
159.223.132.86
164.90.208.56
164.90.228.79
164.92.107.174
164.92.244.132
165.22.34.189
165.22.235.3
165.227.39.235
165.227.84.14
165.227.173.41
167.71.81.114
167.71.175.236
167.99.181.249
167.99.182.39
167.99.210.137
167.172.158.128
167.172.232.142
172.105.16.34
172.105.16.40
172.105.16.105
172.105.16.117
172.105.16.131
172.105.158.219
178.128.207.138
188.166.108.93
192.46.211.230
206.81.12.187
206.81.24.74
206.81.24.227
206.189.2.13
206.189.19.19
206.189.95.232
206.189.225.181
206.189.233.36
207.154.197.113
207.154.212.47
209.38.208.202
209.38.248.17
209.97.180.8
### rapid7_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
5.63.151.96/27
45.79.190.10
66.175.212.77
66.228.41.127
71.6.233.0/24
88.202.190.128/27
109.123.117.224/27
146.185.25.160/27
173.255.225.139
### fofa_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
23.91.97.220
36.255.220.245
36.255.223.21
36.255.223.98
36.255.223.187
37.8.99.117
45.142.154.12/30
45.142.154.16/29
45.142.154.24
45.142.154.29
45.142.154.30/31
45.142.154.32/28
45.142.154.49
45.142.154.51
45.142.154.52/31
45.142.154.54
45.142.154.57
45.142.154.58/31
45.142.154.88/29
45.142.154.96/31
45.142.154.100/30
45.142.154.104/29
45.142.154.112/30
45.142.154.116
45.249.245.55
45.249.245.173
45.249.246.82
45.249.247.109
101.36.106.75
101.36.106.109
101.36.107.46
101.36.108.178
101.36.108.184
101.36.108.202
101.36.108.248
101.36.109.22
101.36.109.144
101.36.110.31
101.36.110.162
101.36.111.38
101.36.111.179
101.36.112.24
101.36.116.230
101.36.117.199
101.36.119.69
101.36.119.242
101.36.122.18
101.36.125.183
101.36.125.211
101.36.127.131
103.72.147.96
103.72.147.224
103.210.21.36
103.218.240.172
103.218.241.58
103.218.241.179
103.218.241.245
103.218.242.181
103.218.243.246
106.75.2.51
106.75.2.130
106.75.2.237
106.75.3.6
106.75.3.148
106.75.5.16
106.75.5.38
106.75.5.79
106.75.5.165
106.75.6.180
106.75.7.31
106.75.7.49
106.75.8.46
106.75.8.69
106.75.8.124
106.75.8.214
106.75.8.241
106.75.9.9
106.75.9.25
106.75.9.73
106.75.9.75
106.75.9.106
106.75.9.126
106.75.9.195
106.75.9.232
106.75.9.241
106.75.9.251
106.75.10.60
106.75.10.97
106.75.11.5
106.75.11.34
106.75.11.100/31
106.75.11.183
106.75.12.4
106.75.12.68
106.75.12.106
106.75.12.228
106.75.12.239
106.75.12.244
106.75.13.37
106.75.13.39
106.75.13.48
106.75.13.75
106.75.13.117
106.75.13.142
106.75.13.144
106.75.13.182
106.75.13.225
106.75.14.37
106.75.14.65
106.75.15.7
106.75.15.156
106.75.15.181
106.75.15.222
106.75.16.92
106.75.16.170
106.75.16.191
106.75.17.238
106.75.18.165
106.75.18.206
106.75.21.34
106.75.21.80
106.75.24.61
106.75.24.87
106.75.24.130
106.75.24.225
106.75.28.137
106.75.28.215
106.75.30.116
106.75.31.42
106.75.37.247
106.75.47.235
106.75.73.94
106.75.75.129
106.75.81.24
106.75.84.37
106.75.84.210
106.75.86.6
106.75.86.15
106.75.86.99
106.75.86.187
106.75.86.208
106.75.87.18
106.75.87.37
106.75.87.38
106.75.87.48
106.75.87.58
106.75.87.156
106.75.87.177
106.75.88.65
106.75.94.54
106.75.94.64
106.75.98.12
106.75.98.33
106.75.129.130
106.75.134.199
106.75.185.40
106.75.186.74
117.50.12.254
118.18.141.205
118.18.151.27
118.26.36.200
118.26.38.29
118.26.38.132
118.145.146.205
118.145.156.187
118.193.33.104
118.193.33.244
118.193.33.249
118.193.34.43
118.193.35.29
118.193.36.220
118.193.37.207
118.193.38.163
118.193.38.178
118.193.39.45
118.193.39.146
118.193.39.149
118.193.40.131
118.193.44.104
118.193.44.203
118.193.45.103
118.193.45.220
118.193.45.222
118.193.45.234
118.193.46.233
118.193.46.245
123.58.209.39
123.58.209.123
123.58.209.194
123.58.209.243
123.58.210.134
123.58.210.227
123.58.212.136
123.58.212.238
123.58.213.22
123.58.213.211
123.58.215.74
123.58.217.155
123.58.219.26
123.58.219.82
123.58.219.98
123.58.219.233
123.63.208.151
123.63.209.115
128.1.131.97
128.1.132.17
128.1.132.84
128.1.132.111
128.1.132.136
128.14.112.1
128.102.102.1
152.32.129.46
152.32.129.77
152.32.129.110
152.32.131.10
152.32.131.53
152.32.131.104
152.32.132.189
152.32.132.230
152.32.133.16
152.32.133.102
152.32.133.174
152.32.133.191
152.32.133.206
152.32.134.45
152.32.134.182
152.32.135.151
152.32.168.24
152.32.169.42
152.32.170.42
152.32.170.240
152.32.172.191
152.32.172.234
152.32.173.103
152.32.185.36
152.32.185.164
152.32.188.12
152.32.188.174
152.32.188.242
152.32.189.134
152.32.189.137
152.32.189.202
152.32.190.31
152.32.190.84
152.32.190.153
152.32.191.47
152.32.192.230
152.32.202.110
152.32.209.108
152.32.209.123
152.32.211.139
152.32.211.153
152.32.213.13
152.32.213.85
152.32.213.95
152.32.213.113
152.32.213.168
152.32.216.8
152.32.216.70
152.32.216.159
152.32.226.138
152.32.226.203
152.32.235.85
152.32.240.77
152.32.240.95
152.32.248.240
152.32.251.11
152.32.251.174
152.32.252.133
152.32.252.154
152.32.253.25
152.32.253.121
152.32.253.122
152.32.253.137
152.32.254.132
152.32.255.169
152.33.61.233
152.197.214.0
152.198.180.15
152.198.180.41
152.198.181.127
152.198.242.233
154.39.47.206
154.39.47.248
154.81.14.4
154.81.14.93
154.81.15.190
154.81.15.232
154.81.239.52
154.85.60.72
156.225.1.12/31
156.225.1.15
156.225.1.16/31
156.225.1.18
156.225.1.29
156.225.1.30/31
156.225.1.32/28
156.225.1.88/29
156.225.1.96/31
156.225.1.100/30
156.225.1.104/29
156.225.1.112/30
156.225.1.116
156.232.52.144
156.232.94.17
156.232.94.39
156.232.94.54
156.232.94.58
156.232.94.72
156.232.94.80
165.111.88.17
165.154.0.193
165.154.36.91
165.154.139.238
165.154.139.243
165.154.157.30
165.154.157.35
165.154.202.146
165.154.206.35
### quake_v4.txt
# стартовый снимок, источник обновлён 2026-11-11
1.192.192.4
1.192.192.6
1.192.192.8
1.192.195.5
1.192.195.8
1.192.195.11
8.208.28.94
8.209.105.161
8.209.106.138
8.209.109.170
8.211.1.129
8.211.134.233
8.211.136.192
8.213.148.33
8.213.219.1
8.213.229.59
8.215.42.96
8.219.74.101
8.220.141.121
27.115.124.2/31
27.115.124.4/31
27.115.124.33
27.115.124.34
27.115.124.40/31
27.115.124.48/31
27.115.124.66/31
27.115.124.68/31
27.115.124.96/31
27.115.124.104
27.115.124.112
43.224.66.142
43.224.66.166
43.224.66.247
45.40.57.23
45.40.57.172
45.43.37.143
45.43.57.144
45.43.60.128
45.43.60.133
45.43.60.185
45.43.60.234
45.43.61.244
45.43.62.77
45.43.63.38
45.43.63.68
45.43.63.181
45.43.63.219
45.249.244.136
45.249.245.54
45.249.246.196
45.255.124.115
45.255.124.132
45.255.124.188
45.255.125.15
45.255.125.203
47.74.12.201
47.81.209.145
47.81.210.51
47.84.189.10
47.88.48.126
47.88.61.66
47.91.105.144
47.91.121.71
47.104.195.99
47.236.177.99
47.239.84.17
47.245.4.222
47.250.179.36
47.250.187.114
47.250.187.232
47.253.5.161
47.253.34.98
47.253.47.30
47.253.47.101
47.253.54.248
47.254.177.197
61.29.249.66
61.29.249.133
80.238.200.108
101.36.97.70
101.36.97.80
101.36.97.187
101.36.98.165
101.36.102.26
101.36.105.50
101.36.105.94
101.36.106.78
101.36.107.65
101.36.107.243
101.36.110.201
101.36.114.124
101.36.114.150
101.36.114.198
101.36.114.209
101.36.114.222
101.36.114.252
101.36.115.241
101.36.116.45
101.36.117.15
101.36.118.248
101.36.121.22
101.36.122.183
101.91.110.4
101.91.110.144
101.198.0.150/31
101.198.0.152
101.198.0.155
101.198.0.156/31
101.198.0.171
101.198.0.180/31
101.198.0.182
101.198.0.184
101.198.0.187
101.198.0.188/31
101.199.254.198/31
101.199.254.200/29
101.199.254.208/31
101.199.254.230/31
101.199.254.232/29
101.199.254.242/31
103.149.26.230
103.211.194.172
103.211.194.214
104.218.164.117
104.218.164.192
104.218.165.188
106.63.26.6/31
106.63.26.8
106.63.26.20
106.63.26.28/31
106.63.26.131
106.63.26.139
106.63.26.140/31
106.63.26.148
106.63.26.155
106.63.26.156/31
107.150.96.65
107.150.101.57
107.150.102.162
107.150.103.88
107.150.103.155
107.150.104.176
107.150.105.104
107.150.106.156
107.150.109.55
107.150.110.13
107.150.111.167
107.150.111.203
107.150.111.212
107.150.112.48
107.150.117.121
107.150.117.187
107.150.117.219
107.155.48.6
110.238.79.180
118.26.37.105
118.26.38.251
118.26.104.19
118.26.104.78
118.26.104.93
118.26.105.52
118.26.105.116
118.26.105.144
118.26.109.7
118.26.109.224
118.26.111.94
118.190.149.150
118.193.32.88
118.193.32.92
118.193.32.119
118.193.35.202
118.193.36.56
118.193.40.191
118.193.47.114
118.193.47.212
118.193.47.223
118.193.56.149
118.193.56.171
118.193.56.184
118.193.56.197
118.193.58.20
118.193.58.125
118.193.58.180
118.193.58.187
118.193.59.10
118.193.59.41
118.193.59.151
118.193.59.194
118.193.59.227
118.193.61.179
118.193.63.139
118.193.64.186
118.193.64.188
118.193.65.209
118.193.68.42
118.193.68.144
118.193.68.150
118.193.68.247
118.193.69.67
118.193.69.146
118.193.69.177
118.193.72.187
118.193.73.8
118.194.228.167
118.194.228.239
118.194.229.98
118.194.230.202
118.194.231.130
118.194.231.192
118.194.231.231
118.194.233.182
118.194.233.185
118.194.233.253
118.194.234.14
118.194.234.29
118.194.236.118
118.194.236.126
118.194.236.134
118.194.236.137
118.194.236.142
118.194.236.219
118.194.238.196
118.194.248.4
118.194.248.105
118.194.248.142
118.194.249.8
118.194.249.72
118.194.249.254
118.194.250.2
118.194.250.31
118.194.251.141
118.194.251.246
123.6.49.4
123.6.49.6
123.6.49.9
123.6.49.10
123.6.49.14/31
123.6.49.17
123.6.49.36
123.6.49.38
123.6.49.41
123.6.49.42
123.6.49.46
123.6.49.49
123.58.192.104
123.58.192.229
123.58.193.194
123.58.196.28
123.58.196.49
123.58.200.21
123.58.200.120
123.58.200.147
123.58.203.63
123.58.203.190
123.58.203.243
123.58.205.100
123.58.207.127
123.58.207.151
123.58.207.155
123.58.209.112
123.58.210.106
128.1.32.99
128.1.32.118
128.1.33.49
128.1.33.94
128.1.38.37
128.1.39.77
128.1.43.38
128.1.43.230
128.1.43.245
128.1.46.183
128.1.47.89
128.1.48.211
128.1.49.131
128.1.49.161
128.1.79.52
128.14.225.253
128.14.226.125
128.14.226.191
128.14.227.37
128.14.227.52
128.14.227.209
128.14.229.76
128.14.231.72
128.14.231.139
128.14.231.154
128.14.232.117
128.14.232.219
128.14.232.254
128.14.233.156
128.14.233.253
128.14.236.30
128.14.236.41
128.14.236.128
128.14.236.224
128.14.237.9
128.14.237.43
128.14.237.120
128.14.237.130
128.14.239.38/31
128.14.239.217
128.14.239.248
134.168.246.4
134.168.246.33
134.168.246.57
134.168.246.92
134.168.246.130
134.168.249.22
134.168.249.100
134.168.249.185
150.107.38.5
150.107.38.40
152.32.128.149
152.32.128.169
152.32.128.204
152.32.130.155
152.32.131.245
152.32.134.156
152.32.135.48
152.32.135.214
152.32.138.187
152.32.138.227
152.32.138.230
152.32.139.9
152.32.139.76
152.32.139.96
152.32.139.190
152.32.140.12
152.32.140.198
152.32.141.2
152.32.141.154
152.32.141.202
152.32.142.75
152.32.142.138
152.32.142.165
152.32.144.111
152.32.145.49
152.32.146.202
152.32.146.235
152.32.147.81
152.32.149.19
152.32.149.47
152.32.149.117
152.32.149.118
152.32.149.178
152.32.149.206
152.32.149.246
152.32.150.7
152.32.150.29
152.32.150.215
152.32.151.39
152.32.151.180
152.32.153.17
152.32.153.53
152.32.153.67
152.32.153.75
152.32.153.148
152.32.153.228
152.32.153.245
152.32.154.106
152.32.156.50
152.32.156.95
152.32.156.117
152.32.156.136/31
152.32.156.158
152.32.157.92
152.32.158.69
152.32.158.74
152.32.158.96
152.32.158.98
152.32.158.219
152.32.159.65
152.32.159.79
152.32.159.97
152.32.159.177
152.32.159.180
152.32.159.203
152.32.159.212
152.32.160.252
152.32.162.60
152.32.162.142
152.32.164.18
152.32.164.115
152.32.164.139
152.32.165.32
152.32.165.241
152.32.169.155
152.32.171.73
152.32.174.186
152.32.176.68
152.32.177.169
152.32.178.47
152.32.180.86
152.32.180.98
152.32.180.138
152.32.181.108
152.32.181.210
152.32.182.76
152.32.183.13
152.32.183.22
152.32.183.209
152.32.185.141
152.32.191.98
152.32.197.12
152.32.197.121
152.32.197.159
152.32.197.166
152.32.198.93
152.32.198.168
152.32.198.210
152.32.199.20
152.32.199.33
152.32.199.73
152.32.199.112
152.32.200.22
152.32.200.117
152.32.200.213
152.32.201.80
152.32.201.119
152.32.201.191
152.32.201.217
152.32.201.247
152.32.202.38
152.32.202.56
152.32.202.95
152.32.202.151
152.32.202.244
152.32.202.250
152.32.204.21
152.32.205.184
152.32.205.225
152.32.206.35
152.32.206.49
152.32.206.51
152.32.206.64
152.32.206.107
152.32.206.181
152.32.206.187
152.32.206.246/31
152.32.207.21
152.32.207.42
152.32.207.88
152.32.207.150
152.32.207.197
152.32.207.234
152.32.208.9
152.32.208.27
152.32.208.73
152.32.208.106
152.32.208.116
152.32.208.124
152.32.212.224
152.32.215.224
152.32.215.226
152.32.216.28
152.32.217.126
152.32.217.163
152.32.217.164
152.32.218.201
152.32.219.77
152.32.219.102
152.32.223.215
152.32.226.102
152.32.227.23
152.32.227.68
152.32.227.252
152.32.228.20
152.32.229.188
152.32.230.94
152.32.233.95
152.32.233.100
152.32.234.97
152.32.235.78
152.32.235.90
152.32.235.96
152.32.235.107
152.32.235.206
152.32.236.116
152.32.236.171
152.32.238.83
152.32.238.132
152.32.238.156
152.32.243.39
152.32.243.98
152.32.243.195
152.32.243.245
152.32.245.44
152.32.245.93
152.32.245.170
152.32.245.196
152.32.249.95
152.32.250.21
152.32.250.36
152.32.251.44
152.32.252.94
152.32.255.94
154.197.56.126
154.197.57.94
156.59.37.117
156.229.16.142
156.229.16.165
156.229.21.54
156.232.100.95
165.154.10.165
165.154.10.175
165.154.10.188
165.154.10.250
165.154.11.52
165.154.11.121
165.154.11.140
165.154.11.149
165.154.11.170
165.154.11.172
165.154.11.210
165.154.12.9
165.154.12.38
165.154.12.82
165.154.12.127
165.154.12.139
165.154.18.108
165.154.18.110
165.154.18.124/31
165.154.29.93
165.154.29.144
165.154.29.173
165.154.32.235
165.154.33.72
165.154.33.91
165.154.36.107
165.154.36.108
165.154.36.113
165.154.36.177
165.154.36.243
165.154.36.245
165.154.48.24
165.154.48.133
165.154.48.160
165.154.48.206
165.154.49.137
165.154.50.219
165.154.50.248
165.154.51.134
165.154.51.243
165.154.54.189
165.154.58.108
165.154.58.251
165.154.59.90
165.154.59.118
165.154.59.168
165.154.100.42
165.154.100.56
165.154.100.58
165.154.100.252
165.154.104.88
165.154.104.103
165.154.104.235
165.154.118.33
165.154.118.43
165.154.118.166
165.154.118.215
165.154.119.20
165.154.119.158
165.154.119.217
165.154.120.2
165.154.120.29
165.154.120.30
165.154.120.226
165.154.121.5
165.154.121.7
165.154.128.17
165.154.128.199
165.154.129.130
165.154.129.188
165.154.132.96
165.154.133.163
165.154.134.152
165.154.134.203
165.154.136.218
165.154.138.33
165.154.138.34
165.154.138.57
165.154.138.79
165.154.138.85
165.154.138.107
165.154.138.123
165.154.138.151
165.154.138.165
165.154.150.65
165.154.151.176
165.154.151.207
165.154.162.102
165.154.162.193
165.154.163.199
165.154.164.21
165.154.164.24
165.154.164.37
165.154.164.57
165.154.164.79
165.154.164.92
165.154.164.112
165.154.164.114
165.154.164.142
165.154.172.87
165.154.172.108
165.154.174.108
165.154.174.206
165.154.179.62
165.154.179.204
165.154.182.72
165.154.182.154
165.154.182.179
165.154.182.187
165.154.182.207
165.154.182.219
165.154.182.221
165.154.187.159
165.154.194.209
165.154.218.158
165.154.221.151
165.154.221.175
169.197.113.175
176.52.132.137
180.163.220.41
180.163.220.42/31
180.163.220.45
180.163.220.46/31
180.163.220.50/31
180.163.220.52
180.163.220.60/31
180.163.220.62
180.163.220.95
180.163.220.96/31
180.163.220.99
180.163.220.100/31
180.163.220.110/31
180.163.220.112
180.163.220.124/31
180.163.220.126
203.123.86.68
213.250.140.76
213.250.149.174
### asn_AS61280.cache
# стартовый снимок, источник обновлён 2026-11-11
185.224.228.0/24
185.224.230.0/23
195.209.122.0/23
212.192.156.0/22
2a0c:a9c7:156::/47
2a0c:a9c7:158::/48
### asn_AS196641.cache
# стартовый снимок, источник обновлён 2026-11-11
194.165.22.0/23
### asn_AS213853.cache
# стартовый снимок, источник обновлён 2026-11-11
62.76.98.0/24
### asn_AS51932.cache
# стартовый снимок, источник обновлён 2026-11-11
91.221.140.0/23
### asn_AS34500.cache
# стартовый снимок, источник обновлён 2026-11-11
80.73.16.0/20
FW_SEED_FILE

cat > "$stage/fw-update.service" <<'FW_SERVICE_FILE'
[Unit]
Description=Update inet fw geo and scanner lists
Wants=network-online.target
After=network-online.target nftables.service

[Service]
Type=oneshot
ExecStartPre=/bin/sh -ec '/usr/sbin/nft list table inet fw >/dev/null 2>&1 || { [ ! -s /opt/fw/fw.nft ] || /usr/sbin/nft -f /opt/fw/fw.nft; }'
ExecStart=/opt/fw/update-fw.sh

[Install]
WantedBy=multi-user.target
FW_SERVICE_FILE

cat > "$stage/fw-update.timer" <<'FW_TIMER_FILE'
[Unit]
Description=Daily inet fw list update

[Timer]
OnCalendar=*-*-* 05:17:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
FW_TIMER_FILE

if ! printf '%s  %s\n%s  %s\n%s  %s\n' \
    'c7ee84c8a7aa53085b41dfc6bc923b18bfcd54cbea08a5b628d5a9f0a03f2bce' "$stage/update-fw.sh" \
    '7d505bbd2b8a9bb0ea1b918c09363b69c4f0fdc2dfb7bcb8e57128306523ea0f' "$stage/fw-menu" \
    '05111a5a7d9680df7bde7b7f24bfaa530a012b6d965e62a477e93fe5f6b3e521' "$stage/seed.txt" | sha256sum -c - >/dev/null; then
  echo 'Блок вставился с искажениями (контрольная сумма не сошлась). Ничего не изменено.' >&2
  exit 1
fi
bash -n "$stage/update-fw.sh"
python3 -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$stage/fw-menu"

# Раскладывает стартовый кэш; уже существующие файлы кэша не трогает.
install_seed() {
  install -d -m 0700 "$FW/scanner-cache"
  awk -v fw="$FW" '
    /^### / {
      path = ($2 ~ /^asn_AS[0-9]+\.cache$/) ? fw "/" $2 : fw "/scanner-cache/" $2
      skip = (system("[ -e \"" path "\" ]") == 0)
      next
    }
    !skip { print > path }
  ' "$stage/seed.txt"
}

DEFAULT_ASN='# ФГУП «ГРЧЦ» (Главный радиочастотный центр)
AS61280
AS196641
AS213853
# ФГУП «Госкорпорация по ОрВД»
AS51932
# ФГУП «ЦТСПИ МГА»
AS34500'

# ---------------------------------------------------------------- обновление
if [ "$mode" = upgrade ]; then
  echo
  echo 'Найдена установка в /opt/fw: обновляю скрипты, настройки и списки не трогаю.'
  install -d -m 0700 /root/fw-rollbacks
  backup=$(mktemp -d /root/fw-rollbacks/upgrade.XXXXXXXX)
  cp -p "$FW/update-fw.sh" "$backup/update-fw.sh"
  for path in "$MENU" "$SERVICE" "$TIMER"; do
    if [ -f "$path" ]; then cp -p "$path" "$backup/${path##*/}"; fi
  done

  install -m 0755 "$stage/update-fw.sh" "$FW/.update-fw.new"
  mv -fT "$FW/.update-fw.new" "$FW/update-fw.sh"
  install -d /usr/local/sbin
  install -m 0755 "$stage/fw-menu" "$MENU.new"
  mv -fT "$MENU.new" "$MENU"
  install -m 0644 "$stage/fw-update.service" "$SERVICE"
  if [ ! -f "$TIMER" ]; then install -m 0644 "$stage/fw-update.timer" "$TIMER"; fi
  systemctl daemon-reload </dev/null
  if [ -f "$FW/v2dat" ]; then
    mv -f "$FW/v2dat" "$backup/v2dat"
    echo 'v2dat больше не нужен: геобазу разбирает сам скрипт. Файл перенесён в резервную копию.'
  fi
  for name in asn.txt extra.txt trusted.txt; do
    if [ ! -e "$FW/$name" ]; then : > "$FW/$name"; fi
  done
  install_seed

  if ! grep -qE '^[[:space:]]*[^#[:space:]]' "$FW/asn.txt"; then
    answer=${FW_DEFAULT_ASN:-}
    if [ -z "$answer" ]; then
      ask answer 'Список ASN пуст. Добавить штатный набор (ГРЧЦ, ОрВД, ЦТСПИ МГА — 5 ASN)? да/нет' 'да'
    fi
    if [ "$answer" = да ]; then
      printf '%s\n' "$DEFAULT_ASN" >> "$FW/asn.txt"
      echo 'Штатные ASN добавлены в /opt/fw/asn.txt.'
    fi
  fi

  "$MENU" --profile </dev/null || true
  echo
  echo 'Пробный запуск (правила не меняются):'
  if "$FW/update-fw.sh" --dry </dev/null; then
    echo
    echo 'Обновление установлено. Действующие правила пока прежние.'
    if systemctl is-enabled --quiet fw-update.service </dev/null && systemctl is-enabled --quiet fw-update.timer </dev/null; then
      echo 'Применить новые: systemctl start fw-update.service'
    else
      echo 'Автозапуск сейчас выключен: без него правила не обновляются и не поднимутся после перезагрузки.'
      echo 'Применить и включить автозапуск: systemctl enable fw-update.service && systemctl enable --now fw-update.timer && systemctl start fw-update.service'
    fi
  else
    echo
    echo 'Скрипты обновлены, но пробный запуск не прошёл: причина выше.' >&2
    echo 'Действующие правила не тронуты.' >&2
  fi
  echo "Откат скриптов: cp -p $backup/update-fw.sh $FW/update-fw.sh && cp -p $backup/fw-menu $MENU"
  if dpkg -s golang-go >/dev/null 2>&1; then
    echo 'Go этому файрволу больше не нужен. Если он стоял только ради него: apt-get purge golang-go && apt-get autoremove'
  fi
  exit 0
fi

# ----------------------------------------------------------- новая установка
PROFILE='не определён'; PORTS=''; NODE_PORTS=''; HOOK=input
while IFS='=' read -r key value; do
  case "$key" in
    PROFILE) PROFILE=$value ;;
    PORTS) PORTS=$value ;;
    NODE_PORTS) NODE_PORTS=$value ;;
    HOOK) HOOK=$value ;;
  esac
done < <(python3 "$stage/fw-menu" --suggest 2>/dev/null </dev/null || true)
[ "$HOOK" = prerouting ] || HOOK=input

echo
echo "Сервер: $PROFILE"
python3 "$stage/fw-menu" --profile 2>/dev/null </dev/null | sed -n '/^  [A-Z]/p' || true
if [ -n "$PORTS" ]; then
  echo "Клиенты подключаются на порты: $PORTS"
else
  echo 'Порты клиентских подключений определить не удалось: укажи их сам.'
  echo 'Это внешние порты Inbound. Порт 80 не указывай: через него продлевается сертификат.'
fi

geo_ports=${FW_GEO_PORTS:-}
if [ -z "$geo_ports" ]; then
  ask geo_ports 'Порты для геофильтра через пробел' "$PORTS"
fi
read -r -a geo_port_list <<< "$geo_ports"
if ! valid_ports "${geo_port_list[@]}"; then
  echo 'Нужен хотя бы один порт от 1 до 65535. Без диалога задай FW_GEO_PORTS. Ничего не установлено.' >&2
  exit 1
fi
geo_ports="${geo_port_list[*]}"
case " $geo_ports " in
  *" 80 "*) echo 'Внимание: порт 80 под геофильтром сломает продление сертификата Let'\''s Encrypt.' ;;
esac

panel_ip=''
if [ -n "$NODE_PORTS" ]; then
  echo "Порт управления нодой ($NODE_PORTS) сейчас открыт для всех адресов."
  panel_ip=${FW_PANEL_IP:-}
  if [ -z "$panel_ip" ]; then
    ask panel_ip 'IP панели, чтобы пускать на этот порт только её (Enter — пропустить)' ''
  fi
  if [ -n "$panel_ip" ]; then
    read -r -a panel_list <<< "$panel_ip"
    panel_ip=$(python3 - "${panel_list[@]}" <<'PY'
import ipaddress
import sys
try:
    networks = [ipaddress.ip_network(value) for value in sys.argv[1:]]
    if any(network.prefixlen == 0 for network in networks):
        raise ValueError("сеть /0")
except ValueError as error:
    sys.exit(f"Адрес панели не распознан: {error}")
print(" ".join(str(n.network_address) if n.prefixlen == n.max_prefixlen else str(n) for n in networks))
PY
    ) || { echo 'Ничего не установлено.' >&2; exit 1; }
  fi
fi

install -d -m 0755 /opt
mkdir -m 0700 "$FW"
{
  echo 'ZKEEN_URL="https://github.com/jameszeroX/zkeen-ip/releases/latest/download/zkeenip.dat"'
  echo 'GEO_TAG="ru"'
  echo 'MIN_RU=1000'
  echo 'API_PORTS=""'
  echo 'API_ALLOW=""'
  printf 'GEO_PORTS="%s"\n' "$geo_ports"
  printf 'HOOK="%s"\n' "$HOOK"
} > "$FW/fw.conf"
printf '%s\n' "$DEFAULT_ASN" > "$FW/asn.txt"
: > "$FW/extra.txt"
: > "$FW/trusted.txt"
ssh_client=${SSH_CONNECTION:-}
ssh_client=${ssh_client%% *}
if [ -n "$ssh_client" ]; then
  printf '%s\n' "$ssh_client" > "$FW/trusted.txt"
  echo "Адрес этого SSH-подключения ($ssh_client) добавлен в доверенные."
fi
if [ -n "$panel_ip" ]; then
  printf "# BEGIN FW-MENU API\nAPI_PORTS='%s'\nAPI_ALLOW='%s'\n# END FW-MENU API\n" "$NODE_PORTS" "$panel_ip" > "$FW/local.conf"
  echo "Порт $NODE_PORTS будет открыт только для: $panel_ip"
fi

install_seed
install -m 0755 "$stage/update-fw.sh" "$FW/update-fw.sh"
install -d /usr/local/sbin
install -m 0755 "$stage/fw-menu" "$MENU"
install -m 0644 "$stage/fw-update.service" "$SERVICE"
install -m 0644 "$stage/fw-update.timer" "$TIMER"
systemctl daemon-reload </dev/null

if [ "$HOOK" = prerouting ]; then
  echo 'Inbound опубликован из Docker-контейнера: фильтр ставится на prerouting.'
fi
echo
echo 'Пробный запуск (правила не применяются):'
if "$FW/update-fw.sh" --dry </dev/null; then
  echo
  echo 'Установлено. Правила ещё не включены.'
  echo 'Включить: fw-menu → «Проверить и применить правила» → «Применить правила и включить таймер».'
else
  echo
  echo 'Файлы установлены, но пробный запуск не прошёл: причина выше.' >&2
  echo 'Правила не включены. Исправь причину и запусти: /opt/fw/update-fw.sh --dry' >&2
fi
INSTALL_FW
