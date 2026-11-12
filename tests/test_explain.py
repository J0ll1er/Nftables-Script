import json, subprocess, sys, textwrap, types
src = open(__import__("pathlib").Path(__file__).resolve().parent.parent / "build" / "fw-menu").read()
ns = {"__name__": "fwmenu_test"}
exec(compile(src, "fw-menu", "exec"), ns)
def rules(hook, extra=""):
    pre = "hook prerouting priority -150" if hook == "prerouting" else "hook input priority -10"
    fib = "fib daddr type != local accept\n" if hook == "prerouting" else ""
    return f'''
table inet fw {{
 set tr4 {{ type ipv4_addr; flags interval; auto-merge; elements = {{ 10.0.0.0/8, 192.0.2.1 }} }}
 set tr6 {{ type ipv6_addr; flags interval; auto-merge; }}
 set sc4 {{ type ipv4_addr; flags interval; auto-merge; elements = {{ 198.51.100.0/24, 203.0.113.0/24, 5.5.5.5 }} }}
 set sc6 {{ type ipv6_addr; flags interval; auto-merge; }}
 set ru4 {{ type ipv4_addr; flags interval; auto-merge; elements = {{ 77.0.0.0/8 }} }}
 set ru6 {{ type ipv6_addr; flags interval; auto-merge; }}
 set ap4 {{ type ipv4_addr; flags interval; auto-merge; elements = {{ 1.1.1.1, 2.2.2.2 }} }}
 set ap6 {{ type ipv6_addr; flags interval; auto-merge; }}
 chain input {{ type filter {pre}; policy accept;
  {fib}
  iif "lo" accept
  ct state established,related accept
  {extra}
  ip saddr @tr4 accept
  ip6 saddr @tr6 accept
  ip saddr @sc4 counter drop comment "scanners"
  ip6 saddr @sc6 counter drop comment "scanners"
  meta l4proto {{ tcp, udp }} th dport {{ 443 }} ip saddr != @ru4 counter drop comment "non-RU"
  meta l4proto {{ tcp, udp }} th dport {{ 443 }} ip6 saddr != @ru6 counter drop comment "non-RU"
 }}
}}'''
api = '''tcp dport 2222 ip saddr != @ap4 counter drop comment "api-lock"
  tcp dport 2222 ip6 saddr != @ap6 counter drop comment "api-lock"
  tcp dport 2222 ip saddr @ap4 accept comment "api-allow"'''
for hook, extra in (("input", api), ("prerouting", ""), ("input", "")):
    open("/tmp/r.nft","w").write(rules(hook, extra))
    out = subprocess.run(["unshare","-n","sh","-c","nft -f /tmp/r.nft && nft -j list table inet fw"],capture_output=True,text=True)
    if out.returncode: print(out.stderr); sys.exit(1)
    print("=====", hook, "api" if extra else "noapi")
    ns["explain_rules"](json.loads(out.stdout)["nftables"])
