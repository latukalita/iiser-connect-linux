"""The animated status orb (same look as the Android app), drawn with Cairo."""
from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from ..engine import Phase  # noqa: E402

PALETTES = {
    True: dict(good=(0x34, 0xD3, 0x99), accent=(0x22, 0xD3, 0xEE), warn=(0xFB, 0xBF, 0x24),
               bad=(0xFB, 0x71, 0x85), idle=(0x7C, 0x8A, 0xB0), bg=(0x07, 0x0B, 0x1E)),
    False: dict(good=(0x05, 0x96, 0x69), accent=(0x0E, 0xA5, 0xE9), warn=(0xD9, 0x77, 0x06),
                bad=(0xE1, 0x1D, 0x48), idle=(0x74, 0x80, 0xA6), bg=(0xF4, 0xF7, 0xFF)),
}

WORKING = {Phase.CHECKING.value, Phase.SIGNING_IN.value, Phase.SIGNING_OUT.value}


def look(phase: str) -> tuple[str, str]:
    """(tone, badge) for a phase. badge: '', check, bang, pause"""
    if phase == Phase.ONLINE.value:
        return "good", "check"
    if phase in WORKING:
        return "accent", ""
    if phase == Phase.NEEDS_LOGIN.value:
        return "warn", "pause"
    if phase == Phase.NO_CREDENTIALS.value:
        return "warn", "bang"
    if phase in (Phase.BAD_CREDENTIALS.value, Phase.REJECTED.value, Phase.ERROR.value):
        return "bad", "bang"
    return "idle", ""


def _c(rgb, a=1.0):
    return (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, a)


def wifi_glyph(cr, cx, cy, r, rgba):
    """Three arcs and a dot; (cx, cy) is the centre of the glyph, r the outer arc radius."""
    base_y = cy + r * 0.46
    cr.set_source_rgba(*rgba)
    cr.set_line_width(r * 0.20)
    cr.set_line_cap(1)  # ROUND
    for k in (0.40, 0.70, 1.0):
        cr.new_sub_path()
        cr.arc(cx, base_y, r * k, math.radians(225), math.radians(315))
        cr.stroke()
    cr.arc(cx, base_y, r * 0.13, 0, 2 * math.pi)
    cr.fill()


class Orb(Gtk.DrawingArea):
    def __init__(self, phase: str = Phase.IDLE.value, size: int = 240):
        super().__init__()
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_halign(Gtk.Align.CENTER)
        self.phase = phase
        self.dark = True
        self._tone_rgb = None          # animated current colour
        self._t0 = None
        self._last_draw = 0.0
        self.set_draw_func(self._draw)
        self.add_tick_callback(self._tick)

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.queue_draw()

    def set_dark(self, dark: bool) -> None:
        self.dark = dark
        self._tone_rgb = None
        self.queue_draw()

    # Animation clock: ~30 fps, and only while the window is actually on screen.
    def _tick(self, widget, clock):
        now = clock.get_frame_time() / 1e6
        if self._t0 is None:
            self._t0 = now
        if now - self._last_draw >= 1 / 30:
            self._last_draw = now
            self.queue_draw()
        return True

    def _draw(self, area, cr, w, h):
        pal = PALETTES[self.dark]
        tone_name, badge = look(self.phase)
        target = pal[tone_name]
        if self._tone_rgb is None:
            self._tone_rgb = list(target)
        else:                                            # ease towards the new colour
            for i in range(3):
                self._tone_rgb[i] += (target[i] - self._tone_rgb[i]) * 0.12
        tone = tuple(self._tone_rgb)

        now = (self._last_draw - (self._t0 or 0.0))
        t = (now / 3.2) % 1.0
        spin = (now / 1.1) % 1.0 * 2 * math.pi
        breath = 1.0 + 0.03 * math.sin(now / 2.2 * math.pi)
        working = self.phase in WORKING
        pulsing = self.phase == Phase.ONLINE.value

        s = min(w, h)
        cx, cy = w / 2, h / 2

        if pulsing or working:
            cr.set_line_width(2)
            for k in range(3):
                prog = (t + k / 3) % 1.0
                cr.set_source_rgba(*_c(tone, (1 - prog) * 0.38))
                cr.arc(cx, cy, s * (0.34 + 0.16 * prog), 0, 2 * math.pi)
                cr.stroke()
        else:
            cr.set_line_width(1.5)
            cr.set_source_rgba(*_c(tone, 0.16))
            cr.arc(cx, cy, s * 0.42, 0, 2 * math.pi)
            cr.stroke()
            cr.set_source_rgba(*_c(tone, 0.08))
            cr.arc(cx, cy, s * 0.49, 0, 2 * math.pi)
            cr.stroke()

        core = s * 0.31 * (breath if pulsing else 1.0)

        import cairo
        halo_r = min(core * 1.9, s * 0.5)      # never wider than the widget, or it shows as a square
        halo = cairo.RadialGradient(cx, cy, core * 0.6, cx, cy, halo_r)
        halo.add_color_stop_rgba(0, *_c(tone, 0.40))
        halo.add_color_stop_rgba(1, *_c(tone, 0.0))
        cr.set_source(halo)
        cr.arc(cx, cy, halo_r, 0, 2 * math.pi)
        cr.fill()

        disc = cairo.LinearGradient(cx - core, cy - core, cx + core, cy + core)
        disc.add_color_stop_rgba(0, *_c(tone, 0.98))
        disc.add_color_stop_rgba(1, *_c(tone, 0.62))
        cr.set_source(disc)
        cr.arc(cx, cy, core, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.18)
        cr.set_line_width(1.5)
        cr.arc(cx, cy, core, 0, 2 * math.pi)
        cr.stroke()

        wifi_glyph(cr, cx, cy - s * 0.015, core * 0.56, (1, 1, 1, 1))

        if working:
            cr.save()
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.set_line_width(4)
            cr.set_line_cap(1)
            cr.new_sub_path()
            cr.arc(cx, cy, core * 1.12, spin - math.pi / 2, spin - math.pi / 2 + math.radians(80))
            cr.stroke()
            cr.restore()

        if badge:
            br = s * 0.085
            bx, by = cx + core * 0.80, cy + core * 0.80
            cr.set_source_rgba(*_c(pal["bg"]))
            cr.arc(bx, by, br + 3, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*_c(tone))
            cr.arc(bx, by, br, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.set_line_width(2.6)
            cr.set_line_cap(1)
            if badge == "check":
                cr.move_to(bx - br * 0.42, by + br * 0.02)
                cr.line_to(bx - br * 0.10, by + br * 0.34)
                cr.line_to(bx + br * 0.46, by - br * 0.30)
                cr.stroke()
            elif badge == "bang":
                cr.move_to(bx, by - br * 0.46)
                cr.line_to(bx, by + br * 0.10)
                cr.stroke()
                cr.arc(bx, by + br * 0.46, 1.5, 0, 2 * math.pi)
                cr.fill()
            elif badge == "pause":
                for dx in (-0.28, 0.28):
                    cr.move_to(bx + br * dx, by - br * 0.40)
                    cr.line_to(bx + br * dx, by + br * 0.40)
                    cr.stroke()


class AppMark(Gtk.DrawingArea):
    """Small gradient app mark for headers."""

    def __init__(self, size: int = 34):
        super().__init__()
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h):
        import cairo
        r = w * 0.30
        cr.new_sub_path()
        cr.arc(w - r, r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, r, r, math.pi, 1.5 * math.pi)
        cr.close_path()
        g = cairo.LinearGradient(0, 0, w, h)
        g.add_color_stop_rgb(0, 0x22 / 255, 0xD3 / 255, 0xEE / 255)
        g.add_color_stop_rgb(1, 0x63 / 255, 0x66 / 255, 0xF1 / 255)
        cr.set_source(g)
        cr.fill()
        wifi_glyph(cr, w / 2, h / 2 - w * 0.02, w * 0.31, (1, 1, 1, 1))
