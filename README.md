# IISER Connect for Linux

Signs this PC in to the IISER Thiruvananthapuram campus network (the Sophos gateway at
`gateway.iisertvm.ac.in`) automatically - no more gateway page. Sister app of the Android version.

* Native GTK4 / libadwaita window (dark + light), command-line tool, and a background service.
* **Only logs in when needed** - it checks whether you're already online first, **every 2 seconds** while
  you're on the campus Wi-Fi (about 0.2 % of one CPU core and 22 MB of RAM).
* **Wakes up with the screen** - turning the screen on, unlocking, or resuming from suspend triggers an
  immediate check, and it signs in if the internet is off.
* **Wi-Fi only** (switchable in Settings) - automatic sign-in only runs while connected to Wi-Fi. Away from the
  campus network (home Wi-Fi, hotspot) it does almost nothing.
* **Finds the gateway itself** when the internet is off, and the window has **Log in** and **Log out**
  buttons next to a live Wi-Fi / gateway status.
* **Safe with your account** - one wrong password and it stops (it never hammers the gateway).
* Password is URL-encoded properly, so characters like `& + % #` work.
* No servers, no analytics. The password is kept in `~/.config/iiser-connect/credentials`
  (mode 600, directory mode 700) and only ever sent to the gateway over HTTPS.

## Install

```bash
./install.sh            # installs under ~/.local, no root needed
```

Then open **IISER Connect** from the app menu, enter your campus login, and press *Save and connect*.
That also turns on the background service (`systemd --user`), which starts every time you log in.

Needs Python 3.10+, and for the window `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` (installed by default on
Ubuntu 22.04 GNOME). `./uninstall.sh` removes it (`--purge` also deletes the saved login).

If you still have the old `iiser-login.timer` (from `login.sh`), turn it off - it logs in every minute
even when you're online: `systemctl --user disable --now iiser-login.timer`.

## Command line

```
iiser-connect setup          save username/password (password is prompted, never on the command line)
iiser-connect status         what the watcher last saw      (--json for scripts)
iiser-connect login          sign in now, even if auto sign-in is paused
iiser-connect logout         sign out (pauses auto sign-in until you sign in again)
iiser-connect check          check now and sign in only if needed
iiser-connect forget         delete saved login + settings
iiser-connect gui | daemon
```

## How it works

`iiser-connect daemon` (the systemd service) decides when to look:

| situation | looks every | why |
|---|---|---|
| on the campus network (gateway found) | **2 s** | notice a lost session at once, sign in again |
| screen turns on / unlock / resume from suspend | right away | the internet may have dropped while you were away |
| network change (`nmcli monitor`) | right away (2 s debounce, 3 retries) | a newly joined network needs a moment |
| paused (wrong password, signed out, refused) | 10 s | nothing can happen automatically; just keeps the window fresh |
| online but no gateway here (home Wi-Fi) | 2 min | not the campus network - events do the work |
| no Wi-Fi and "Only on Wi-Fi" is on | 30 s | events do the work |

A look is one tiny plain-HTTP request to Google's `generate_204` (about 1 KB on the wire):

1. `204` -> already online, do nothing.
2. Otherwise the portal's own redirect says where the gateway is on this network. The address is only a
   *hint for where to connect*: the certificate must still be valid for an `*.iisertvm.ac.in` name, so a
   fake portal can never collect your password. If the gateway doesn't answer (home, hotspot) the network
   is left alone. If DNS fails it falls back to the gateway's IP, still verifying the real host name.
3. Otherwise `POST /login.xml` (`mode=191&username=..&password=..&a=<ms>&producttype=0`).
   Wrong password -> paused until you update it. Login limit / expired -> backs off 10 minutes.
   Automatic attempts are at least 15 s apart, so the 2-second watch can never hammer the gateway.
4. **Log out** (button or `iiser-connect logout`) pauses automatic sign-in on purpose - the screen turning
   on will *not* sign you back in until you press **Log in**.

The screen/resume triggers use `gdbus monitor` on GNOME's `org.gnome.ScreenSaver` and logind's
`PrepareForSleep`; if either isn't available the 2-second watch still covers it.

The interface only reads/writes small files in `~/.local/state/iiser-connect/` that the daemon also
uses, so the window and the service always agree.

## Tests

```bash
python3 -m unittest discover -s tests -v     # 37 tests, uses tools/mock_portal.py, never touches your real config
tools/shot.sh home dark out.png              # render a screen to a PNG (XWayland + ImageMagick)
```

The mock portal reproduces the gateway's replies (success, wrong password, login limit, session
expiry), so none of this needs - or touches - your real account.
