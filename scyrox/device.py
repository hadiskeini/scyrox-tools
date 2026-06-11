"""Device discovery + open convenience.

Prefers hidapi (cross-platform). Falls back to Linux /dev/hidraw via stdlib so the
tool still works with no third-party package on the same box as the daemon.
"""

from __future__ import annotations

import os
import sys

from .protocol import (
    VID, PID_WIRED, KNOWN_PIDS, REPORT_ID,
    HidApiTransport, HidrawTransport, ScyroxDevice,
)


def _have_hidapi() -> bool:
    try:
        import hid  # noqa: F401
        return True
    except Exception:
        return False


# --- Linux hidraw discovery (stdlib; mirrors the daemon) ---

def _report_ids(descriptor_path: str) -> set[int]:
    try:
        with open(descriptor_path, "rb") as f:
            d = f.read()
    except OSError:
        return set()
    ids, i = set(), 0
    while i < len(d):
        prefix = d[i]
        if prefix == 0xFE:
            if i + 2 >= len(d):
                break
            i += 3 + d[i + 1]
            continue
        size = prefix & 0x03
        if size == 3:
            size = 4
        if prefix & 0xFC == 0x84 and size >= 1 and i + 1 < len(d):
            ids.add(d[i + 1])
        i += 1 + size
    return ids


def _hidraw_candidates() -> list[tuple[str, int]]:
    """[(/dev/hidrawN, pid)] for Scyrox interfaces speaking report 0x08.

    Wired sorts first (matches the daemon's preference)."""
    out = []
    base = "/sys/class/hidraw"
    if not os.path.isdir(base):
        return out
    for entry in sorted(os.listdir(base)):
        sysdir = f"{base}/{entry}/device"
        try:
            with open(f"{sysdir}/uevent") as f:
                props = dict(ln.strip().split("=", 1) for ln in f if "=" in ln)
        except OSError:
            continue
        hid_id = props.get("HID_ID", "").upper()
        pid = next((p for p in KNOWN_PIDS
                    if hid_id == f"0003:{VID:08X}:{p:08X}"), None)
        if pid is None or not props.get("HID_PHYS"):
            continue
        if REPORT_ID not in _report_ids(f"{sysdir}/report_descriptor"):
            continue
        out.append((f"/dev/{entry}", pid))
    out.sort(key=lambda c: (c[1] != PID_WIRED, c[0]))
    return out


def list_devices() -> list[dict]:
    """Describe attached Scyrox config interfaces, from whichever backend works."""
    if _have_hidapi():
        devs = HidApiTransport.enumerate()
        # vendor collection (report id 8) shows up as usage_page 0xFF.. — but
        # hidapi can't read report ids, so filter on usage_page when present.
        picked = [d for d in devs
                  if d.get("usage_page", 0) >= 0xFF00] or devs
        return [{"backend": "hidapi", "path": d["path"],
                 "pid": d["product_id"],
                 "product": d.get("product_string")} for d in picked]
    return [{"backend": "hidraw", "path": p, "pid": pid, "product": None}
            for p, pid in _hidraw_candidates()]


def _open_path(path) -> ScyroxDevice:
    """Open one interface with whichever backend is active."""
    if _have_hidapi():
        return ScyroxDevice(HidApiTransport(
            path=path.encode() if isinstance(path, str) else path))
    return ScyroxDevice(HidrawTransport(
        path.decode() if not isinstance(path, str) else path))


def open_device(path=None, probe: bool = True) -> ScyroxDevice:
    """Open a Scyrox config interface.

    With no explicit path, probe every candidate (the 8K dongle can expose the
    vendor report id on several nodes) and return the first that actually answers
    a flash read — so we don't depend on guessing the right /dev/hidrawN."""
    if path is not None:
        return _open_path(path)
    cands = [d["path"] for d in list_devices()]
    if not cands:
        raise RuntimeError(
            "no Scyrox device found "
            "(on Linux without the `hid` package, HID access needs root)")
    last = None
    for p in cands:
        dev = _open_path(p)
        if not probe or dev.read_flash_chunk(0, 2) is not None:
            return dev
        dev.close()
        last = dev
    raise RuntimeError(
        "Scyrox interface(s) found but none answered a flash read — the mouse may "
        "be asleep (wake/move it; on a dongle the mouse must be powered on), or "
        "you may lack permission on /dev/hidrawN (try sudo).")
