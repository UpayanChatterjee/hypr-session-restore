# hypr-session-restore

macOS-style **"reopen windows on login"** for [Hyprland](https://hypr.land) 0.55+.

Log out, reboot, or shut down — on your next login every window comes back on the workspace it was on, silently, without moving your focus.
Floating windows return to their exact position and size; pinned, fullscreen and scratchpad states come back too.

A single dependency-free Python script: no daemon framework, no compositor patches, no plugin — just `hyprctl` and `/proc`.

## How it works

- **`save`** snapshots every mapped window: class, workspace, geometry, floating/pinned/fullscreen state, and a relaunch command recovered from `/proc/<pid>/cmdline` + `/proc/<pid>/cwd`.
- **`restore`** (run on Hyprland startup) relaunches each window with PID-tracked window rules:

  ```lua
  hl.dsp.exec_cmd([[zen-bin]], { workspace = '2 silent' })
  ```

  so everything opens on the right workspace without stealing focus.
  A sweep pass then places the windows that escape PID tracking, from forking and single-instance apps.
  It never moves one that sits in a group, because moving a member moves them all.

  Every window is launched, not every process: only a browser is launched once per process, because it reopens its remaining windows from its own session and a second launch would duplicate them.
- **Groups** are rebuilt at the end, from the windows the sweep placed.
  Hyprland's Lua API exposes the group object, so `hl.get_window("address:…").group:add(…)` takes an existing window: the first member of each saved group becomes a group and the rest are added by address, in saved left-to-right order.
  Nothing depends on focus, on direction or on arrival order, and no group is ever locked.

  That is what lets every window launch in one pass — a group that does not exist yet cannot swallow what opens next — so a slow app overlaps with the others instead of holding up the queue.
  Six windows whose apps each take three seconds to start come back in five seconds rather than nineteen.
- **Monitors** are restored last: these workspaces bind to no monitor, so at boot they pile onto whichever one was focused.
  Each is moved as a whole onto the monitor it was saved on, by name, because Hyprland renumbers monitors across boots.
  A single-monitor machine skips the pass.
- **`shutdown`** saves, then SIGTERMs the browsers so they write their sessions: a plain close hits the "close N tabs?" dialog, the process is killed, and Chromium records `exit_type=Crashed` and drops the tabs.
  Run it from the power menu in place of `save`.
- **`daemon`** re-saves every 60 s, so even a power-button shutdown or a crash restores a session at most a minute stale.
  Wiring `save` into your power menu (see below) makes clean logouts exact.

### Terminals get special treatment

A terminal's command line says nothing about what was in it, so the script walks the terminal's process tree instead:

- If a known TUI was running (`yazi`, `nvim`, `btop`, `htop`, `ranger`, `lf`), it is relaunched in its working directory: `kitty -d ~/Downloads yazi`.
- Otherwise the terminal reopens in the shell's last working directory.

Supported terminals: **kitty**, **foot**, **alacritty** (trivial to extend — see `TERMINALS` at the top of the script).

### Apps restore their own state

The script reopens windows; content restoration rides on each app's own persistence, which works better than you'd expect:

| App | What comes back |
| --- | --- |
| sioyek / okular / zathura | last document at last page (document path from cmdline) |
| Obsidian | last open vault(s) |
| Firefox / Zen / Chromium | own session restore (tabs) |
| Dolphin | open tabs — enable *Settings → Startup → "same locations as when closed"* |
| yazi / nvim in a terminal | relaunched in the same directory |

## What it can't do

These are compositor and protocol limits, not bugs:

- **Exact tiling layout**: Hyprland doesn't expose the dwindle/master split tree.
  Windows land on the right workspace and monitor and re-tile in saved left-to-right order — close, but not split-for-split identical.
  Floating windows *are* pixel-exact.
- **An app slower than `SWEEP_TIMEOUT` is missed.** The sweep ends as soon as every window is accounted for, so the wait costs nothing when they turn up, but one that has not mapped a window by then is left unplaced and out of its group.
  It is named in the log either way.
- **Unsaved in-app state**: terminal scrollback, scroll positions, unsaved edits.
  True macOS-grade restore needs the Wayland `xdg-session-management` protocol, which apps don't implement yet.
- Multiple same-class windows from one single-instance process are matched best-effort by the sweep pass.
- Apps launched purely via D-Bus activation (meaningless cmdline) may not relaunch. Rare.

## Requirements

- Hyprland **>= 0.55** (uses the Lua-era dispatcher syntax; older hyprlang-based versions are not supported)
- Python 3.9+ (stdlib only)

## Install

```sh
git clone https://github.com/UpayanChatterjee/hypr-session-restore
mkdir -p ~/.config/hypr/scripts
install -m755 hypr-session-restore/hypr-session-restore ~/.config/hypr/scripts/
```

Add to your Hyprland startup (e.g. `~/.config/hypr/hyprland/execs.lua`):

```lua
hl.on("hyprland.start", function()
    -- ... your other autostarts ...

    -- Session restore: reopen previous session's windows, then keep saving state
    hl.exec_cmd("sleep 2 && ~/.config/hypr/scripts/hypr-session-restore restore")
    hl.exec_cmd("~/.config/hypr/scripts/hypr-session-restore daemon")
end)
```

That alone gives you working session restore (at most 60 s stale).

### Recommended: wire `save` into your power menu

For an exact snapshot on clean exits, run `save` right before logout/shutdown/reboot.
Whatever launches those commands — a shell power menu, wlogout, a keybind — prepend the save:

```sh
~/.config/hypr/scripts/hypr-session-restore save; hyprctl dispatch 'hl.dsp.exit()'
~/.config/hypr/scripts/hypr-session-restore save; systemctl poweroff
~/.config/hypr/scripts/hypr-session-restore save; systemctl reboot
```

<details>
<summary>Example: caelestia shell</summary>

In `~/.config/caelestia/shell.json`:

```json
"session": {
    "commands": {
        "logout":   ["sh", "-c", "~/.config/hypr/scripts/hypr-session-restore save; hyprctl dispatch 'hl.dsp.exit()'"],
        "reboot":   ["sh", "-c", "~/.config/hypr/scripts/hypr-session-restore save; systemctl reboot"],
        "shutdown": ["sh", "-c", "~/.config/hypr/scripts/hypr-session-restore save; systemctl poweroff"]
    }
}
```

(Leave hibernate alone — it resumes the live session natively.)

</details>

## Configuration

Everything lives at the top of the script:

| Knob | Meaning |
| --- | --- |
| `EXCLUDE_CLASSES` | window classes never saved/restored — add anything your autostart already launches (bars, tray bridges, scratchpad daemons) |
| `TERMINALS` | terminal class → (binary, cwd flag, exec flag) |
| `TUI_PROGRAMS` | programs relaunched inside their terminal |
| `SAVE_INTERVAL` | daemon save period (default 60 s) |

Environment variables (no script editing needed):

- `HYPR_SESSION_EXCLUDE="class1,class2"` — extra excluded classes
- `HYPR_SESSION_DIR` — state directory (default `$XDG_STATE_HOME/hypr-session-restore`)

Kill switch — skip restore on next login without uninstalling anything:

```sh
touch ~/.local/state/hypr-session-restore/disabled
```

## FAQ

**Why not a hyprpm plugin?** hyprpm distributes compiled C++ plugins that link against Hyprland internals.
This needs none of that — `hyprctl` exposes everything required, and a script survives Hyprland updates without recompilation.

**Does it need a specific bar/shell?** No.
It talks only to `hyprctl` and `/proc`.
Any bar, any launcher, or none.

**What did the last shutdown actually save?** `cat ~/.local/state/hypr-session-restore/last-shutdown.json`.
The daemon overwrites `session.json` 90 seconds into the next login, so that copy is the only one that still answers the question afterwards.

**Restore ran but nothing appeared?** Check `cat ~/.local/state/hypr-session-restore/session.json` — if it's empty you saved an empty session.
Also note restore aborts if more than 3 windows are already open (double-restore guard).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Stdlib only, no compositor, no fixtures.
The suite covers the command recovery restore depends on — Chromium's flattened `argv`, an AppImage's mount path, D-Bus activation, terminal working directories — and the restore pass itself: spawn order, the double-restore abort, the sweep, group rebuilding, and the Lua the compositor receives.
`/proc` and `hyprctl` are read through small named functions, so tests substitute them rather than mock a compositor.

## License

MIT
