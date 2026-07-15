"""macOS menu bar power monitor.

Menu bar title is the net battery power flow (+XX.XW charging,
-XX.XW discharging). Clicking it opens an animated flow graphic: input
streams on the left (adapter / battery), output streams on the right
(system, USB devices, battery), with dots traveling the streams that
fade from yellow (input) to blue (output).

Real-time wattage (input/system/battery) comes from direct SMC reads
(see smc.py) — ioreg's AppleSmartBattery telemetry is a cache that was
confirmed, in this project, to sit frozen for 20+ seconds at a stretch
even polled every second, since it's only refreshed on the battery
gauge IC's own schedule. ioreg is still used for values that don't need
to be sub-second and have no SMC equivalent: battery %, voltage,
adapter identity, and per-port USB-out wattage.

Both the data poll and the animation run in NSRunLoopCommonModes, so
everything keeps updating while the menu is open (ordinary timers
pause during menu tracking). The data poll runs SMC reads every 3 s
always, and re-runs ioreg every 10 s (or 3 s while a USB device is
actively charging) — see SMC_POLL_SECONDS / IOREG_POLL_SECONDS_*
below.

Run: .venv/bin/python power_monitor.py
"""

import math
import subprocess
import time

import AppKit
import Foundation
import objc
import rumps

from parser import (
    PowerReading,
    apply_smc_overlay,
    compute_streams,
    is_idle_at_full_charge,
    parse_ioreg,
)
from smc import SMCReader

SMC_POLL_SECONDS = 3  # live wattage: title + flow graphic

# ioreg has no "watch" mode (each call is a one-shot subprocess spawn,
# ~11ms) and is only needed for fields with no SMC equivalent: battery %,
# voltage, adapter identity, and per-port USB-out wattage (checked the
# full SMC sensor key list — none of these exist as raw electrical
# sensors; USB-out in particular is PD-negotiation state, not a sensor
# rail). Battery %/voltage/adapter identity barely move — 10s is plenty.
# USB-out is the one exception worth tracking closely: if a device is
# actively charging, poll it at the same cadence as SMC so it feels live.
IOREG_POLL_SECONDS_IDLE = 10.0
IOREG_POLL_SECONDS_USB_ACTIVE = float(SMC_POLL_SECONDS)
IOREG_CMD = ["ioreg", "-rw0", "-r", "-n", "AppleSmartBattery"]
IDLE_GLYPH = "▪"  # menu bar title when is_idle_at_full_charge() (parser.py)

VIEW_W, VIEW_H = 360, 195
PULSE_PERIOD_S = 3.5  # seconds for one yellow-to-blue traversal
PULSE_COUNT = 3
# Fraction of each half-leg (in-ribbon / out-ribbon travel) over which
# dots gather into / split from a single bead at the center, so one
# input dot becoming several output dots reads as a drop dividing
# rather than a jump cut. 0.25 of a half-leg ≈ 0.44 s.
PULSE_MERGE_ZONE = 0.25
# Fraction of nominal speed the bead keeps while passing through the
# center: it decelerates into the merge and accelerates back out, but
# never stalls (1.0 would disable the slowdown entirely).
PULSE_MERGE_SPEED = 0.3
DOT_GLOW_ALPHA = 0.22
DOT_CORE_ALPHA = 0.95
FRAME_INTERVAL = 1 / 30  # animation runs only while the menu is open

YELLOW = (1.00, 0.80, 0.20)
BLUE = (0.25, 0.55, 1.00)


def read_ioreg() -> str:
    return subprocess.run(
        IOREG_CMD, capture_output=True, text=True, timeout=10, check=True
    ).stdout


def _color(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        rgb[0], rgb[1], rgb[2], alpha
    )


def _lerp_rgb(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _smoothstep(t):
    """0→1 with zero slope at both ends; input clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _merged_alpha(target, n):
    """Per-dot alpha such that n coincident dots composite to `target`
    (otherwise the stacked glows brighten the merged bead noticeably)."""
    return 1.0 - (1.0 - target) ** (1.0 / n) if n > 1 else target


def _decel_into(t, v):
    """Monotone remap of leg progress t∈[0,1]: unit speed at t=0 easing
    down to speed v (0<v≤1) at t=1, with g(0)=0 and g(1)=1 preserved —
    dots decelerate into the leg's end but never come to a stop."""
    return t + (1.0 - v) * t * t * (1.0 - t)


def _bezier_point(t, x0, y0, x1, y1):
    """Point at t on the ribbon center line: a cubic bezier flat at both
    ends (control points sit at the horizontal midpoint)."""
    cx = (x0 + x1) / 2
    mt = 1 - t
    x = mt**3 * x0 + 3 * mt**2 * t * cx + 3 * mt * t**2 * cx + t**3 * x1
    y = mt**3 * y0 + 3 * mt**2 * t * y0 + 3 * mt * t**2 * y1 + t**3 * y1
    return x, y


def _stack(streams, px_per_w, top, usable_h, gap, min_h):
    """Stack stream slices vertically, centered; returns [(y, h), ...]."""
    heights = [max(w * px_per_w, min_h) for _, w in streams]
    block = sum(heights) + gap * max(len(streams) - 1, 0)
    y = top + (usable_h - block) / 2
    slices = []
    for h in heights:
        slices.append((y, h))
        y += h + gap
    return slices


class PowerStreamView(AppKit.NSView):
    """The flow graphic embedded in the dropdown menu.

    The underlying reading only changes every 3-10s (via setReading_),
    but drawRect_ fires at 30fps to animate the pulses. Rebuilding every
    NSFont/NSColor/NSGradient/NSBezierPath from scratch on every single
    frame — even though almost none of it had actually changed since the
    last one — measured at ~15% sustained CPU and steadily growing RSS
    while the menu was open (PyObjC bridge + allocation overhead, not
    the actual Core Graphics rasterization, which is comparatively
    cheap). Fix: build the static scene (everything except the moving
    pulses) once per setReading_() call and cache it; drawRect_ just
    replays the cached objects each frame via the exact same draw calls,
    in the exact same order, with the exact same parameters, and
    computes only the genuinely-animated pulses fresh — same pixels,
    just not rebuilt 30x/sec for data that hasn't changed.
    """

    def initWithFrame_(self, frame):
        self = objc.super(PowerStreamView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._reading = None
        self._anim = None
        self._scene = None
        # Fonts, dynamic system colors (these resolve light/dark at each
        # actual draw call regardless of when the reference was obtained,
        # so caching the object is safe), and the two ribbon gradients
        # never change — build once instead of on every scene rebuild.
        self._font = AppKit.NSFont.systemFontOfSize_(10)
        self._bold = AppKit.NSFont.boldSystemFontOfSize_(10)
        self._tiny = AppKit.NSFont.systemFontOfSize_(9)
        self._label_c = AppKit.NSColor.labelColor()
        self._dim_c = AppKit.NSColor.secondaryLabelColor()
        self._yellow_90 = _color(YELLOW, 0.9)
        self._blue_90 = _color(BLUE, 0.9)
        mid_rgb = _lerp_rgb(YELLOW, BLUE, 0.5)
        self._grad_in = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            _color(YELLOW, 0.32), _color(mid_rgb, 0.32)
        )
        self._grad_out = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            _color(mid_rgb, 0.32), _color(BLUE, 0.32)
        )
        return self

    def isFlipped(self):
        return True

    def setReading_(self, reading):
        self._reading = reading
        self._scene = self._build_scene(reading)
        self.setNeedsDisplay_(True)

    # A menu item's view sits in a window only while the menu is open;
    # run the 30 fps animation timer only then, on common modes so it
    # keeps ticking during menu event tracking.
    def viewDidMoveToWindow(self):
        if self.window() is not None:
            if self._anim is None:
                self._anim = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                    FRAME_INTERVAL, self, "animTick:", None, True
                )
                Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(
                    self._anim, Foundation.NSRunLoopCommonModes
                )
        elif self._anim is not None:
            self._anim.invalidate()
            self._anim = None

    def animTick_(self, _timer):
        self.setNeedsDisplay_(True)

    # These are plain Python helpers, not Objective-C overrides — without
    # @objc.python_method, PyObjC's class-dict processing tries to infer an
    # Objective-C selector signature from the name (methods with no
    # trailing underscore are treated as zero-argument selectors) and
    # raises BadPrototypeError since these take several arguments.
    @objc.python_method
    def _text(self, s, x, y, font, color, align="left"):
        attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: color,
        }
        ns = Foundation.NSString.stringWithString_(s)
        w = ns.sizeWithAttributes_(attrs).width
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        ns.drawAtPoint_withAttributes_((x, y), attrs)

    @objc.python_method
    def _band_path(self, xa, ya, ha, xb, yb, hb):
        """Just the ribbon geometry — the gradient fill itself is one of
        the two cached self._grad_in/_grad_out objects, reused across
        every frame and every scene rebuild since the colors never
        change (only this path's position/size does, per stream)."""
        p = AppKit.NSBezierPath.bezierPath()
        cx = (xa + xb) / 2
        p.moveToPoint_((xa, ya))
        p.curveToPoint_controlPoint1_controlPoint2_((xb, yb), (cx, ya), (cx, yb))
        p.lineToPoint_((xb, yb + hb))
        p.curveToPoint_controlPoint1_controlPoint2_(
            (xa, ya + ha), (cx, yb + hb), (cx, ya + ha)
        )
        p.closePath()
        return p

    @objc.python_method
    def _dot(self, x, y, radius, rgb, glow_a=DOT_GLOW_ALPHA, core_a=DOT_CORE_ALPHA):
        _color(rgb, glow_a).setFill()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            Foundation.NSMakeRect(x - radius * 2.4, y - radius * 2.4, radius * 4.8, radius * 4.8)
        ).fill()
        _color(rgb, core_a).setFill()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(
            Foundation.NSMakeRect(x - radius, y - radius, radius * 2, radius * 2)
        ).fill()

    @objc.python_method
    def _build_scene(self, r):
        """Everything drawRect_ used to build from scratch every frame
        except the pulses — same math, same order, same parameters as
        the original inline version, just computed once per reading
        instead of once per animation frame. Returns None for the
        "No power data" case (matching the old early return)."""
        W = self.bounds().size.width
        H = self.bounds().size.height
        font, bold, tiny = self._font, self._bold, self._tiny
        label_c, dim_c = self._label_c, self._dim_c

        inputs, outputs = compute_streams(r) if r else ([], [])
        if not inputs and not outputs:
            return None

        bar_w = 5.0
        x_left_bar = 104.0
        x_right_bar = W - 110.0
        x0 = x_left_bar + bar_w  # ribbons span x0..x1
        x1 = x_right_bar
        xc = (x0 + x1) / 2
        top = 20.0
        usable = H - top - 8.0
        gap = 8.0

        total_in = sum(w for _, w in inputs)
        total_out = sum(w for _, w in outputs)
        n_max = max(len(inputs), len(outputs), 1)
        px_per_w = min(
            1.8, (usable - gap * (n_max - 1)) / max(total_in, total_out, 1e-9)
        )

        in_slices = _stack(inputs, px_per_w, top, usable, gap, min_h=12.0)
        out_slices = _stack(outputs, px_per_w, top, usable, gap, min_h=12.0)
        # Center "bus" the streams converge to / diverge from (gapless).
        bus_in = _stack(inputs, px_per_w, top, usable, gap=0.0, min_h=2.0)
        bus_out = _stack(outputs, px_per_w, top, usable, gap=0.0, min_h=2.0)

        bands = []  # (path, gradient), drawn first, same as original order
        for (y, h), (by, bh) in zip(in_slices, bus_in):
            bands.append((self._band_path(x0, y, h, xc, by, bh), self._grad_in))
        for (y, h), (by, bh) in zip(out_slices, bus_out):
            bands.append((self._band_path(xc, by, bh, x1, y, h), self._grad_out))

        # Node bars and labels
        bars = []  # (path, color)
        texts = []  # (s, x, y, font, color, align)
        for (name, w), (y, h) in zip(inputs, in_slices):
            bars.append((
                AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    Foundation.NSMakeRect(x_left_bar, y, bar_w, h), 2, 2
                ),
                self._yellow_90,
            ))
            cy = y + h / 2
            texts.append((name, x_left_bar - 7, cy - 12, font, dim_c, "right"))
            texts.append((f"{w:.1f} W", x_left_bar - 7, cy, bold, label_c, "right"))
        for (name, w), (y, h) in zip(outputs, out_slices):
            bars.append((
                AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    Foundation.NSMakeRect(x_right_bar, y, bar_w, h), 2, 2
                ),
                self._blue_90,
            ))
            cy = y + h / 2
            texts.append((name, x_right_bar + bar_w + 7, cy - 12, font, dim_c, "left"))
            texts.append((f"{w:.1f} W", x_right_bar + bar_w + 7, cy, bold, label_c, "left"))

        texts.append((f"In {total_in:.1f} W", 8, 4, tiny, dim_c, "left"))
        texts.append((f"Out {total_out:.1f} W", W - 8, 4, tiny, dim_c, "right"))

        return {
            "bands": bands,
            "bars": bars,
            "texts": texts,
            "in_slices": in_slices,
            "bus_in": bus_in,
            "out_slices": out_slices,
            "bus_out": bus_out,
            "x0": x0,
            "x1": x1,
            "xc": xc,
            # Where all streams meet vertically: _stack() centers every
            # stack on top + usable/2, so both bus stacks share this.
            "merge_y": top + usable / 2.0,
        }

    def drawRect_(self, rect):
        scene = self._scene
        if scene is None:
            W = self.bounds().size.width
            H = self.bounds().size.height
            self._text("No power data", W / 2, H / 2 - 6, self._font, self._dim_c, align="center")
            return

        # Same draw calls, same order as the original: bands, then bars
        # + their labels, then the total captions, then pulses on top.
        for path, gradient in scene["bands"]:
            gradient.drawInBezierPath_angle_(path, 0.0)
        for path, color in scene["bars"]:
            color.setFill()
            path.fill()
        for s, x, y, font, color, align in scene["texts"]:
            self._text(s, x, y, font, color, align=align)

        # Pulses: travel input ribbons (first half of the cycle), then
        # output ribbons, color fading yellow -> blue with progress.
        # Inside the merge zone around the center, the vertical position,
        # radius, and alpha are all eased so the input dots gather into
        # one swollen bead that then splits apart onto the output ribbons
        # — the two halves meet at identical geometry, so the count
        # changing (e.g. 1 input dot -> 3 output dots) is invisible.
        # Horizontally the bead keeps flowing: _decel_into() slows it to
        # PULSE_MERGE_SPEED of nominal through the center, no full stop.
        # Genuinely animated, so recomputed fresh every frame.
        x0, x1, xc = scene["x0"], scene["x1"], scene["xc"]
        in_slices, bus_in = scene["in_slices"], scene["bus_in"]
        out_slices, bus_out = scene["out_slices"], scene["bus_out"]
        my = scene["merge_y"]
        n_in = max(len(in_slices), 1)
        n_out = max(len(out_slices), 1)
        swell = max(n_in, n_out) ** (1.0 / 3.0)  # merged-bead radius factor
        glow_in = _merged_alpha(DOT_GLOW_ALPHA, n_in)
        core_in = _merged_alpha(DOT_CORE_ALPHA, n_in)
        glow_out = _merged_alpha(DOT_GLOW_ALPHA, n_out)
        core_out = _merged_alpha(DOT_CORE_ALPHA, n_out)
        now = time.time()
        for k in range(PULSE_COUNT):
            p = ((now / PULSE_PERIOD_S) + k / PULSE_COUNT) % 1.0
            rgb = _lerp_rgb(YELLOW, BLUE, p)
            radius = 2.6 + 0.9 * math.sin(now * 5 + k * 2.1)  # the pulsating
            if p < 0.5:
                t = _decel_into(p * 2, PULSE_MERGE_SPEED)
                # 0 while traveling, easing to 1 as the dots reach the
                # center and gather into the merged bead.
                m = _smoothstep((t - (1.0 - PULSE_MERGE_ZONE)) / PULSE_MERGE_ZONE)
                glow_a = DOT_GLOW_ALPHA + (glow_in - DOT_GLOW_ALPHA) * m
                core_a = DOT_CORE_ALPHA + (core_in - DOT_CORE_ALPHA) * m
                slices, bus = in_slices, bus_in
            else:
                # Mirror of the input warp: slow leaving the center,
                # easing back to unit speed toward the right-hand bars.
                t = 1.0 - _decel_into(1.0 - (p - 0.5) * 2, PULSE_MERGE_SPEED)
                # 1 at the center (all dots coincide in the merged bead),
                # easing to 0 as they pull apart onto their own ribbons.
                m = 1.0 - _smoothstep(t / PULSE_MERGE_ZONE)
                glow_a = DOT_GLOW_ALPHA + (glow_out - DOT_GLOW_ALPHA) * m
                core_a = DOT_CORE_ALPHA + (core_out - DOT_CORE_ALPHA) * m
                slices, bus = out_slices, bus_out
            r_d = radius * (1.0 + (swell - 1.0) * m)
            for (y, h), (by, bh) in zip(slices, bus):
                if p < 0.5:
                    px, py = _bezier_point(t, x0, y + h / 2, xc, by + bh / 2)
                else:
                    px, py = _bezier_point(t, xc, by + bh / 2, x1, y + h / 2)
                py += (my - py) * m
                self._dot(px, py, r_d, rgb, glow_a, core_a)


class _PollTarget(AppKit.NSObject):
    """NSTimer target bridging to a plain Python callback."""

    def fire_(self, _timer):
        self._cb()


class PowerMonitorApp(rumps.App):
    def __init__(self):
        super().__init__("PowerMonitor", title="…", quit_button="Quit")

        try:
            self._smc = SMCReader()
        except OSError:
            self._smc = None  # falls back to ioreg-only wattage, still functional

        # Cached slow-field snapshot from the last successful ioreg call;
        # each SMC tick overlays fresh wattage onto this rather than
        # re-running ioreg itself. self._ioreg_interval adapts between
        # IOREG_POLL_SECONDS_IDLE and _USB_ACTIVE based on the last reading.
        self._base_reading = PowerReading()
        self._last_ioreg_time = 0.0
        self._ioreg_interval = IOREG_POLL_SECONDS_IDLE

        self.stream_view = None
        graphic_item = None
        try:
            self.stream_view = PowerStreamView.alloc().initWithFrame_(
                Foundation.NSMakeRect(0, 0, VIEW_W, VIEW_H)
            )
            graphic_item = rumps.MenuItem("Power flow")
            graphic_item._menuitem.setView_(self.stream_view)
        except Exception:
            self.stream_view = None

        self.item_batt_info = rumps.MenuItem("Battery: —")
        self.item_adapter = rumps.MenuItem("Adapter: —")

        if self.stream_view is not None:
            self.item_input = self.item_system = self.item_usb = None
            self.item_battery = None
            self.menu = [
                graphic_item,
                None,  # separator
                self.item_batt_info,
                self.item_adapter,
            ]
        else:
            # Fallback if the custom-view embedding ever fails: plain text.
            self.item_input = rumps.MenuItem("Total power in: —")
            self.item_system = rumps.MenuItem("System draw: —")
            self.item_usb = rumps.MenuItem("USB-C port: —")
            self.item_battery = rumps.MenuItem("Battery: —")
            self.menu = [
                self.item_input,
                None,
                self.item_system,
                self.item_usb,
                self.item_battery,
                None,
                self.item_batt_info,
                self.item_adapter,
            ]

        # One timer at the fine-grained SMC cadence, on NSRunLoopCommonModes
        # so it keeps firing while the menu is open (NSTimer on the default
        # mode pauses during menu tracking — this is what makes the
        # title/graphic keep updating live even with the dropdown open).
        # Every tick refreshes SMC wattage (cheap, no subprocess); the ioreg
        # subprocess only actually runs when self._ioreg_interval has
        # elapsed, which tick() shortens to match once a USB device is
        # seen charging so that number feels just as live.
        self._tick_target = _PollTarget.alloc().init()
        self._tick_target._cb = self.tick
        self._tick_timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            SMC_POLL_SECONDS, self._tick_target, "fire:", None, True
        )
        Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(
            self._tick_timer, Foundation.NSRunLoopCommonModes
        )

        self.refresh_ioreg()  # populates self._base_reading before first display

    def tick(self):
        if time.time() - self._last_ioreg_time >= self._ioreg_interval:
            self.refresh_ioreg()
        else:
            self.refresh_smc()

    def _set_hidden(self, item: rumps.MenuItem, hidden: bool):
        try:
            item._menuitem.setHidden_(hidden)
        except AttributeError:
            if hidden:
                item.title = "—"

    def refresh_ioreg(self):
        """Adaptive-cadence tick: re-read battery %, voltage, adapter
        identity, and USB-out wattage. On failure, keeps the last
        known-good values — the live SMC-sourced wattage from
        refresh_smc() keeps working independently of ioreg's health."""
        self._last_ioreg_time = time.time()
        try:
            self._base_reading = parse_ioreg(read_ioreg())
        except Exception:
            pass
        self._ioreg_interval = (
            IOREG_POLL_SECONDS_USB_ACTIVE
            if self._base_reading.usb_out_w > 0.05
            else IOREG_POLL_SECONDS_IDLE
        )
        self.refresh_smc()

    def refresh_smc(self):
        """Fast tick: overlay live SMC wattage onto the cached ioreg
        snapshot and redraw. This is the one that makes the menu bar
        title update every SMC_POLL_SECONDS."""
        r = self._base_reading
        if self._smc is not None:
            try:
                r = apply_smc_overlay(r, self._smc.read_power_keys())
            except OSError:
                self._smc = None  # disable for the rest of the session

        # Menu bar title: signed net battery flow, or a plain glyph when
        # plugged into a capable adapter at high charge — see
        # is_idle_at_full_charge() in parser.py for why a fixed wattage
        # band alone isn't enough to tell real signal from trickle noise.
        # Still fully clickable either way; only the title text changes.
        if r.battery_w is None:
            self.title = "⚠️"
        elif is_idle_at_full_charge(r):
            self.title = IDLE_GLYPH
        else:
            self.title = f"{r.battery_w:+.1f}W"

        if self.stream_view is not None:
            self.stream_view.setReading_(r)
        else:
            self._refresh_text_fallback(r)

        info = []
        if r.soc_percent is not None:
            info.append(f"{r.soc_percent}%")
        if r.voltage_v is not None:
            info.append(f"{r.voltage_v:.2f}V")
        self.item_batt_info.title = "Battery: " + " · ".join(info) if info else "Battery: —"

        if r.external_connected and r.adapter_name:
            self.item_adapter.title = f"Adapter: {r.adapter_name}"
            self._set_hidden(self.item_adapter, False)
        elif r.external_connected:
            self.item_adapter.title = "Adapter: connected"
            self._set_hidden(self.item_adapter, False)
        else:
            self._set_hidden(self.item_adapter, True)

    def _refresh_text_fallback(self, r):
        self.item_input.title = (
            f"Total power in: {r.input_w:.1f} W" if r.input_w is not None else "Total power in: —"
        )
        self.item_system.title = (
            f"System draw: {r.laptop_w:.1f} W" if r.laptop_w is not None else "System draw: —"
        )
        if r.usb_out_w > 0.05:
            self.item_usb.title = f"USB-C port (device charging): {r.usb_out_w:.1f} W"
            self._set_hidden(self.item_usb, False)
        else:
            self._set_hidden(self.item_usb, True)
        if r.battery_w is None:
            self.item_battery.title = "Battery flow: —"
        elif is_idle_at_full_charge(r):
            self.item_battery.title = "Fully charged / idle"
        elif r.battery_w > 0:
            self.item_battery.title = f"Charging battery: {r.battery_w:+.1f} W"
        else:
            self.item_battery.title = f"Discharging battery: {r.battery_w:+.1f} W"


if __name__ == "__main__":
    PowerMonitorApp().run()
