"""Scyrox HID transport + framing.

Transport-agnostic protocol layer, decoded from the official web driver bundle
(see PROTOCOL.md). The 17-byte frame format and checksum here are byte-identical
to the working battery daemon's `_cmd`/`_crc`, which is our ground truth.

Two transports are provided:
  * HidApiTransport  - cross-platform (Linux + macOS), needs the `hid` package.
  * HidrawTransport  - Linux-only, stdlib-only (same path the daemon uses).

`ScyroxDevice` wraps a transport with the command/flash operations.
"""

from __future__ import annotations

import os
import time
from enum import IntEnum

# --- device identity ---
VID = 0x3554
PID_DONGLE = 0xF5F7
PID_WIRED = 0xF5F6
PID_8K_DONGLE = 0xF637
KNOWN_PIDS = (PID_DONGLE, PID_WIRED, PID_8K_DONGLE)

REPORT_ID = 0x08          # `wn` in the bundle; the vendor collection's report id
FRAME_LEN = 16            # data bytes after the report id (wire frame = 17 bytes)
RESP_LEN = 17             # report id + 16 data bytes, as read back

# Type offset added into byte 4 (`so()`): mouse=0, keyboard=128. We only do mice.
TYPE_OFFSET_MOUSE = 0


class Cmd(IntEnum):
    """Command codes (`Je` in the bundle)."""
    ENCRYPTION_DATA = 1
    PC_DRIVER_STATUS = 2
    DEVICE_ONLINE = 3
    BATTERY_LEVEL = 4
    DONGLE_ENTER_PAIR = 5
    GET_PAIR_STATE = 6
    WRITE_FLASH = 7
    READ_FLASH = 8
    CLEAR_SETTING = 9
    STATUS_CHANGED = 10
    GET_CURRENT_CONFIG = 14
    SET_CURRENT_CONFIG = 15
    READ_VERSION_ID = 18
    SET_4K_DONGLE_RGB = 20
    GET_4K_DONGLE_RGB = 21
    SET_LONG_RANGE_MODE = 22
    GET_LONG_RANGE_MODE = 23
    SET_DONGLE_RGB_BAR = 24
    GET_DONGLE_RGB_BAR = 25
    GET_DONGLE_VERSION = 29
    SET_DONGLE3_RGB = 44
    GET_DONGLE3_RGB = 45


# --- framing ---

def checksum(buf15: bytes) -> int:
    """Frame checksum byte. buf15 = the first 15 bytes of the 16-byte frame.

    Matches the daemon's `_crc` and the bundle's `Mt(buf) - wn`:
        (0x55 - (sum(buf[0..14]) & 0xFF) - REPORT_ID) & 0xFF
    """
    return (0x55 - (sum(buf15) & 0xFF) - REPORT_ID) & 0xFF


def build_command(cmd: int, payload: bytes = b"",
                  type_offset: int = TYPE_OFFSET_MOUSE) -> bytes:
    """A generic command frame (`Mr`): cmd@0, len@4, payload@5+, crc@15."""
    if len(payload) > 10:
        raise ValueError("payload exceeds frame capacity (bytes 5..14)")
    buf = bytearray(FRAME_LEN)
    buf[0] = cmd
    buf[4] = (len(payload) + type_offset) & 0xFF
    buf[5:5 + len(payload)] = payload
    buf[15] = checksum(buf[:15])
    return bytes(buf)


def build_read_flash(addr: int, count: int) -> bytes:
    """ReadFlashData frame: cmd@0, addr@2-3 (big-endian), count@4, crc@15.

    Flash commands address differently from `Mr`: the address lives at bytes 2-3
    and the length at byte 4 (low nibble), matching how responses are parsed."""
    if not 1 <= count <= 15:
        raise ValueError("flash chunk count must be 1..15")
    buf = bytearray(FRAME_LEN)
    buf[0] = Cmd.READ_FLASH
    buf[2] = (addr >> 8) & 0xFF
    buf[3] = addr & 0xFF
    buf[4] = count & 0x0F
    buf[15] = checksum(buf[:15])
    return bytes(buf)


def build_write_flash(addr: int, data: bytes) -> bytes:
    """WriteFlashData frame: cmd@0, addr@2-3 (BE), len@4, data@5+, crc@15."""
    if not 1 <= len(data) <= 10:
        raise ValueError("flash write chunk must be 1..10 bytes (frame capacity)")
    buf = bytearray(FRAME_LEN)
    buf[0] = Cmd.WRITE_FLASH
    buf[2] = (addr >> 8) & 0xFF
    buf[3] = addr & 0xFF
    buf[4] = len(data) & 0x0F
    buf[5:5 + len(data)] = data
    buf[15] = checksum(buf[:15])
    return bytes(buf)


def parse_flash_response(resp: bytes) -> tuple[int, int, bytes]:
    """(addr, count, data) from a ReadFlash/WriteFlash response frame.

    resp is the 17-byte report (report id at [0]); per the bundle:
        addr = (resp[3] << 8) | resp[4]   # +1 vs frame: report id shifts everything
        count = resp[5] & 0x0F
        data  = resp[6 : 6 + count]
    """
    addr = (resp[3] << 8) | resp[4]
    count = resp[5] & 0x0F
    return addr, count, bytes(resp[6:6 + count])


# --- transports ---

class Transport:
    """Minimal HID transport: write a frame, read a 17-byte response."""

    def write(self, frame16: bytes) -> None: ...
    def read(self, timeout: float = 1.0) -> bytes | None: ...
    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class HidApiTransport(Transport):
    """Cross-platform transport via the `hid` (hidapi) package."""

    def __init__(self, path: bytes | None = None,
                 vid: int = VID, pid: int | None = None):
        import hid  # lazy: only needed for this backend
        self._hid = hid
        self.dev = hid.device()
        if path is not None:
            self.dev.open_path(path)
        else:
            self.dev.open(vid, pid)
        self.dev.set_nonblocking(True)

    def write(self, frame16: bytes) -> None:
        # hidapi expects the report id as the first byte of the write buffer.
        self.dev.write(bytes([REPORT_ID]) + frame16)

    def read(self, timeout: float = 1.0) -> bytes | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = self.dev.read(RESP_LEN)
            if data and len(data) == RESP_LEN and data[0] == REPORT_ID:
                return bytes(data)
            time.sleep(0.002)
        return None

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:
            pass

    @staticmethod
    def enumerate(vid: int = VID, pids=KNOWN_PIDS) -> list[dict]:
        import hid
        out = []
        for d in hid.enumerate(vid, 0):
            if d["product_id"] in pids:
                out.append(d)
        return out


class HidrawTransport(Transport):
    """Linux-only stdlib transport over /dev/hidrawN (as the daemon uses)."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def write(self, frame16: bytes) -> None:
        os.write(self.fd, bytes([REPORT_ID]) + frame16)

    def read(self, timeout: float = 1.0) -> bytes | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                r = os.read(self.fd, RESP_LEN)
                if len(r) == RESP_LEN and r[0] == REPORT_ID:
                    return r
            except BlockingIOError:
                time.sleep(0.005)
        return None

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


# --- high-level device ---

class ScyroxDevice:
    """Command/flash operations over a Transport."""

    def __init__(self, transport: Transport):
        self.t = transport

    def close(self) -> None:
        self.t.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _txn(self, frame16: bytes, expect_cmd: int, timeout: float = 1.0,
             retries: int = 5, validate=None) -> bytes | None:
        """Send a frame, return the first matching response.

        A response is `[report_id, cmd, status_or_pad, ...]` — so the command
        echo is at resp[1] (exactly as the daemon's `_recv` checks `r[1]`).
        `validate(resp)` may impose an extra check (e.g. the echoed flash addr).
        Retries mirror the bundle's `Ks`."""
        for _ in range(retries):
            self.t.write(frame16)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                resp = self.t.read(timeout=timeout)
                if resp is None:
                    break
                if resp[1] == expect_cmd and (validate is None or validate(resp)):
                    return resp
        return None

    # --- battery / presence (same commands the daemon uses) ---

    def online(self) -> bool:
        resp = self._txn(build_command(Cmd.DEVICE_ONLINE), Cmd.DEVICE_ONLINE)
        return bool(resp and resp[6] == 1)

    def battery(self) -> dict | None:
        resp = self._txn(build_command(Cmd.BATTERY_LEVEL), Cmd.BATTERY_LEVEL)
        if not resp:
            return None
        return {
            "level": min(resp[6], 100),
            "charging": resp[7] == 1,
            "voltage_mv": (resp[8] << 8) | resp[9],
        }

    # --- flash ---

    def read_flash_chunk(self, addr: int, count: int) -> bytes | None:
        # Verify the response echoes the requested address (the device echoes
        # the request header in bytes 1..5), so a stale frame can't be mistaken
        # for this chunk while paging.
        resp = self._txn(
            build_read_flash(addr, count), Cmd.READ_FLASH,
            validate=lambda r: (r[3] << 8 | r[4]) == addr)
        if not resp:
            return None
        _, n, data = parse_flash_response(resp)
        return data[:count] if n >= count else data

    def read_flash(self, addr: int, length: int, chunk: int = 10) -> bytes:
        """Read `length` bytes from `addr`, paging in <=10-byte chunks."""
        out = bytearray()
        off = 0
        while off < length:
            n = min(chunk, length - off)
            got = self.read_flash_chunk(addr + off, n)
            if got is None:
                raise IOError(f"flash read failed at 0x{addr + off:04x}")
            out += got[:n]
            off += n
        return bytes(out)

    def write_flash_chunk(self, addr: int, data: bytes) -> bool:
        resp = self._txn(build_write_flash(addr, data), Cmd.WRITE_FLASH)
        return resp is not None

    def write_flash(self, addr: int, data: bytes, chunk: int = 10) -> None:
        off = 0
        while off < len(data):
            piece = data[off:off + chunk]
            if not self.write_flash_chunk(addr + off, piece):
                raise IOError(f"flash write failed at 0x{addr + off:04x}")
            off += len(piece)

    def write_setting(self, addr: int, value: int) -> bool:
        """Write a single setting as the firmware expects: a 2-byte
        [value, 0x55-value] complement pair (the bundle's `st` primitive)."""
        return self.write_flash_chunk(
            addr, bytes([value & 0xFF, (0x55 - value) & 0xFF]))

    def factory_reset(self) -> bool:
        """ClearSetting (cmd 9): restore the device to defaults. The driver
        then polls StatusChanged until the reset completes and re-reads flash."""
        resp = self._txn(build_command(Cmd.CLEAR_SETTING), Cmd.CLEAR_SETTING)
        return resp is not None
