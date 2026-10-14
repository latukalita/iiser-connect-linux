"""Gtk application entry point."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, Gio, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME
from .window import MainWindow


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.win: MainWindow | None = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        scheme = os.environ.get("IISER_CONNECT_SCHEME")
        if scheme:
            Adw.StyleManager.get_default().set_color_scheme(
                Adw.ColorScheme.FORCE_DARK if scheme == "dark" else Adw.ColorScheme.FORCE_LIGHT)

    def do_activate(self):
        if self.win is None:
            self.win = MainWindow(self)
        self.win.present()
        page = os.environ.get("IISER_CONNECT_PAGE", "")      # used by tools/shot.sh
        if page in ("welcome", "account", "home"):
            self.win.stack.set_visible_child_name(page)
        elif page == "settings":
            self.win.open_settings()


def main(argv=None) -> int:
    GLib.set_application_name(APP_NAME)
    return App().run(argv or sys.argv)
