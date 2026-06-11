# Scyrox HID protocol

Reverse-engineered from the official web driver bundle
(`https://www.scyrox.net/assets/index-*.js`, a single Vue SPA file) and
cross-checked by adversarial verification of each subsystem against the source.
Confidence is **high** unless a line says VERIFY (decoded, but a real-dump
confirmation of units/range is wanted).

Device: VID `0x3554`, PIDs `0xF5F7` (dongle) / `0xF5F6` (wired) / `0xF637` ("8K
Dongle"). Sensor `3950` (some SKUs `3955`), MCU `NRF52833`.

## Wire frame

Every command is a 17-byte HID output report. WebHID prepends the report id, so the
driver builds a 16-byte buffer and calls `sendReport(8, buf)`.

```
byte  0      command code (see Commands)
byte  1..3   0
byte  4      payload length + type offset  (so(): mouse=0, keyboard=128)
byte  5..14  payload (≤10 bytes)
byte  15     checksum
```

**Report id** = `8` (`wn`). **Checksum** (`Mt` + `-wn`), byte-identical to the
daemon's `_crc`:
```
byte15 = (0x55 - (sum(buf[0..14]) & 0xFF) - 8) & 0xFF
```
(The bundle's `Mt` returns `0x55 - (sum & 0xFF)` unmasked; the final `& 0xFF`
happens implicitly when stored into the byte — our explicit mask matches.)

Builders: `Mr(cmd, payload)` (sets byte4 = len + so()), `Ra(cmd)` (no payload),
`Ks(frame)` = send + wait for response with ≤5 retries. **Ks success**: the device
echoes the request header — 3 bytes for writes, **5 bytes (0..4) for reads
(cmd 8)**; also `We[1]===1` short-circuits to success. Responses land in `We`.

## No handshake / no encryption gate

`EncryptionData` (cmd 1) is **identity only** — `ai()` sends 4 random + 4 zero bytes;
the reply yields `cid/mid/type` (used to label the device and cap report rate). The
driver has no `needEncrypt`/`isEncrypt` flag and never encrypts any command.
**A standalone tool can send `ReadFlashData`/`WriteFlashData` cold** with just the
correct report id + checksum. `PCDriverStatus` (2), `DeviceOnLine` (3),
`GetCurrentConfig` (14), `ReadVersionID` (18) are UI priming, not read preconditions.

`type` (response `We[11]`) → report-rate cap: `0`→1000, `1`→4000, `2`→1000 (wired),
`3`→8000 (wired), `4`→2000, `5`→8000.

## Commands (`Je`)

| code | name | notes |
|----:|------|------|
| 1 | EncryptionData | identity handshake (not crypto) |
| 2 | PCDriverStatus | byte5: 1=driver present, 0=leaving (response is a no-op) |
| 3 | DeviceOnLine | `We[5]==1` online; `We[6..8]`=addr — **daemon uses this** |
| 4 | BatteryLevel | `We[6]`=level, `We[7]`=charging, `We[8..9]`=mV — **daemon uses this** |
| 5/6 | DongleEnterPair / GetPairState | pairing |
| 7 | WriteFlashData | **persistent write; autocommits, no separate save** |
| 8 | ReadFlashData | read a chunk (also the `dn(addr,len)` read-request builder) |
| 9 | ClearSetting | factory reset; then poll StatusChanged; gated on `!Fn` |
| 10 | StatusChanged | async device notification (e.g. reset done) |
| 14/15 | Get/SetCurrentConfig | profile index (`We[5]`); SetCurrentConfig re-reads flash |
| 18 | ReadVersionID | `v{We[5]}.{hex(We[6])}` |
| 20/21/24/25/44/45 | dongle RGB get/set | 4K / RGB-bar / 3-RGB modes |
| 22/23 | Set/GetLongRangeMode | long-distance 2.4G |
| 29 | GetDongleVersion | |

## Flash addressing

Config lives in a 16 KB image (mirror `ne`). Read and write are addressed and
paged at **≤10 bytes/chunk** (payload occupies bytes 5..14):

```
ReadFlashData(8) / WriteFlashData(7) request:
  byte0 = cmd
  byte2 = addr >> 8 ;  byte3 = addr & 0xFF      # 16-bit big-endian
  byte4 = count + so()                          # so()=0 (mouse), count ≤ 10
  byte5.. = data (writes only)
Response (report id at [0] shifts everything +1):
  addr = (We[3] << 8) | We[4] ;  count = We[5] & 0x0F ;  data = We[6 : 6+count]
```
Read verify: the device echoes request bytes 0..4; the driver advances `addr += 10`
on a 5-byte header match, retries up to 5×. Connect-time bulk read = `xr(0, 256)`.

## Complement-pair settings (the `st` primitive)

Most single settings are a **2-byte `[value, 0x55-value]` pair**. The firmware writes
the complement; the driver trusts a field only when `(value + comp) & 0xFF == 0x55`.
`st(off, v)` = `WriteFlashData` with payload `[v, 0x55-v]`. Signed fields (AngleTune)
store two's-complement in the value byte. **Writes must emit the complement** — the
library's `write_setting` / `FlashImage.set_setting` do this.

This is confirmed by a real captured dump: offset 0 = `[1, 84]` → value 1
(=1000 Hz), complement `0x55-1 = 84`.

**4-byte structured entries** (DPI value, DPI color, KeyFunction) take the same
idea: byte 3 = `(0x55 - sum(byte0..byte2)) & 0xFF`. Verified on hardware, e.g. DPI
stage `1f 1f 00 17` → `0x55-0x3e = 0x17`. The 7-byte Light block's byte 6 is the
same `Mt` checksum over its first 6 bytes. **Uninitialized fields read `0xFF`** —
the complement/checksum is invalid, which is how a real value is told from flash
garbage (the library gates optional fields like `angle_tune` on this).

## Flash layout (`Se`) + codecs

| offset | setting | size | encoding |
|------:|---------|-----|----------|
| 0 | report_rate | [v,comp] | byte: 125→8,250→4,500→2,1000→1,**2000→16,4000→32,8000→64**. enc `≤1000:1000/hz else hz/2000*16`; dec `≥16:b/16*2000 else 1000/b` |
| 2 | max_dpi_stage | [v,comp] | active stage count |
| 4 | current_dpi | [v,comp] | active stage index (**u8**, not u16) |
| 8 | key_operation | [v,comp] | bit0=left active, bit1=right active (also `Scyrox20K` reuses 8) |
| 10 | lod | [v,comp] | enum per sensor (LODOptions[type]) — VERIFY units |
| 12 | dpi_value | 8×4 | `[xLow, yLow, hiBits, crc]`; `aX = xLow + ((hiBits>>2 & 3)<<8)`, `aY = yLow + ((hiBits>>6 & 3)<<8)`. **Sensor 3950: DPI = (a+1)×50** (verified: 0x1f→1600, 0x3f→3200, 0x7f→6400). Res-flag multipliers extend the range. 3955 sensor uses 6912, 6 bytes/stage |
| 44 | dpi_color | 8×4 | `[R, G, B, crc]` per stage (crc = 0x55−sum) |
| 76 | dpi_effect_mode | 1 | enum (read as 8-byte block w/ 78/80/82) |
| 78 | dpi_effect_brightness | 1 | UI 1..10 via lut: 1→16,5→128,9→230,10→255, else 30*(lvl-1) |
| 80 | dpi_effect_speed | 1 | raw |
| 82 | dpi_effect_state | 1 | 0/1 |
| 96 | key_function | 4/btn | `[type, paramHi, paramLo, crc]` (see Buttons) |
| 160 | light | 7 | `[mode,R,G,B,speed,bright,blkcrc]`; blkcrc=`Mt` of bytes 0..5 |
| 167 | light_enable | [v,comp] | 1=on, 0=off (absolute literal, not in `Se`) |
| 169 | debounce_time | [v,comp] | ms — VERIFY |
| 171 | motion_sync | [v,comp] | 0/1 |
| 173 | sleep_time | [v,comp] | minutes? — VERIFY |
| 175 | angle_snap | [v,comp] | 0/1 |
| 177 | ripple | [v,comp] | 0/1 |
| 179 | moving_off_light | [v,comp] | 0/1 |
| 181 | performance_state | [v,comp] | 0/1 |
| 183 | performance | [v,comp] | level (default 6) |
| 185 | sensor_mode | [v,comp] | enum (SensorModeOptions index) |
| 189 | angle_tune | [v,comp] | signed int8, −30..+30° |
| 191 | angle_tune_state | [v,comp] | 0/1 |
| 225 | sensor_fps_20k | [v,comp] | 0/1 |
| 227 | wheel_debounce_time | [v,comp] | ms |
| 229 | debounce_release_time | [v,comp] | ms |
| 256 | shortcut_key | 32/btn | keystroke record (see Buttons) |
| 768 | macro | 384/slot | macro storage (see Macros) |
| 6912 | sensor_3955_dpi | 6/stage | 3955-sensor DPI block |

**Lighting**: mode 0 = color+brightness+speed editable; 1/4/5 = color only;
6 = color+speed; 3 = speed only; 2 = none (`Gh`). speed/brightness clamp 0..9.
`Light.color` is 3 raw RGB bytes (`Buffer_To_Color` returns the `rgb(r,g,b)` string).

## Buttons (`KeyFunction` @96, `ShortcutKey` @256, `Macro` @768)

6 buttons (default; 8 slots reserved), 4 bytes each: `[type, paramHi, paramLo, crc]`,
param big-endian. Function-type enum `Ro`:
`Disable:0, MouseKey:1, DPISwitch:2, LeftRightRoll:3, FireKey:4, ShortcutKey:5,
Macro:6, ReportRateSwitch:7, LightSwitch:8, ProfileSwitch:9, DPILock:10,
UpDownRoll:11, LeftKey:256`.

- **MouseKey (1)**: param (16-bit BE) = button bitmask in the **high byte** —
  `0x0100=L, 0x0200=R, 0x0400=Mid, 0x0800=side1, 0x1000=side2` (verified: button 0
  = `01 01 00 53`). The `crc` byte (byte 3) = `0x55 − sum(type+paramHi+paramLo)`.
- **ShortcutKey (5)/FireKey (4)**: real data in the 32-byte record at `256 + btn*32`.
  byte0 = count×2, then count press-frames + count release-frames, 3 bytes each:
  press `[type|0x80, val&0xFF, val>>8]`, release `[type|0x40, ...]`; trailing 0 + `Mt`.
  Media key: `[2, 0x82, lo, hi, 0x42, lo, hi, 0, crc]`. Keyboard usage table `Ur`:
  type 0 = modifier bitmask (LCtrl1/LShift2/LAlt4/LWin8/RCtrl16/…), type 1 = HID usage.
- **Macro (6)**: param low byte (`paramLo`) = cycle/repeat count; body at `768 + btn*384`.
- **DPILock (10)**: param via `E0()`/`S0()` sensor DPI table.

## Macros (`@768`, 384 bytes/slot)

`base = 768 + index*384`. Layout: name-len `[0]` (≤30), UTF-8 name `[1..30]`,
event-count `[31]` (≤70), then 5-byte events from `[32]`:
```
event[0] = (status_code << 6) | (type & 0x0F)   # status 2→down(0), 1→up(1)
event[1..2] = value  (little-endian uint16, the keycode)
event[3..4] = delay  (big-endian uint16, ms)
```
then a trailing checksum = `Mt(eventbuf) - eventCount` (mouse path `u6`). (The
keyboard build uses a different serializer `oy` that leads with count and doesn't
subtract — not used for mice.)

## Commit / verify / reset

- **No commit command** — `WriteFlashData` autocommits per chunk. The driver mirrors
  bytes into `ne` only when every chunk's `Ks` succeeded.
- **Verify-on-read**: re-read and compare the echoed 5-byte header (≤5 retries).
- **Factory reset** `ClearSetting(9)`: `[9,0,0,0, so()=0, …, crc]`, gated on `!Fn`,
  then poll `StatusChanged` (~20×300 ms) until `isRestoring` clears, then re-read.
- **Profiles**: `SetCurrentConfig(15)` payload `[idx]`, then full re-read (`!Fn` only).
- `SyncCRC`/`*_flash_map` (localStorage per-chunk CRC) is a **keyboard-only** caching
  optimization (`ve.SyncCRC=9504`); not used for mice — ignore for this project.

## Validated against hardware (3950 sensor dump)

Frame/CRC, complement pairs, 4-byte-entry checksums, report-rate codec, DPI
formula `(a+1)×50`, DPI colors, the mouse-button bitmask, and the Light block +
its checksum are all confirmed against a real dump (see
`tests/fixtures/real_dump_3950.hex`).

## Remaining VERIFY items

1. `lod` / `debounce_time` / `sleep_time` are raw ints (storage confirmed); their
   value→unit *labels* (mm / ms / minutes) live in the site's i18n, not the logic.
   This dump had lod=3, debounce=0, sleep=1.
2. DPI res-flag multipliers (`hiBits` nonzero) for the extreme DPI ranges — this
   dump only exercised the ×1 case.
3. `performance` / `sensor_mode` enum value→label mapping (i18n only).
