#!/usr/bin/env python3
"""pop-updater — GTK4/libadwaita update utility for Pop!_OS.

Modes:
  pop-updater          launches the GUI
  pop-updater --check  headless: refresh counts, write cache, notify if updates exist
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "pop-updater"
CACHE_FILE = CACHE_DIR / "last-check.json"
CONFIG_FILE = Path.home() / ".config" / "pop-updater.json"
TRAY_SERVICE = "pop-updater-tray.service"


# ---------- backend: each source returns (count, detail_lines) ----------

def _run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, "", str(e)


def check_apt():
    # Refresh runs via `sudo -n` thanks to /etc/sudoers.d/pop-updater-apt
    # (NOPASSWD scoped to `apt-get update` only). Falls back to pkexec if
    # the rule is missing.
    rc_u, _, _ = _run(["sudo", "-n", "/usr/bin/apt-get", "update", "-qq"], timeout=180)
    if rc_u != 0:
        _run(["pkexec", "apt-get", "update", "-qq"], timeout=180)
    rc, out, _ = _run(["apt", "list", "--upgradable"], timeout=60)
    if rc != 0:
        return -1, ["apt check failed"]
    items = []
    for line in out.splitlines():
        if "/" not in line or "upgradable" not in line:
            continue
        head, _, tail = line.partition("[upgradable from: ")
        parts = head.split()
        pkg = parts[0].split("/")[0] if parts else line.strip()
        newver = parts[1] if len(parts) > 1 else "?"
        oldver = tail.rstrip("]").strip() if tail else "?"
        items.append(f"{pkg}  {oldver} → {newver}")
    return len(items), items


def check_flatpak():
    rc, out, _ = _run(
        ["flatpak", "remote-ls", "--updates", "--columns=application,version,branch"],
        timeout=120,
    )
    if rc != 0:
        return -1, ["flatpak check failed"]
    items = []
    for line in out.splitlines():
        cols = [c.strip() for c in line.split("\t")]
        if not cols or not cols[0]:
            continue
        app = cols[0]
        ver = cols[1] if len(cols) > 1 else ""
        branch = cols[2] if len(cols) > 2 else ""
        if ver:
            items.append(f"{app}  {ver}")
        elif branch:
            items.append(f"{app}  ({branch})")
        else:
            items.append(app)
    return len(items), items


def check_firmware():
    _run(["fwupdmgr", "refresh", "--force"], timeout=60)
    rc, out, _ = _run(["fwupdmgr", "get-updates", "--json"], timeout=60)
    if rc != 0:
        return 0, []
    try:
        data = json.loads(out)
        devices = data.get("Devices", [])
        names = []
        for d in devices:
            for r in d.get("Releases", []):
                names.append(f"{d.get('Name', '?')} → {r.get('Version', '?')}")
        return len(names), names
    except json.JSONDecodeError:
        return 0, []


def check_release():
    rc, out, err = _run(["pop-upgrade", "release", "check"], timeout=30)
    combined = (out + err).lower()
    if "available" in combined and "not" not in combined.split("available")[0][-15:]:
        detail = [l.rstrip() for l in (out + err).splitlines() if l.strip()]
        return 1, detail or ["new Pop!_OS release available"]
    return 0, []


def check_snap():
    rc, out, _ = _run(["snap", "refresh", "--list"], timeout=60)
    if rc != 0:
        return -1, ["snap check failed"]
    items = []
    for line in out.splitlines():
        s = line.strip()
        low = s.lower()
        if not s or low.startswith("name") or "up to date" in low or "all snaps" in low:
            continue
        parts = s.split()
        if len(parts) >= 2:
            items.append(f"{parts[0]}  {parts[1]}")
    return len(items), items


def check_cargo():
    rc, out, _ = _run(["cargo", "install-update", "--list"], timeout=60)
    if rc != 0:
        return -1, ["cargo-install-update check failed"]
    items = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[-1] != "Yes":
            continue
        items.append(f"{parts[0]}  {parts[1]} → {parts[2]}")
    return len(items), items


def check_brew():
    rc, out, _ = _run(["brew", "outdated", "--json=v2"], timeout=120)
    if rc != 0:
        return -1, ["brew check failed"]
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return 0, []
    items = []
    for f in data.get("formulae", []) + data.get("casks", []):
        name = f.get("name", "?")
        installed = ", ".join(f.get("installed_versions", [])) or "?"
        latest = f.get("current_version", "?")
        items.append(f"{name}  {installed} → {latest}")
    return len(items), items


def _pypi_info(pkg, version=None):
    import urllib.request
    import urllib.error
    url = (
        f"https://pypi.org/pypi/{pkg}/{version}/json"
        if version else
        f"https://pypi.org/pypi/{pkg}/json"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pop-updater"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read()).get("info", {})
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def _pypi_latest(pkg):
    info = _pypi_info(pkg)
    return info.get("version") if info else None


def _strip_html(text):
    if not text:
        return text
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _truncate(text, limit=240):
    if not text:
        return text
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def _normalize_item(item):
    """Parse 'name  oldver → newver' (or firmware 'Name → Version') into a dict
    with name, summary (display text), and version (the new/target version)."""
    if isinstance(item, dict):
        return {
            "name": item.get("name", ""),
            "summary": item.get("summary", ""),
            "version": item.get("version", ""),
        }
    if not isinstance(item, str):
        return {"name": str(item), "summary": "", "version": ""}

    name, sep, rest = item.partition("  ")
    if sep:
        summary = rest.strip()
        version = summary.split(" → ", 1)[1].strip() if " → " in summary else summary
        return {"name": name.strip(), "summary": summary, "version": version}

    name2, sep2, rest2 = item.partition(" → ")
    if sep2:
        return {
            "name": name2.strip(),
            "summary": f"→ {rest2.strip()}",
            "version": rest2.strip(),
        }
    return {"name": item, "summary": "", "version": ""}


def check_pipx():
    rc, out, _ = _run(["pipx", "list", "--json"], timeout=30)
    if rc != 0:
        return -1, ["pipx check failed"]
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return 0, []
    items = []
    for name, meta in data.get("venvs", {}).items():
        main = meta.get("metadata", {}).get("main_package", {})
        installed = main.get("package_version", "?")
        pkg = main.get("package", name)
        latest = _pypi_latest(pkg)
        if latest and latest != installed:
            items.append(f"{name}  {installed} → {latest}")
    return len(items), items


def check_nix():
    rc, out, err = _run(["nix-env", "-u", "--dry-run"], timeout=60)
    items = []
    for line in (out + err).splitlines():
        s = line.strip()
        if s.startswith("upgrading"):
            items.append(s[len("upgrading "):].strip().strip("'"))
    return len(items), items


def check_all():
    results = {}
    for key, src in active_sources().items():
        count, items = src["check"]()
        results[key] = {"label": src["label"], "count": count, "items": items}
    results["_timestamp"] = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(results, indent=2))
    return results


def load_cache():
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text())
    except json.JSONDecodeError:
        return None


def load_config():
    if not CONFIG_FILE.exists():
        return {"auto_install": False}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except json.JSONDecodeError:
        return {"auto_install": False}


def save_config(cfg):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------- install commands (yield log lines) ----------

def install_apt():
    yield "$ pkexec apt-get upgrade -y\n"
    p = subprocess.Popen(
        ["pkexec", "apt-get", "upgrade", "-y"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in p.stdout:
        yield line
    p.wait()


def install_flatpak():
    yield "$ flatpak update -y\n"
    p = subprocess.Popen(
        ["flatpak", "update", "-y"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in p.stdout:
        yield line
    p.wait()


def install_firmware():
    yield "$ fwupdmgr update -y\n"
    p = subprocess.Popen(
        ["fwupdmgr", "update", "-y", "--no-reboot-check"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in p.stdout:
        yield line
    p.wait()


def install_snap():
    yield "$ pkexec snap refresh\n"
    p = subprocess.Popen(
        ["pkexec", "snap", "refresh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in p.stdout:
        yield line
    p.wait()


def install_cargo():
    yield "$ cargo install-update -a\n"
    p = subprocess.Popen(
        ["cargo", "install-update", "-a"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in p.stdout:
        yield line
    p.wait()


def install_brew():
    yield "$ brew upgrade\n"
    p = subprocess.Popen(
        ["brew", "upgrade"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in p.stdout:
        yield line
    p.wait()


def install_pipx():
    yield "$ pipx upgrade-all\n"
    p = subprocess.Popen(
        ["pipx", "upgrade-all"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in p.stdout:
        yield line
    p.wait()


def install_nix():
    yield "$ nix-env -u\n"
    p = subprocess.Popen(
        ["nix-env", "-u"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in p.stdout:
        yield line
    p.wait()


# ---------- per-source metadata fetchers (description + homepage) ----------
# Called lazily when the detail page opens. Each returns (description, homepage)
# with either field possibly None. Run on a worker thread — must be safe to call
# without holding the GTK lock.

def _parse_apt_changelog(text):
    """Pull the bullet points of the most recent changelog entry."""
    bullets = []
    in_entry = False
    headers_seen = 0
    for line in text.splitlines():
        if re.match(r"^\S+\s+\(", line):
            headers_seen += 1
            if headers_seen > 1:
                break
            in_entry = True
            continue
        if not in_entry:
            continue
        if line.startswith(" --"):
            break
        s = line.strip()
        if s.startswith("*") or s.startswith("-"):
            bullets.append(s.lstrip("*- ").strip())
        elif bullets and s and not s[0].isupper() is False:
            # Continuation line — append to last bullet.
            bullets[-1] += " " + s
    return "; ".join(bullets[:4]) if bullets else None


def meta_apt(name, version):
    rc, out, _ = _run(["apt", "changelog", name], timeout=20)
    desc = _parse_apt_changelog(out) if rc == 0 else None
    # Launchpad's per-source changelog page is the closest universal "where
    # did this update come from" link for Ubuntu-derived packages on Pop!_OS.
    home = f"https://launchpad.net/ubuntu/+source/{name}/+changelog"
    return desc, home


def meta_flatpak(app_id, version):
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            f"https://flathub.org/api/v2/appstream/{app_id}",
            headers={"User-Agent": "pop-updater"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None, f"https://flathub.org/apps/{app_id}"
    desc = None
    releases = data.get("releases") or []
    matched = None
    for rel in releases:
        if version and rel.get("version") == version:
            matched = rel
            break
    rel = matched or (releases[0] if releases else None)
    if rel:
        desc = _strip_html(rel.get("description") or rel.get("description_html"))
    return desc, f"https://flathub.org/apps/{app_id}"


def meta_snap(name, version):
    rc, out, _ = _run(["snap", "info", name], timeout=15)
    desc = None
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("summary:"):
                desc = line.split(":", 1)[1].strip()
                break
    return desc, f"https://snapcraft.io/{name}"


def meta_firmware(name, version):
    rc, out, _ = _run(["fwupdmgr", "get-updates", "--json"], timeout=30)
    if rc != 0:
        return None, None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, None
    for d in data.get("Devices", []):
        if d.get("Name") != name:
            continue
        for r in d.get("Releases", []):
            if version and r.get("Version") != version:
                continue
            desc = _strip_html(r.get("Description") or r.get("Summary"))
            home = r.get("DetailsUrl") or r.get("Homepage")
            return desc, home
        # Fallback if version didn't match any specific release
        if d.get("Releases"):
            r = d["Releases"][0]
            desc = _strip_html(r.get("Description") or r.get("Summary"))
            home = r.get("DetailsUrl") or r.get("Homepage")
            return desc, home
    return None, None


def meta_release(_name, version):
    return "Pop!_OS distribution upgrade — see release notes", "https://pop.system76.com/"


def meta_pipx(name, version):
    # Version-specific PyPI page often carries release notes from long_description.
    info = _pypi_info(name, version) or _pypi_info(name)
    desc = info.get("summary") if info else None
    home = (
        f"https://pypi.org/project/{name}/{version}/"
        if version else f"https://pypi.org/project/{name}/"
    )
    return desc, home


def meta_cargo(name, version):
    import urllib.request
    import urllib.error
    url = (
        f"https://crates.io/api/v1/crates/{name}/{version}"
        if version else f"https://crates.io/api/v1/crates/{name}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pop-updater"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None, f"https://crates.io/crates/{name}/{version}" if version else f"https://crates.io/crates/{name}"
    ver = data.get("version") or {}
    desc = ver.get("description") or (data.get("crate") or {}).get("description")
    home = f"https://crates.io/crates/{name}/{version}" if version else f"https://crates.io/crates/{name}"
    return desc, home


def meta_brew(name, version):
    rc, out, _ = _run(["brew", "info", "--json=v2", name], timeout=30)
    desc = None
    if rc == 0:
        try:
            data = json.loads(out)
            for f in data.get("formulae", []) + data.get("casks", []):
                if f.get("name") == name or f.get("token") == name:
                    desc = f.get("desc")
                    break
        except json.JSONDecodeError:
            pass
    return desc, f"https://formulae.brew.sh/formula/{name}"


def meta_nix(name, version):
    base = re.sub(r"-[0-9].*$", "", name)
    rc, out, _ = _run(["nix-env", "-qa", "--description", base], timeout=30)
    desc = None
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                desc = parts[1].strip()
                break
    return desc, f"https://search.nixos.org/packages?query={base}"


# ---------- source registry: each entry self-describes its availability ----------
# Adding a new package manager? Append an entry here and the GUI will pick it up
# automatically on the next Check Now (a row appears when `available()` flips True).

SOURCES = {
    "apt": {
        "label": "System packages (apt)",
        "available": lambda: shutil.which("apt-get") is not None,
        "check": check_apt,
        "installer": install_apt,
        "metadata": meta_apt,
    },
    "flatpak": {
        "label": "Flatpak apps",
        "available": lambda: shutil.which("flatpak") is not None,
        "check": check_flatpak,
        "installer": install_flatpak,
        "metadata": meta_flatpak,
    },
    "snap": {
        "label": "Snap apps",
        "available": lambda: shutil.which("snap") is not None,
        "check": check_snap,
        "installer": install_snap,
        "metadata": meta_snap,
    },
    "firmware": {
        "label": "Firmware (fwupd)",
        "available": lambda: shutil.which("fwupdmgr") is not None,
        "check": check_firmware,
        "installer": install_firmware,
        "metadata": meta_firmware,
    },
    "release": {
        "label": "Pop!_OS release",
        "available": lambda: shutil.which("pop-upgrade") is not None,
        "check": check_release,
        "installer": None,
        "metadata": meta_release,
    },
    "brew": {
        "label": "Homebrew",
        "available": lambda: shutil.which("brew") is not None,
        "check": check_brew,
        "installer": install_brew,
        "metadata": meta_brew,
    },
    "pipx": {
        "label": "pipx (Python CLIs)",
        "available": lambda: shutil.which("pipx") is not None,
        "check": check_pipx,
        "installer": install_pipx,
        "metadata": meta_pipx,
    },
    "cargo": {
        "label": "Cargo (Rust)",
        "available": lambda: shutil.which("cargo") is not None
                             and shutil.which("cargo-install-update") is not None,
        "check": check_cargo,
        "installer": install_cargo,
        "metadata": meta_cargo,
    },
    "nix": {
        "label": "Nix (user profile)",
        "available": lambda: shutil.which("nix-env") is not None,
        "check": check_nix,
        "installer": install_nix,
        "metadata": meta_nix,
    },
}

# Preferred install order when "Install All Updates" runs. Keys not present
# in SOURCES (or unavailable) are skipped.
INSTALL_ORDER = ("flatpak", "snap", "apt", "brew", "pipx", "cargo", "nix", "firmware")


def active_sources():
    return {k: v for k, v in SOURCES.items() if v["available"]()}


# ---------- headless --check entry point ----------

def headless_check():
    results = check_all()
    total = sum(
        v["count"] for k, v in results.items()
        if k != "_timestamp" and isinstance(v["count"], int) and v["count"] > 0
    )
    if total > 0 and shutil.which("notify-send"):
        body_parts = []
        for k, v in results.items():
            if k == "_timestamp":
                continue
            if isinstance(v["count"], int) and v["count"] > 0:
                body_parts.append(f"• {v['label']}: {v['count']}")
        subprocess.run([
            "notify-send",
            "--app-name=Pop Updater",
            "--icon=system-software-update",
            f"{total} updates available",
            "\n".join(body_parts),
        ])
        cfg = load_config()
        if cfg.get("auto_install"):
            for key in INSTALL_ORDER:
                if results.get(key, {}).get("count", 0) <= 0:
                    continue
                installer = SOURCES.get(key, {}).get("installer")
                if installer is None:
                    continue
                for _ in installer():
                    pass
    print(f"pop-updater: {total} updates")
    return 0


# ---------- GUI ----------

def run_gui():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk, Adw, GLib, Gio  # noqa: E402

    Adw.init()

    class UpdaterWindow(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Pop Updater")
            self.set_default_size(640, 820)

            self.toast_overlay = Adw.ToastOverlay()
            self.set_content(self.toast_overlay)

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.toast_overlay.set_child(outer)

            header = Adw.HeaderBar()
            self.check_btn = Gtk.Button(label="Check Now")
            self.check_btn.add_css_class("suggested-action")
            self.check_btn.connect("clicked", lambda *_: self.do_check())
            header.pack_end(self.check_btn)
            outer.append(header)

            self.stack = Gtk.Stack()
            self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
            self.stack.set_vexpand(True)
            self.stack.set_hexpand(True)
            outer.append(self.stack)

            # main page
            self.main_page = Adw.PreferencesPage()
            self.summary_group = Adw.PreferencesGroup(title="Available updates")
            self.main_page.add(self.summary_group)
            self.rows = {}
            for key, src in active_sources().items():
                self._add_row(key, src["label"])

            self.last_check_label = Gtk.Label(label="Never checked", xalign=0.5)
            self.last_check_label.add_css_class("dim-label")
            self.last_check_label.set_margin_top(12)
            self.main_page.add(self._wrap(self.last_check_label))

            self.install_btn = Gtk.Button(label="Install All Updates")
            self.install_btn.add_css_class("suggested-action")
            self.install_btn.add_css_class("pill")
            self.install_btn.set_halign(Gtk.Align.CENTER)
            self.install_btn.set_margin_top(8)
            self.install_btn.set_margin_bottom(8)
            self.install_btn.connect("clicked", lambda *_: self.do_install())
            self.main_page.add(self._wrap(self.install_btn))

            prefs_group = Adw.PreferencesGroup(
                title="Update tray",
                description=(
                    "When on, Pop Updater runs in your system tray. It checks "
                    "for updates ~20 seconds after login, then again every "
                    "6 hours, and notifies you when updates are available. "
                    "When off, the tray is removed and nothing runs in the "
                    "background — you'd need to open this app manually."
                ),
            )
            self.tray_row = Adw.SwitchRow(
                title="Show update tray icon",
                subtitle="Autostarts at login • periodic checks • desktop notifications",
            )
            self.tray_row.connect("notify::active", self._on_tray_toggled)
            prefs_group.add(self.tray_row)

            self.auto_install_row = Adw.SwitchRow(
                title="Auto-install on login (advanced)",
                subtitle="Risky: can break running session if compositor/kernel updates land",
            )
            self.auto_install_row.connect("notify::active", self._on_auto_install_toggled)
            prefs_group.add(self.auto_install_row)
            self.main_page.add(prefs_group)

            self.stack.add_named(self.main_page, "main")

            # install/log page
            log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            log_box.set_margin_top(12); log_box.set_margin_bottom(12)
            log_box.set_margin_start(12); log_box.set_margin_end(12)
            scroller = Gtk.ScrolledWindow()
            scroller.set_vexpand(True)
            self.log_view = Gtk.TextView()
            self.log_view.set_editable(False)
            self.log_view.set_monospace(True)
            scroller.set_child(self.log_view)
            log_box.append(scroller)
            self.back_btn = Gtk.Button(label="Back")
            self.back_btn.set_halign(Gtk.Align.CENTER)
            self.back_btn.connect("clicked", lambda *_: self.stack.set_visible_child_name("main"))
            self.back_btn.set_sensitive(False)
            log_box.append(self.back_btn)
            self.stack.add_named(log_box, "log")

            # detail page
            detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            detail_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            detail_top.set_margin_top(8); detail_top.set_margin_bottom(4)
            detail_top.set_margin_start(8); detail_top.set_margin_end(8)
            detail_back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
            detail_back.set_tooltip_text("Back")
            detail_back.add_css_class("flat")
            detail_back.connect("clicked", lambda *_: self.stack.set_visible_child_name("main"))
            detail_top.append(detail_back)
            self.detail_title = Gtk.Label(label="Details", xalign=0)
            self.detail_title.add_css_class("title-3")
            self.detail_title.set_hexpand(True)
            detail_top.append(self.detail_title)
            detail_box.append(detail_top)

            detail_scroller = Gtk.ScrolledWindow()
            detail_scroller.set_vexpand(True)
            detail_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            detail_inner.set_margin_top(8); detail_inner.set_margin_bottom(12)
            detail_inner.set_margin_start(12); detail_inner.set_margin_end(12)
            self.detail_list = Gtk.ListBox()
            self.detail_list.set_selection_mode(Gtk.SelectionMode.NONE)
            self.detail_list.add_css_class("boxed-list")
            detail_inner.append(self.detail_list)
            detail_scroller.set_child(detail_inner)
            detail_box.append(detail_scroller)
            self.stack.add_named(detail_box, "detail")

            self._load_state()
            cached = load_cache()
            if cached:
                self._render(cached)
            else:
                GLib.idle_add(self.do_check)

        def _wrap(self, widget):
            g = Adw.PreferencesGroup()
            g.add(widget)
            return g

        def _add_row(self, key, label):
            row = Adw.ActionRow(title=label, subtitle="—")
            badge = Gtk.Label(label="…")
            badge.add_css_class("dim-label")
            row.add_suffix(badge)
            chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
            chevron.add_css_class("dim-label")
            chevron.set_visible(False)
            row.add_suffix(chevron)
            row.badge = badge
            row.chevron = chevron
            row.source_key = key
            row.set_activatable(False)
            row.connect("activated", lambda r: self._show_detail(r.source_key))
            self.summary_group.add(row)
            self.rows[key] = row

        def _load_state(self):
            cfg = load_config()
            self.auto_install_row.set_active(cfg.get("auto_install", False))
            enabled = subprocess.run(
                ["systemctl", "--user", "is-enabled", TRAY_SERVICE],
                capture_output=True, text=True,
            ).stdout.strip() == "enabled"
            self.tray_row.set_active(enabled)

        def _on_tray_toggled(self, row, *_):
            # `--now` starts/stops the service as well as enabling/disabling it,
            # so the tray icon appears/disappears immediately instead of waiting
            # for the next login.
            action = "enable" if row.get_active() else "disable"
            subprocess.run(
                ["systemctl", "--user", action, "--now", TRAY_SERVICE],
                capture_output=True,
            )

        def _on_auto_install_toggled(self, row, *_):
            cfg = load_config()
            cfg["auto_install"] = row.get_active()
            save_config(cfg)

        def do_check(self):
            self.check_btn.set_sensitive(False)
            self.check_btn.set_label("Checking…")
            import threading
            def worker():
                results = check_all()
                GLib.idle_add(self._render, results)
                GLib.idle_add(self._finish_check)
            threading.Thread(target=worker, daemon=True).start()
            return False

        def _finish_check(self):
            self.check_btn.set_sensitive(True)
            self.check_btn.set_label("Check Now")
            self.toast_overlay.add_toast(Adw.Toast(title="Check complete"))

        def _render(self, results):
            self._last_results = results
            present = [k for k in results if k != "_timestamp"]

            # Remove rows for sources that disappeared (uninstalled).
            for key in list(self.rows.keys()):
                if key not in present:
                    self.summary_group.remove(self.rows[key])
                    del self.rows[key]

            # Add rows for newly-present sources.
            for key in present:
                if key not in self.rows:
                    label = results[key].get("label", SOURCES.get(key, {}).get("label", key))
                    self._add_row(key, label)

            total_installable = 0
            for key in present:
                row = self.rows[key]
                data = results.get(key, {})
                count = data.get("count")
                row.set_activatable(False)
                row.chevron.set_visible(False)
                if count is None:
                    row.set_subtitle("Not installed")
                    row.badge.set_label("—")
                elif count == -1:
                    row.set_subtitle("Check failed")
                    row.badge.set_label("!")
                elif count == 0:
                    row.set_subtitle("Up to date")
                    row.badge.set_label("✓")
                else:
                    items = data.get("items", [])
                    names = []
                    for it in items[:3]:
                        norm = _normalize_item(it)
                        if norm["name"]:
                            names.append(norm["name"])
                    sample = ", ".join(names)
                    if len(items) > 3:
                        sample += f", +{len(items) - 3} more"
                    row.set_subtitle(sample or f"{count} updates")
                    row.badge.set_label(str(count))
                    row.set_activatable(True)
                    row.chevron.set_visible(True)
                    if SOURCES.get(key, {}).get("installer"):
                        total_installable += count
            ts = results.get("_timestamp")
            if ts:
                age = int(time.time() - ts)
                if age < 60:
                    self.last_check_label.set_label("Last checked just now")
                elif age < 3600:
                    self.last_check_label.set_label(f"Last checked {age // 60} min ago")
                else:
                    self.last_check_label.set_label(time.strftime("Last checked %H:%M", time.localtime(ts)))
            self.install_btn.set_sensitive(total_installable > 0)
            if total_installable == 0:
                self.install_btn.set_label("Everything is up to date")
            else:
                self.install_btn.set_label(f"Install {total_installable} Updates")

        def _show_detail(self, key):
            results = getattr(self, "_last_results", None) or load_cache() or {}
            data = results.get(key, {})
            raw_items = data.get("items", []) or []
            label = SOURCES.get(key, {}).get("label", key)
            self.detail_title.set_label(f"{label}  ({len(raw_items)})")

            # Clear any rows from a previous detail view.
            child = self.detail_list.get_first_child()
            while child:
                self.detail_list.remove(child)
                child = self.detail_list.get_first_child()

            if not raw_items:
                self.detail_list.append(Adw.ActionRow(title="No details available"))
                self.stack.set_visible_child_name("detail")
                return

            detail_rows = []
            for raw in raw_items:
                item = _normalize_item(raw)
                row = Adw.ActionRow(title=item["name"] or "?")
                base_subtitle = item.get("summary", "")
                row.set_subtitle(base_subtitle)
                row.set_subtitle_lines(3)
                link_btn = Gtk.Button.new_from_icon_name("emblem-symbolic-link")
                link_btn.set_tooltip_text("Open project page")
                link_btn.add_css_class("flat")
                link_btn.set_valign(Gtk.Align.CENTER)
                link_btn.set_visible(False)
                row.add_suffix(link_btn)
                row.link_btn = link_btn
                row.base_subtitle = base_subtitle
                self.detail_list.append(row)
                detail_rows.append((row, item))

            self.stack.set_visible_child_name("detail")

            meta_fn = SOURCES.get(key, {}).get("metadata")
            if not meta_fn:
                return

            # Token guards against stale fetches after navigating away.
            self._detail_token = token = object()

            def fetch():
                for row, item in detail_rows:
                    if token is not getattr(self, "_detail_token", None):
                        return
                    try:
                        desc, home = meta_fn(item["name"], item.get("version", ""))
                    except Exception:
                        desc, home = None, None
                    GLib.idle_add(self._apply_meta, token, row, desc, home)

            import threading
            threading.Thread(target=fetch, daemon=True).start()

        def _apply_meta(self, token, row, desc, home):
            if token is not getattr(self, "_detail_token", None):
                return False
            if desc:
                desc = _truncate(desc, 280)
                base = getattr(row, "base_subtitle", "") or ""
                row.set_subtitle(f"{base}\n{desc}" if base else desc)
            if home:
                btn = row.link_btn
                btn.set_visible(True)
                btn.connect("clicked", lambda *_: self._open_uri(home))
            return False

        def _open_uri(self, uri):
            Gio.AppInfo.launch_default_for_uri(uri, None)

        def do_install(self):
            cached = load_cache() or {}
            queue = [
                k for k in INSTALL_ORDER
                if cached.get(k, {}).get("count", 0) > 0
                and SOURCES.get(k, {}).get("installer") is not None
            ]
            if not queue:
                return
            self.stack.set_visible_child_name("log")
            self.back_btn.set_sensitive(False)
            buf = self.log_view.get_buffer()
            buf.set_text("")
            import threading
            def append(text):
                end = buf.get_end_iter()
                buf.insert(end, text)
                self.log_view.scroll_to_iter(buf.get_end_iter(), 0.0, False, 0, 0)
                return False
            def worker():
                for key in queue:
                    GLib.idle_add(append, f"\n=== {SOURCES[key]['label']} ===\n")
                    for line in SOURCES[key]["installer"]():
                        GLib.idle_add(append, line)
                GLib.idle_add(append, "\n=== Done ===\n")
                GLib.idle_add(self.back_btn.set_sensitive, True)
                GLib.idle_add(self.do_check)
            threading.Thread(target=worker, daemon=True).start()

    class UpdaterApp(Adw.Application):
        def __init__(self):
            super().__init__(application_id="org.william.pop-updater",
                             flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

        def do_activate(self):
            win = self.props.active_window or UpdaterWindow(self)
            win.present()

    app = UpdaterApp()
    return app.run(sys.argv[:1])


# ---------- persistent tray ----------
# Runs as its own GTK3 process (GTK3 + GTK4 cannot coexist). Clicking "Open
# Pop Updater" spawns the existing GTK4 GUI as a separate subprocess.

TRAY_CHECK_INTERVAL_SECONDS = 6 * 3600
TRAY_INITIAL_DELAY_SECONDS = 20


def run_tray():
    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator3
    except (ValueError, ImportError):
        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3
        except (ValueError, ImportError):
            sys.stderr.write(
                "Tray needs AppIndicator. Install with:\n"
                "  sudo apt install gir1.2-ayatanaappindicator3-0.1\n"
            )
            return 1
    from gi.repository import Gtk, GLib  # noqa: E402

    indicator = AppIndicator3.Indicator.new(
        "pop-updater",
        "system-software-update",
        AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
    )
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
    indicator.set_title("Pop Updater")

    menu = Gtk.Menu()
    status_item = Gtk.MenuItem(label="Loading…")
    status_item.set_sensitive(False)
    menu.append(status_item)
    menu.append(Gtk.SeparatorMenuItem())
    open_item = Gtk.MenuItem(label="Open Pop Updater")
    menu.append(open_item)
    check_item = Gtk.MenuItem(label="Check for updates now")
    menu.append(check_item)
    menu.append(Gtk.SeparatorMenuItem())
    quit_item = Gtk.MenuItem(label="Quit tray")
    menu.append(quit_item)
    menu.show_all()
    indicator.set_menu(menu)

    state = {"last_total": -1}

    def total_from(results):
        return sum(
            v["count"] for k, v in results.items()
            if k != "_timestamp" and isinstance(v.get("count"), int) and v["count"] > 0
        )

    def render(results):
        total = total_from(results)
        if total <= 0:
            status_item.set_label("No updates available")
            indicator.set_icon_full("system-software-update", "No updates")
        else:
            status_item.set_label(f"{total} update{'s' if total != 1 else ''} available")
            indicator.set_icon_full("software-update-available", "Updates available")
            # Notify only on the rising edge (new updates appeared since last render).
            if state["last_total"] >= 0 and total > state["last_total"] and shutil.which("notify-send"):
                body_parts = []
                for k, v in results.items():
                    if k == "_timestamp":
                        continue
                    if isinstance(v.get("count"), int) and v["count"] > 0:
                        body_parts.append(f"• {v['label']}: {v['count']}")
                subprocess.run([
                    "notify-send",
                    "--app-name=Pop Updater",
                    "--icon=software-update-available",
                    f"{total} updates available",
                    "\n".join(body_parts),
                ])
        state["last_total"] = total

    def refresh_from_cache():
        cached = load_cache()
        if cached:
            render(cached)
        return False

    def do_check():
        check_item.set_sensitive(False)
        check_item.set_label("Checking…")
        import threading

        def worker():
            try:
                results = check_all()
            except Exception as e:
                results = None
                sys.stderr.write(f"pop-updater tray check failed: {e}\n")

            def finish():
                check_item.set_sensitive(True)
                check_item.set_label("Check for updates now")
                if results is not None:
                    render(results)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def on_open(_):
        # Launch the GTK4 GUI as a separate process; tray keeps running.
        subprocess.Popen([sys.argv[0]], start_new_session=True)

    open_item.connect("activate", on_open)
    check_item.connect("activate", lambda *_: do_check())
    quit_item.connect("activate", lambda *_: Gtk.main_quit())

    refresh_from_cache()
    GLib.timeout_add_seconds(TRAY_INITIAL_DELAY_SECONDS, lambda: do_check() or False)

    def periodic():
        do_check()
        return True

    GLib.timeout_add_seconds(TRAY_CHECK_INTERVAL_SECONDS, periodic)

    Gtk.main()
    return 0


# ---------- entrypoint ----------

if __name__ == "__main__":
    if "--tray" in sys.argv:
        sys.exit(run_tray())
    if "--check" in sys.argv:
        sys.exit(headless_check())
    sys.exit(run_gui())
