#!/usr/bin/env python3
"""Scyrox V6 battery daemon.

Polls the Scyrox V6 mouse over its 2.4 GHz dongle for battery state and
republishes the value through a virtual HID device (uhid) so UPower / GNOME
display it natively (Settings → Power → Devices).

Run as root (needs /dev/uhid):
    sudo python scyroxd.py
"""

import logging
import os
import signal
import struct
import subprocess
import sys
import time

# --- Real device (Scyrox dongle, vendor-specific interface) ---
VID = 0x3554
PID = 0xF5F7
SCYROX_RID = 0x08
CMD_ONLINE = 0x03
CMD_BATTERY = 0x04

# --- Virtual device (uhid) ---
UHID_PATH = "/dev/uhid"
UHID_EVT_SIZE = 4376
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_DESTROY = 1
BUS_USB = 0x03
NAME = "Scyrox V6"

POLL_S = 60

# Mouse application with X/Y/buttons (Report 1, never sent) + battery (Report 2).
# The interactive fields are required — the kernel's hid-input layer skips
# battery setup on a "Mouse" collection that has no real input fields.
RD = bytes([
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,
    0x85, 0x01, 0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x03,
    0x15, 0x00, 0x25, 0x01, 0x95, 0x03,
    0x75, 0x01, 0x81, 0x02, 0x95, 0x01,
    0x75, 0x05, 0x81, 0x03,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08,
    0x95, 0x02, 0x81, 0x06, 0xC0,
    0x85, 0x02, 0x05, 0x06, 0x09, 0x20,
    0x15, 0x00, 0x25, 0x64, 0x75, 0x08,
    0x95, 0x01, 0x81, 0x02, 0xC0,
])

log = logging.getLogger("scyroxd")


# --- Scyrox HID protocol ---

def _crc(p15):
    return (0x55 - (sum(p15) & 0xFF) - SCYROX_RID) & 0xFF


def _cmd(cmd, params=b""):
    p = bytearray(16)
    p[0] = cmd
    p[5:5 + len(params)] = params
    p[15] = _crc(bytes(p[:15]))
    return bytes([SCYROX_RID]) + bytes(p)


def _recv(fd, expected, timeout=1.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = os.read(fd, 17)
            if len(r) == 17 and r[0] == SCYROX_RID and r[1] == expected:
                return r
        except BlockingIOError:
            time.sleep(0.005)
    return None


def find_scyrox_hidraw():
    """Return /dev/hidrawN that matches Scyrox dongle interface 1, or None."""
    for entry in sorted(os.listdir("/sys/class/hidraw")):
        try:
            with open(f"/sys/class/hidraw/{entry}/device/uevent") as f:
                props = dict(
                    ln.strip().split("=", 1)
                    for ln in f if "=" in ln)
        except OSError:
            continue
        if props.get("HID_ID", "").upper() != f"0003:{VID:08X}:{PID:08X}":
            continue
        if not props.get("HID_PHYS", "").endswith("input1"):
            continue
        return f"/dev/{entry}"
    return None


def query_mouse(fd):
    """Return dict with online/level/charging/voltage_mv, or None on no response."""
    try:
        os.write(fd, _cmd(CMD_ONLINE))
        r = _recv(fd, CMD_ONLINE)
        if not r:
            return None
        if r[6] != 1:
            return {"online": False}
        os.write(fd, _cmd(CMD_BATTERY))
        r = _recv(fd, CMD_BATTERY)
        if not r:
            return {"online": True}
        return {
            "online": True,
            "level": r[6],
            "charging": r[7] == 1,
            "voltage_mv": (r[8] << 8) | r[9],
        }
    except OSError as e:
        log.error("mouse I/O error: %s", e)
        return None


# --- uhid helpers ---

def _evt(etype, payload):
    buf = bytearray(UHID_EVT_SIZE)
    struct.pack_into("=I", buf, 0, etype)
    buf[4:4 + len(payload)] = payload
    return bytes(buf)


def uhid_open():
    fd = os.open(UHID_PATH, os.O_RDWR | os.O_NONBLOCK)
    payload = (
        NAME.encode().ljust(128, b"\0")[:128]
        + bytes(64) + bytes(64)
        + struct.pack("=HHIIII", len(RD), BUS_USB, VID, PID, 0x0100, 0)
        + RD.ljust(4096, b"\0")
    )
    os.write(fd, _evt(UHID_CREATE2, payload))
    time.sleep(0.3)
    while True:
        try:
            os.read(fd, UHID_EVT_SIZE)
        except BlockingIOError:
            break
    return fd


def uhid_push_battery(fd, level):
    data = bytes([0x02, level])
    os.write(fd, _evt(UHID_INPUT2, struct.pack("=H", len(data)) + data))


def uhid_close(fd):
    try:
        os.write(fd, _evt(UHID_DESTROY, b""))
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def nudge_upower():
    """UPower 1.91.x misses the initial add event for uhid-backed batteries.
    Re-fire a synthetic udev add for our power_supply to pull it in."""
    try:
        ps = "/sys/class/power_supply"
        ours = sorted(
            (os.path.join(ps, n) for n in os.listdir(ps) if n.startswith("hid-")),
            key=os.path.getmtime)
        if not ours:
            return
        subprocess.run(
            ["udevadm", "trigger", "--action=add", ours[-1]],
            check=False, timeout=5)
    except Exception as e:
        log.warning("nudge_upower failed: %s", e)


# --- main ---

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    if not os.path.exists(UHID_PATH):
        sys.exit("missing /dev/uhid (try: sudo modprobe uhid)")

    uhid_fd = uhid_open()
    log.info("virtual HID '%s' ready", NAME)

    real_fd = None
    last_level = None
    nudged = False
    failures = 0
    dongle_missing_logged = False

    def cleanup(*_):
        log.info("shutting down")
        nonlocal real_fd
        if real_fd is not None:
            try:
                os.close(real_fd)
            except OSError:
                pass
        uhid_close(uhid_fd)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    while True:
        if real_fd is None:
            hidraw = find_scyrox_hidraw()
            if not hidraw:
                if not dongle_missing_logged:
                    log.info("Scyrox dongle not present, waiting")
                    dongle_missing_logged = True
                time.sleep(POLL_S)
                continue
            try:
                real_fd = os.open(hidraw, os.O_RDWR | os.O_NONBLOCK)
                log.info("opened %s", hidraw)
                dongle_missing_logged = False
                failures = 0
            except OSError as e:
                log.warning("open %s failed: %s", hidraw, e)
                time.sleep(POLL_S)
                continue

        state = query_mouse(real_fd)

        if state is None:
            failures += 1
            if failures >= 3:
                log.warning("3 consecutive failures, reopening hidraw")
                try:
                    os.close(real_fd)
                except OSError:
                    pass
                real_fd = None
                failures = 0
            time.sleep(5)
            continue

        failures = 0

        if state.get("online") and "level" in state:
            level = state["level"]
            if level != last_level:
                log.info(
                    "battery=%d%% charging=%s voltage=%d mV",
                    level, state["charging"], state["voltage_mv"])
                last_level = level
            uhid_push_battery(uhid_fd, level)
            if not nudged:
                time.sleep(0.5)
                nudge_upower()
                nudged = True
        elif not state.get("online"):
            log.debug("mouse asleep")

        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
