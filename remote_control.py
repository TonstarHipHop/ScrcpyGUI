from __future__ import annotations

import hmac
import html
import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse


SnapshotCallback = Callable[[], list[dict[str, str]]]
AirplaneCallback = Callable[[str, bool], str]
LogCallback = Callable[[str], None]


@dataclass(frozen=True)
class RemoteControlInfo:
    host: str
    port: int
    pin: str

    @property
    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?{urlencode({'pin': self.pin})}"

    @property
    def tailscale_url_hint(self) -> str:
        return f"http://<this-pc-tailscale-ip>:{self.port}/?{urlencode({'pin': self.pin})}"


class RemoteControlServer:
    def __init__(
        self,
        *,
        info: RemoteControlInfo,
        snapshot_callback: SnapshotCallback,
        airplane_callback: AirplaneCallback,
        log_callback: LogCallback | None = None,
    ) -> None:
        self.info = info
        self.snapshot_callback = snapshot_callback
        self.airplane_callback = airplane_callback
        self.log_callback = log_callback
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return

        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.info.host, self.info.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="scrcpy-gui-remote",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if not self._pin_ok():
                    self._send_text("Forbidden", status=HTTPStatus.FORBIDDEN)
                    return

                path = urlparse(self.path).path
                if path in {"", "/"}:
                    self._send_html(owner._render_page())
                    return
                if path == "/api/devices":
                    self._send_json({"devices": owner.snapshot_callback()})
                    return
                self._send_text("Not found", status=HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path != "/airplane":
                    self._send_text("Not found", status=HTTPStatus.NOT_FOUND)
                    return

                fields = self._read_form()
                if not self._pin_ok(fields):
                    self._send_text("Forbidden", status=HTTPStatus.FORBIDDEN)
                    return

                device_id = (fields.get("device_id") or [""])[0]
                action = (fields.get("action") or [""])[0].lower()
                enabled = action == "on"
                if action not in {"on", "off"} or not device_id:
                    self._send_text("Bad request", status=HTTPStatus.BAD_REQUEST)
                    return

                try:
                    notice = owner.airplane_callback(device_id, enabled)
                except Exception as exc:
                    owner._log(f"Remote airplane toggle failed: {exc}")
                    self._send_html(owner._render_page(error=str(exc)))
                    return

                self._send_html(owner._render_page(notice=notice))

            def log_message(self, format: str, *args: object) -> None:
                owner._log("Remote HTTP: " + (format % args))

            def _pin_ok(self, fields: dict[str, list[str]] | None = None) -> bool:
                parsed = urlparse(self.path)
                values = parse_qs(parsed.query)
                if fields:
                    values.update(fields)
                provided = (
                    self.headers.get("X-ScrcpyGUI-Token")
                    or self.headers.get("X-ScrcpyGUI-Pin")
                    or (values.get("pin") or [""])[0]
                    or (values.get("token") or [""])[0]
                )
                return hmac.compare_digest(str(provided), owner.info.pin)

            def _read_form(self) -> dict[str, list[str]]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                return parse_qs(body)

            def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(payload, indent=2).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
                body = text.encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
                body = text.encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _render_page(self, notice: str = "", error: str = "") -> str:
        devices = self.snapshot_callback()
        rows = "\n".join(render_device_row(device, self.info.pin) for device in devices)
        if not rows:
            rows = "<p class='empty'>No USB-ready phones are currently available.</p>"

        notice_html = (
            f"<div class='notice'>{html.escape(notice)}</div>"
            if notice
            else ""
        )
        error_html = (
            f"<div class='error'>{html.escape(error)}</div>"
            if error
            else ""
        )
        refresh_query = html.escape(urlencode({"pin": self.info.pin}))
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Scrcpy GUI Remote</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f7f7f5; color: #1e2328; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 18px; }}
    h1 {{ font-size: 22px; margin: 8px 0 4px; }}
    .subtle {{ color: #5a646e; font-size: 14px; margin: 0 0 18px; }}
    .device {{ background: #fff; border: 1px solid #d8d8d2; border-radius: 8px; padding: 14px; margin: 12px 0; }}
    .name {{ font-weight: 700; font-size: 17px; }}
    .meta {{ color: #5a646e; font-size: 13px; margin-top: 4px; overflow-wrap: anywhere; }}
    .buttons {{ display: flex; gap: 10px; margin-top: 12px; }}
    button, .refresh {{ border: 1px solid #b8b8b0; border-radius: 7px; background: #fff; color: #1e2328; font-size: 16px; padding: 10px 12px; text-decoration: none; }}
    button.primary {{ background: #1f6f4a; border-color: #1f6f4a; color: white; }}
    button.warn {{ background: #a14027; border-color: #a14027; color: white; }}
    .notice {{ background: #e8f5ed; border: 1px solid #97c9aa; border-radius: 8px; padding: 10px; margin: 12px 0; }}
    .error {{ background: #fff0ed; border: 1px solid #d5998b; border-radius: 8px; padding: 10px; margin: 12px 0; }}
    .empty {{ background: #fff; border: 1px solid #d8d8d2; border-radius: 8px; padding: 14px; }}
  </style>
</head>
<body>
  <main>
    <h1>Scrcpy GUI Remote</h1>
    <p class="subtle">USB-ready Android phones only. Keep the desktop app open.</p>
    {notice_html}
    {error_html}
    <a class="refresh" href="/?{refresh_query}">Refresh</a>
    {rows}
  </main>
</body>
</html>"""

    def _log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(message)


def render_device_row(device: dict[str, str], pin: str) -> str:
    name = html.escape(device.get("friendly_name") or device.get("serial") or "Unknown")
    serial = html.escape(device.get("serial") or "")
    model = html.escape(device.get("model") or "Unknown model")
    manufacturer = html.escape(device.get("manufacturer") or "")
    mac = html.escape(device.get("wifi_mac") or "Unknown MAC")
    device_id = html.escape(device.get("device_id") or "")
    pin_value = html.escape(pin)
    return f"""<section class="device">
  <div class="name">{name}</div>
  <div class="meta">{manufacturer} {model}<br>Serial: {serial}<br>Wi-Fi MAC: {mac}</div>
  <div class="buttons">
    <form method="post" action="/airplane">
      <input type="hidden" name="pin" value="{pin_value}">
      <input type="hidden" name="device_id" value="{device_id}">
      <input type="hidden" name="action" value="on">
      <button class="warn" type="submit">Airplane On</button>
    </form>
    <form method="post" action="/airplane">
      <input type="hidden" name="pin" value="{pin_value}">
      <input type="hidden" name="device_id" value="{device_id}">
      <input type="hidden" name="action" value="off">
      <button class="primary" type="submit">Airplane Off</button>
    </form>
  </div>
</section>"""
