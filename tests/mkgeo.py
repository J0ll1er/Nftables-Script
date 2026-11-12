#!/usr/bin/env python3
"""Делает минимальный geoip.dat (protobuf GeoIPList) с категорией RU для тестов."""
import ipaddress
import sys


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field(number, payload):
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def cidr(network):
    body = field(1, network.network_address.packed) + varint(2 << 3) + varint(network.prefixlen)
    return field(2, body)


networks = [ipaddress.ip_network(f"10.{i // 256}.{i % 256}.0/24") for i in range(1500)]
networks.append(ipaddress.ip_network("2001:db8::/32"))
entry = field(1, b"RU") + b"".join(cidr(n) for n in networks)
open(sys.argv[1], "wb").write(field(1, entry))
