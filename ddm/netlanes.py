import shutil
import socket
import subprocess
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection

SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
SKIP_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "vnet", "tailscale", "tun", "tap", "wg", "zt", "lxc", "podman", "cni", "flannel")
SYS_NET = Path("/sys/class/net")


class BoundAdapter(HTTPAdapter):
    def __init__(self, iface, **kw):
        self.iface = iface
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        opts = list(HTTPConnection.default_socket_options)
        opts.append((socket.SOL_SOCKET, SO_BINDTODEVICE, self.iface.encode() + b"\0"))
        pool_kwargs["socket_options"] = opts
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        opts = list(HTTPConnection.default_socket_options)
        opts.append((socket.SOL_SOCKET, SO_BINDTODEVICE, self.iface.encode() + b"\0"))
        proxy_kwargs["socket_options"] = opts
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def bound_session(iface, pool=8):
    s = requests.Session()
    adapter = BoundAdapter(iface, pool_connections=2, pool_maxsize=pool, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _default_route_ifaces():
    found = {}
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) >= 7 and parts[1] == "00000000" and parts[7] == "00000000":
                    metric = int(parts[6])
                    found[parts[0]] = min(metric, found.get(parts[0], metric))
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    try:
        with open("/proc/net/ipv6_route") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 10 and set(parts[0]) == {"0"} and parts[1] == "00":
                    dev = parts[9]
                    metric = int(parts[5], 16)
                    if dev != "lo":
                        found.setdefault(dev, metric)
    except (OSError, ValueError):
        pass
    return found


def _kind(name):
    if (SYS_NET / name / "wireless").exists() or (SYS_NET / name / "phy80211").exists():
        return "wifi", "Wi-Fi"
    if name.startswith(("enx", "usb", "rndis")):
        return "usb", "USB internet paylaşımı"
    if name.startswith(("wwan", "wwp", "ppp")):
        return "mobile", "Mobil modem"
    if name.startswith(("en", "eth")):
        return "ethernet", "Kablolu"
    return "other", "Ağ"


def _ipv4(name):
    try:
        import fcntl
        import struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            data = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))
            return socket.inet_ntoa(data[20:24])
        finally:
            s.close()
    except OSError:
        return ""


def _metered(name):
    nmcli = shutil.which("nmcli")
    if not nmcli:
        return None
    try:
        out = subprocess.run([nmcli, "-t", "-g", "GENERAL.METERED", "device", "show", name],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    val = out.stdout.strip().lower()
    if val.startswith("yes"):
        return True
    if val.startswith("no"):
        return False
    return None


def list_uplinks():
    routes = _default_route_ifaces()
    out = []
    for name, metric in sorted(routes.items(), key=lambda kv: kv[1]):
        if name.startswith(SKIP_PREFIXES):
            continue
        try:
            state = (SYS_NET / name / "operstate").read_text().strip()
        except OSError:
            state = "unknown"
        if state == "down":
            continue
        kind, label = _kind(name)
        out.append({"name": name, "kind": kind, "label": label, "metric": metric, "addr": _ipv4(name),
                    "metered": _metered(name)})
    return out


def iface_exists(name):
    return (SYS_NET / name).exists() or name in [n for _, n in socket.if_nameindex()]
