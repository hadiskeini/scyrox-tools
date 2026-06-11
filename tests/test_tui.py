"""Headless TUI tests (Textual Pilot) driving the real app against a fake device.

Requires the `tui` extra (textual) + pytest-asyncio. Skipped if textual is absent.
"""

import os
import pytest

textual = pytest.importorskip("textual")

from scyrox import protocol as P
from scyrox.flash import FlashImage, OFFSETS
from scyrox.tui import ScyroxTUI


def _real_dump():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "real_dump_3950.hex")
    with open(path) as f:
        return bytes(int(x, 16) for x in f.read().split())


class FakeRWTransport(P.Transport):
    """Read/write/battery-capable fake device backed by an in-memory flash."""

    def __init__(self, flash):
        self.flash = bytearray(flash)
        self._pending = None

    def write(self, frame16):
        cmd = frame16[0]
        out = bytearray(P.RESP_LEN)
        out[0], out[1] = P.REPORT_ID, cmd
        if cmd == P.Cmd.READ_FLASH:
            addr = (frame16[2] << 8) | frame16[3]
            n = frame16[4] & 0x0F
            out[3], out[4], out[5] = frame16[2], frame16[3], n
            out[6:6 + n] = self.flash[addr:addr + n]
        elif cmd == P.Cmd.WRITE_FLASH:
            addr = (frame16[2] << 8) | frame16[3]
            n = frame16[4] & 0x0F
            self.flash[addr:addr + n] = frame16[5:5 + n]   # persist
            out[3], out[4], out[5] = frame16[2], frame16[3], n
        elif cmd == P.Cmd.BATTERY_LEVEL:
            out[6], out[7] = 88, 0
        self._pending = bytes(out)

    def read(self, timeout=1.0):
        r, self._pending = self._pending, None
        return r

    def close(self):
        pass


def _make_app(tmp_path):
    fake = FakeRWTransport(_real_dump())
    app = ScyroxTUI(
        opener=lambda path: P.ScyroxDevice(fake),
        lister=lambda: [{"path": "fake", "pid": 0xF637, "backend": "fake",
                         "product": "SCYROX"}],
        backup_path=str(tmp_path / "tui-backup.bin"),
    )
    return app, fake


def _text(app, selector):
    """Rendered text of a Static, across Textual versions."""
    w = app.query_one(selector)
    for attr in ("renderable", "_content"):
        if hasattr(w, attr):
            return str(getattr(w, attr))
    return str(w.render())


async def test_tui_loads_and_populates(tmp_path):
    app, _fake = _make_app(tmp_path)
    async with app.run_test():
        from textual.widgets import Select, Switch
        assert app.query_one("#report_rate", Select).value == 1000
        assert app.query_one("#t_motion_sync", Switch).value is True   # from dump
        assert app.original is not None and app.dev is not None        # connected
        assert app.original.dpi_xy(0) == (1600, 1600)
        assert app._diff() == []                                       # no phantom edits
        assert "1600" in _text(app, "#ro_body")                        # DPI rendered


async def test_tui_edit_and_apply_writes_device(tmp_path):
    app, fake = _make_app(tmp_path)
    async with app.run_test() as pilot:
        from textual.widgets import Switch
        app.query_one("#t_motion_sync", Switch).value = False  # toggle off
        await pilot.pause()
        assert len(app._diff()) > 0                            # change pending
        await pilot.click("#apply")                            # apply
        await pilot.pause()
    off = OFFSETS["motion_sync"]
    assert fake.flash[off] == 0                                # written to device
    assert (fake.flash[off] + fake.flash[off + 1]) & 0xFF == 0x55   # valid complement
    assert os.path.exists(tmp_path / "tui-backup.bin")         # backed up first


async def test_tui_apply_with_no_changes(tmp_path):
    app, fake = _make_app(tmp_path)
    before = bytes(fake.flash)
    async with app.run_test() as pilot:
        assert app._diff() == []          # loading created no phantom changes
        app.action_apply()
        await pilot.pause()
        assert "Nothing to apply" in _text(app, "#status")
    assert bytes(fake.flash) == before   # untouched
