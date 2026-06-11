#!/usr/bin/env python3
"""Guided hardware test for a structured (DPI) write round-trip.

Exercises the same path the TUI's Apply uses: read flash -> set_dpi_xy ->
write the changed byte ranges -> re-read -> verify value + entry checksum.
Backs up first and restores at the end, so your config is left unchanged.

Run with the daemon stopped (it shares the device):
    sudo systemctl stop scyroxd
    python3 tools/dpi-write-test.py
    sudo systemctl start scyroxd
"""
import sys

from scyrox.device import open_device
from scyrox.flash import FlashImage, OFFSETS


def changed_runs(a: FlashImage, b: FlashImage):
    offs = sorted({o for o, _x, _y in a.diff(b)})
    runs = []
    for o in offs:
        if runs and o == runs[-1][1]:
            runs[-1][1] = o + 1
        else:
            runs.append([o, o + 1])
    return runs


def write_changes(dev, base: FlashImage, target: FlashImage):
    for start, end in changed_runs(base, target):
        dev.write_flash(start, bytes(target.data[start:end]))


def stage0_ok(img: FlashImage, expect):
    off = OFFSETS["dpi_value"]
    crc_ok = img.data[off + 3] == img.entry_checksum(*img.data[off:off + 3])
    return img.dpi_xy(0) == (expect, expect) and crc_ok


def main():
    dev = open_device()
    original = FlashImage(dev.read_flash(0, 256))
    with open("dpi-test-backup.bin", "wb") as f:
        f.write(original.data)

    cur = original.dpi_xy(0)[0]
    new = 800 if cur != 800 else 1600
    print(f"stage 0 DPI is currently {cur}; writing {new}…  (backup: dpi-test-backup.bin)")

    target = FlashImage(bytes(original.data))
    target.set_dpi_xy(0, new, new)
    write_changes(dev, original, target)

    readback = FlashImage(dev.read_flash(0, 256))
    if not stage0_ok(readback, new):
        print(f"FAIL: device read back {readback.dpi_xy(0)} (expected {new}). "
              "Restoring backup…")
        write_changes(dev, readback, original)
        return 1
    print(f"PASS: device now reports stage 0 = {readback.dpi_xy(0)[0]} DPI, "
          "checksum valid.")
    print("(Optional: open scyrox.net or feel the cursor speed to confirm.)")

    try:
        input("Press Enter to restore your original config… ")
    except EOFError:
        pass

    write_changes(dev, readback, original)
    restored = FlashImage(dev.read_flash(0, 256))
    if restored.dpi_xy(0)[0] == cur and restored.data == original.data:
        print(f"Restored: stage 0 back to {cur} DPI; flash matches the backup.")
        return 0
    print("WARNING: restore mismatch — run: "
          "scyrox restore dpi-test-backup.bin --yes")
    return 1


if __name__ == "__main__":
    sys.exit(main())
