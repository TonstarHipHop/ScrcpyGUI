from __future__ import annotations

import re
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from adb_service import AdbService, windows_creationflags


QUANT_RACER_RELATIVE_CFG = Path("3proxy-0.9.5-x64") / "bin64" / "3proxy.cfg"
BEGIN_MARKER = "# BEGIN scrcpyGUI managed proxies"
END_MARKER = "# END scrcpyGUI managed proxies"
SCRCPY_PROXY_PORT_START = 3080
SCRCPY_PROXY_PORT_END = 4079

ANDROID_ADAPTER_DESC_RE = re.compile(
    r"(remote ndis|rndis|android|usb.*(tether|ethernet)|cdc ncm)",
    re.IGNORECASE,
)
IOS_USB_ADAPTER_DESC_RE = re.compile(
    r"(apple mobile device ethernet|apple usb ethernet|apple mobile device usb ethernet)",
    re.IGNORECASE,
)
TETHER_IFACE_RE = re.compile(r"(rndis|usb|ncm)", re.IGNORECASE)
ROUTE_SRC_RE = re.compile(
    r"\bdev\s+(?P<iface>\S+).*?\bsrc\s+(?P<ip>\d+\.\d+\.\d+\.\d+)"
)
LLADDR_RE = re.compile(r"\blladdr\s+(?P<mac>[0-9A-Fa-f:]{17})\b")
IPV4_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+)")
SOCKS_RE = re.compile(
    r"^\s*socks\b.*?(?:^|\s)-p(?P<port>\d+)\b.*?(?:^|\s)-i(?P<internal>\S+)"
    r"\b.*?(?:^|\s)-e(?P<external>\S+)",
    re.IGNORECASE,
)
SERIAL_RE = re.compile(r"\bserial=(?P<serial>\S+)")
SOURCE_RE = re.compile(r"\bsource=(?P<source>\S+)")
MODEL_RE = re.compile(r"\bmodel=(?P<model>.*?)(?=\s+adapter=|\s+mac=|\s+source=|$)")
ADAPTER_RE = re.compile(r"\badapter=(?P<adapter>.*?)(?=\s+mac=|\s+source=|$)")
MAC_RE = re.compile(r"\bmac=(?P<mac>\S+)")


@dataclass(frozen=True)
class AndroidTetherIdentity:
    serial: str
    tether_iface: str = ""
    phone_tether_ip: str = ""
    host_mac: str = ""


@dataclass(frozen=True)
class TetherAdapter:
    name: str
    description: str
    mac: str
    ipv4: str
    gateway: str = ""


@dataclass(frozen=True)
class ProxyMapping:
    serial: str
    source: str
    model: str
    adapter_name: str
    adapter_mac: str
    listen_host: str
    listen_port: int
    external_ip: str

    @property
    def proxy_url(self) -> str:
        return f"socks5://{self.listen_host}:{self.listen_port}"

    @property
    def source_label(self) -> str:
        source = self.source.lower()
        if source == "scrcpygui":
            return "scrcpyGUI"
        if source in {"android", "ios"}:
            return "Quant-Racer"
        if self.source:
            return self.source
        return "Quant-Racer"


@dataclass(frozen=True)
class ChromeProxyLaunch:
    process: subprocess.Popen[bytes]
    profile_dir: Path
    command: tuple[str, ...]


@dataclass(frozen=True)
class ProxyConfig:
    cfg_path: Path
    exists: bool
    mappings: tuple[ProxyMapping, ...]
    used_ports: frozenset[int]
    error: str = ""

    @property
    def by_serial(self) -> dict[str, ProxyMapping]:
        return {mapping.serial: mapping for mapping in self.mappings if mapping.serial}

    @property
    def managed_mappings(self) -> tuple[ProxyMapping, ...]:
        return tuple(
            mapping
            for mapping in self.mappings
            if mapping.source.lower() == "scrcpygui"
        )


def find_quant_racer_root(scrcpy_root: Path) -> Path | None:
    candidate = scrcpy_root.resolve().parent / "Quant-Racer"
    if candidate.exists():
        return candidate
    return None


def quant_racer_cfg_path(scrcpy_root: Path) -> Path:
    root = find_quant_racer_root(scrcpy_root)
    if root is None:
        return scrcpy_root.resolve().parent / "Quant-Racer" / QUANT_RACER_RELATIVE_CFG
    return root / QUANT_RACER_RELATIVE_CFG


def load_quant_racer_proxy_config(scrcpy_root: Path) -> ProxyConfig:
    cfg_path = quant_racer_cfg_path(scrcpy_root)
    if not cfg_path.exists():
        return ProxyConfig(
            cfg_path=cfg_path,
            exists=False,
            mappings=(),
            used_ports=frozenset(),
            error=f"3proxy config not found: {cfg_path}",
        )

    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ProxyConfig(
            cfg_path=cfg_path,
            exists=True,
            mappings=(),
            used_ports=frozenset(),
            error=str(exc),
        )

    mappings, used_ports = parse_proxy_config(text)
    return ProxyConfig(
        cfg_path=cfg_path,
        exists=True,
        mappings=tuple(mappings),
        used_ports=frozenset(used_ports),
    )


def parse_proxy_config(text: str) -> tuple[list[ProxyMapping], set[int]]:
    mappings: list[ProxyMapping] = []
    used_ports: set[int] = set()
    current_comment = ""
    in_managed_block = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == BEGIN_MARKER:
            in_managed_block = True
            current_comment = ""
            continue
        if line == END_MARKER:
            in_managed_block = False
            current_comment = ""
            continue
        if not line:
            continue
        if line.startswith("#"):
            current_comment = line
            continue

        socks_match = SOCKS_RE.search(line)
        if not socks_match:
            continue

        port = int(socks_match.group("port"))
        used_ports.add(port)
        serial = _regex_group(SERIAL_RE, current_comment, "serial")
        if not serial:
            current_comment = ""
            continue

        source = _regex_group(SOURCE_RE, current_comment, "source")
        if not source and in_managed_block:
            source = "scrcpyGUI"

        mappings.append(
            ProxyMapping(
                serial=serial,
                source=source,
                model=_regex_group(MODEL_RE, current_comment, "model"),
                adapter_name=_regex_group(ADAPTER_RE, current_comment, "adapter"),
                adapter_mac=_normalize_mac(_regex_group(MAC_RE, current_comment, "mac")),
                listen_host=socks_match.group("internal"),
                listen_port=port,
                external_ip=socks_match.group("external"),
            )
        )
        current_comment = ""

    return mappings, used_ports


def add_scrcpy_proxy_entry(
    scrcpy_root: Path,
    adb: AdbService,
    serial: str,
    model: str = "",
) -> ProxyMapping:
    config = load_quant_racer_proxy_config(scrcpy_root)
    if not config.exists:
        raise RuntimeError(config.error)
    if config.error:
        raise RuntimeError(config.error)

    existing = config.by_serial.get(serial)
    if existing:
        return existing

    identity = discover_android_tether_identity(adb, serial)
    adapters = discover_android_tether_adapters()
    adapter = select_tether_adapter(identity, adapters)
    if adapter is None:
        raise RuntimeError("No tether adapter matched this USB phone.")

    port = choose_available_port(config.used_ports)
    mapping = ProxyMapping(
        serial=serial,
        source="scrcpyGUI",
        model=model,
        adapter_name=adapter.name,
        adapter_mac=adapter.mac,
        listen_host="127.0.0.1",
        listen_port=port,
        external_ip=adapter.ipv4,
    )

    old_text = config.cfg_path.read_text(encoding="utf-8")
    managed = {
        existing_mapping.serial: existing_mapping
        for existing_mapping in config.managed_mappings
    }
    managed[serial] = mapping
    new_text = replace_managed_block(old_text, managed.values())
    config.cfg_path.write_text(new_text, encoding="utf-8")
    return mapping


def discover_android_tether_identity(
    adb: AdbService,
    serial: str,
) -> AndroidTetherIdentity:
    routes = _adb_stdout(adb, ["-s", serial, "shell", "ip", "-4", "route"])
    tether_iface = ""
    phone_tether_ip = ""

    route_matches = list(ROUTE_SRC_RE.finditer(routes))
    for route_match in route_matches:
        iface = route_match.group("iface")
        if TETHER_IFACE_RE.search(iface):
            tether_iface = iface
            phone_tether_ip = route_match.group("ip")
            break

    if not tether_iface and route_matches:
        tether_iface = route_matches[0].group("iface")
        phone_tether_ip = route_matches[0].group("ip")

    host_mac = ""
    if tether_iface:
        neigh = _adb_stdout(
            adb,
            ["-s", serial, "shell", "ip", "neigh", "show", "dev", tether_iface],
            allow_failure=True,
        )
        for line in neigh.splitlines():
            lladdr_match = LLADDR_RE.search(line)
            if lladdr_match:
                host_mac = _normalize_mac(lladdr_match.group("mac"))
                break

    return AndroidTetherIdentity(
        serial=serial,
        tether_iface=tether_iface,
        phone_tether_ip=phone_tether_ip,
        host_mac=host_mac,
    )


def discover_android_tether_adapters() -> list[TetherAdapter]:
    completed = subprocess.run(
        ["ipconfig", "/all"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=windows_creationflags(),
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "ipconfig failed")

    adapters: list[TetherAdapter] = []
    for entry in parse_ipconfig_adapters(completed.stdout):
        if entry.get("disconnected") == "true":
            continue
        description = entry.get("description") or ""
        ipv4 = entry.get("ipv4") or ""
        if not ipv4:
            continue
        if not ANDROID_ADAPTER_DESC_RE.search(description):
            continue
        if IOS_USB_ADAPTER_DESC_RE.search(description):
            continue
        adapters.append(
            TetherAdapter(
                name=entry.get("name") or "Unknown",
                description=description,
                mac=_normalize_mac(entry.get("mac") or ""),
                ipv4=ipv4,
                gateway=entry.get("gateway") or "",
            )
        )
    return adapters


def parse_ipconfig_adapters(text: str) -> list[dict[str, str]]:
    adapters: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    waiting_gateway_continuation = False

    def flush_current() -> None:
        nonlocal current
        if current:
            adapters.append(current)
        current = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.endswith(":") and " adapter " in stripped.lower():
            flush_current()
            adapter_name = stripped.split(" adapter ", 1)[1].rstrip(":").strip()
            current = {
                "name": adapter_name,
                "description": "",
                "mac": "",
                "ipv4": "",
                "gateway": "",
                "disconnected": "false",
            }
            waiting_gateway_continuation = False
            continue

        if current is None:
            continue

        lower = stripped.lower()
        if "media state" in lower and "disconnected" in lower:
            current["disconnected"] = "true"

        if stripped.startswith("Description"):
            current["description"] = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Physical Address"):
            current["mac"] = _normalize_mac(stripped.split(":", 1)[1].strip())
            continue
        if stripped.startswith("IPv4 Address"):
            ip_match = IPV4_RE.search(stripped)
            if ip_match:
                current["ipv4"] = ip_match.group(1)
            continue
        if stripped.startswith("Default Gateway"):
            ip_match = IPV4_RE.search(stripped)
            if ip_match:
                current["gateway"] = ip_match.group(1)
                waiting_gateway_continuation = False
            else:
                waiting_gateway_continuation = True
            continue
        if waiting_gateway_continuation:
            ip_match = IPV4_RE.search(stripped)
            if ip_match:
                current["gateway"] = ip_match.group(1)
            waiting_gateway_continuation = False

    flush_current()
    return adapters


def select_tether_adapter(
    identity: AndroidTetherIdentity,
    adapters: list[TetherAdapter],
) -> TetherAdapter | None:
    if identity.host_mac:
        for adapter in adapters:
            if adapter.mac and adapter.mac == identity.host_mac:
                return adapter

    if identity.phone_tether_ip:
        for adapter in adapters:
            if adapter.gateway and adapter.gateway == identity.phone_tether_ip:
                return adapter

    if len(adapters) == 1:
        return adapters[0]

    return None


def choose_available_port(used_ports: Iterable[int]) -> int:
    blocked = set(int(port) for port in used_ports)
    for port in range(SCRCPY_PROXY_PORT_START, SCRCPY_PROXY_PORT_END + 1):
        if port in blocked:
            continue
        if is_local_port_listening("127.0.0.1", port):
            continue
        return port
    raise RuntimeError("No available scrcpyGUI proxy ports in 3080-4079.")


def replace_managed_block(text: str, mappings: Iterable[ProxyMapping]) -> str:
    cleaned = remove_managed_block(text).rstrip()
    block = render_managed_block(mappings)
    if not block:
        return cleaned + "\n"
    return cleaned + "\n\n" + block + "\n"


def remove_managed_block(text: str) -> str:
    begin = text.find(BEGIN_MARKER)
    if begin < 0:
        return text
    end = text.find(END_MARKER, begin)
    if end < 0:
        return text[:begin]
    end += len(END_MARKER)
    return text[:begin].rstrip() + "\n" + text[end:].lstrip("\r\n")


def render_managed_block(mappings: Iterable[ProxyMapping]) -> str:
    ordered = sorted(mappings, key=lambda mapping: (mapping.listen_port, mapping.serial))
    if not ordered:
        return ""

    lines = [BEGIN_MARKER]
    for mapping in ordered:
        model = _comment_value(mapping.model or "unknown")
        adapter = _comment_value(mapping.adapter_name or "unknown")
        mac = _comment_value(mapping.adapter_mac or "unknown")
        lines.append(
            f"# source=scrcpyGUI serial={mapping.serial} model={model} "
            f"adapter={adapter} mac={mac}"
        )
        lines.append(
            f"socks -p{mapping.listen_port} -i{mapping.listen_host} -e{mapping.external_ip}"
        )
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    lines.append(END_MARKER)
    return "\n".join(lines)


def is_local_port_listening(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def find_chrome_path() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate

    for command in ("chrome.exe", "chrome"):
        resolved = shutil.which(command)
        if resolved:
            return resolved

    raise RuntimeError("Chrome was not found.")


def build_chrome_proxy_command(
    mapping: ProxyMapping,
    user_data_dir: Path,
    url: str = "about:blank",
) -> list[str]:
    chrome_path = find_chrome_path()
    return [
        chrome_path,
        f"--user-data-dir={user_data_dir}",
        f"--proxy-server={mapping.proxy_url}",
        "--new-window",
        url,
    ]


def launch_chrome_with_proxy(
    mapping: ProxyMapping,
    user_data_dir: Path | None = None,
    url: str = "about:blank",
) -> ChromeProxyLaunch:
    if user_data_dir is None:
        user_data_dir = make_temp_chrome_profile_dir(mapping.serial)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    command = build_chrome_proxy_command(mapping, user_data_dir, url=url)
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=windows_creationflags(),
    )
    return ChromeProxyLaunch(
        process=process,
        profile_dir=user_data_dir,
        command=tuple(command),
    )


def make_temp_chrome_profile_dir(serial: str = "") -> Path:
    profile_hint = safe_profile_name(serial)
    return Path(tempfile.mkdtemp(prefix=f"scrcpyGUI_chrome_{profile_hint}_"))


def safe_profile_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned or "device"


def _adb_stdout(
    adb: AdbService,
    args: list[str],
    allow_failure: bool = False,
) -> str:
    result = adb.run_adb(args, timeout=20)
    if result.ok or allow_failure:
        return result.stdout
    raise RuntimeError(result.stderr or result.stdout or "ADB command failed")


def _normalize_mac(mac: str | None) -> str:
    if not mac:
        return ""
    return re.sub(r"[^0-9A-Fa-f]", "", mac).upper()


def _comment_value(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _regex_group(pattern: re.Pattern[str], text: str, group: str) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    return match.group(group).strip()
