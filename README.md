# Scrcpy Device Manager

Small Windows-first Python GUI for managing a room full of Android devices with
USB ADB, wireless ADB, and scrcpy.

## Requirements

- Python 3.12+
- `adb.exe` available on PATH
- `scrcpy.exe` available on PATH for launch buttons
- Google Chrome for `Launch Chrome Via Proxy`

This app uses only Python's standard library.

## Run

```powershell
python main.py
```

Or double-click `launch_scrcpy_gui.bat`. The launcher checks for `adb` and
`scrcpy` on PATH and uses `winget` to install missing tools when possible.

The app stores your device registry in `devices.json` next to `main.py`.

## Workflow

1. Plug a phone into the PC with USB debugging enabled.
2. Press `Refresh`.
3. Select the device and rename it if useful.
4. Check the `Wi-Fi MAC` column and use that value when creating a router
   DHCP reservation.
5. Press `Prepare Wireless`.
6. After the app stores the wireless endpoint, reserve that IP in your router and
   disable randomized MAC address for your home Wi-Fi network on the phone.
7. Move the phone back to the charging station.
8. Use `Launch Wireless` when you want to open scrcpy.

Wireless ADB usually survives Wi-Fi drops, but it does not survive a phone
reboot. If a phone reboots, plug it in over USB and run `Prepare Wireless` again.

scrcpy launches use `--no-audio` by default, which is friendlier for headless
PCs or remote desktop sessions with no available audio output device. The
scrcpy window title is set to the device's friendly name from the app.

## Notes

- The app uses a lazy hybrid connection policy. It stores IP/port details and
  reconnects before launching scrcpy instead of forcing every phone to stay
  connected forever.
- Use `Reconnect All Wireless` when you want to warm up every saved wireless
  ADB endpoint at once. The app disconnects wireless endpoints after about an
  hour of app inactivity, but it leaves endpoints alone while a scrcpy process
  launched by the app is still running.
- `Airplane On` and `Airplane Off` are enabled only for phones currently ready
  over USB ADB.
- A small PIN-protected remote page starts with the GUI on port `2451`. The
  app logs the local URL and a Tailscale URL template on startup. From your
  iPhone, open:

  ```text
  http://<this-pc-tailscale-ip>:2451/?pin=2451
  ```

  The page does a fresh ADB scan, lists USB-ready phones, and exposes
  `Airplane On` / `Airplane Off` buttons for each one. Keep the desktop app
  open for this to work. If Windows Firewall prompts, allow private/Tailscale
  network access.
- If this folder sits next to `Quant-Racer`, the app reads
  `..\Quant-Racer\3proxy-0.9.5-x64\bin64\3proxy.cfg`. Existing Quant-Racer
  proxy entries are reused. For a plugged-in USB phone missing from that config,
  `Add Proxy To 3proxy Config` appends a scrcpyGUI-managed block using ports
  `3080-4079`.
- Scrcpy GUI does not start, stop, or reload 3proxy. If a proxy entry is newly
  appended, restart or reload Quant-Racer's 3proxy before launching Chrome
  through that port.
- `Launch Chrome Via Proxy` starts Chrome with a fresh temporary user-data
  directory each time and `--proxy-server=socks5://127.0.0.1:<port>`.
- `devices.json` is personal runtime data and should not be committed if this
  folder later becomes a Git repository.
