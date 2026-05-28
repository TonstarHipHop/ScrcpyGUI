# Scrcpy Device Manager

Small Windows-first Python GUI for managing a room full of Android devices with
USB ADB, wireless ADB, and scrcpy.

## Requirements

- Python 3.12+
- `adb.exe` available on PATH
- `scrcpy.exe` available on PATH for launch buttons

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

## Notes

- The app uses a lazy hybrid connection policy. It stores IP/port details and
  reconnects before launching scrcpy instead of forcing every phone to stay
  connected forever.
- `devices.json` is personal runtime data and should not be committed if this
  folder later becomes a Git repository.
