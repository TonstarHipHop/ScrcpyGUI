from __future__ import annotations

import queue
import subprocess
import traceback
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable

from adb_service import AdbDevice, AdbService, parse_endpoint
from device_registry import DEFAULT_PORT, DeviceRecord, DeviceRegistry, utc_now_iso
from quant_racer_proxy import (
    ProxyConfig,
    ProxyMapping,
    add_scrcpy_proxy_entry,
    is_local_port_listening,
    launch_chrome_with_proxy,
    load_quant_racer_proxy_config,
)
from remote_control import (
    RemoteControlInfo,
    RemoteControlServer,
)


APP_TITLE = "Scrcpy Device Manager"
REGISTRY_PATH = Path(__file__).with_name("devices.json")
REMOTE_CONTROL_HOST = "0.0.0.0"
REMOTE_CONTROL_PORT = 2451
REMOTE_CONTROL_PIN = "2451"
WIRELESS_IDLE_DISCONNECT_AFTER = timedelta(hours=1)
IDLE_CHECK_INTERVAL_MS = 5 * 60 * 1000
OK_MARK = "\u2705"
WARN_MARK = "\u26a0\ufe0f"
INFO_MARK = "\u26aa"


@dataclass
class DeviceStatus:
    usb_serial: str = ""
    usb_state: str = ""
    wireless_serial: str = ""
    wireless_state: str = ""


class ScrcpyGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x760")
        self.minsize(980, 620)

        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="scrcpy-gui")
        self.registry = DeviceRegistry(REGISTRY_PATH)
        self.adb = AdbService(log_callback=self.enqueue_log)
        self.app_root = Path(__file__).resolve().parent
        self.remote_info = RemoteControlInfo(
            host=REMOTE_CONTROL_HOST,
            port=REMOTE_CONTROL_PORT,
            pin=REMOTE_CONTROL_PIN,
        )
        self.remote_server = RemoteControlServer(
            info=self.remote_info,
            snapshot_callback=self.remote_usb_device_snapshot,
            airplane_callback=self.remote_set_airplane_mode,
            log_callback=self.enqueue_log,
        )

        self.status_by_id: dict[str, DeviceStatus] = {}
        self.scrcpy_processes_by_endpoint: dict[str, Any] = {}
        self.chrome_processes: list[Any] = []
        self.proxy_config: ProxyConfig = load_quant_racer_proxy_config(self.app_root)
        self.proxy_listener_status: dict[tuple[str, int], bool] = {}
        self.selected_device_id = ""
        self.adb_available = self.adb.adb_available()
        self.scrcpy_available = self.adb.scrcpy_available()
        self.reload_proxy_state()

        self._build_widgets()
        self._set_tool_status()
        self.start_remote_control_server()
        self._reload_table()
        self.after(100, self._drain_ui_queue)
        self.after(250, self.refresh_devices)
        self.after(IDLE_CHECK_INTERVAL_MS, self.disconnect_idle_wireless_devices)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if not self.scrcpy_available:
            messagebox.showwarning(
                "scrcpy not found",
                "scrcpy.exe was not found on PATH. The app can still manage ADB "
                "devices, but scrcpy launch buttons are disabled until scrcpy is "
                "available on PATH.",
            )

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        root.rowconfigure(2, weight=0)

        toolbar = ttk.Frame(root)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(5, weight=1)

        self.adb_status_var = tk.StringVar()
        self.scrcpy_status_var = tk.StringVar()
        self.remote_status_var = tk.StringVar(value="Remote: starting")
        self.busy_var = tk.StringVar(value="Ready")

        ttk.Label(toolbar, textvariable=self.adb_status_var).grid(row=0, column=0, padx=(0, 16))
        ttk.Label(toolbar, textvariable=self.scrcpy_status_var).grid(
            row=0, column=1, padx=(0, 16)
        )
        ttk.Label(toolbar, textvariable=self.remote_status_var).grid(
            row=0, column=2, padx=(0, 16)
        )
        ttk.Button(toolbar, text="Refresh", command=self.refresh_devices).grid(
            row=0, column=3, padx=(0, 16)
        )
        self.reconnect_all_button = ttk.Button(
            toolbar,
            text="Reconnect All Wireless",
            command=self.reconnect_all_wireless,
        )
        self.reconnect_all_button.grid(row=0, column=4, padx=(0, 16))
        ttk.Label(toolbar, textvariable=self.busy_var, anchor="e").grid(
            row=0, column=5, sticky="e"
        )

        main_pane = ttk.PanedWindow(root, orient=tk.HORIZONTAL)
        main_pane.grid(row=1, column=0, sticky="nsew")

        table_frame = ttk.Frame(main_pane)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        main_pane.add(table_frame, weight=5)

        columns = (
            "name",
            "manufacturer",
            "model",
            "wifi_mac",
            "serial",
            "usb",
            "wireless",
            "proxy",
            "endpoint",
            "last_seen",
        )
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        headings = {
            "name": "Name",
            "manufacturer": "Manufacturer",
            "model": "Model",
            "wifi_mac": "Wi-Fi MAC",
            "serial": "Android Serial",
            "usb": "USB",
            "wireless": "Wireless",
            "proxy": "Proxy",
            "endpoint": "Saved Endpoint",
            "last_seen": "Last Seen",
        }
        widths = {
            "name": 180,
            "manufacturer": 110,
            "model": 130,
            "wifi_mac": 135,
            "serial": 170,
            "usb": 90,
            "wireless": 95,
            "proxy": 145,
            "endpoint": 130,
            "last_seen": 150,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor=tk.W, stretch=True)

        y_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        x_scroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self.on_selection_changed)
        self.tree.bind("<Double-1>", lambda _event: self.rename_selected())

        actions = ttk.LabelFrame(main_pane, text="Selected Device", padding=10)
        actions.columnconfigure(0, weight=1)
        main_pane.add(actions, weight=1)

        self.selected_summary_var = tk.StringVar(value="No device selected")
        ttk.Label(actions, textvariable=self.selected_summary_var, wraplength=210).grid(
            row=0, column=0, sticky="ew", pady=(0, 10)
        )

        self.rename_button = ttk.Button(actions, text="Rename", command=self.rename_selected)
        self.prepare_button = ttk.Button(
            actions, text="Prepare Wireless", command=self.prepare_selected_wireless
        )
        self.launch_usb_button = ttk.Button(
            actions, text="Launch USB", command=self.launch_selected_usb
        )
        self.launch_wireless_button = ttk.Button(
            actions, text="Launch Wireless", command=self.launch_selected_wireless
        )
        self.reconnect_button = ttk.Button(
            actions, text="Reconnect Wireless", command=self.reconnect_selected_wireless
        )
        self.add_proxy_button = ttk.Button(
            actions,
            text="Add Proxy To 3proxy Config",
            command=self.add_selected_proxy,
        )
        self.launch_proxy_chrome_button = ttk.Button(
            actions,
            text="Launch Chrome Via Proxy",
            command=self.launch_selected_proxy_chrome,
        )
        self.airplane_on_button = ttk.Button(
            actions,
            text="Airplane On",
            command=lambda: self.set_selected_airplane_mode(True),
        )
        self.airplane_off_button = ttk.Button(
            actions,
            text="Airplane Off",
            command=lambda: self.set_selected_airplane_mode(False),
        )
        self.delete_button = ttk.Button(actions, text="Delete", command=self.delete_selected)

        for index, button in enumerate(
            (
                self.rename_button,
                self.prepare_button,
                self.launch_usb_button,
                self.launch_wireless_button,
                self.reconnect_button,
                self.add_proxy_button,
                self.launch_proxy_chrome_button,
                self.airplane_on_button,
                self.airplane_off_button,
                self.delete_button,
            ),
            start=1,
        ):
            button.grid(row=index, column=0, sticky="ew", pady=3)

        log_frame = ttk.LabelFrame(root, text="Command Log", padding=8)
        log_frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=11, wrap=tk.WORD, state=tk.DISABLED)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")
        ttk.Button(log_frame, text="Clear Log", command=self.clear_log).grid(
            row=1, column=0, sticky="e", pady=(6, 0)
        )

        self.update_action_states()

    def _set_tool_status(self) -> None:
        self.adb_available = self.adb.adb_available()
        self.scrcpy_available = self.adb.scrcpy_available()
        self.adb_status_var.set(
            f"ADB: {'ready' if self.adb_available else 'missing'} ({self.adb.adb_path})"
        )
        self.scrcpy_status_var.set(
            "scrcpy: "
            + ("ready" if self.scrcpy_available else "missing from PATH")
        )

    def start_remote_control_server(self) -> None:
        try:
            self.remote_server.start()
        except OSError as exc:
            self.remote_status_var.set(f"Remote: failed :{self.remote_info.port}")
            self.enqueue_log(f"Remote control failed to start: {exc}")
            return

        self.remote_status_var.set(f"Remote: :{self.remote_info.port}")
        self.enqueue_log(
            "Remote control listening on "
            f"0.0.0.0:{self.remote_info.port}. Local URL: {self.remote_info.local_url}"
        )
        self.enqueue_log(
            "From your iPhone on Tailscale, use: "
            + self.remote_info.tailscale_url_hint
        )

    def refresh_devices(self) -> None:
        if not self.adb_available:
            self.enqueue_log("ADB is not available; refresh skipped.")
            return

        self.run_task(
            "Refreshing devices",
            self.adb.list_devices,
            on_success=self._handle_refresh_success,
        )

    def _handle_refresh_success(self, devices: list[AdbDevice]) -> None:
        statuses: dict[str, DeviceStatus] = {}
        new_records: list[DeviceRecord] = []
        seen_at = utc_now_iso()
        registry_changed = False

        for device in devices:
            if device.is_wireless:
                endpoint = parse_endpoint(device.serial)
                if not endpoint:
                    continue
                host, port = endpoint
                existing = self.registry.find_by_endpoint(host, port)
                if device.state != "device" and existing is None:
                    self.enqueue_log(
                        f"Ignoring unsaved wireless endpoint {device.serial} "
                        f"with state '{device.state}'."
                    )
                    continue

                if device.state == "device":
                    record, created = self.registry.upsert_wireless_device(
                        endpoint_serial=device.serial,
                        host=host,
                        port=port,
                        model=device.model,
                        manufacturer=device.manufacturer,
                        wifi_mac=device.wifi_mac,
                        seen_at=seen_at,
                    )
                else:
                    record = existing
                    created = False

                if record is None:
                    continue
                if device.state == "device" and not record.last_wireless_use:
                    record.last_wireless_use = seen_at
                    registry_changed = True
                status = statuses.setdefault(record.device_id, DeviceStatus())
                status.wireless_serial = device.serial
                status.wireless_state = device.state
            else:
                record, created = self.registry.upsert_usb_device(
                    serial=device.serial,
                    model=device.model,
                    manufacturer=device.manufacturer,
                    wifi_mac=device.wifi_mac,
                    seen_at=seen_at,
                )
                status = statuses.setdefault(record.device_id, DeviceStatus())
                status.usb_serial = device.serial
                status.usb_state = device.state

            if created:
                new_records.append(record)
                registry_changed = True

        self.status_by_id = statuses
        if registry_changed or devices:
            self.registry.save()
        self.reload_proxy_state()
        self._reload_table()
        self._show_router_reminders(new_records)
        self.enqueue_log(f"Refresh complete: {len(devices)} ADB device(s) seen.")

    def _reload_table(self) -> None:
        selected = self.selected_device_id
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        for record in self.registry.all_devices():
            status = self.status_by_id.get(record.device_id, DeviceStatus())
            proxy_mapping = self.proxy_mapping_for_record(record, status)
            self.tree.insert(
                "",
                tk.END,
                iid=record.device_id,
                values=(
                    record.friendly_name,
                    record.manufacturer,
                    record.model,
                    record.wifi_mac,
                    record.android_serial,
                    self._usb_label(status),
                    self._wireless_label(record, status),
                    self._proxy_label(proxy_mapping),
                    record.endpoint,
                    record.last_seen,
                ),
            )

        if selected and selected in self.tree.get_children():
            self.tree.selection_set(selected)
        self.update_action_states()

    def _usb_label(self, status: DeviceStatus) -> str:
        if not status.usb_serial:
            return f"{WARN_MARK} Offline"
        return (
            f"{OK_MARK} Ready"
            if status.usb_state == "device"
            else f"{WARN_MARK} {status.usb_state}"
        )

    def _wireless_label(self, record: DeviceRecord, status: DeviceStatus) -> str:
        if status.wireless_serial:
            return (
                f"{OK_MARK} Ready"
                if status.wireless_state == "device"
                else f"{WARN_MARK} {status.wireless_state}"
            )
        if record.endpoint:
            return f"{WARN_MARK} Saved"
        return f"{INFO_MARK} None"

    def reload_proxy_state(self, log_errors: bool = False) -> None:
        self.proxy_config = load_quant_racer_proxy_config(self.app_root)
        self.proxy_listener_status = {}
        for mapping in self.proxy_config.mappings:
            key = (mapping.listen_host, mapping.listen_port)
            if key not in self.proxy_listener_status:
                self.proxy_listener_status[key] = is_local_port_listening(
                    mapping.listen_host,
                    mapping.listen_port,
                )

        if log_errors and self.proxy_config.error:
            self.enqueue_log(f"Proxy config warning: {self.proxy_config.error}")

    def proxy_mapping_for_record(
        self,
        record: DeviceRecord,
        status: DeviceStatus | None = None,
    ) -> ProxyMapping | None:
        serial = ""
        if status and status.usb_serial:
            serial = status.usb_serial
        if not serial:
            serial = record.android_serial
        if not serial:
            return None
        return self.proxy_config.by_serial.get(serial)

    def selected_proxy_mapping(self) -> ProxyMapping | None:
        record = self.selected_record()
        if not record:
            return None
        return self.proxy_mapping_for_record(record, self.selected_status())

    def _proxy_label(self, mapping: ProxyMapping | None) -> str:
        if self.proxy_config.error:
            return f"{WARN_MARK} No cfg"
        if mapping is None:
            return f"{INFO_MARK} None"

        is_listening = self.proxy_listener_status.get(
            (mapping.listen_host, mapping.listen_port),
            False,
        )
        state = OK_MARK if is_listening else WARN_MARK
        suffix = "" if is_listening else " reload"
        return f"{state} {mapping.source_label} :{mapping.listen_port}{suffix}"

    def on_selection_changed(self, _event: tk.Event[Any] | None = None) -> None:
        selection = self.tree.selection()
        self.selected_device_id = selection[0] if selection else ""
        self.update_action_states()

    def update_action_states(self) -> None:
        record = self.selected_record()
        status = self.selected_status()
        has_selection = record is not None
        has_ready_usb = bool(status and status.usb_serial and status.usb_state == "device")
        has_endpoint = bool(record and record.endpoint)
        proxy_mapping = self.selected_proxy_mapping() if record else None
        can_add_proxy = (
            has_ready_usb
            and proxy_mapping is None
            and self.proxy_config.exists
            and not self.proxy_config.error
        )
        can_launch_proxy = has_ready_usb and proxy_mapping is not None

        self.rename_button.configure(state=tk.NORMAL if has_selection else tk.DISABLED)
        self.delete_button.configure(state=tk.NORMAL if has_selection else tk.DISABLED)
        self.prepare_button.configure(
            state=tk.NORMAL if has_ready_usb and self.adb_available else tk.DISABLED
        )
        self.launch_usb_button.configure(
            state=tk.NORMAL
            if has_ready_usb and self.scrcpy_available
            else tk.DISABLED
        )
        self.launch_wireless_button.configure(
            state=tk.NORMAL
            if has_endpoint and self.adb_available and self.scrcpy_available
            else tk.DISABLED
        )
        self.reconnect_button.configure(
            state=tk.NORMAL if has_endpoint and self.adb_available else tk.DISABLED
        )
        self.add_proxy_button.configure(
            state=tk.NORMAL if can_add_proxy and self.adb_available else tk.DISABLED
        )
        self.launch_proxy_chrome_button.configure(
            state=tk.NORMAL if can_launch_proxy else tk.DISABLED
        )
        self.airplane_on_button.configure(
            state=tk.NORMAL if has_ready_usb and self.adb_available else tk.DISABLED
        )
        self.airplane_off_button.configure(
            state=tk.NORMAL if has_ready_usb and self.adb_available else tk.DISABLED
        )
        self.reconnect_all_button.configure(
            state=tk.NORMAL
            if self.adb_available and any(record.endpoint for record in self.registry.all_devices())
            else tk.DISABLED
        )

        if record:
            self.selected_summary_var.set(
                f"{record.friendly_name}\nUSB: {self._usb_label(status)}\n"
                f"Wireless: {self._wireless_label(record, status)}\n"
                f"Proxy: {self._proxy_label(proxy_mapping)}\n"
                f"Wi-Fi MAC: {record.wifi_mac or 'Unknown'}"
            )
        else:
            self.selected_summary_var.set("No device selected")

    def selected_record(self) -> DeviceRecord | None:
        if not self.selected_device_id:
            return None
        return self.registry.get(self.selected_device_id)

    def selected_status(self) -> DeviceStatus:
        if not self.selected_device_id:
            return DeviceStatus()
        return self.status_by_id.get(self.selected_device_id, DeviceStatus())

    def rename_selected(self) -> None:
        record = self.selected_record()
        if not record:
            return

        new_name = simpledialog.askstring(
            "Rename device",
            "Friendly name:",
            initialvalue=record.friendly_name,
            parent=self,
        )
        if new_name is None:
            return

        self.registry.rename(record.device_id, new_name)
        self.registry.save()
        self._reload_table()

    def prepare_selected_wireless(self) -> None:
        record = self.selected_record()
        status = self.selected_status()
        if not record or not status.usb_serial:
            return

        device_id = record.device_id
        usb_serial = status.usb_serial

        def task() -> str:
            return self.adb.prepare_wireless(usb_serial, DEFAULT_PORT)

        def success(host: str) -> None:
            updated = self.registry.set_wireless_endpoint(device_id, host, DEFAULT_PORT)
            self.registry.mark_wireless_used(updated.device_id)
            self.registry.save()
            self.enqueue_log(
                f"Wireless prepared for {updated.friendly_name}: {host}:{DEFAULT_PORT}"
            )
            self._show_router_reminders([updated])
            self.refresh_devices()

        self.run_task(
            f"Preparing wireless for {record.friendly_name}",
            task,
            on_success=success,
        )

    def reconnect_selected_wireless(self) -> None:
        record = self.selected_record()
        if not record or not record.endpoint:
            return

        device_id = record.device_id
        host = record.wireless_host
        port = record.wireless_port

        def task() -> bool:
            self.adb.ensure_connected(host, port)
            return True

        def success(_ready: bool) -> None:
            self.registry.mark_wireless_used(device_id)
            self.registry.save()
            self.enqueue_log(f"Wireless ready: {host}:{port}")
            self.refresh_devices()

        self.run_task(
            f"Reconnecting {record.friendly_name}",
            task,
            on_success=success,
        )

    def launch_selected_usb(self) -> None:
        record = self.selected_record()
        status = self.selected_status()
        if not record or not status.usb_serial:
            return

        serial = status.usb_serial
        self.run_task(
            f"Launching USB scrcpy for {record.friendly_name}",
            lambda: self.adb.launch_scrcpy(serial),
            on_success=lambda process: self.enqueue_log(
                f"USB scrcpy started for {serial} (pid {process.pid})"
            ),
        )

    def launch_selected_wireless(self) -> None:
        record = self.selected_record()
        if not record or not record.endpoint:
            return

        device_id = record.device_id
        host = record.wireless_host
        port = record.wireless_port
        endpoint = record.endpoint

        def task() -> Any:
            self.adb.ensure_connected(host, port)
            return self.adb.launch_scrcpy(endpoint)

        def success(process: Any) -> None:
            self.scrcpy_processes_by_endpoint[endpoint] = process
            self.registry.mark_wireless_used(device_id)
            self.registry.save()
            self.enqueue_log(f"Wireless scrcpy started for {endpoint} (pid {process.pid})")
            self.refresh_devices()

        self.run_task(
            f"Launching wireless scrcpy for {record.friendly_name}",
            task,
            on_success=success,
        )

    def add_selected_proxy(self) -> None:
        record = self.selected_record()
        status = self.selected_status()
        if not record or not status.usb_serial or status.usb_state != "device":
            return

        serial = status.usb_serial
        model = record.model

        def task() -> ProxyMapping:
            return add_scrcpy_proxy_entry(self.app_root, self.adb, serial, model)

        def success(mapping: ProxyMapping) -> None:
            self.reload_proxy_state(log_errors=True)
            self.enqueue_log(
                "Proxy config entry ready for "
                f"{record.friendly_name}: socks5://127.0.0.1:{mapping.listen_port}"
            )
            self._reload_table()
            messagebox.showinfo(
                "Proxy entry added",
                "The proxy entry is now in Quant-Racer's 3proxy.cfg.\n\n"
                "If 3proxy is already running, restart or reload it before using "
                "this new port.",
                parent=self,
            )

        self.run_task(
            f"Adding proxy config entry for {record.friendly_name}",
            task,
            on_success=success,
        )

    def launch_selected_proxy_chrome(self) -> None:
        record = self.selected_record()
        mapping = self.selected_proxy_mapping()
        if not record or not mapping:
            return

        if not is_local_port_listening(mapping.listen_host, mapping.listen_port):
            self.reload_proxy_state(log_errors=True)
            self._reload_table()
            messagebox.showwarning(
                "Proxy is not listening",
                f"Nothing is listening on 127.0.0.1:{mapping.listen_port} yet.\n\n"
                "If this entry was just appended, restart or reload Quant-Racer's "
                "3proxy instance before launching Chrome through it.",
                parent=self,
            )
            return

        def task() -> Any:
            launch = launch_chrome_with_proxy(mapping)
            self.enqueue_log("$ " + subprocess.list2cmdline(list(launch.command)))
            return launch

        def success(launch: Any) -> None:
            self.chrome_processes.append(launch.process)
            self.enqueue_log(
                "Chrome started through "
                f"{mapping.proxy_url} for {record.friendly_name} "
                f"(pid {launch.process.pid}, temp profile {launch.profile_dir})"
            )

        self.run_task(
            f"Launching Chrome via proxy for {record.friendly_name}",
            task,
            on_success=success,
        )

    def set_selected_airplane_mode(self, enabled: bool) -> None:
        record = self.selected_record()
        status = self.selected_status()
        if not record or not status.usb_serial or status.usb_state != "device":
            return

        serial = status.usb_serial
        action = "on" if enabled else "off"

        def task() -> bool:
            self.adb.set_airplane_mode(serial, enabled)
            return True

        def success(_ok: bool) -> None:
            self.enqueue_log(
                f"Airplane mode turned {action} for {record.friendly_name}."
            )
            self.refresh_devices()

        self.run_task(
            f"Turning airplane mode {action} for {record.friendly_name}",
            task,
            on_success=success,
        )

    def remote_usb_device_snapshot(self) -> list[dict[str, str]]:
        if not self.adb_available:
            return []

        try:
            adb_devices = self.adb.list_devices()
        except Exception as exc:
            self.enqueue_log(f"Remote device snapshot failed: {exc}")
            return self.current_usb_device_snapshot()

        snapshot: list[dict[str, str]] = []
        for device in adb_devices:
            if device.is_wireless or device.state != "device":
                continue
            record = self.registry.get(device.serial)
            snapshot.append(
                {
                    "device_id": device.serial,
                    "friendly_name": (
                        record.friendly_name
                        if record
                        else " ".join(
                            part for part in (device.manufacturer, device.model) if part
                        )
                        or device.serial
                    ),
                    "serial": device.serial,
                    "model": record.model if record and record.model else device.model,
                    "manufacturer": (
                        record.manufacturer
                        if record and record.manufacturer
                        else device.manufacturer
                    ),
                    "wifi_mac": record.wifi_mac if record and record.wifi_mac else device.wifi_mac,
                }
            )
        return snapshot

    def current_usb_device_snapshot(self) -> list[dict[str, str]]:
        snapshot: list[dict[str, str]] = []
        for record in self.registry.all_devices():
            status = self.status_by_id.get(record.device_id, DeviceStatus())
            if not status.usb_serial or status.usb_state != "device":
                continue
            snapshot.append(
                {
                    "device_id": status.usb_serial,
                    "friendly_name": record.friendly_name,
                    "serial": status.usb_serial,
                    "model": record.model,
                    "manufacturer": record.manufacturer,
                    "wifi_mac": record.wifi_mac,
                }
            )
        return snapshot

    def remote_set_airplane_mode(self, device_id: str, enabled: bool) -> str:
        if parse_endpoint(device_id):
            raise RuntimeError("Remote airplane mode is only available for USB devices.")

        record = self.registry.get(device_id)
        status = self.status_by_id.get(device_id, DeviceStatus())
        serial = status.usb_serial if status.usb_serial else device_id
        name = record.friendly_name if record else serial

        state = self.adb.get_state(serial)
        if not state.ok or state.stdout.strip() != "device":
            raise RuntimeError(f"{name} is not ready over USB ADB.")

        action = "on" if enabled else "off"
        self.enqueue_log(f"Remote request: turning airplane mode {action} for {name}.")
        self.adb.set_airplane_mode(serial, enabled)
        self.ui_queue.put(("call", self.refresh_devices))
        return f"Airplane mode turned {action} for {name}."

    def reconnect_all_wireless(self) -> None:
        records = [record for record in self.registry.all_devices() if record.endpoint]
        if not records:
            self.enqueue_log("No saved wireless endpoints to reconnect.")
            return

        def task() -> list[tuple[str, str, bool, str]]:
            results: list[tuple[str, str, bool, str]] = []
            for record in records:
                try:
                    self.adb.ensure_connected(record.wireless_host, record.wireless_port)
                except Exception as exc:
                    results.append((record.device_id, record.endpoint, False, str(exc)))
                else:
                    results.append((record.device_id, record.endpoint, True, "ready"))
            return results

        def success(results: list[tuple[str, str, bool, str]]) -> None:
            ready_count = 0
            for device_id, endpoint, ok, message in results:
                record = self.registry.get(device_id)
                name = record.friendly_name if record else endpoint
                if ok:
                    ready_count += 1
                    self.registry.mark_wireless_used(device_id)
                    self.enqueue_log(f"Reconnect all: {name} ready at {endpoint}")
                else:
                    self.enqueue_log(f"Reconnect all: {name} failed at {endpoint}: {message}")

            self.registry.save()
            self.enqueue_log(
                f"Reconnect all complete: {ready_count}/{len(results)} endpoint(s) ready."
            )
            self.refresh_devices()

        self.run_task(
            f"Reconnecting {len(records)} wireless endpoint(s)",
            task,
            on_success=success,
        )

    def disconnect_idle_wireless_devices(self) -> None:
        self.prune_scrcpy_processes()
        idle_records = [
            record
            for record in self.registry.all_devices()
            if record.endpoint
            and self.status_by_id.get(record.device_id, DeviceStatus()).wireless_state
            == "device"
            and self.is_wireless_idle(record)
            and record.endpoint not in self.scrcpy_processes_by_endpoint
        ]

        if idle_records and self.adb_available:
            def task() -> list[str]:
                disconnected: list[str] = []
                for record in idle_records:
                    result = self.adb.disconnect(record.endpoint)
                    if result.ok:
                        disconnected.append(record.endpoint)
                return disconnected

            def success(disconnected: list[str]) -> None:
                if disconnected:
                    self.enqueue_log(
                        "Idle disconnect complete: " + ", ".join(disconnected)
                    )
                    self.refresh_devices()

            self.run_task(
                f"Disconnecting {len(idle_records)} idle wireless endpoint(s)",
                task,
                on_success=success,
            )

        self.after(IDLE_CHECK_INTERVAL_MS, self.disconnect_idle_wireless_devices)

    def prune_scrcpy_processes(self) -> None:
        finished = [
            endpoint
            for endpoint, process in self.scrcpy_processes_by_endpoint.items()
            if process.poll() is not None
        ]
        for endpoint in finished:
            del self.scrcpy_processes_by_endpoint[endpoint]
        self.chrome_processes = [
            process for process in self.chrome_processes if process.poll() is None
        ]

    def is_wireless_idle(self, record: DeviceRecord) -> bool:
        last_use = parse_utc_iso(record.last_wireless_use)
        if last_use is None:
            return False
        return datetime.utcnow() - last_use >= WIRELESS_IDLE_DISCONNECT_AFTER

    def delete_selected(self) -> None:
        record = self.selected_record()
        if not record:
            return

        if not messagebox.askyesno(
            "Delete device",
            f"Delete {record.friendly_name} from the saved registry?",
            parent=self,
        ):
            return

        endpoint = record.endpoint
        friendly_name = record.friendly_name
        self.registry.delete(record.device_id)
        self.registry.save()
        self.selected_device_id = ""
        self._reload_table()

        if endpoint and self.adb_available:
            self.run_task(
                f"Disconnecting {friendly_name} wireless endpoint",
                lambda: self.adb.disconnect(endpoint),
                on_success=lambda _result: self.refresh_devices(),
            )

    def _show_router_reminders(self, records: list[DeviceRecord]) -> None:
        pending = [record for record in records if not record.router_reminder_acknowledged]
        if not pending:
            return

        names = "\n".join(
            f"- {record.friendly_name}"
            + (f" ({record.wifi_mac})" if record.wifi_mac else "")
            for record in pending
        )
        messagebox.showinfo(
            "New device registered",
            "For stable wireless ADB, reserve each new phone's IP address in your "
            "router and disable randomized MAC address for your home Wi-Fi network.\n\n"
            f"New device(s):\n{names}",
            parent=self,
        )
        for record in pending:
            self.registry.acknowledge_router_reminder(record.device_id)
        self.registry.save()
        self._reload_table()

    def run_task(
        self,
        label: str,
        func: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
    ) -> None:
        self.busy_var.set(label + "...")
        self.enqueue_log(label + "...")

        def runner() -> None:
            try:
                result = func()
            except Exception as exc:
                self.ui_queue.put(("error", (label, exc, traceback.format_exc())))
            else:
                self.ui_queue.put(("success", (label, result, on_success)))

        self.executor.submit(runner)

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self.append_log(str(payload))
                elif kind == "call":
                    payload()
                elif kind == "success":
                    label, result, on_success = payload
                    self.busy_var.set("Ready")
                    if on_success:
                        on_success(result)
                    self.update_action_states()
                elif kind == "error":
                    label, exc, trace = payload
                    self.busy_var.set("Ready")
                    self.append_log(trace)
                    messagebox.showerror(label, str(exc), parent=self)
                    self.update_action_states()
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    def enqueue_log(self, message: str) -> None:
        self.ui_queue.put(("log", message))

    def append_log(self, message: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, message.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def clear_log(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def on_close(self) -> None:
        self.remote_server.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()


def parse_utc_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z"))
    except ValueError:
        return None


def main() -> None:
    app = ScrcpyGui()
    app.mainloop()


if __name__ == "__main__":
    main()
