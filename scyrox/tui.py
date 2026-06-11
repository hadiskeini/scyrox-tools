"""Scyrox configuration TUI (Textual).

Runs unprivileged on Linux/macOS. Reads the config flash into a working
`FlashImage`; edits mutate the working copy; Apply writes only the changed
complement-pair settings (after a backup), so it is safe and explicit.

The high-confidence complement-pair settings are editable. DPI stages and the
lighting block are shown read-only pending live-sensor/units validation
(see PROTOCOL.md VERIFY items) — they decode cleanly but their write paths need
the per-sensor table / block checksum, which we add once confirmed on hardware.

Run:  scyrox-tui   (or: python -m scyrox.tui)   — needs the `tui` extra.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button, Footer, Header, Input, Label, Select, Static, Switch,
)

from .device import open_device, list_devices
from .flash import (
    FlashImage, OFFSETS, PRIMARY_REGION_LEN, LIGHT_MODE_CAPS,
)

REPORT_RATES = [125, 250, 500, 1000, 2000, 4000, 8000]

# Editable complement-pair toggles: (label, flash-key, FlashImage attribute).
TOGGLES = [
    ("Motion sync", "motion_sync", "motion_sync"),
    ("Angle snap", "angle_snap", "angle_snap"),
    ("Ripple control", "ripple", "ripple_control"),
    ("Light off while moving", "moving_off_light", "moving_off_light"),
    ("20K FPS mode", "sensor_fps_20k", "sensor_fps_20k"),
    ("RGB lighting on", "light_enable", "light_enabled"),
]


class ScyroxTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #status { height: auto; padding: 0 1; color: $text-muted; }
    .panel { border: round $primary; padding: 0 1; margin: 1 1; height: auto; }
    .panel > Label { text-style: bold; }
    .row { height: auto; }
    .row > Label { width: 28; padding: 1 0; }
    #actions { height: auto; padding: 0 1; }
    #pending { color: $warning; padding: 0 1; }
    """
    BINDINGS = [
        ("r", "reload", "Reload"),
        ("a", "apply", "Apply"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, path=None, opener=None, lister=None, backup_path=None):
        super().__init__()
        self.path = path
        # Injectable for testing (default to the real device functions).
        self._opener = opener or open_device
        self._lister = lister or list_devices
        self.backup_path = backup_path or "scyrox-tui-backup.bin"
        self.dev = None
        self.original: FlashImage | None = None   # last-read device state
        self.work: FlashImage | None = None        # edited working copy

    # --- layout ---
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("Connecting…", id="status")
        with VerticalScroll():
            with Vertical(classes="panel", id="perf"):
                yield Label("Performance")
                with Horizontal(classes="row"):
                    yield Label("Polling rate (Hz)")
                    yield Select([(str(r), r) for r in REPORT_RATES],
                                 id="report_rate", allow_blank=False)
                with Horizontal(classes="row"):
                    yield Label("Angle tune (°, -30..30)")
                    yield Input(id="angle_tune", restrict=r"-?\d*")
            with Vertical(classes="panel", id="toggles"):
                yield Label("Toggles")
                for label, key, _attr in TOGGLES:
                    with Horizontal(classes="row"):
                        yield Label(label)
                        yield Switch(id=f"t_{key}")
            with Vertical(classes="panel", id="readonly"):
                yield Label("Read-only (validation pending)")
                yield Static(id="ro_body")
        yield Static("", id="pending")
        with Horizontal(id="actions"):
            yield Button("Reload (r)", id="reload")
            yield Button("Apply (a)", id="apply", variant="primary")
        yield Footer()

    # --- lifecycle ---
    def on_mount(self) -> None:
        self.load_from_device()

    def load_from_device(self) -> None:
        status = self.query_one("#status", Static)
        try:
            if self.dev is None:
                if not self._lister():
                    status.update("No Scyrox device found. Connect it and press r.")
                    return
                self.dev = self._opener(self.path)
            data = self.dev.read_flash(0, PRIMARY_REGION_LEN)
            batt = self.dev.battery()
        except Exception as e:  # noqa: BLE001 - surface any device error in the UI
            status.update(f"Device error: {e}")
            return
        self.original = FlashImage(data)
        b = (f"  battery {batt['level']}% "
             f"{'⚡' if batt['charging'] else ''}") if batt else ""
        status.update(f"Connected.{b}")
        self.refresh_widgets()

    def refresh_widgets(self) -> None:
        # Widgets reflect the device baseline. They are the source of truth for
        # edits; the working image is *derived* from them on demand (see
        # current_image), so stray init events can't create phantom changes.
        w = self.original
        with self.prevent(Select.Changed, Switch.Changed, Input.Changed):
            self.query_one("#report_rate", Select).value = w.report_rate_hz
            self.query_one("#angle_tune", Input).value = str(w.angle_tune_deg)
            for _label, key, attr in TOGGLES:
                self.query_one(f"#t_{key}", Switch).value = bool(getattr(w, attr))
        caps = ", ".join(LIGHT_MODE_CAPS.get(w.light_mode, ())) or "none"

        def _stage(i):
            x, y = w.dpi_xy(i)
            d = f"{x}" if x == y else f"{x}x{y}"
            return f"    stage {i}: {d} dpi  color {w.dpi_color(i)}"
        stages = "\n".join(_stage(i) for i in range(min(w.max_dpi_stage, 8) or 1))
        self.query_one("#ro_body", Static).update(
            f"DPI: {w.max_dpi_stage} stages, active #{w.current_dpi_stage}\n"
            f"{stages}\n"
            f"Lighting: {'on' if w.light_enabled else 'off'}, mode {w.light_mode} "
            f"(editable: {caps}), "
            f"color {w.light_color}, speed {w.light_speed}, "
            f"brightness {w.light_brightness}\n"
            f"LOD raw {w.setting(OFFSETS['lod'])}, "
            f"debounce raw {w.setting(OFFSETS['debounce_time'])}, "
            f"sleep raw {w.setting(OFFSETS['sleep_time'])}")
        self.update_pending()

    def current_image(self) -> "FlashImage":
        """The image implied by the current widget values, atop the baseline.

        Only controls the user actually changed rewrite bytes — a control left
        at its loaded value leaves the original bytes untouched (so unset 0xFF
        fields aren't normalized into spurious writes)."""
        img = FlashImage(bytes(self.original.data))
        o = self.original
        sel = self.query_one("#report_rate", Select).value
        if isinstance(sel, int) and sel != o.report_rate_hz:
            img.report_rate_hz = sel
        try:
            a = max(-30, min(30, int(self.query_one("#angle_tune", Input).value or 0)))
            if a != o.angle_tune_deg:
                img.angle_tune_deg = a
        except ValueError:
            pass
        for _label, key, attr in TOGGLES:
            v = self.query_one(f"#t_{key}", Switch).value
            if v != bool(getattr(o, attr)):
                setattr(img, attr, v)
        return img

    # --- edits just trigger a recompute; values are read from the widgets ---
    def on_select_changed(self, e: Select.Changed) -> None:
        self.update_pending()

    def on_switch_changed(self, e: Switch.Changed) -> None:
        self.update_pending()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        self.update_pending()

    def _diff(self):
        if not self.original:
            return []
        return self.original.diff(self.current_image())

    def update_pending(self) -> None:
        if not self.original:
            return
        n = len(self._diff())
        self.query_one("#pending", Static).update(
            "No pending changes." if not n else f"{n} byte(s) pending — press a to apply.")

    # --- actions ---
    def on_button_pressed(self, e: Button.Pressed) -> None:
        if e.button.id == "reload":
            self.action_reload()
        elif e.button.id == "apply":
            self.action_apply()

    def action_reload(self) -> None:
        self.load_from_device()

    def action_apply(self) -> None:
        if not self.dev or not self.original:
            return
        work = self.current_image()
        changed = self.original.diff(work)
        if not changed:
            self.query_one("#status", Static).update("Nothing to apply.")
            return
        # Only known complement-pair offsets are writable here; each changed
        # value byte is rewritten with its complement via write_setting.
        writable = {OFFSETS[k] for k in (
            "report_rate", "angle_tune", *[k for _l, k, _a in TOGGLES])}
        offsets = sorted({off for off, _o, _n in changed if off in writable})
        try:
            with open(self.backup_path, "wb") as f:   # safety net before writing
                f.write(self.original.data)
            for off in offsets:
                self.dev.write_setting(off, work.data[off])
        except Exception as e:  # noqa: BLE001
            self.query_one("#status", Static).update(f"Write failed: {e}")
            return
        self.query_one("#status", Static).update(
            f"Applied {len(offsets)} setting(s) (backup: {self.backup_path}). Re-reading…")
        self.load_from_device()


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="scyrox-tui", description=__doc__)
    p.add_argument("--path", help="explicit HID device path (else auto-detect)")
    args = p.parse_args(argv)
    ScyroxTUI(path=args.path).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
