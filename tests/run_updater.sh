#!/bin/bash
# run_updater.sh ИМЯ_СЦЕНАРИЯ [--dry]  — запускает собранный update-fw.sh в песочнице.
# Переменные сценария: SCEN_RIPE, SCEN_IPVERSE, SCEN_GEO, SCEN_LISTS (ok|down); CACHE_ASN=1 кладёт кэш ASN.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
name=$1; shift
T=$ROOT/t/$name
rm -rf "$T"; mkdir -p "$T/fw/scanner-cache" "$T/stubs"
cp "$ROOT/tests/stubs/curl" "$ROOT/tests/stubs/nft" "$ROOT/tests/stubs/flock" "$T/stubs/"
chmod +x "$T/stubs/"*
python3 -I "$ROOT/tests/mkgeo.py" "$T/geo.dat"
sed "s#^DIR=/opt/fw#DIR=$T/fw#" "$ROOT/build/update-fw.sh" > "$T/fw/update-fw.sh"
chmod +x "$T/fw/update-fw.sh"
cat > "$T/fw/fw.conf" <<'EOF'
ZKEEN_URL="https://example.invalid/zkeenip.dat"
GEO_TAG="ru"
GEO_PORTS="443"
MIN_RU=1000
API_PORTS=""
API_ALLOW=""
EOF
printf 'AS61280\nAS34500\n' > "$T/fw/asn.txt"
: > "$T/fw/extra.txt"; echo 192.0.2.250 > "$T/fw/trusted.txt"
[ -z "${TEST_CONF_APPEND:-}" ] || printf '%s\n' "$TEST_CONF_APPEND" >> "$T/fw/fw.conf"
[ -z "${TEST_WIDE_OK:-}" ] || printf '%s\n' "$TEST_WIDE_OK" > "$T/fw/wide-ok.txt"
[ -z "${TEST_TRUSTED:-}" ] || printf '%s\n' "$TEST_TRUSTED" >> "$T/fw/trusted.txt"
[ "${TEST_LAN+x}" != x ] || printf 'LAN_ALLOW="%s"\n' "$TEST_LAN" >> "$T/fw/fw.conf"
[ -z "${TEST_CACHE_NAME:-}" ] || printf '%s\n' "${TEST_CACHE_CONTENT:-192.0.2.1}" > "$T/fw/scanner-cache/$TEST_CACHE_NAME"
[ -z "${TEST_CACHE_AGE_DAYS:-}" ] || python3 - "$T/fw/scanner-cache/$TEST_CACHE_NAME" "$TEST_CACHE_AGE_DAYS" <<'PY'
import os, sys, time
age = time.time() - float(sys.argv[2]) * 86400
os.utime(sys.argv[1], (age, age))
PY
case "${CACHE_ASN:-0}" in
  1) CACHE_LIST="61280 34500" ;;
  0) CACHE_LIST="" ;;
  *) CACHE_LIST="$CACHE_ASN" ;;
esac
for n in $CACHE_LIST; do echo 185.224.228.0/24 > "$T/fw/asn_AS$n.cache"; done
[ -z "${TEST_ASN_CACHE_AGE_DAYS:-}" ] || python3 - "$T/fw" "$TEST_ASN_CACHE_AGE_DAYS" <<'PY'
from pathlib import Path
import os, sys, time
age = time.time() - float(sys.argv[2]) * 86400
for path in Path(sys.argv[1]).glob('asn_*.cache'):
    os.utime(path, (age, age))
PY
export STUB_DIR=$T GEO_DAT=$T/geo.dat PATH="$T/stubs:$PATH"
"$T/fw/update-fw.sh" "$@"
status=$?
echo "код возврата: $status"
echo "--- обращений к RIPEstat: $(wc -l < "$T/ripe_calls" 2>/dev/null || echo 0), к ipverse: $(wc -l < "$T/ipverse_calls" 2>/dev/null || echo 0)"
echo "--- wide-pending:"; if [ -s "$T/fw/wide-pending.txt" ]; then cat "$T/fw/wide-pending.txt"; else echo "(нет)"; fi
echo "--- errors.log:"; if [ -s "$T/fw/errors.log" ]; then cat "$T/fw/errors.log"; else echo "(пусто)"; fi
ls -l "$T/fw/errors.log" 2>/dev/null | awk '{print "права: " $1}'
