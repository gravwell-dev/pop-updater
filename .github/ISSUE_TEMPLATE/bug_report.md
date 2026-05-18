---
name: Bug report
about: Report a problem with Pop Updater
title: ""
labels: bug
assignees: ""
---

**What went wrong**

<!-- A short description of the issue. -->

**Where it happened**

- [ ] GUI (`pop-updater`)
- [ ] Tray (`pop-updater-tray.service`)
- [ ] Headless check (`pop-updater --check`)
- [ ] Install / uninstall script

**Steps to reproduce**

1.
2.
3.

**Expected vs. actual**

Expected:

Got:

**Environment**

Paste the output of:

```
cat /etc/os-release | head -5
echo $XDG_CURRENT_DESKTOP
python3 --version
```

**Logs**

For tray bugs, paste the last ~50 lines:
`journalctl --user -u pop-updater-tray.service -n 50`

For "the tray icon isn't appearing":
`busctl --user list | grep StatusNotifier`

For GUI bugs, run from a terminal and paste stderr:
`pop-updater 2>&1 | tail -30`

**Anything else**

<!-- Screenshots, related issues, ideas for a fix, etc. -->
