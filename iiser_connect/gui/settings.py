"""Settings window (Adw.PreferencesWindow)."""
from __future__ import annotations

import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import __version__
from ..cli import daemon_running
from ..engine import Reason, check
from ..store import DEFAULT_HOST, DEFAULT_IP


class SettingsWindow(Adw.PreferencesWindow):
    def __init__(self, main):
        super().__init__(title="Settings", transient_for=main, modal=True, default_width=540, default_height=720,
                         search_enabled=False)
        self.main = main
        self.store = main.store
        cfg = self.store.load()
        page = Adw.PreferencesPage()
        self.add(page)

        # -- account --------------------------------------------------------------------
        g = Adw.PreferencesGroup(title="Account", description="Your campus login. The password is kept in a private file (mode 600) on this PC.")
        self.user = Gtk.Entry(text=cfg.username, placeholder_text="Username", hexpand=True)
        self.pw = Gtk.PasswordEntry(placeholder_text="New password", show_peek_icon=True, hexpand=True)
        for w in (self.user, self.pw):
            w.set_size_request(-1, 46)
        self.user.connect("changed", self._changed)
        self.pw.connect("changed", self._changed)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        col.append(self.user)
        col.append(self.pw)
        self.save_btn = Gtk.Button(label="Save and sign in", sensitive=False, margin_top=4)
        self.save_btn.add_css_class("gradient")
        self.save_btn.connect("clicked", self._save_account)
        col.append(self.save_btn)
        g.add(col)
        page.add(g)

        # -- behaviour ------------------------------------------------------------------
        g = Adw.PreferencesGroup(title="Behaviour")
        self.auto = Gtk.Switch(active=cfg.auto_login, valign=Gtk.Align.CENTER)
        r = Adw.ActionRow(title="Auto sign-in", subtitle="Sign in whenever the campus network asks for it", activatable_widget=self.auto)
        r.add_suffix(self.auto)
        self.auto.connect("notify::active", lambda sw, _p: main.store.update(auto_login=sw.get_active(), **({"pause": None} if sw.get_active() else {})))
        g.add(r)
        self.wifi_sw = Gtk.Switch(active=cfg.wifi_only, valign=Gtk.Align.CENTER)
        r = Adw.ActionRow(title="Only on Wi-Fi", subtitle="Sign in automatically only while connected to Wi-Fi. Switch off to also cover a cable.",
                          subtitle_lines=3, activatable_widget=self.wifi_sw)
        r.add_suffix(self.wifi_sw)
        self.wifi_sw.connect("notify::active", self._wifi_only_changed)
        g.add(r)
        self.notify_sw = Gtk.Switch(active=cfg.notify, valign=Gtk.Align.CENTER)
        r = Adw.ActionRow(title="Notifications", subtitle="Quiet heads-up on success, and alerts if something fails", activatable_widget=self.notify_sw)
        r.add_suffix(self.notify_sw)
        self.notify_sw.connect("notify::active", lambda sw, _p: main.store.update(notify=sw.get_active()))
        g.add(r)
        page.add(g)

        # -- background service ---------------------------------------------------------
        g = Adw.PreferencesGroup(title="Background service",
                                 description="Signs you in automatically from the moment you log in to this PC, even with this window closed.")
        self.svc_row = Adw.ActionRow(title="Service")
        self.svc_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.svc_btn.connect("clicked", self._service_clicked)
        self.svc_row.add_suffix(self.svc_btn)
        g.add(self.svc_row)
        page.add(g)
        self._update_service()
        GLib.timeout_add_seconds(2, lambda: (self._update_service(), self.get_visible())[1])

        # -- advanced -------------------------------------------------------------------
        g = Adw.PreferencesGroup(title="Advanced", description="Only change these if IISER moves its gateway. The password is only ever sent to this address, over HTTPS.")
        self.host = Gtk.Entry(text=cfg.host, hexpand=True, placeholder_text=DEFAULT_HOST)
        self.ip = Gtk.Entry(text=cfg.ip, hexpand=True, placeholder_text=DEFAULT_IP)
        for w in (self.host, self.ip):
            w.set_size_request(-1, 46)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        col.append(Gtk.Label(label="Gateway address", xalign=0, css_classes=["small-dim"]))
        col.append(self.host)
        col.append(Gtk.Label(label="Backup IP (used if the name won't resolve)", xalign=0, css_classes=["small-dim"]))
        col.append(self.ip)
        b = Gtk.Button(label="Save gateway", margin_top=4)
        b.add_css_class("ghost")
        b.connect("clicked", lambda *_: self.store.update(host=self.host.get_text().strip() or DEFAULT_HOST, ip=self.ip.get_text().strip()))
        col.append(b)
        g.add(col)
        page.add(g)

        # -- privacy --------------------------------------------------------------------
        g = Adw.PreferencesGroup(title="Privacy",
                                 description=f"Nothing leaves this PC except the sign-in to the IISER gateway. No accounts, no analytics, no servers. IISER Connect {__version__}.")
        forget = Gtk.Button(label="Forget my login and reset", margin_top=2)
        forget.add_css_class("destructive-action")
        forget.connect("clicked", self._forget)
        g.add(forget)
        page.add(g)

    # ---------------------------------------------------------------------------
    def _wifi_only_changed(self, sw, _p):
        self.store.update(wifi_only=sw.get_active())
        self.main.run_bg(lambda: check(Reason.APP_OPEN, self.store))      # apply it right away

    def _changed(self, *_):
        self.save_btn.set_sensitive(bool(self.user.get_text().strip() and self.pw.get_text()))

    def _save_account(self, *_):
        self.store.set_credentials(self.user.get_text(), self.pw.get_text())
        self.pw.set_text("")
        self.main.run_bg(lambda: check(Reason.MANUAL, self.store))
        self.close()

    def _update_service(self):
        running = daemon_running(self.store)
        self.svc_row.set_subtitle("Running" if running else "Not running")
        self.svc_btn.set_label("Restart" if running else "Turn on")
        self.svc_btn.remove_css_class("suggested-action")
        if not running:
            self.svc_btn.add_css_class("suggested-action")

    def _service_clicked(self, *_):
        def go():
            verb = "restart" if daemon_running(self.store) else "enable"
            cmd = ["systemctl", "--user", "restart", "iiser-connect.service"] if verb == "restart" else \
                  ["systemctl", "--user", "enable", "--now", "iiser-connect.service"]
            subprocess.run(cmd, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.main.run_bg(go)

    def _forget(self, *_):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text="Forget everything?",
                              secondary_text="This removes your saved login and settings from this PC and stops auto sign-in.")
        d.add_button("Cancel", Gtk.ResponseType.CANCEL)
        d.add_button("Forget", Gtk.ResponseType.ACCEPT).add_css_class("destructive-action")

        def resp(dlg, r):
            dlg.destroy()
            if r == Gtk.ResponseType.ACCEPT:
                self.store.forget_everything()
                self.main.stack.set_visible_child_name("welcome")
                self.main.refresh()
                self.close()
        d.connect("response", resp)
        d.present()
