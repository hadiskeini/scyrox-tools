"""scyrox command-line tool.

Read-only by default. `dump`, `backup`, `list`, `battery` never write to the mouse.
`set`/`restore` write flash and require --yes.
"""

from __future__ import annotations

import argparse
import sys

from .device import open_device, list_devices
from .flash import (
    FlashImage, OFFSETS, PRIMARY_REGION_LEN,
    MOUSE_BUTTON_MASK, KEY_FUNCTION_TYPES, LIGHT_MODE_CAPS,
)


def _hexdump(data: bytes, width: int = 16) -> str:
    lines = []
    names = {v: k for k, v in OFFSETS.items() if v < len(data)}
    for off in range(0, len(data), width):
        row = data[off:off + width]
        hexpart = " ".join(f"{b:02x}" for b in row)
        labels = [f"{names[o]}@{o}" for o in range(off, off + len(row))
                  if o in names]
        tag = ("  <- " + ", ".join(labels)) if labels else ""
        lines.append(f"{off:04x}  {hexpart:<{width*3}}{tag}")
    return "\n".join(lines)


def cmd_list(args):
    devs = list_devices()
    if not devs:
        print("no Scyrox device found")
        return 1
    for d in devs:
        print(f"{d['backend']:7} pid=0x{d['pid']:04x} "
              f"{d['product'] or ''} {d['path']}")
    return 0


def cmd_battery(args):
    with open_device(args.path) as dev:
        info = dev.battery()
    if not info:
        print("no response (mouse asleep or offline?)")
        return 1
    print(f"battery: {info['level']}%  "
          f"{'charging' if info['charging'] else 'discharging'}  "
          f"{info['voltage_mv']} mV")
    return 0


def cmd_dump(args):
    with open_device(args.path) as dev:
        data = dev.read_flash(0, args.length)
    print(_hexdump(data))
    return 0


def cmd_backup(args):
    with open_device(args.path) as dev:
        data = dev.read_flash(0, args.length)
    with open(args.file, "wb") as f:
        f.write(data)
    print(f"wrote {len(data)} bytes to {args.file}")
    return 0


def cmd_get(args):
    with open_device(args.path) as dev:
        data = dev.read_flash(0, PRIMARY_REGION_LEN)
    img = FlashImage(data)
    print(f"report_rate   : {img.report_rate_hz} Hz")
    print(f"motion_sync   : {img.motion_sync}")
    print(f"angle_snap    : {img.angle_snap}")
    print(f"ripple        : {img.ripple_control}")
    print(f"angle_tune    : {img.angle_tune_deg}°")
    print(f"lod (raw)     : {img.setting(OFFSETS['lod'])}")
    print(f"debounce (raw): {img.setting(OFFSETS['debounce_time'])}")
    print(f"sleep (raw)   : {img.setting(OFFSETS['sleep_time'])}")
    print(f"\nDPI: {img.max_dpi_stage} stage(s), active #{img.current_dpi_stage}")
    for i in range(min(img.max_dpi_stage, 8) or 1):
        x, y = img.dpi_xy(i)
        d = f"{x}" if x == y else f"{x}x{y}"
        print(f"  stage {i}: {d} dpi  color {img.dpi_color(i)}")
    caps = ", ".join(LIGHT_MODE_CAPS.get(img.light_mode, ())) or "none"
    print(f"\nLighting: {'on' if img.light_enabled else 'off'}, mode {img.light_mode} "
          f"(editable: {caps}), color {img.light_color}, "
          f"speed {img.light_speed}, brightness {img.light_brightness}")
    print("\nButtons:")
    for b in range(6):
        ftype, param = img.key_function(b)
        name = KEY_FUNCTION_TYPES.get(ftype, f"type{ftype}")
        if name == "mouse_key":
            name = f"mouse:{MOUSE_BUTTON_MASK.get(param, hex(param))}"
        print(f"  button {b}: {name}" + (f" (0x{param:04x})" if param else ""))
    return 0


# Settings that are safe to write headlessly: each is a single [value,comp] pair.
WRITABLE = {
    "report_rate", "motion_sync", "angle_snap", "ripple", "moving_off_light",
    "sensor_fps_20k", "light_enable", "angle_tune", "lod", "debounce_time",
    "sleep_time", "performance", "sensor_mode", "key_operation",
}


def _parse_value(name, raw):
    from .flash import report_rate_to_flash
    if name == "report_rate":
        return report_rate_to_flash(int(raw))
    if name == "angle_tune":
        v = int(raw)
        return v + 256 if v < 0 else v
    low = raw.lower()
    if low in ("on", "true", "yes"):
        return 1
    if low in ("off", "false", "no"):
        return 0
    return int(raw)


def cmd_set(args):
    if args.setting not in WRITABLE:
        print(f"not writable: {args.setting} (one of: {', '.join(sorted(WRITABLE))})",
              file=sys.stderr)
        return 2
    if not args.yes:
        print("refusing to write without --yes", file=sys.stderr)
        return 2
    value = _parse_value(args.setting, args.value)
    with open_device(args.path) as dev:
        before = dev.read_flash(0, PRIMARY_REGION_LEN)
        with open("pre-write-backup.bin", "wb") as f:  # safety net for this write
            f.write(before)
        if not dev.write_setting(OFFSETS[args.setting], value):
            print("write not acknowledged", file=sys.stderr)
            return 1
        after = dev.read_flash(0, PRIMARY_REGION_LEN)
    off = OFFSETS[args.setting]
    print(f"set {args.setting}: flash[{off}]={before[off]} -> {after[off]} "
          f"(complement {after[off + 1]}); backup saved to pre-write-backup.bin")
    return 0 if after[off] == (value & 0xFF) else 1


def cmd_restore(args):
    if not args.yes:
        print("refusing to write without --yes", file=sys.stderr)
        return 2
    with open(args.file, "rb") as f:
        data = f.read()
    with open_device(args.path) as dev:
        dev.write_flash(0, data)
    print(f"restored {len(data)} bytes from {args.file}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="scyrox", description=__doc__)
    p.add_argument("--path", help="explicit HID device path (else auto-detect)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list attached Scyrox interfaces").set_defaults(
        func=cmd_list)
    sub.add_parser("battery", help="read battery level").set_defaults(
        func=cmd_battery)

    d = sub.add_parser("dump", help="annotated hex dump of config flash")
    d.add_argument("--length", type=int, default=PRIMARY_REGION_LEN)
    d.set_defaults(func=cmd_dump)

    b = sub.add_parser("backup", help="save config flash to a file")
    b.add_argument("file")
    b.add_argument("--length", type=int, default=PRIMARY_REGION_LEN)
    b.set_defaults(func=cmd_backup)

    sub.add_parser("get", help="show decoded settings").set_defaults(func=cmd_get)

    s = sub.add_parser("set", help="write one setting (e.g. set report_rate 8000)")
    s.add_argument("setting")
    s.add_argument("value", help="Hz / on|off / number / degrees")
    s.add_argument("--yes", action="store_true", help="confirm the write")
    s.set_defaults(func=cmd_set)

    r = sub.add_parser("restore", help="write a backup file to flash")
    r.add_argument("file")
    r.add_argument("--yes", action="store_true", help="confirm the write")
    r.set_defaults(func=cmd_restore)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (RuntimeError, IOError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
