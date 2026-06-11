# scyrox

Native, **offline** configuration for Scyrox mice on Linux and macOS — no website,
no WebHID. The official driver lives only at scyrox.net; this project reimplements
its HID protocol so you keep full control of the mouse if that site ever disappears.

Two independent pieces share one reverse-engineered protocol (`PROTOCOL.md`):

1. **`scyroxd`** — the original Linux battery daemon. Polls battery over the dongle
   or USB cable and republishes it through a virtual `uhid` device so UPower/GNOME
   show it natively. Installed via `install.sh` + systemd. *Unchanged by this work.*
2. **`scyrox` / `scyrox-tui`** — the new cross-platform configurator (this is the
   part that grew the project beyond "battery").

## Status

The protocol was decoded **statically** from the official web bundle and verified
subsystem-by-subsystem (see `PROTOCOL.md`). The library, CLI, and TUI are built and
unit-tested offline; the read path's framing is byte-identical to the daemon's
shipped, hardware-proven implementation. Live-hardware validation of a few unit
fields (marked VERIFY in `PROTOCOL.md`) is the remaining step.

## Install

Linux runs with **zero dependencies** (uses `/dev/hidraw`). For macOS — or a nicer
Linux backend — install the cross-platform HID extra:

```sh
pip install -e .            # library + `scyrox` CLI (Linux, stdlib hidraw)
pip install -e '.[hidapi]'  # + cross-platform HID (required on macOS)
pip install -e '.[tui]'     # + the Textual TUI
pip install -e '.[all]'     # everything
```

## Usage

```sh
scyrox list                 # detect attached Scyrox interfaces
scyrox battery              # battery level / charging / voltage
scyrox dump                 # annotated hex dump of the config flash (read-only)
scyrox backup my.bin        # save config flash to a file (read-only)
scyrox get                  # decoded settings (DPI, buttons, lighting, …)
scyrox set report_rate 8000 --yes   # write one setting (auto-backs-up first)
scyrox set motion_sync off --yes
scyrox restore my.bin --yes # write a full backup back to the device
scyrox-tui                  # interactive terminal UI
```

`list`/`battery`/`dump`/`backup`/`get` never write to the mouse. `restore` and the
TUI's Apply write flash and always back up first / require confirmation.

> On Linux, HID access needs permission on `/dev/hidrawN` (run with `sudo`, or add a
> udev rule). The `scyroxd` daemon holds the device, so stop it during writes:
> `sudo systemctl stop scyroxd` … `sudo systemctl start scyroxd`.

## Layout

```
scyrox/protocol.py   frame/CRC, command enum, hidapi + hidraw transports, ScyroxDevice
scyrox/flash.py      flash offset map, value codecs, FlashImage typed accessors
scyrox/device.py     discovery / open (hidapi-first, hidraw fallback)
scyrox/cli.py        the `scyrox` command
scyrox/tui.py        the Textual `scyrox-tui`
scyroxd.py           the Linux battery daemon (separate; install.sh + systemd)
tests/               offline protocol/codec tests
PROTOCOL.md          the reverse-engineered protocol spec
```
