from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


LogCallback = Callable[[str], None]


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class AdbDevice:
    serial: str
    state: str
    details: dict[str, str] = field(default_factory=dict)
    wifi_mac: str = ""

    @property
    def is_wireless(self) -> bool:
        return parse_endpoint(self.serial) is not None

    @property
    def model(self) -> str:
        return self.details.get("model", "")

    @property
    def manufacturer(self) -> str:
        return self.details.get("manufacturer", "")


class AdbService:
    def __init__(
        self,
        adb_path: str | None = None,
        scrcpy_path: str | None = None,
        log_callback: LogCallback | None = None,
    ) -> None:
        self.adb_path = adb_path or shutil.which("adb") or "adb"
        self.scrcpy_path = scrcpy_path or shutil.which("scrcpy") or "scrcpy"
        self.log_callback = log_callback

    def adb_available(self) -> bool:
        return executable_exists(self.adb_path)

    def scrcpy_available(self) -> bool:
        return executable_exists(self.scrcpy_path)

    def list_devices(self) -> list[AdbDevice]:
        result = self.run_adb(["devices", "-l"], timeout=20)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "adb devices failed")
        devices = parse_adb_devices(result.stdout)
        for device in devices:
            if device.state == "device":
                device.wifi_mac = self.get_wifi_mac(device.serial)
        return devices

    def enable_tcpip(self, serial: str, port: int = 5555) -> CommandResult:
        return self.run_adb(["-s", serial, "tcpip", str(port)], timeout=30)

    def get_wifi_ip(self, serial: str) -> str:
        route = self.run_adb(["-s", serial, "shell", "ip", "route"], timeout=15)
        ip_address = parse_src_ip(route.stdout)
        if ip_address:
            return ip_address

        wlan = self.run_adb(
            ["-s", serial, "shell", "ip", "-f", "inet", "addr", "show", "wlan0"],
            timeout=15,
        )
        ip_address = parse_inet_ip(wlan.stdout)
        if ip_address:
            return ip_address

        ifconfig = self.run_adb(["-s", serial, "shell", "ifconfig", "wlan0"], timeout=15)
        ip_address = parse_inet_ip(ifconfig.stdout)
        if ip_address:
            return ip_address

        raise RuntimeError("Could not find a Wi-Fi IP address for this device.")

    def get_wifi_mac(self, serial: str) -> str:
        address_file = self.run_adb(
            ["-s", serial, "shell", "cat", "/sys/class/net/wlan0/address"],
            timeout=10,
        )
        mac_address = parse_mac_address(address_file.stdout)
        if mac_address:
            return mac_address

        link = self.run_adb(["-s", serial, "shell", "ip", "link", "show", "wlan0"], timeout=10)
        mac_address = parse_mac_address(link.stdout)
        if mac_address:
            return mac_address

        addr = self.run_adb(["-s", serial, "shell", "ip", "addr", "show", "wlan0"], timeout=10)
        return parse_mac_address(addr.stdout)

    def connect(self, host: str, port: int = 5555) -> CommandResult:
        return self.run_adb(["connect", f"{host}:{port}"], timeout=30)

    def disconnect(self, endpoint: str) -> CommandResult:
        return self.run_adb(["disconnect", endpoint], timeout=15)

    def get_state(self, serial: str) -> CommandResult:
        return self.run_adb(["-s", serial, "get-state"], timeout=15)

    def prepare_wireless(self, serial: str, port: int = 5555) -> str:
        ip_address = ""
        try:
            ip_address = self.get_wifi_ip(serial)
        except RuntimeError:
            self.log("Wi-Fi IP was not available before tcpip; will retry after tcpip.")

        tcpip = self.enable_tcpip(serial, port)
        if not tcpip.ok:
            raise RuntimeError(tcpip.stderr or tcpip.stdout or "adb tcpip failed")

        time.sleep(2)
        if not ip_address:
            ip_address = self.get_wifi_ip(serial)

        connect = self.connect(ip_address, port)
        if not connection_result_success(connect, f"{ip_address}:{port}"):
            raise RuntimeError(connect.stderr or connect.stdout or "adb connect failed")

        state = self.get_state(f"{ip_address}:{port}")
        if not state.ok or state.stdout.strip() != "device":
            raise RuntimeError(state.stderr or state.stdout or "wireless device is not ready")

        return ip_address

    def launch_scrcpy(self, serial: str) -> int:
        command = [self.scrcpy_path, "-s", serial]
        self.log_command(command)
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=windows_creationflags(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError("scrcpy was not found on PATH.") from exc
        self.log(f"scrcpy launched for {serial} (pid {process.pid})")
        return process.pid

    def run_adb(self, args: list[str], timeout: int = 30) -> CommandResult:
        return self.run_command([self.adb_path, *args], timeout=timeout)

    def run_command(self, command: list[str], timeout: int = 30) -> CommandResult:
        self.log_command(command)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=windows_creationflags(),
            )
            result = CommandResult(
                command=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except FileNotFoundError as exc:
            result = CommandResult(command=command, returncode=127, stderr=str(exc))
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                command=command,
                returncode=124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or f"Timed out after {timeout} seconds",
            )

        self.log_result(result)
        return result

    def log_command(self, command: list[str]) -> None:
        self.log("$ " + subprocess.list2cmdline(command))

    def log_result(self, result: CommandResult) -> None:
        if result.stdout.strip():
            self.log("stdout: " + result.stdout.strip())
        if result.stderr.strip():
            self.log("stderr: " + result.stderr.strip())
        if result.returncode != 0:
            self.log(f"exit code: {result.returncode}")

    def log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)


def executable_exists(path: str) -> bool:
    try:
        return shutil.which(path) is not None or Path(path).is_file()
    except (OSError, TypeError, ValueError):
        return False


def parse_adb_devices(output: str) -> list[AdbDevice]:
    devices: list[AdbDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        state = parts[1]
        details: dict[str, str] = {}
        for token in parts[2:]:
            if ":" not in token:
                continue
            key, value = token.split(":", 1)
            details[key] = value.replace("_", " ")

        devices.append(AdbDevice(serial=serial, state=state, details=details))
    return devices


def parse_endpoint(serial: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d+)", serial)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def parse_src_ip(output: str) -> str:
    match = re.search(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b", output)
    return match.group(1) if match else ""


def parse_inet_ip(output: str) -> str:
    match = re.search(r"\binet(?: addr:)?\s*(\d{1,3}(?:\.\d{1,3}){3})\b", output)
    return match.group(1) if match else ""


def parse_mac_address(output: str) -> str:
    match = re.search(r"\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\b", output)
    return match.group(1).lower() if match else ""


def connection_result_success(result: CommandResult, endpoint: str) -> bool:
    text = f"{result.stdout}\n{result.stderr}".lower()
    endpoint_text = endpoint.lower()
    if any(word in text for word in ("failed", "unable", "cannot", "refused")):
        return False
    return (
        result.ok
        and (
            f"connected to {endpoint_text}" in text
            or f"already connected to {endpoint_text}" in text
            or "already connected" in text
        )
    )


def windows_creationflags() -> int:
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0
