from __future__ import annotations

import queue
import traceback
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable

from adb_service import AdbDevice, AdbService, connection_result_success, parse_endpoint
from device_registry import DEFAULT_PORT, DeviceRecord, DeviceRegistry, utc_now_iso


APP_TITLE = "Scrcpy Device Manager"
REGISTRY_PATH = Path(__file__).with_name("devices.json")


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

        self.status_by_id: dict[str, DeviceStatus] = {}
        self.selected_device_id = ""
        self.adb_available = self.adb.adb_available()
        self.scrcpy_available = self.adb.scrcpy_available()

        self._build_widgets()
        self._set_tool_status()
        self._reload_table()
        self.after(100, self._drain_ui_queue)
        self.after(250, self.refresh_devices)
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
        toolbar.columnconfigure(3, weight=1)

        self.adb_status_var = tk.StringVar()
        self.scrcpy_status_var = tk.StringVar()
        self.busy_var = tk.StringVar(value="Ready")

        ttk.Label(toolbar, textvariable=self.adb_status_var).grid(row=0, column=0, padx=(0, 16))
        ttk.Label(toolbar, textvariable=self.scrcpy_status_var).grid(
            row=0, column=1, padx=(0, 16)
        )
        ttk.Button(toolbar, text="Refresh", command=self.refresh_devices).grid(
            row=0, column=2, padx=(0, 16)
        )
        ttk.Label(toolbar, textvariable=self.busy_var, anchor="e").grid(
            row=0, column=3, sticky="e"
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
        self.delete_button = ttk.Button(actions, text="Delete", command=self.delete_selected)

        for index, button in enumerate(
            (
                self.rename_button,
                self.prepare_button,
                self.launch_usb_button,
                self.launch_wireless_button,
                self.reconnect_button,
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

        self.status_by_id = statuses
        self.registry.save()
        self._reload_table()
        self._show_router_reminders(new_records)
        self.enqueue_log(f"Refresh complete: {len(devices)} ADB device(s) seen.")

    def _reload_table(self) -> None:
        selected = self.selected_device_id
        for item_id in self.tree.get_children():
            self.tree.delete(item_id)

        for record in self.registry.all_devices():
            status = self.status_by_id.get(record.device_id, DeviceStatus())
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
                    record.endpoint,
                    record.last_seen,
                ),
            )

        if selected and selected in self.tree.get_children():
            self.tree.selection_set(selected)
        self.update_action_states()

    def _usb_label(self, status: DeviceStatus) -> str:
        if not status.usb_serial:
            return "Offline"
        return "Ready" if status.usb_state == "device" else status.usb_state

    def _wireless_label(self, record: DeviceRecord, status: DeviceStatus) -> str:
        if status.wireless_serial:
            return "Ready" if status.wireless_state == "device" else status.wireless_state
        if record.endpoint:
            return "Saved"
        return "None"

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

        if record:
            self.selected_summary_var.set(
                f"{record.friendly_name}\nUSB: {self._usb_label(status)}\n"
                f"Wireless: {self._wireless_label(record, status)}\n"
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
            updated.last_seen_wireless = utc_now_iso()
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
            result = self.adb.connect(host, port)
            if not connection_result_success(result, f"{host}:{port}"):
                raise RuntimeError(result.stderr or result.stdout or "adb connect failed")
            state = self.adb.get_state(f"{host}:{port}")
            if not state.ok or state.stdout.strip() != "device":
                raise RuntimeError(state.stderr or state.stdout or "wireless device not ready")
            return True

        def success(_ready: bool) -> None:
            updated = self.registry.get(device_id)
            if updated:
                updated.last_seen_wireless = utc_now_iso()
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
            on_success=lambda _pid: self.enqueue_log(f"USB scrcpy started for {serial}"),
        )

    def launch_selected_wireless(self) -> None:
        record = self.selected_record()
        if not record or not record.endpoint:
            return

        device_id = record.device_id
        host = record.wireless_host
        port = record.wireless_port
        endpoint = record.endpoint

        def task() -> int:
            result = self.adb.connect(host, port)
            if not connection_result_success(result, endpoint):
                raise RuntimeError(result.stderr or result.stdout or "adb connect failed")
            state = self.adb.get_state(endpoint)
            if not state.ok or state.stdout.strip() != "device":
                raise RuntimeError(state.stderr or state.stdout or "wireless device not ready")
            return self.adb.launch_scrcpy(endpoint)

        def success(_pid: int) -> None:
            updated = self.registry.get(device_id)
            if updated:
                updated.last_seen_wireless = utc_now_iso()
                self.registry.save()
            self.enqueue_log(f"Wireless scrcpy started for {endpoint}")
            self.refresh_devices()

        self.run_task(
            f"Launching wireless scrcpy for {record.friendly_name}",
            task,
            on_success=success,
        )

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
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()


def main() -> None:
    app = ScrcpyGui()
    app.mainloop()


if __name__ == "__main__":
    main()
