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
import select
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
UHID_DESTROY = 1
UHID_GET_REPORT = 9
UHID_GET_REPORT_REPLY = 10
UHID_CREATE2 = 11
UHID_INPUT2 = 12
UHID_SET_REPORT = 13
UHID_SET_REPORT_REPLY = 14
BUS_USB = 0x03
NAME = "Scyrox V6"

# Battery report (matches report ID 2 in RD below): [report_id, level%].
BATTERY_RID = 0x02

POLL_S = 60

# Persisted last-known level. We seed the kernel/UPower with this at startup so
# the very first GET_REPORT (UPower's coldplug capacity read) returns a valid
# value instead of an error. If UPower reads an error there it discards the
# battery as invalid and it never shows in Settings.
STATE_FILE = "/var/lib/scyroxd/last_level"
SEED_DEFAULT = 50

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
    data = bytes([BATTERY_RID, level])
    os.write(fd, _evt(UHID_INPUT2, struct.pack("=H", len(data)) + data))


def uhid_service(fd, level):
    """Drain pending uhid events, answering GET_REPORT/SET_REPORT.

    The kernel issues a GET_REPORT to read the battery's capacity before any
    input report has populated it (UPower does this at session start). If the
    daemon never replies, the kernel blocks on the request until it times out —
    which is what stalled UPower and plymouth for ~60s at boot. We must answer
    promptly with the current level (or an error if we don't have one yet, which
    still returns immediately instead of hanging)."""
    while True:
        try:
            buf = os.read(fd, UHID_EVT_SIZE)
        except BlockingIOError:
            return
        except OSError as e:
            log.warning("uhid read error: %s", e)
            return
        if len(buf) < 4:
            continue
        etype = struct.unpack_from("=I", buf, 0)[0]
        if etype == UHID_GET_REPORT:
            # u.get_report: id (u32), rnum (u8), rtype (u8)
            rid = struct.unpack_from("=I", buf, 4)[0]
            if level is None:
                # No reading yet: reply EIO so the kernel returns at once.
                reply = struct.pack("=IHH", rid, 5, 0)  # err=EIO, size=0
            else:
                data = bytes([BATTERY_RID, level])
                reply = struct.pack("=IHH", rid, 0, len(data)) + data
            os.write(fd, _evt(UHID_GET_REPORT_REPLY, reply))
        elif etype == UHID_SET_REPORT:
            # u.set_report begins with id (u32); ack so the kernel doesn't wait.
            rid = struct.unpack_from("=I", buf, 4)[0]
            os.write(fd, _evt(UHID_SET_REPORT_REPLY, struct.pack("=IH", rid, 0)))


def uhid_close(fd):
    try:
        os.write(fd, _evt(UHID_DESTROY, b""))
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def load_seed():
    """Last level persisted from a previous run, or SEED_DEFAULT if none/bad."""
    try:
        with open(STATE_FILE) as f:
            v = int(f.read().strip())
        if 0 <= v <= 100:
            return v
    except (OSError, ValueError):
        pass
    return SEED_DEFAULT


def save_level(level):
    """Persist the latest level so the next boot seeds an accurate value."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(level))
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        log.warning("could not persist level: %s", e)


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
    # Seed from the last persisted reading so UPower's coldplug GET_REPORT gets a
    # valid capacity (a None here would reply EIO and UPower drops the battery).
    cur_level = load_seed()
    uhid_push_battery(uhid_fd, cur_level)
    log.info("seeded battery=%d%% (will update on first mouse poll)", cur_level)
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

    def poll_mouse():
        """Run one open/query cycle. Returns seconds to wait before next poll."""
        nonlocal real_fd, cur_level, nudged, failures, dongle_missing_logged

        if real_fd is None:
            hidraw = find_scyrox_hidraw()
            if not hidraw:
                if not dongle_missing_logged:
                    log.info("Scyrox dongle not present, waiting")
                    dongle_missing_logged = True
                return POLL_S
            try:
                real_fd = os.open(hidraw, os.O_RDWR | os.O_NONBLOCK)
                log.info("opened %s", hidraw)
                dongle_missing_logged = False
                failures = 0
            except OSError as e:
                log.warning("open %s failed: %s", hidraw, e)
                return POLL_S

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
            return 5

        failures = 0

        if state.get("online") and "level" in state:
            level = state["level"]
            if level != cur_level:
                log.info(
                    "battery=%d%% charging=%s voltage=%d mV",
                    level, state["charging"], state["voltage_mv"])
                save_level(level)
            cur_level = level
            uhid_push_battery(uhid_fd, level)
            if not nudged:
                nudge_upower()
                nudged = True
        elif not state.get("online"):
            log.debug("mouse asleep")

        return POLL_S

    # Event loop: service uhid (GET_REPORT/SET_REPORT) the instant the kernel
    # asks, while polling the mouse on the POLL_S cadence in between. Blocking
    # only in select() — never in a bare sleep — keeps the kernel from stalling
    # on an unanswered report request.
    next_poll = 0.0
    while True:
        timeout = max(0.0, next_poll - time.monotonic())
        ready, _, _ = select.select([uhid_fd], [], [], timeout)
        if ready:
            uhid_service(uhid_fd, cur_level)
            continue
        next_poll = time.monotonic() + poll_mouse()


if __name__ == "__main__":
    main()
