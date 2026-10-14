"""Main window: welcome -> login form -> home, plus a settings window."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gio, Gtk  # noqa: E402

from .. import APP_NAME, __version__
from ..cli import daemon_running
from ..engine import Phase, Reason, check, sign_out
from ..store import DEFAULT_HOST, DEFAULT_IP, Store
from .orb import WORKING, AppMark, Orb

COPY = {
    Phase.ONLINE.value: ("You're online", "Signed in to the campus network"),
    Phase.CHECKING.value: ("Checking…", "Looking at your connection"),
    Phase.SIGNING_IN.value: ("Signing you in…", "Talking to the IISER gateway"),
    Phase.SIGNING_OUT.value: ("Signing out…", "Telling the gateway you're done"),
    Phase.NO_GATEWAY.value: ("No internet here", "This doesn't look like the IISER network, so I'm leaving it alone."),
    Phase.NO_WIFI.value: ("Not on Wi-Fi", "Auto sign-in only runs on Wi-Fi. Join the campus Wi-Fi, or switch \"Only on Wi-Fi\" off in Settings."),
    Phase.NEEDS_LOGIN.value: ("Sign-in needed", "Click below to sign in to the gateway."),
    Phase.BAD_CREDENTIALS.value: ("Check your password", "The gateway didn't accept your login. Auto sign-in is paused so your account can't get locked. Update it in Settings."),
    Phase.REJECTED.value: ("The gateway said no", "It refused the sign-in."),
    Phase.ERROR.value: ("Couldn't reach the gateway", "Something went wrong. I'll try again shortly."),
    Phase.NO_CREDENTIALS.value: ("Add your login", "Enter your campus username and password to get started."),
    Phase.IDLE.value: ("Getting ready…", "One moment"),
}


def friendly_time(ts: float) -> str:
    if not ts:
        return "Never"
    s = int(time.time() - ts)
    if s < 60:
        return "Just now"
    if s < 3600:
        return f"{s // 60} min ago"
    lt, now = time.localtime(ts), time.localtime()
    if (lt.tm_year, lt.tm_yday) == (now.tm_year, now.tm_yday):
        return time.strftime("Today %H:%M", lt)
    if time.time() - ts < 2 * 86400 and lt.tm_yday == now.tm_yday - 1:
        return time.strftime("Yesterday %H:%M", lt)
    return time.strftime("%d %b", lt)


def short_time(ts: float) -> str:
    lt, now = time.localtime(ts), time.localtime()
    same = (lt.tm_year, lt.tm_yday) == (now.tm_year, now.tm_yday)
    return time.strftime("%H:%M" if same else "%d %b %H:%M", lt)


def icon(name: str, size: int = 20) -> Gtk.Image:
    img = Gtk.Image.new_from_icon_name(name)
    img.set_pixel_size(size)
    return img


def label(text: str, *classes: str, wrap=False, xalign=0.5, justify=Gtk.Justification.CENTER) -> Gtk.Label:
    lb = Gtk.Label(label=text, wrap=wrap, xalign=xalign, justify=justify)
    for c in classes:
        lb.add_css_class(c)
    return lb


def gradient_button(text: str, icon_name: str | None = None) -> Gtk.Button:
    box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
    if icon_name:
        box.append(icon(icon_name))
    box.append(Gtk.Label(label=text))
    b = Gtk.Button(child=box)
    b.add_css_class("gradient")
    return b


def ghost_button(text: str, icon_name: str | None = None) -> Gtk.Button:
    box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
    if icon_name:
        box.append(icon(icon_name))
    box.append(Gtk.Label(label=text))
    b = Gtk.Button(child=box)
    b.add_css_class("ghost")
    return b


class ActionButton(Gtk.Button):
    """A button whose label/icon/spinner are built once and then just updated (no rebuilding, no flicker)."""

    def __init__(self, text: str, icon_name: str, css: str):
        super().__init__()
        self._icon = icon(icon_name)
        self._spin = Gtk.Spinner(visible=False)
        self._text = Gtk.Label(label=text)
        box = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        for w in (self._icon, self._spin, self._text):
            box.append(w)
        self.set_child(box)
        self.add_css_class(css)

    def face(self, text: str, icon_name: str, busy: bool = False) -> None:
        self._text.set_label(text)
        self._icon.set_from_icon_name(icon_name)
        self._icon.set_visible(not busy)
        self._spin.set_visible(busy)
        self._spin.set_spinning(busy)


def glass(*children: Gtk.Widget, margin: int = 0) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("glass")
    for c in children:
        box.append(c)
    return box


def boxed_list() -> Gtk.ListBox:
    lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    lb.add_css_class("boxed-list")
    return lb


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, store: Store | None = None):
        super().__init__(application=app, title=APP_NAME, default_width=470, default_height=800)
        self.store = store or Store()
        self._busy = False
        self._refresh_id = 0
        self._settings_win: Adw.PreferencesWindow | None = None

        self.style = Adw.StyleManager.get_default()
        self.style.connect("notify::dark", lambda *_: self._apply_theme())

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("app-bg")
        self.bg = outer
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        title = Gtk.Box(spacing=10)
        title.append(AppMark(26))
        title.append(label(APP_NAME, "heading"))
        header.set_title_widget(title)
        self.gear = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        self.gear.connect("clicked", lambda *_: self.open_settings())
        header.pack_end(self.gear)
        outer.append(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT, transition_duration=320, vexpand=True)
        outer.append(self.stack)
        self.set_content(outer)

        self._build_welcome()
        self._build_account()
        self._build_home()
        self._apply_theme()

        # live updates: the daemon/CLI write status files, we watch them
        self._monitors = []
        for d in (self.store.state_dir, self.store.cfg_dir):
            d.mkdir(parents=True, exist_ok=True)
            m = Gio.File.new_for_path(str(d)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            m.connect("changed", self._on_file_changed)
            self._monitors.append(m)
        GLib.timeout_add_seconds(5, self._tick)

        self.stack.set_visible_child_name("home" if self.store.has_credentials() else "welcome")
        self.refresh()
        if self.store.has_credentials():
            self.run_bg(lambda: check(Reason.APP_OPEN, self.store))

    # ------------------------------------------------------------------ theme
    def _apply_theme(self):
        dark = self.style.get_dark()
        self.bg.remove_css_class("light" if dark else "dark")
        self.bg.add_css_class("dark" if dark else "light")
        for orb in (self.home_orb, self.welcome_orb):
            orb.set_dark(dark)

    # ------------------------------------------------------------------ welcome
    def _build_welcome(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        clamp = Adw.Clamp(maximum_size=440, margin_start=20, margin_end=20, margin_bottom=22, child=box)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)
        self.welcome_orb = Orb(Phase.ONLINE.value, 210)
        box.append(self.welcome_orb)
        t = label("Campus Wi-Fi,\nsigned in for you", "hero-title-big")
        t.set_margin_top(6)
        box.append(t)
        s = label("Set it up once. Every time this PC joins the IISER network, it signs in on its own - no more gateway page.",
                  "hero-sub", wrap=True)
        s.set_margin_top(10)
        s.set_margin_bottom(20)
        box.append(s)
        rows = boxed_list()
        for ic, title, body in (
            ("emblem-system-symbolic", "Fully automatic", "Runs quietly in the background, from login."),
            ("changes-prevent-symbolic", "Safe with your account", "One wrong password and it stops - it never hammers the gateway."),
            ("security-high-symbolic", "Talks only to IISER", "Sent over HTTPS to the gateway - nowhere else, no tracking."),
        ):
            r = Adw.ActionRow(title=title, subtitle=body, subtitle_lines=3)
            chip = icon(ic, 22)
            chip.add_css_class("icon-chip")
            r.add_prefix(chip)
            rows.append(r)
        box.append(glass(rows))
        spacer = Gtk.Box(vexpand=True)
        spacer.set_size_request(-1, 18)
        box.append(spacer)
        go = gradient_button("Get started", "go-next-symbolic")
        go.connect("clicked", lambda *_: self.stack.set_visible_child_name("account"))
        box.append(go)
        self.stack.add_named(scroll, "welcome")

    # ------------------------------------------------------------------ account form
    def _build_account(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        clamp = Adw.Clamp(maximum_size=440, margin_start=20, margin_end=20, margin_top=18, margin_bottom=22, child=box)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)
        row = Gtk.Box(spacing=10)
        row.append(AppMark(34))
        row.append(label("Step 2 of 2", "small-dim"))
        box.append(row)
        h = label("Your campus login", "hero-title-big", xalign=0, justify=Gtk.Justification.LEFT)
        h.set_margin_top(18)
        box.append(h)
        sub = label("Use the same username and password you type on the gateway sign-in page.", "hero-sub",
                    wrap=True, xalign=0, justify=Gtk.Justification.LEFT)
        sub.set_margin_top(8)
        sub.set_margin_bottom(22)
        box.append(sub)

        self.acc_user = Gtk.Entry(placeholder_text="Username", hexpand=True, primary_icon_name="avatar-default-symbolic")
        self.acc_pass = Gtk.PasswordEntry(placeholder_text="Password", show_peek_icon=True, hexpand=True)
        for w in (self.acc_user, self.acc_pass):
            w.set_size_request(-1, 48)
        self.acc_user.connect("changed", self._acc_changed)
        self.acc_pass.connect("changed", self._acc_changed)
        self.acc_pass.connect("activate", lambda *_: self._finish_account())
        self.acc_user.connect("activate", lambda *_: self.acc_pass.grab_focus())
        fields = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        fields.append(self.acc_user)
        fields.append(self.acc_pass)
        box.append(fields)

        self.acc_notify = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        nrow = Adw.ActionRow(title="Sign-in notifications", subtitle="A quiet heads-up when you're signed in, or if something needs attention.",
                             subtitle_lines=2, activatable_widget=self.acc_notify)
        nrow.add_suffix(self.acc_notify)
        nl = boxed_list()
        nl.append(nrow)
        g = glass(nl)
        g.set_margin_top(20)
        box.append(g)

        box.append(Gtk.Box(vexpand=True))
        self.acc_save = gradient_button("Save and connect", "emblem-ok-symbolic")
        self.acc_save.set_sensitive(False)
        self.acc_save.set_margin_top(24)
        self.acc_save.connect("clicked", lambda *_: self._finish_account())
        box.append(self.acc_save)
        back = Gtk.Button(label="Back")
        back.add_css_class("flat")
        back.set_margin_top(6)
        back.connect("clicked", lambda *_: self.stack.set_visible_child_name("welcome"))
        box.append(back)
        self.stack.add_named(scroll, "account")

    def _acc_changed(self, *_):
        self.acc_save.set_sensitive(bool(self.acc_user.get_text().strip() and self.acc_pass.get_text()))

    def _finish_account(self):
        user, pw = self.acc_user.get_text().strip(), self.acc_pass.get_text()
        if not (user and pw):
            return
        self.store.set_credentials(user, pw)
        self.store.update(notify=self.acc_notify.get_active())
        self.acc_pass.set_text("")
        self.stack.set_visible_child_name("home")
        self.refresh()
        self.run_bg(lambda: check(Reason.MANUAL, self.store))
        if not daemon_running(self.store):               # they asked for automatic sign-in: turn it on
            self._start_service()

    # ------------------------------------------------------------------ home
    def _build_home(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        clamp = Adw.Clamp(maximum_size=440, margin_start=20, margin_end=20, margin_bottom=26, child=box)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)

        self.home_orb = Orb(Phase.IDLE.value, 236)
        box.append(self.home_orb)
        self.title = label("Getting ready…", "hero-title")
        self.title.set_margin_top(4)
        box.append(self.title)
        self.sub = label("One moment", "hero-sub", wrap=True)
        self.sub.set_margin_top(8)
        self.sub.set_margin_start(10)
        self.sub.set_margin_end(10)
        box.append(self.sub)

        # Log in / Log out - both always visible; only the one that makes sense is enabled
        self.login_btn = ActionButton("Log in", "network-wireless-symbolic", "gradient")
        self.logout_btn = ActionButton("Log out", "system-log-out-symbolic", "ghost")
        self.login_btn.connect("clicked", self._on_login)
        self.logout_btn.connect("clicked", lambda *_: self.run_bg(lambda: sign_out(self.store)))
        buttons = Gtk.Box(spacing=12, homogeneous=True, margin_top=22)
        buttons.append(self.login_btn)
        buttons.append(self.logout_btn)
        box.append(buttons)
        recheck = Gtk.Button(halign=Gtk.Align.CENTER)
        rb = Gtk.Box(spacing=6)
        rb.append(icon("view-refresh-symbolic", 14))
        rb.append(Gtk.Label(label="Check again"))
        recheck.set_child(rb)
        recheck.add_css_class("flat")
        recheck.set_margin_top(4)
        recheck.connect("clicked", lambda *_: self.run_bg(lambda: check(Reason.APP_OPEN, self.store)))
        self.recheck = recheck
        box.append(recheck)

        # what it found on this network
        self.net_wifi = self._net_row("network-wireless-symbolic", "Wi-Fi")
        self.net_gw = self._net_row("network-server-symbolic", "Gateway")
        nl = boxed_list()
        nl.append(self.net_wifi[0])
        nl.append(self.net_gw[0])
        ng = glass(nl)
        ng.set_margin_top(14)
        box.append(ng)

        # auto sign-in switch
        self.auto_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._auto_handler = self.auto_switch.connect("notify::active", self._on_auto)
        self.auto_row = Adw.ActionRow(title="Auto sign-in", subtitle="", activatable_widget=self.auto_switch)
        self.auto_row.add_suffix(self.auto_switch)
        al = boxed_list()
        al.append(self.auto_row)
        g = glass(al)
        g.set_margin_top(14)
        box.append(g)

        # stats
        stats = Gtk.Box(homogeneous=False, spacing=0, margin_top=14, margin_bottom=14)
        self.stat_last = self._stat("Last sign-in")
        self.stat_count = self._stat("Sign-ins")
        self.stat_user = self._stat("Account")
        for i, (w, _) in enumerate((self.stat_last, self.stat_count, self.stat_user)):
            if i:
                sep = Gtk.Box()
                sep.add_css_class("stat-sep")
                stats.append(sep)
            stats.append(w)
        sg = glass(stats)
        sg.set_margin_top(14)
        box.append(sg)

        # background-service tip
        self.svc_label = label("Turn on the background service so it signs in by itself, even when this window is closed.",
                               "hero-sub", wrap=True, xalign=0, justify=Gtk.Justification.LEFT)
        self.svc_btn = ghost_button("Turn on background service", "system-run-symbolic")
        self.svc_btn.add_css_class("warn-ghost")
        self.svc_btn.connect("clicked", self._start_service)
        head = Gtk.Box(spacing=12, margin_top=14, margin_start=14, margin_end=14)
        chip = icon("dialog-warning-symbolic", 22)
        chip.add_css_class("icon-chip")
        chip.add_css_class("warn")
        head.append(chip)
        tcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        tcol.append(label("Make it automatic", "heading", xalign=0, justify=Gtk.Justification.LEFT))
        tcol.append(self.svc_label)
        head.append(tcol)
        self.svc_card = glass(head)
        self.svc_btn.set_margin_start(14)
        self.svc_btn.set_margin_end(14)
        self.svc_btn.set_margin_top(12)
        self.svc_btn.set_margin_bottom(14)
        self.svc_card.append(self.svc_btn)
        self.svc_card.set_margin_top(14)
        box.append(self.svc_card)

        # activity
        ah = Gtk.Box(margin_top=22, margin_bottom=8, margin_start=6)
        ah.append(label("ACTIVITY", "caption-caps", xalign=0, justify=Gtk.Justification.LEFT))
        ah.get_first_child().set_hexpand(True)
        clear = Gtk.Button(label="Clear")
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_: (self.store.clear_log(), self.refresh()))
        ah.append(clear)
        box.append(ah)
        self.activity = boxed_list()
        self.empty_row = Adw.ActionRow(title="Nothing yet", subtitle="Sign-ins will show up here.")
        box.append(glass(self.activity))
        foot = label("Your password stays on this PC (private file, mode 600) and is only ever sent to the IISER gateway.",
                     "small-dim", wrap=True)
        foot.set_margin_top(18)
        foot.set_margin_start(16)
        foot.set_margin_end(16)
        box.append(foot)
        self.stack.add_named(scroll, "home")

    def _net_row(self, icon_name: str, title: str):
        row = Adw.ActionRow(title=title, subtitle="Checking…")
        chip = icon(icon_name, 20)
        chip.add_css_class("icon-chip")
        chip.add_css_class("small")
        chip.set_valign(Gtk.Align.CENTER)
        row.add_prefix(chip)
        dot = Gtk.Label(label="●", valign=Gtk.Align.CENTER)
        dot.add_css_class("dot")
        row.add_suffix(dot)
        return row, dot

    @staticmethod
    def _set_net(row_dot, text: str, level: str) -> None:
        row, dot = row_dot
        row.set_subtitle(text)
        for lv in ("success", "info", "warning", "error"):
            dot.remove_css_class(f"dot-{lv}")
        dot.add_css_class(f"dot-{level}")

    def _stat(self, caption: str):
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True, homogeneous=False)
        v = label("-", "stat-value")
        v.set_ellipsize(3)  # END
        col.append(v)
        col.append(label(caption, "stat-label"))
        return col, v

    # ------------------------------------------------------------------ actions
    def run_bg(self, fn):
        def work():
            try:
                fn()
            finally:
                GLib.idle_add(self._queue_refresh)
        threading.Thread(target=work, daemon=True).start()

    def _on_login(self, *_):
        if self.store.read_status().phase == Phase.BAD_CREDENTIALS.value:
            self.open_settings()
        else:
            self.run_bg(lambda: check(Reason.MANUAL, self.store))

    def _on_auto(self, sw, _p):
        on = sw.get_active()
        cfg = self.store.load()
        if cfg.auto_login == on:
            return
        self.store.update(auto_login=on, **({"pause": None} if on else {}))
        if on:
            self.run_bg(lambda: check(Reason.APP_OPEN, self.store))
        self.refresh()

    def _start_service(self, *_):
        def go():
            subprocess.run(["systemctl", "--user", "enable", "--now", "iiser-connect.service"], timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.run_bg(go)

    # ------------------------------------------------------------------ refresh
    WATCHED = {"status.json", "activity.json", "config.json", "credentials"}

    def _on_file_changed(self, _mon, file, other, _event):
        names = {f.get_basename() for f in (file, other) if f is not None}
        if names & self.WATCHED:
            self._queue_refresh()

    def _queue_refresh(self, *_):
        if not self._refresh_id:
            self._refresh_id = GLib.timeout_add(150, self._do_refresh)
        return False

    def _do_refresh(self):
        self._refresh_id = 0
        self.refresh()
        return False

    def _tick(self):
        self.refresh()
        return True

    def refresh(self):
        cfg, st = self.store.load(), self.store.read_status()
        if not self.store.has_credentials() and self.stack.get_visible_child_name() == "home":
            self.stack.set_visible_child_name("welcome")
        phase = st.phase
        title, sub = COPY.get(phase, COPY[Phase.IDLE.value])
        if phase == Phase.ONLINE.value and st.gateway == "":
            sub = "The internet works. This isn't the IISER network, so there's nothing to sign in to."
        elif st.detail and phase in (Phase.ONLINE.value, Phase.NEEDS_LOGIN.value, Phase.REJECTED.value):
            sub = st.detail
        elif st.detail and phase == Phase.ERROR.value:
            sub = f"{st.detail}. I'll try again shortly."
        self.title.set_label(title)
        self.sub.set_label(sub)
        self.home_orb.set_phase(phase)

        working = phase in WORKING
        online = phase == Phase.ONLINE.value
        bad = phase == Phase.BAD_CREDENTIALS.value
        self.login_btn.face("Update password" if bad else "Log in",
                            "dialog-password-symbolic" if bad else "network-wireless-symbolic",
                            busy=phase in (Phase.CHECKING.value, Phase.SIGNING_IN.value))
        self.logout_btn.face("Log out", "system-log-out-symbolic", busy=phase == Phase.SIGNING_OUT.value)
        self.login_btn.set_sensitive(not working and not online and phase != Phase.NO_CREDENTIALS.value)
        self.logout_btn.set_sensitive(not working and online and st.gateway != "")
        self.recheck.set_sensitive(not working)

        # what it found on this network
        if st.wifi is None:
            self._set_net(self.net_wifi, "Not checked yet", "info")
        elif st.wifi:
            self._set_net(self.net_wifi, "Connected", "success")
        else:
            self._set_net(self.net_wifi, "Not connected" + (" - waiting for Wi-Fi" if cfg.wifi_only else ""), "warning")
        if st.gateway:
            self._set_net(self.net_gw, f"{st.gateway} - found", "success")
        elif st.gateway == "":
            self._set_net(self.net_gw, "Not found on this network", "warning")
        else:
            self._set_net(self.net_gw, "Looking…", "info")

        # auto switch
        with self.auto_switch.handler_block(self._auto_handler):
            self.auto_switch.set_active(cfg.auto_login)
        if cfg.pause and cfg.auto_login:
            self.auto_row.set_subtitle("Paused - sign in once to resume")
        elif cfg.auto_login:
            self.auto_row.set_subtitle("Signs in whenever the campus network asks")
        else:
            self.auto_row.set_subtitle("Off - you'll sign in manually")

        self.stat_last[1].set_label(friendly_time(cfg.last_login))
        self.stat_count[1].set_label(str(cfg.login_count))
        self.stat_user[1].set_label(cfg.username or "-")

        self.svc_card.set_visible(not daemon_running(self.store))

        # activity rows (rebuilt only when something new happened)
        entries = self.store.activity()[:8]
        sig = tuple((e["t"], e["m"]) for e in entries)
        if sig == getattr(self, "_activity_sig", None):
            return
        self._activity_sig = sig
        while (r := self.activity.get_first_child()) is not None:
            self.activity.remove(r)
        if not entries:
            self.activity.append(self.empty_row)
        for e in entries:
            row = Adw.ActionRow(title=GLib.markup_escape_text(e["m"]), subtitle=short_time(e["t"]), title_lines=2)
            dot = Gtk.Label(label="●")
            dot.add_css_class("dot")
            dot.add_css_class(f"dot-{e['l']}")
            row.add_prefix(dot)
            self.activity.append(row)

    # ------------------------------------------------------------------ settings
    def open_settings(self):
        if self._settings_win is not None:
            self._settings_win.present()
            return
        from .settings import SettingsWindow
        self._settings_win = SettingsWindow(self)
        self._settings_win.connect("close-request", lambda *_: setattr(self, "_settings_win", None) or False)
        self._settings_win.present()
