"""Ограничение частоты SSH на настоящем nftables в отдельном сетевом пространстве (нужны root, nft и unshare).
Без них тест пропускается. Трафик идёт по loopback внутри пространства, правило iif lo в тест не включено."""
import os
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest

CAN = os.geteuid() == 0 and shutil.which("nft") and shutil.which("unshare")

SCENARIO = textwrap.dedent('''
    import fcntl, socket, struct, subprocess, sys, threading, time
    rate, burst, steps = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    ctl = socket.socket()
    flags = struct.unpack("16sH14s", fcntl.ioctl(ctl, 0x8913, struct.pack("16sH14s", b"lo", 0, b"")))[1]
    fcntl.ioctl(ctl, 0x8914, struct.pack("16sH14s", b"lo", flags | 1, b""))
    rules = (
        "add table inet fw\\ndelete table inet fw\\ntable inet fw {\\n"
        " set sr4 { type ipv4_addr . inet_service; size 65536; flags dynamic,timeout; timeout 7d; counter; }\\n"
        " set sr6 { type ipv6_addr . inet_service; size 65536; flags dynamic,timeout; timeout 7d; counter; }\\n"
        " chain input {\\n"
        "  type filter hook input priority -10; policy accept;\\n  ct state established,related accept\\n"
        f"  tcp dport {{ 22 }} ct state new meter ssh4 {{ ip saddr timeout 5m limit rate over {rate}/minute burst {burst} packets }} add @sr4 {{ ip saddr . tcp dport }} counter drop comment \\"ssh-rate\\"\\n"
        f"  tcp dport {{ 22 }} ct state new meter ssh6 {{ ip6 saddr & ffff:ffff:ffff:ffff:: timeout 5m limit rate over {rate}/minute burst {burst} packets }} add @sr6 {{ ip6 saddr . tcp dport }} counter drop comment \\"ssh-rate\\"\\n"
        " }\\n}\\n")
    subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)
    try:
        server = socket.socket(socket.AF_INET6)
        server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("::", 22)); has_v6 = True
    except OSError:
        server = socket.socket(socket.AF_INET)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", 22)); has_v6 = False
    server.listen(256)
    held = []
    def accept():
        while True:
            conn, _ = server.accept()
            held.append(conn)
    threading.Thread(target=accept, daemon=True).start()

    def attempt(address, count):
        ok = 0
        for _ in range(count):
            s = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET)
            s.settimeout(0.1)
            try:
                s.connect((address, 22)); ok += 1
            except OSError:
                pass
            s.close()
        return ok

    def recorded():
        text = subprocess.run(["nft", "-n", "list", "set", "inet", "fw", "sr4"], text=True, capture_output=True).stdout
        return text.split("elements = ", 1)[1].split("}", 1)[0].strip() if "elements" in text else "none"

    if steps == "v4":
        print("first", attempt("127.0.0.1", 20)); print("again", attempt("127.0.0.1", 3))
        print("recorded", recorded().replace(" ", "_"))
        time.sleep(5.6); print("later", attempt("127.0.0.1", 4))
    elif steps == "within":
        print("ok", attempt("127.0.0.1", 5)); print("recorded", recorded().replace(" ", "_"))
    elif steps == "v6" and not has_v6:
        print("skip")
    elif steps == "v6":
        print("first", attempt("::1", 20)); print("again", attempt("::1", 3))
    elif steps == "session":
        live = socket.create_connection(("127.0.0.1", 22), timeout=2)
        attempt("127.0.0.1", 30)
        live.send(b"x"); time.sleep(0.2); live.send(b"y"); print("alive")
''')


def run_scenario(rate, burst, steps):
    out = subprocess.run(["unshare", "-n", "python3", "-I", "-c", SCENARIO, str(rate), str(burst), steps],
                         text=True, capture_output=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr)
    return {line.split()[0]: line.split()[1] if len(line.split()) > 1 else "" for line in out.stdout.splitlines()}


@unittest.skipUnless(CAN, "нужны root, nft и unshare")
class RealSsh(unittest.TestCase):
    def test_burst_passes_excess_is_dropped_and_rate_recovers(self):
        result = run_scenario(12, 5, "v4")
        self.assertGreaterEqual(int(result["first"]), 5)    # пачка проходит
        self.assertLessEqual(int(result["first"]), 8)       # избыток отброшен
        self.assertEqual(int(result["again"]), 0)           # сразу после пачки новых нет
        self.assertGreaterEqual(int(result["later"]), 1)    # через несколько секунд доступ возвращается сам

    def test_only_exceeding_addresses_are_recorded_with_a_counter(self):
        within = run_scenario(12, 5, "within")
        self.assertEqual(within["recorded"], "none")         # в пределах порога набор пуст
        over = run_scenario(12, 5, "v4")["recorded"]
        self.assertRegex(over, r"^\{_127\.0\.0\.1_\._22_counter_packets_\d+_bytes_\d+_expires_")

    def test_ipv6_is_limited_too(self):
        result = run_scenario(12, 5, "v6")
        if "skip" in result:
            self.skipTest("в этой системе нет IPv6")
        self.assertLessEqual(int(result["first"]), 8)
        self.assertEqual(int(result["again"]), 0)

    def test_established_session_is_not_cut_off(self):
        self.assertIn("alive", run_scenario(12, 5, "session"))


if __name__ == "__main__":
    unittest.main()
