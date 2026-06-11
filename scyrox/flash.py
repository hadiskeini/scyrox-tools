"""Scyrox config-flash model: offset map, value codecs, typed accessors.

Decoded from the web driver bundle and cross-checked by adversarial verification
(see PROTOCOL.md). Key structural facts:

  * Most single settings are stored as a 2-byte [value, 0x55-value] *complement
    pair*. The firmware writes the complement and the driver only trusts a field
    when (value + complement) & 0xFF == 0x55. Writes must emit the complement.
  * The whole config lives in a 16 KB flash space; the first ~256 bytes hold the
    primary settings, with macros at 768 and the 3955-sensor DPI block at 6912.

Fields marked VERIFY are decoded but want a real-dump confirmation of units.
"""

from __future__ import annotations

COMPLEMENT_BASE = 0x55  # value + complement == 0x55 marks a valid field

# Setting -> byte offset in the config flash image (`Se` in the bundle, plus a
# few absolute literals the driver uses for the Light sub-fields).
OFFSETS = {
    "report_rate": 0,        # [value, comp]; see codec
    "max_dpi_stage": 2,      # [value, comp] active stage count
    "current_dpi": 4,        # [value, comp] active stage index (u8, NOT u16)
    "key_operation": 8,      # [value, comp] bit0=left active, bit1=right active
    "lod": 10,               # [value, comp] lift-off; enum per sensor   VERIFY
    "dpi_value": 12,         # 8 stages x 4 bytes (packed X/Y), spans 12..43
    "dpi_color": 44,         # 8 stages x 4 bytes (RGB + spare), spans 44..75
    "dpi_effect_mode": 76,
    "dpi_effect_brightness": 78,   # UI 1..10 via o6/s6 lookup
    "dpi_effect_speed": 80,
    "dpi_effect_state": 82,
    "key_function": 96,      # 4 bytes/button: [type, paramHi, paramLo, crc]
    "light": 160,            # 7-byte block: [mode,R,G,B,speed,bright,blkcrc]
    "light_mode": 160,
    "light_color": 161,
    "light_speed": 164,      # 0..9 (reader clamps)
    "light_brightness": 165,  # 0..9 (reader clamps)
    "light_block_crc": 166,
    "light_enable": 167,     # [value, comp] 1=on, 0=off
    "debounce_time": 169,    # [value, comp] ms                          VERIFY
    "motion_sync": 171,      # [value, comp] 0/1
    "sleep_time": 173,       # [value, comp] minutes?                    VERIFY
    "angle_snap": 175,       # [value, comp] 0/1
    "ripple": 177,           # [value, comp] 0/1
    "moving_off_light": 179,  # [value, comp] 0/1
    "performance_state": 181,  # [value, comp] 0/1
    "performance": 183,      # [value, comp] level (default 6)
    "sensor_mode": 185,      # [value, comp] enum
    "angle_tune": 189,       # [value, comp] signed int8 degrees -30..30
    "angle_tune_state": 191,  # [value, comp] 0/1
    "sensor_fps_20k": 225,   # [value, comp] 0/1
    "wheel_debounce_time": 227,   # [value, comp] ms
    "debounce_release_time": 229,  # [value, comp] ms
    "shortcut_key": 256,     # 32 bytes/button keystroke record
    "macro": 768,            # 384 bytes/macro slot
    "sensor_3955_dpi": 6912,  # 6 bytes/stage (3955 sensor only)
}

# Region the official driver bulk-reads at connect (xr(0, 256)).
PRIMARY_REGION_LEN = 256

# Button param is a 16-bit big-endian field; the mouse-button bitmask lives in
# the high byte (verified against a real dump: button 0 = 0x0100 = left click).
MOUSE_BUTTON_MASK = {
    0x0100: "left", 0x0200: "right", 0x0400: "middle",
    0x0800: "side1", 0x1000: "side2",
}

# Sensor 3950: actual DPI = (raw_index + 1) * DPI_STEP (validated on hardware:
# raw 0x1f -> 1600, 0x3f -> 3200, 0x7f -> 6400).
DPI_STEP = 50

# Button function-type enum (`Ro` / MouseKeyFunction).
KEY_FUNCTION_TYPES = {
    0: "disable", 1: "mouse_key", 2: "dpi_switch", 3: "left_right_roll",
    4: "fire_key", 5: "shortcut_key", 6: "macro", 7: "report_rate_switch",
    8: "light_switch", 9: "profile_switch", 10: "dpi_lock", 11: "up_down_roll",
    256: "left_key",
}

# Lighting-mode editability (`Gh`): which of color/brightness/speed apply.
LIGHT_MODE_CAPS = {
    0: ("color", "brightness", "speed"), 1: ("color",), 2: (),
    3: ("speed",), 4: ("color",), 5: ("color",), 6: ("color", "speed"),
}

# --- report-rate codec (mouse path v5 encode / Kh decode) ---
# Stored byte at offset 0 (low byte of the [value, comp] pair).
_RATE_TO_FLASH = {125: 8, 250: 4, 500: 2, 1000: 1, 2000: 16, 4000: 32, 8000: 64}
_FLASH_TO_RATE = {v: k for k, v in _RATE_TO_FLASH.items()}


def report_rate_to_flash(hz: int) -> int:
    if hz in _RATE_TO_FLASH:
        return _RATE_TO_FLASH[hz]
    return int(1000 / hz) if hz <= 1000 else int(hz / 2000 * 16)


def report_rate_from_flash(v: int) -> int:
    if v in _FLASH_TO_RATE:
        return _FLASH_TO_RATE[v]
    return int(v / 16 * 2000) if v >= 16 else (int(1000 / v) if v else 0)


# --- DPI-effect brightness codec (o6 encode / s6 decode), UI level 1..10 ---
def dpi_brightness_to_flash(level: int) -> int:
    return {1: 16, 5: 128, 9: 230, 10: 255}.get(level, 30 * (level - 1))


def dpi_brightness_from_flash(b: int) -> int:
    if b and b % 30 == 0:
        return b // 30 + 1
    return {16: 1, 128: 5, 230: 9, 255: 10}.get(b, 5)


class FlashImage:
    """A mutable view over the config flash bytes with typed accessors."""

    def __init__(self, data: bytes | bytearray):
        self.data = bytearray(data)

    def __len__(self) -> int:
        return len(self.data)

    # --- raw access ---
    def u8(self, off: int) -> int:
        return self.data[off]

    def set_u8(self, off: int, val: int) -> None:
        self.data[off] = val & 0xFF

    def u16be(self, off: int) -> int:
        return (self.data[off] << 8) | self.data[off + 1]

    def color(self, off: int) -> tuple[int, int, int]:
        return self.data[off], self.data[off + 1], self.data[off + 2]

    def set_color(self, off: int, rgb: tuple[int, int, int]) -> None:
        self.data[off:off + 3] = bytes(rgb)

    # --- complement-pair settings (the `st` primitive) ---
    def setting(self, off: int) -> int:
        """Value byte of a [value, complement] pair."""
        return self.data[off]

    def setting_valid(self, off: int) -> bool:
        return (self.data[off] + self.data[off + 1]) & 0xFF == COMPLEMENT_BASE

    def set_setting(self, off: int, val: int) -> None:
        """Write a [value, complement] pair as the firmware expects."""
        self.data[off] = val & 0xFF
        self.data[off + 1] = (COMPLEMENT_BASE - val) & 0xFF

    def signed_setting(self, off: int) -> int:
        v = self.data[off]
        return v - 256 if v >= 128 else v

    def set_signed_setting(self, off: int, val: int) -> None:
        self.set_setting(off, val + 256 if val < 0 else val)

    # --- typed settings (high-confidence subset) ---
    @property
    def report_rate_hz(self) -> int:
        return report_rate_from_flash(self.setting(OFFSETS["report_rate"]))

    @report_rate_hz.setter
    def report_rate_hz(self, hz: int) -> None:
        self.set_setting(OFFSETS["report_rate"], report_rate_to_flash(hz))

    @property
    def max_dpi_stage(self) -> int:
        return self.setting(OFFSETS["max_dpi_stage"])

    @property
    def current_dpi_stage(self) -> int:
        return self.setting(OFFSETS["current_dpi"])

    def _bool_prop(name, off_key):  # noqa: N805 - descriptor factory
        def getter(self):
            return bool(self.setting(OFFSETS[off_key]))

        def setter(self, on):
            self.set_setting(OFFSETS[off_key], 1 if on else 0)
        return property(getter, setter, doc=name)

    motion_sync = _bool_prop("motion sync", "motion_sync")
    angle_snap = _bool_prop("angle snap", "angle_snap")
    ripple_control = _bool_prop("ripple control", "ripple")
    moving_off_light = _bool_prop("light off while moving", "moving_off_light")
    sensor_fps_20k = _bool_prop("20K FPS mode", "sensor_fps_20k")
    light_enabled = _bool_prop("RGB on/off", "light_enable")
    del _bool_prop

    @property
    def angle_tune_deg(self) -> int:
        # Optional fields read 0xFF (uninitialized) on devices that never set
        # them; the complement check separates a real value from flash garbage.
        if not self.setting_valid(OFFSETS["angle_tune"]):
            return 0
        return self.signed_setting(OFFSETS["angle_tune"])

    @angle_tune_deg.setter
    def angle_tune_deg(self, deg: int) -> None:
        self.set_signed_setting(OFFSETS["angle_tune"], deg)

    # --- lighting (7-byte block at 160 + enable at 167) ---
    @property
    def light_mode(self) -> int:
        return self.u8(OFFSETS["light_mode"])

    @property
    def light_color(self) -> tuple[int, int, int]:
        return self.color(OFFSETS["light_color"])

    @property
    def light_speed(self) -> int:
        return min(self.u8(OFFSETS["light_speed"]), 9)

    @property
    def light_brightness(self) -> int:
        return min(self.u8(OFFSETS["light_brightness"]), 9)

    # --- 4-byte structured entries (byte 3 = 0x55 - sum of first 3) ---
    @staticmethod
    def entry_checksum(b0: int, b1: int, b2: int) -> int:
        return (COMPLEMENT_BASE - (b0 + b1 + b2)) & 0xFF

    def write_entry(self, off: int, b0: int, b1: int, b2: int) -> None:
        self.data[off:off + 4] = bytes(
            [b0, b1, b2, self.entry_checksum(b0, b1, b2)])

    # --- DPI ---
    def dpi_stage_raw(self, stage: int) -> bytes:
        off = OFFSETS["dpi_value"] + stage * 4
        return bytes(self.data[off:off + 4])

    def dpi_xy(self, stage: int) -> tuple[int, int]:
        """Per-stage (x, y) DPI. Resolution flags select a multiplier the sensor
        table defines; for the common no-flag case DPI = (index+1)*DPI_STEP."""
        off = OFFSETS["dpi_value"] + stage * 4
        x_lo, y_lo, bits = self.data[off], self.data[off + 1], self.data[off + 2]
        x = x_lo + (((bits >> 2) & 3) << 8)
        y = y_lo + (((bits >> 6) & 3) << 8)
        return (x + 1) * DPI_STEP, (y + 1) * DPI_STEP

    def dpi_color(self, stage: int) -> tuple[int, int, int]:
        return self.color(OFFSETS["dpi_color"] + stage * 4)

    def set_dpi_color(self, stage: int, rgb: tuple[int, int, int]) -> None:
        self.write_entry(OFFSETS["dpi_color"] + stage * 4, *rgb)

    # --- buttons ---
    def key_function(self, button: int) -> tuple[int, int]:
        """(function_type, param) for a button's 4-byte KeyFunction entry."""
        off = OFFSETS["key_function"] + button * 4
        return self.u8(off), self.u16be(off + 1)

    def diff(self, other: "FlashImage") -> list[tuple[int, int, int]]:
        """(offset, self_byte, other_byte) where the two images differ."""
        n = min(len(self.data), len(other.data))
        return [(i, self.data[i], other.data[i])
                for i in range(n) if self.data[i] != other.data[i]]
