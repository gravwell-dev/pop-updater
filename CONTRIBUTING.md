# Contributing to Pop Updater

Thanks for thinking about contributing — this is a small project, and small
contributions are very welcome.

> **Project status:** unofficial community tool. Not affiliated with
> System76 or the Pop!_OS project.

## Reporting a bug

Open an issue at https://github.com/gravwell-dev/pop-updater/issues with:

- Your Pop!_OS / Ubuntu version (`cat /etc/os-release`)
- Your desktop (`echo $XDG_CURRENT_DESKTOP`)
- Whether the bug is in the **GUI** (`pop-updater`) or the **tray**
  (`pop-updater-tray.service`)
- Relevant logs:
  - GUI errors: re-run from a terminal as `pop-updater` and paste stderr
  - Tray errors: `journalctl --user -u pop-updater-tray.service -n 50`
- Steps to reproduce, and what you expected vs. what happened

For tray icons that don't appear, also paste:
`busctl --user list | grep StatusNotifier`

## Running locally during development

```bash
git clone https://github.com/gravwell-dev/pop-updater.git
cd pop-updater
python3 pop_updater.py            # run the GUI directly
python3 pop_updater.py --tray     # run the tray (stop the systemd unit
                                  # first if it's running:
                                  # systemctl --user stop pop-updater-tray)
python3 pop_updater.py --check    # headless one-shot check
```

You **don't** need to run `./install.sh` to develop — only to deploy. The
install script writes to `~/.config/`, `~/.local/`, and `/etc/sudoers.d/`;
running directly from the clone bypasses all of that.

## Adding a new package manager

See the *"Adding a new package manager"* section of the README. The short
version: append a dict entry to `SOURCES` in `pop_updater.py` with
`label`, `available`, `check`, `installer`, and `metadata` callables. The
GUI and tray pick it up automatically — no UI code to touch.

## Code style

- Match what's already in `pop_updater.py`: 4-space indent, standard
  library only (Python 3.10+), no unnecessary abstractions.
- Comments only where the *why* isn't obvious. Don't narrate what code
  does — names should do that.
- Don't run `Black` or similar over the whole file — keep diffs minimal.

## Pull requests

Small PRs land faster than large ones. If you're considering a substantial
change (new source, big refactor, UI rework), open an issue first to align
on the approach.

Sign-off / DCO is not required. By submitting a PR you agree your
contribution is licensed under the same MIT license as the rest of the
project.
