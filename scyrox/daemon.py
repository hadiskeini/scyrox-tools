#!/usr/bin/env python3
"""Scyrox V6 battery daemon.

Polls the Scyrox V6 mouse for battery state — over its 2.4 GHz dongle
(3554:f5f7) or directly over the USB cable (3554:f5f6, used while charging) —
and republishes level + charging status through a virtual HID device (uhid)
so UPower / GNOME display it natively (Settings → Power → Devices).

Run as root (needs /dev/uhid):
    sudo scyroxd
"""

import logging
import os
import select
import signal
import struct
import subprocess
import sys
import time

from . import protocol

# --- Real device (Compx/Scyrox, vendor-specific interface) ---
# Framing constants + command codes come from the shared, unit-tested protocol
# module (its checksum/frame builder are proven byte-identical to this daemon).
VID = protocol.VID
PID_DONGLE = protocol.PID_DONGLE
PID_WIRED = protocol.PID_WIRED  # the mouse itself, when plugged in via USB cable
SCYROX_RID = protocol.REPORT_ID
CMD_ONLINE = int(protocol.Cmd.DEVICE_ONLINE)
CMD_BATTERY = int(protocol.Cmd.BATTERY_LEVEL)

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

# Battery report (report ID 2 in RD below): [report_id, level%, charging].
BATTERY_RID = 0x02

POLL_S = 60           # battery poll cadence on the dongle (radio traffic)
POLL_WIRED_S = 15     # on the cable: no battery cost, show charge progress
SCAN_S = 5            # cheap sysfs scan for cable/dongle plug events
RETRY_S = 5           # re-poll delay after a failed query
ATTACH_BACKOFF_S = 15  # leave an interface that failed probing alone this long
DROP_BACKOFF_S = 10   # backoff after a working source stops answering

# Persisted last-known level. We seed the kernel/UPower with this at startup so
# the very first GET_REPORT (UPower's coldplug capacity read) returns a valid
# value instead of an error. If UPower reads an error there it discards the
# battery as invalid and it never shows in Settings.
STATE_FILE = "/var/lib/scyroxd/last_level"
SEED_DEFAULT = 50

# Mouse application with X/Y/buttons (Report 1, never sent) + battery
# (Report 2). The interactive fields are required — the kernel's hid-input
# layer skips battery setup on a "Mouse" collection that has no real input
# fields.
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
    # Report 2, byte 1: Battery Strength. Must stay the first data byte —
    # the kernel's coldplug capacity query reads the byte right after the
    # report ID raw, without parsing fields (hidinput_query_battery_capacity).
    0x85, 0x02, 0x05, 0x06, 0x09, 0x20,
    0x15, 0x00, 0x25, 0x64, 0x75, 0x08,
    0x95, 0x01, 0x81, 0x02,
    # Report 2, byte 2 bit 0: Battery System / Charging (kernel 6.3+ maps it
    # to POWER_SUPPLY_STATUS_CHARGING/DISCHARGING; changes bypass the 30 s
    # capacity ratelimit and emit a udev change event immediately). 7-bit pad.
    0x05, 0x85, 0x09, 0x44, 0x15, 0x00,
    0x25, 0x01, 0x75, 0x01, 0x95, 0x01,
    0x81, 0x02, 0x75, 0x07, 0x95, 0x01,
    0x81, 0x03, 0xC0,
])

log = logging.getLogger("scyroxd")


# --- Scyrox HID protocol ---

def _cmd(cmd, params=b""):
    """17-byte wire frame: report id + the shared protocol command frame."""
    return bytes([SCYROX_RID]) + protocol.build_command(cmd, params)


def _recv(fd, expected, timeout=1.0, on_idle=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            r = os.read(fd, 17)
            if len(r) == 17 and r[0] == SCYROX_RID and r[1] == expected:
                return r
        except BlockingIOError:
            if on_idle:
                on_idle()
            time.sleep(0.005)
    return None


def _report_ids(descriptor_path):
    """Report IDs declared in a HID report descriptor (minimal item walk)."""
    try:
        with open(descriptor_path, "rb") as f:
            d = f.read()
    except OSError:
        return set()
    ids = set()
    i = 0
    while i < len(d):
        prefix = d[i]
        if prefix == 0xFE:  # long item
            if i + 2 >= len(d):
                break
            i += 3 + d[i + 1]
            continue
        size = prefix & 0x03
        if size == 3:
            size = 4
        if prefix & 0xFC == 0x84 and size >= 1 and i + 1 < len(d):  # Report ID
            ids.add(d[i + 1])
        i += 1 + size
    return ids


def find_scyrox_candidates():
    """[(/dev/hidrawN, pid, ident)] for Scyrox interfaces speaking report 0x08.

    The protocol lives in the vendor collection that declares report ID 8 —
    matching on that (rather than a fixed interface index) works for both the
    dongle and the wired mouse. Wired sorts first: while the cable is plugged
    the radio link is down, so only the wired interface has live data.

    ident is the kernel HID instance name (e.g. "0003:3554:F5F7.000E"); its
    counter changes on every replug, so backoffs keyed by it never stick to a
    fresh device that happens to reuse the same /dev/hidrawN node."""
    out = []
    for entry in sorted(os.listdir("/sys/class/hidraw")):
        sysdir = f"/sys/class/hidraw/{entry}/device"
        try:
            with open(f"{sysdir}/uevent") as f:
                props = dict(
                    ln.strip().split("=", 1)
                    for ln in f if "=" in ln)
        except OSError:
            continue
        hid_id = props.get("HID_ID", "").upper()
        pid = next(
            (p for p in (PID_WIRED, PID_DONGLE)
             if hid_id == f"0003:{VID:08X}:{p:08X}"), None)
        if pid is None:
            continue
        if not props.get("HID_PHYS"):
            continue  # our own uhid device — no physical path
        if SCYROX_RID not in _report_ids(f"{sysdir}/report_descriptor"):
            continue
        ident = os.path.basename(os.path.realpath(sysdir))
        out.append((f"/dev/{entry}", pid, ident))
    out.sort(key=lambda c: (c[1] != PID_WIRED, c[0]))
    return out


def query_mouse(fd, wired=False, on_idle=None):
    """Return dict with online/level/charging/voltage_mv, or None on no response."""
    try:
        if not wired:
            # Dongle: ask whether the radio link to the mouse is up first —
            # the dongle answers on the mouse's behalf even when it's asleep.
            # Skipped when wired: the command means "wireless mouse online",
            # and its semantics on the cable interface are unreliable; there
            # the device answering the battery query at all proves presence.
            os.write(fd, _cmd(CMD_ONLINE))
            r = _recv(fd, CMD_ONLINE, on_idle=on_idle)
            if not r:
                return None
            if r[6] != 1:
                return {"online": False}
        os.write(fd, _cmd(CMD_BATTERY))
        b = _recv(fd, CMD_BATTERY, on_idle=on_idle)
        if not b:
            return None if wired else {"online": True}
        return {
            "online": True,
            "level": min(b[6], 100),
            "charging": b[7] == 1,
            "voltage_mv": (b[8] << 8) | b[9],
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
        + struct.pack("=HHIIII", len(RD), BUS_USB, VID, PID_DONGLE, 0x0100, 0)
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


def uhid_push_battery(fd, level, charging):
    data = bytes([BATTERY_RID, level, 1 if charging else 0])
    os.write(fd, _evt(UHID_INPUT2, struct.pack("=H", len(data)) + data))


def uhid_service(fd, level, charging):
    """Drain pending uhid events, answering GET_REPORT/SET_REPORT.

    The kernel issues a GET_REPORT to read the battery's capacity before any
    input report has populated it (UPower does this at session start). If the
    daemon never replies, the kernel blocks on the request until it times out —
    which is what stalled UPower and plymouth for ~60s at boot. We must answer
    promptly with the current level."""
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
            data = bytes([BATTERY_RID, level, 1 if charging else 0])
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


# --- daemon ---

class Daemon:
    """Tracks the attached Scyrox interface and the last published state."""

    def __init__(self, uhid_fd):
        self.uhid_fd = uhid_fd
        self.fd = None
        self.path = None
        self.pid = None
        self.ident = None
        # Seed from the last persisted reading so UPower's coldplug GET_REPORT
        # gets a valid capacity (an error reply makes UPower drop the battery).
        self.level = load_seed()
        self.charging = False
        self.failures = 0
        self.bad_until = {}  # ident -> monotonic time before which not to probe
        self.nudged = False
        self.no_source_logged = False

    def push(self):
        uhid_push_battery(self.uhid_fd, self.level, self.charging)

    def _service_uhid(self):
        """Answer kernel report requests that arrive mid-probe: a pending
        GET_REPORT must never wait behind slow mouse I/O (the kernel blocks
        on it — the historical boot-hang). Passed as on_idle into _recv."""
        ready, _, _ = select.select([self.uhid_fd], [], [], 0)
        if ready:
            uhid_service(self.uhid_fd, self.level, self.charging)

    def close_source(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.path = None
        self.pid = None
        self.ident = None
        self.failures = 0

    def attach(self, path, pid, ident):
        """Probe one interface; on response make it the source and return the
        probe's state dict, else back off from it and return None."""
        try:
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            log.warning("open %s failed: %s", path, e)
            self.bad_until[ident] = time.monotonic() + ATTACH_BACKOFF_S
            return None
        state = query_mouse(fd, pid == PID_WIRED, self._service_uhid)
        if state is None:
            os.close(fd)
            self.bad_until[ident] = time.monotonic() + ATTACH_BACKOFF_S
            return None
        self.close_source()
        self.fd, self.path, self.pid, self.ident = fd, path, pid, ident
        self.no_source_logged = False
        log.info("using %s (%s %04x:%04x)", path,
                 "wired" if pid == PID_WIRED else "dongle", VID, pid)
        return state

    def scan(self):
        """Cheap sysfs check for plug events. Attaches/switches sources as
        needed; returns the probe's state dict when one was queried."""
        now = time.monotonic()
        cands = find_scyrox_candidates()
        idents = {ident for _, _, ident in cands}
        self.bad_until = {
            i: t for i, t in self.bad_until.items()
            if t > now and i in idents}
        if self.path is not None and self.ident not in idents:
            log.info("%s gone", self.path)
            self.close_source()
            if self.charging:
                # The cable was yanked: nothing may take over (no dongle),
                # so clear the stale charging state right here.
                self.charging = False
                self.push()
        for path, pid, ident in cands:
            if self.fd is not None and (
                    self.pid == PID_WIRED or pid != PID_WIRED):
                continue  # only ever switch upwards: dongle -> wired
            if self.bad_until.get(ident, 0) > now:
                continue
            state = self.attach(path, pid, ident)
            if state is not None:
                return state
        if self.fd is None and not cands and not self.no_source_logged:
            log.info("no Scyrox interface present, waiting")
            self.no_source_logged = True
        return None

    def poll(self):
        """One battery query on the attached source; returns next poll delay."""
        if self.fd is None:
            return POLL_S
        return self.handle(
            query_mouse(self.fd, self.pid == PID_WIRED, self._service_uhid))

    def handle(self, state):
        """Publish a query result; returns the delay until the next poll."""
        if state is None:
            self.failures += 1
            if self.failures >= 3:
                log.warning("3 consecutive failures, dropping %s", self.path)
                self.bad_until[self.ident] = time.monotonic() + DROP_BACKOFF_S
                self.close_source()
            return RETRY_S
        self.failures = 0
        if state.get("online") and "level" in state:
            level, charging = state["level"], state["charging"]
            if (level, charging) != (self.level, self.charging):
                log.info(
                    "battery=%d%% charging=%s voltage=%d mV",
                    level, charging, state["voltage_mv"])
            if level != self.level:
                save_level(level)
            self.level, self.charging = level, charging
            self.push()
            if not self.nudged:
                nudge_upower()
                self.nudged = True
        elif not state.get("online") and self.charging and not any(
                pid == PID_WIRED for _, pid, _ in find_scyrox_candidates()):
            # The dongle lost sight of the mouse and no cable interface is
            # present: whatever we knew about charging is stale (cable gone,
            # mouse asleep). Level stays — it's still roughly right — but
            # report discharging. The wired-candidate check matters: while a
            # cable interface exists, "offline" from the dongle just means
            # the mouse radio is off because it's charging — not a yank.
            self.charging = False
            self.push()
        return POLL_WIRED_S if self.pid == PID_WIRED else POLL_S


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    if not os.path.exists(UHID_PATH):
        sys.exit("missing /dev/uhid (try: sudo modprobe uhid)")

    uhid_fd = uhid_open()
    log.info("virtual HID '%s' ready", NAME)

    daemon = Daemon(uhid_fd)
    daemon.push()
    log.info("seeded battery=%d%% (will update on first mouse poll)",
             daemon.level)

    def cleanup(*_):
        log.info("shutting down")
        daemon.close_source()
        uhid_close(uhid_fd)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    # Event loop: service uhid (GET_REPORT/SET_REPORT) the instant the kernel
    # asks, scan sysfs every SCAN_S for cable/dongle plug events, and poll the
    # mouse on the current cadence in between. Blocking only in select() —
    # never in a bare sleep — keeps the kernel from stalling on an unanswered
    # report request.
    next_scan = 0.0
    next_poll = 0.0
    while True:
        timeout = max(0.0, min(next_scan, next_poll) - time.monotonic())
        ready, _, _ = select.select([uhid_fd], [], [], timeout)
        if ready:
            uhid_service(uhid_fd, daemon.level, daemon.charging)
            continue
        if time.monotonic() >= next_scan:
            state = daemon.scan()
            if state is not None:
                next_poll = time.monotonic() + daemon.handle(state)
            next_scan = time.monotonic() + SCAN_S
        if time.monotonic() >= next_poll:
            next_poll = time.monotonic() + daemon.poll()


if __name__ == "__main__":
    main()
