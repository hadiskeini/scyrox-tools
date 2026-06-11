"""Offline protocol tests — no device needed.

The decisive check is parity with the battery daemon's known-good framing
(scyroxd.py `_cmd`/`_crc`), which has shipped and works against real hardware.
"""

import pytest

from scyrox import protocol as P
from scyrox.flash import (
    FlashImage, OFFSETS,
    report_rate_to_flash, report_rate_from_flash,
)

SCYROX_RID = 0x08


def _daemon_cmd(cmd, params=b""):
    """Reimplementation of scyroxd.py's `_cmd` (17-byte wire frame)."""
    p = bytearray(16)
    p[0] = cmd
    p[5:5 + len(params)] = params
    p[15] = (0x55 - (sum(p[:15]) & 0xFF) - SCYROX_RID) & 0xFF
    return bytes([SCYROX_RID]) + bytes(p)


@pytest.mark.parametrize("cmd", [P.Cmd.DEVICE_ONLINE, P.Cmd.BATTERY_LEVEL])
def test_command_frame_matches_daemon(cmd):
    mine = bytes([P.REPORT_ID]) + P.build_command(cmd)
    assert mine == _daemon_cmd(cmd)


def test_checksum_formula():
    buf = bytearray(16)
    buf[0] = P.Cmd.BATTERY_LEVEL
    assert buf[15] == 0  # not set yet
    assert P.checksum(buf[:15]) == _daemon_cmd(P.Cmd.BATTERY_LEVEL)[16]


def test_read_flash_framing():
    fr = P.build_read_flash(0x0102, 8)
    assert fr[0] == P.Cmd.READ_FLASH
    assert fr[2] == 0x01 and fr[3] == 0x02  # big-endian addr
    assert fr[4] == 8
    assert fr[15] == P.checksum(fr[:15])


def test_write_flash_framing_and_parse_roundtrip():
    data = bytes([10, 20, 30])
    wf = P.build_write_flash(0x00AC, data)
    assert wf[0] == P.Cmd.WRITE_FLASH
    assert wf[2] == 0x00 and wf[3] == 0xAC
    assert wf[4] == 3 and wf[5:8] == data
    # device echoes the 16-byte frame after the report id
    resp = bytes([P.REPORT_ID]) + wf
    addr, count, got = P.parse_flash_response(resp)
    assert (addr, count, got) == (0x00AC, 3, data)


def test_flash_chunk_count_bounds():
    with pytest.raises(ValueError):
        P.build_read_flash(0, 16)
    with pytest.raises(ValueError):
        P.build_read_flash(0, 0)


@pytest.mark.parametrize("hz", [125, 250, 500, 1000, 2000, 4000, 8000])
def test_report_rate_codec_roundtrips(hz):
    assert report_rate_from_flash(report_rate_to_flash(hz)) == hz


def test_report_rate_exact_bytes():
    # mouse-path encoding verified against the bundle (v5/Kh)
    assert {hz: report_rate_to_flash(hz) for hz in
            (125, 250, 500, 1000, 2000, 4000, 8000)} == \
        {125: 8, 250: 4, 500: 2, 1000: 1, 2000: 16, 4000: 32, 8000: 64}


def test_decodes_real_captured_dump():
    # ~/Downloads capture: flash offset 0 = [1, 84] (value 1 + complement 0x55-1)
    img = FlashImage(bytes([1, 84] + [0] * 300))
    assert img.report_rate_hz == 1000
    assert img.setting_valid(OFFSETS["report_rate"])


def test_complement_pair_writes():
    img = FlashImage(bytes(300))
    img.set_setting(OFFSETS["motion_sync"], 1)
    off = OFFSETS["motion_sync"]
    assert img.data[off] == 1 and (img.data[off] + img.data[off + 1]) & 0xFF == 0x55
    img.angle_tune_deg = -30
    assert img.signed_setting(OFFSETS["angle_tune"]) == -30


def test_flash_image_accessors():
    img = FlashImage(bytes(OFFSETS["debounce_release_time"] + 4))
    img.report_rate_hz = 8000
    assert img.report_rate_hz == 8000
    img.motion_sync = True
    assert img.motion_sync and img.data[OFFSETS["motion_sync"]] == 1
    img.set_color(OFFSETS["dpi_color"], (1, 2, 3))
    assert img.dpi_color(0) == (1, 2, 3)


import os

from scyrox.flash import MOUSE_BUTTON_MASK, KEY_FUNCTION_TYPES


def _real_dump():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "real_dump_3950.hex")
    with open(path) as f:
        return bytes(int(x, 16) for x in f.read().split())


def test_real_dump_decodes_correctly():
    """End-to-end against a real Scyrox 3950 flash dump (the ground truth)."""
    img = FlashImage(_real_dump())
    assert len(img) == 256
    assert img.report_rate_hz == 1000
    assert img.motion_sync is True
    assert img.angle_snap is False and img.ripple_control is False
    assert img.max_dpi_stage == 1 and img.current_dpi_stage == 0
    assert img.dpi_xy(0) == (1600, 1600)          # raw 0x1f -> (31+1)*50
    assert img.dpi_xy(3) == (3200, 3200)          # raw 0x3f
    assert img.dpi_color(0) == (255, 0, 0)        # red
    # button 0 = MouseKey, param 0x0100 = left click
    assert img.key_function(0) == (1, 0x0100)
    assert MOUSE_BUTTON_MASK[0x0100] == "left"
    assert KEY_FUNCTION_TYPES[img.key_function(5)[0]] == "disable"
    # lighting block (mode 1, magenta, off) + its checksum byte
    assert img.light_mode == 1 and img.light_color == (255, 0, 255)
    assert img.light_speed == 7 and img.light_brightness == 9
    assert img.light_enabled is False
    # uninitialized optional field reads as 0 via the complement-validity gate
    assert img.angle_tune_deg == 0


def test_real_dump_all_checksums_valid():
    """Every complement pair and 4-byte entry checksum in the dump verifies."""
    img = FlashImage(_real_dump())
    for key in ("report_rate", "max_dpi_stage", "current_dpi", "lod",
                "motion_sync", "angle_snap", "ripple", "light_enable"):
        assert img.setting_valid(OFFSETS[key]), key
    for stage in range(8):
        for base in (OFFSETS["dpi_value"], OFFSETS["dpi_color"]):
            off = base + stage * 4
            b = img.data[off:off + 4]
            assert b[3] == img.entry_checksum(b[0], b[1], b[2]), (base, stage)
    for btn in range(6):
        off = OFFSETS["key_function"] + btn * 4
        b = img.data[off:off + 4]
        assert b[3] == img.entry_checksum(b[0], b[1], b[2]), btn


class FakeTransport(P.Transport):
    """In-memory device that answers like real hidraw: responses are
    [report_id, cmd, status/pad, addr_hi, addr_lo, len, data...]."""

    def __init__(self, flash):
        self.flash = bytearray(flash)
        self._pending = None

    def write(self, frame16):
        cmd = frame16[0]
        out = bytearray(P.RESP_LEN)
        out[0] = P.REPORT_ID
        out[1] = cmd
        if cmd == P.Cmd.READ_FLASH:
            addr = (frame16[2] << 8) | frame16[3]
            n = frame16[4] & 0x0F
            out[3], out[4], out[5] = frame16[2], frame16[3], n
            out[6:6 + n] = self.flash[addr:addr + n]
        elif cmd == P.Cmd.BATTERY_LEVEL:
            out[6], out[7], out[8], out[9] = 73, 1, 0x0F, 0xA0
        elif cmd == P.Cmd.DEVICE_ONLINE:
            out[6] = 1
        self._pending = bytes(out)

    def read(self, timeout=1.0):
        r, self._pending = self._pending, None
        return r

    def close(self):
        pass


def test_device_read_flash_roundtrips_through_transport():
    flash = bytes((i * 7) & 0xFF for i in range(256))
    dev = P.ScyroxDevice(FakeTransport(flash))
    assert dev.read_flash(0, 256) == flash          # paged in <=10B chunks
    assert dev.read_flash(50, 10) == flash[50:60]


def test_device_battery_and_online():
    dev = P.ScyroxDevice(FakeTransport(bytes(256)))
    assert dev.online() is True
    assert dev.battery() == {"level": 73, "charging": True, "voltage_mv": 0x0FA0}


def test_set_dpi_xy_roundtrips_with_checksum():
    img = FlashImage(bytes(256))
    img.set_dpi_xy(0, 1600, 1600)
    assert img.dpi_xy(0) == (1600, 1600)
    img.set_dpi_xy(1, 26000, 26000)          # exercises the high bits
    assert img.dpi_xy(1) == (26000, 26000)
    for stage in (0, 1):
        off = OFFSETS["dpi_value"] + stage * 4
        assert img.data[off + 3] == img.entry_checksum(*img.data[off:off + 3])


def test_set_light_block_checksum():
    img = FlashImage(bytes(256))
    img.set_light(2, (10, 20, 30), 5, 7)
    base = OFFSETS["light"]
    assert img.light_mode == 2 and img.light_color == (10, 20, 30)
    assert img.light_speed == 5 and img.light_brightness == 7
    block = img.data[base:base + 6]
    assert img.data[base + 6] == (0x55 - (sum(block) & 0xFF)) & 0xFF


def test_set_dpi_color_checksum():
    img = FlashImage(bytes(256))
    img.set_dpi_color(3, (1, 2, 3))
    off = OFFSETS["dpi_color"] + 3 * 4
    assert img.dpi_color(3) == (1, 2, 3)
    assert img.data[off + 3] == img.entry_checksum(1, 2, 3)


def test_flash_image_diff():
    a = FlashImage(bytes([0, 0, 0, 0]))
    b = FlashImage(bytes([0, 9, 0, 7]))
    assert a.diff(b) == [(1, 0, 9), (3, 0, 7)]
