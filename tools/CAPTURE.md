# Capture checklist (optional cross-check)

The protocol is already decoded statically (see `../PROTOCOL.md`), so this is only
for **validating** edge cases / VERIFY items against the official driver.

## Setup
1. Install `webhid-logger.user.js` in Tampermonkey (it matches `scyrox.net`).
2. Open https://www.scyrox.net/ in a WebHID browser (Chrome/Brave), connect the mouse.
3. Open DevTools console to watch `[HID]` lines.

## Per-setting capture
For each setting you want to confirm, isolate it:
1. `Ctrl+Shift+K` — clear the buffer.
2. Change exactly **one** setting in the UI (e.g. polling rate 1000 → 8000).
3. `Ctrl+Shift+L` — downloads `scyrox-hid-<ts>.json`.

The log annotates each `WriteFlashData`/`ReadFlashData` frame with its flash
`addr`/`len`/`data`, so a single change shows up as one or a few writes at a known
offset — directly confirming the offset and encoding in `PROTOCOL.md`.

## Highest-value items to confirm
- **LOD**, **DebounceTime**, **SleepTime** units (offsets 10 / 169 / 173).
- **Per-stage DPI** encoding (offset 12) vs the displayed DPI — to derive the live
  sensor table.
- That a setting change is a **single `WriteFlashData`** (autocommit, no save cmd).

Hand the JSON files to the tooling and they'll be diffed against `scyrox dump`.
