from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_PORT = 5555


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@dataclass
class DeviceRecord:
    device_id: str
    friendly_name: str
    android_serial: str
    model: str = ""
    manufacturer: str = ""
    wifi_mac: str = ""
    wireless_host: str = ""
    wireless_port: int = DEFAULT_PORT
    last_seen_usb: str = ""
    last_seen_wireless: str = ""
    router_reminder_acknowledged: bool = False

    @property
    def endpoint(self) -> str:
        if not self.wireless_host:
            return ""
        return f"{self.wireless_host}:{self.wireless_port}"

    @property
    def last_seen(self) -> str:
        return max(self.last_seen_usb, self.last_seen_wireless)


class DeviceRegistry:
    def __init__(self, path: str | Path = "devices.json") -> None:
        self.path = Path(path)
        self.devices: dict[str, DeviceRecord] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.devices = {}
            return

        with self.path.open("r", encoding="utf-8") as file:
            raw = json.load(file)

        devices: dict[str, DeviceRecord] = {}
        for item in raw.get("devices", []):
            record = self._record_from_json(item)
            devices[record.device_id] = record
        self.devices = devices

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "devices": [asdict(record) for record in self.all_devices()],
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
            file.write("\n")
        os.replace(temp_path, self.path)

    def all_devices(self) -> list[DeviceRecord]:
        return sorted(
            self.devices.values(),
            key=lambda record: (
                record.friendly_name.lower(),
                record.android_serial.lower(),
                record.endpoint.lower(),
            ),
        )

    def get(self, device_id: str) -> DeviceRecord | None:
        return self.devices.get(device_id)

    def upsert_usb_device(
        self,
        serial: str,
        model: str = "",
        manufacturer: str = "",
        wifi_mac: str = "",
        seen_at: str | None = None,
    ) -> tuple[DeviceRecord, bool]:
        record = self.devices.get(serial)
        created = record is None

        if record is None:
            record = DeviceRecord(
                device_id=serial,
                android_serial=serial,
                friendly_name=self._default_name(serial, model, manufacturer),
            )
            self.devices[record.device_id] = record

        self._merge_metadata(record, model, manufacturer, wifi_mac)
        record.last_seen_usb = seen_at or utc_now_iso()
        return record, created

    def upsert_wireless_device(
        self,
        endpoint_serial: str,
        host: str,
        port: int,
        model: str = "",
        manufacturer: str = "",
        wifi_mac: str = "",
        seen_at: str | None = None,
    ) -> tuple[DeviceRecord, bool]:
        existing = self.find_by_endpoint(host, port)
        record = existing
        created = False

        if record is None:
            record = self.devices.get(endpoint_serial)

        if record is None:
            created = True
            record = DeviceRecord(
                device_id=endpoint_serial,
                android_serial=endpoint_serial,
                friendly_name=self._default_name(endpoint_serial, model, manufacturer),
                wireless_host=host,
                wireless_port=port,
            )
            self.devices[record.device_id] = record

        record.wireless_host = host
        record.wireless_port = port
        self._merge_metadata(record, model, manufacturer, wifi_mac)
        record.last_seen_wireless = seen_at or utc_now_iso()
        return record, created

    def set_wireless_endpoint(self, device_id: str, host: str, port: int) -> DeviceRecord:
        target = self.devices[device_id]
        existing = self.find_by_endpoint(host, port)

        if existing and existing.device_id != target.device_id:
            self._merge_records(target, existing)
            del self.devices[existing.device_id]

        target.wireless_host = host
        target.wireless_port = port
        return target

    def rename(self, device_id: str, friendly_name: str) -> None:
        record = self.devices[device_id]
        record.friendly_name = friendly_name.strip() or record.friendly_name

    def delete(self, device_id: str) -> None:
        self.devices.pop(device_id, None)

    def acknowledge_router_reminder(self, device_id: str) -> None:
        record = self.devices.get(device_id)
        if record:
            record.router_reminder_acknowledged = True

    def find_by_endpoint(self, host: str, port: int) -> DeviceRecord | None:
        for record in self.devices.values():
            if record.wireless_host == host and int(record.wireless_port) == int(port):
                return record
        return None

    def _record_from_json(self, item: dict[str, Any]) -> DeviceRecord:
        return DeviceRecord(
            device_id=str(item.get("device_id", "")),
            friendly_name=str(item.get("friendly_name", "")),
            android_serial=str(item.get("android_serial", "")),
            model=str(item.get("model", "")),
            manufacturer=str(item.get("manufacturer", "")),
            wifi_mac=str(item.get("wifi_mac", "")),
            wireless_host=str(item.get("wireless_host", "")),
            wireless_port=int(item.get("wireless_port", DEFAULT_PORT) or DEFAULT_PORT),
            last_seen_usb=str(item.get("last_seen_usb", "")),
            last_seen_wireless=str(item.get("last_seen_wireless", "")),
            router_reminder_acknowledged=bool(
                item.get("router_reminder_acknowledged", False)
            ),
        )

    def _merge_metadata(
        self,
        record: DeviceRecord,
        model: str = "",
        manufacturer: str = "",
        wifi_mac: str = "",
    ) -> None:
        if model:
            record.model = model
        if manufacturer:
            record.manufacturer = manufacturer
        if wifi_mac:
            record.wifi_mac = wifi_mac
        if not record.friendly_name:
            record.friendly_name = self._default_name(
                record.android_serial, record.model, record.manufacturer
            )

    def _merge_records(self, target: DeviceRecord, source: DeviceRecord) -> None:
        if self._looks_default_name(target) and not self._looks_default_name(source):
            target.friendly_name = source.friendly_name
        if not target.model and source.model:
            target.model = source.model
        if not target.manufacturer and source.manufacturer:
            target.manufacturer = source.manufacturer
        if not target.wifi_mac and source.wifi_mac:
            target.wifi_mac = source.wifi_mac
        if not target.last_seen_usb and source.last_seen_usb:
            target.last_seen_usb = source.last_seen_usb
        if not target.last_seen_wireless and source.last_seen_wireless:
            target.last_seen_wireless = source.last_seen_wireless
        target.router_reminder_acknowledged = (
            target.router_reminder_acknowledged
            or source.router_reminder_acknowledged
        )

    def _looks_default_name(self, record: DeviceRecord) -> bool:
        return record.friendly_name in {
            record.android_serial,
            record.endpoint,
            self._default_name(record.android_serial, record.model, record.manufacturer),
        }

    def _default_name(self, serial: str, model: str = "", manufacturer: str = "") -> str:
        parts = [part for part in (manufacturer, model) if part]
        if parts:
            return " ".join(parts)
        return serial
