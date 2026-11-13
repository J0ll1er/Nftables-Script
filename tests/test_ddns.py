"""DDNS-имена в доверенных: разрешение, фильтры, кэш при сбое DNS."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest


ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "build" / "update-fw.sh").read_text(encoding="utf-8")
START = TEXT.index("<<'PY_DDNS'\n") + len("<<'PY_DDNS'\n")
SOURCE = TEXT[START:TEXT.index("\nPY_DDNS\n", START) + 1]


class Resolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.names = self.dir / "names"
        self.cache = self.dir / "cache"
        self.out = self.dir / "out"
        self.answers = {}

    def tearDown(self):
        self.tmp.cleanup()

    def fake_socket(self):
        module = types.ModuleType("socket")
        module.AF_INET, module.AF_INET6, module.IPPROTO_TCP = 2, 10, 6
        module.gaierror = type("gaierror", (OSError,), {})

        def getaddrinfo(name, port, *args, **kwargs):
            value = self.answers.get(name)
            if value is None:
                raise module.gaierror("нет имени")
            return [(2 if ":" not in item else 10, 1, 6, "", (item, 0)) for item in value]
        module.getaddrinfo = getaddrinfo
        return module

    def run_resolver(self, trusted_lines, dry="0", max_hours="72"):
        self.names.write_text("\n".join(trusted_lines) + "\n", encoding="utf-8")
        saved = sys.modules.get("socket")
        sys.modules["socket"] = self.fake_socket()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                argv = sys.argv
                sys.argv = ["x", str(self.names), str(self.cache), str(self.out), dry, max_hours]
                try:
                    exec(compile(SOURCE, "ddns", "exec"), {"__name__": "__main__"})
                finally:
                    sys.argv = argv
        finally:
            sys.modules["socket"] = saved
        return out.getvalue(), (self.out.read_text().split() if self.out.exists() else [])

    def test_name_is_resolved_and_ip_lines_are_ignored(self):
        self.answers["home.example.com"] = ["93.184.216.34", "2606:2800:220:1::1"]
        text, addresses = self.run_resolver(["1.2.3.4  # свой", "Home.Example.com  # дом", "# комментарий"])
        self.assertEqual(sorted(addresses), ["2606:2800:220:1::1", "93.184.216.34"])

    def test_private_loopback_and_multicast_answers_are_rejected(self):
        self.answers["evil.example.com"] = ["127.0.0.1"]
        self.answers["lan.example.com"] = ["192.168.1.5", "10.0.0.1"]
        text, addresses = self.run_resolver(["evil.example.com", "lan.example.com"])
        self.assertEqual(addresses, [])
        self.assertIn("WARN", text)

    def test_too_many_addresses_are_rejected(self):
        self.answers["many.example.com"] = [f"93.184.216.{n}" for n in range(1, 12)]
        text, addresses = self.run_resolver(["many.example.com"])
        self.assertEqual(addresses, [])
        self.assertIn("WARN", text)

    def test_dns_failure_uses_fresh_cache_and_warns(self):
        self.answers["home.example.com"] = ["93.184.216.34"]
        self.run_resolver(["home.example.com"])
        del self.answers["home.example.com"]
        text, addresses = self.run_resolver(["home.example.com"])
        self.assertEqual(addresses, ["93.184.216.34"])
        self.assertIn("WARN", text)

    def test_stale_cache_is_dropped(self):
        self.cache.write_text(f"home.example.com 93.184.216.34 {int(time.time()) - 100 * 3600}\n")
        text, addresses = self.run_resolver(["home.example.com"], max_hours="72")
        self.assertEqual(addresses, [])
        self.assertIn("WARN", text)

    def test_dry_run_does_not_write_cache(self):
        self.answers["home.example.com"] = ["93.184.216.34"]
        self.run_resolver(["home.example.com"], dry="1")
        self.assertFalse(self.cache.exists())

    def test_address_change_replaces_cached_one(self):
        self.answers["home.example.com"] = ["93.184.216.34"]
        self.run_resolver(["home.example.com"])
        self.answers["home.example.com"] = ["93.184.216.99"]
        text, addresses = self.run_resolver(["home.example.com"])
        self.assertEqual(addresses, ["93.184.216.99"])

    def test_invalid_names_are_ignored(self):
        text, addresses = self.run_resolver(["bad_name", "-x.example.com", "a..b.com", "localhost"])
        self.assertEqual(addresses, [])


if __name__ == "__main__":
    unittest.main()
