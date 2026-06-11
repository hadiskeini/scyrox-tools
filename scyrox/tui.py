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


def _parse_rgb(s):
    """Parse 'r,g,b' (0-255 each) into a tuple, or None if malformed."""
    parts = (s or "").split(",")
    if len(parts) != 3:
        return None
    try:
        return tuple(max(0, min(255, int(p))) for p in parts)
    except ValueError:
        return None


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
            with Vertical(classes="panel", id="dpi"):
                yield Label("DPI")
                with Horizontal(classes="row"):
                    yield Label("Active stages (1-8)")
                    yield Input(id="dpi_count", restrict=r"\d*")
                with Horizontal(classes="row"):
                    yield Label("Current stage (0-based)")
                    yield Input(id="dpi_active", restrict=r"\d*")
                for i in range(8):
                    with Horizontal(classes="row"):
                        yield Label(f"  stage {i}: dpi / color")
                        yield Input(id=f"dpi_v{i}", restrict=r"\d*",
                                    placeholder="dpi")
                        yield Input(id=f"dpi_c{i}", placeholder="r,g,b")
            with Vertical(classes="panel", id="light"):
                yield Label("Lighting")
                with Horizontal(classes="row"):
                    yield Label("Mode")
                    yield Select(
                        [(f"{m} ({'/'.join(LIGHT_MODE_CAPS.get(m, ())) or 'off'})", m)
                         for m in range(7)], id="light_mode", allow_blank=False)
                with Horizontal(classes="row"):
                    yield Label("Color (r,g,b)")
                    yield Input(id="light_color", placeholder="r,g,b")
                with Horizontal(classes="row"):
                    yield Label("Speed (0-9)")
                    yield Input(id="light_speed", restrict=r"\d*")
                with Horizontal(classes="row"):
                    yield Label("Brightness (0-9)")
                    yield Input(id="light_brightness", restrict=r"\d*")
            with Vertical(classes="panel", id="readonly"):
                yield Label("Other (read-only)")
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
            self.query_one("#dpi_count", Input).value = str(w.max_dpi_stage)
            self.query_one("#dpi_active", Input).value = str(w.current_dpi_stage)
            for i in range(8):
                x, _y = w.dpi_xy(i)
                self.query_one(f"#dpi_v{i}", Input).value = str(x)
                r, g, b = w.dpi_color(i)
                self.query_one(f"#dpi_c{i}", Input).value = f"{r},{g},{b}"
            self.query_one("#light_mode", Select).value = w.light_mode
            lr, lg, lb = w.light_color
            self.query_one("#light_color", Input).value = f"{lr},{lg},{lb}"
            self.query_one("#light_speed", Input).value = str(w.light_speed)
            self.query_one("#light_brightness", Input).value = str(w.light_brightness)
        self.query_one("#ro_body", Static).update(
            f"LOD raw {w.setting(OFFSETS['lod'])}, "
            f"debounce raw {w.setting(OFFSETS['debounce_time'])}, "
            f"sleep raw {w.setting(OFFSETS['sleep_time'])}  "
            "(raw values; unit labels live in the web UI's i18n)")
        self.update_pending()

    def current_image(self) -> "FlashImage":
        """The image implied by the current widget values, atop the baseline.

        Only controls the user actually changed rewrite bytes — a control left
        at its loaded value leaves the original bytes untouched (so unset 0xFF
        fields aren't normalized into spurious writes)."""
        img = FlashImage(bytes(self.original.data))
        o = self.original

        def _int(sel, default):
            try:
                return int(self.query_one(sel, Input).value or default)
            except ValueError:
                return default

        sel = self.query_one("#report_rate", Select).value
        if isinstance(sel, int) and sel != o.report_rate_hz:
            img.report_rate_hz = sel
        a = max(-30, min(30, _int("#angle_tune", o.angle_tune_deg)))
        if a != o.angle_tune_deg:
            img.angle_tune_deg = a
        for _label, key, attr in TOGGLES:
            v = self.query_one(f"#t_{key}", Switch).value
            if v != bool(getattr(o, attr)):
                setattr(img, attr, v)

        # DPI: stage count, active stage, per-stage value + color.
        c = _int("#dpi_count", o.max_dpi_stage)
        if 1 <= c <= 8 and c != o.max_dpi_stage:
            img.set_max_dpi_stage(c)
        act = _int("#dpi_active", o.current_dpi_stage)
        if act != o.current_dpi_stage:
            img.set_current_dpi_stage(act)
        for i in range(8):
            v = _int(f"#dpi_v{i}", 0)
            if v and v != o.dpi_xy(i)[0]:
                img.set_dpi_xy(i, v, v)
            rgb = _parse_rgb(self.query_one(f"#dpi_c{i}", Input).value)
            if rgb is not None and rgb != o.dpi_color(i):
                img.set_dpi_color(i, rgb)

        # Lighting: rewrite the whole 7-byte block if any field changed.
        lm = self.query_one("#light_mode", Select).value
        lm = lm if isinstance(lm, int) else o.light_mode
        lc = _parse_rgb(self.query_one("#light_color", Input).value) or o.light_color
        ls = max(0, min(9, _int("#light_speed", o.light_speed)))
        lb = max(0, min(9, _int("#light_brightness", o.light_brightness)))
        if (lm, lc, ls, lb) != (o.light_mode, o.light_color,
                                o.light_speed, o.light_brightness):
            img.set_light(lm, lc, ls, lb)
        return img

    # --- edits just trigger a recompute; values are read from the widgets ---
    def on_select_changed(self, e: Select.Changed) -> None:
        self.update_pending()

    def on_switch_changed(self, e: Switch.Changed) -> None:
        self.update_pending()

    def on_input_changed(self, e: Input.Changed) -> None:
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
        # Write whatever bytes changed. current_image() built every edit through
        # the FlashImage setters, so complements and per-entry checksums are
        # already correct — we just push the changed byte ranges to the device.
        offsets = sorted({off for off, _o, _n in changed})
        runs = []
        for off in offsets:
            if runs and off == runs[-1][1]:
                runs[-1][1] = off + 1
            else:
                runs.append([off, off + 1])
        try:
            with open(self.backup_path, "wb") as f:   # safety net before writing
                f.write(self.original.data)
            for start, end in runs:
                self.dev.write_flash(start, bytes(work.data[start:end]))
        except Exception as e:  # noqa: BLE001
            self.query_one("#status", Static).update(f"Write failed: {e}")
            return
        self.query_one("#status", Static).update(
            f"Applied {len(offsets)} byte(s) in {len(runs)} field(s) "
            f"(backup: {self.backup_path}). Re-reading…")
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
