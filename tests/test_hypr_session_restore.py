"""Regression tests for the command recovery that restore depends on.

Every case here is a window that failed to come back on a real machine. The
/proc and hyprctl readers are small named functions, so the whole save path is
exercised against fixtures without a compositor.

Run: python3 -m unittest discover -s tests -v
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "hypr-session-restore")


def load_script():
    """Import the extensionless script as a module."""
    loader = importlib.machinery.SourceFileLoader("hsr", SCRIPT)
    spec = importlib.util.spec_from_file_location("hsr", SCRIPT, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hsr = load_script()

# Brave's real command line, as Chromium reports it: one argument holding the
# whole thing, because it rewrites argv to set the process title.
BRAVE_BLOB = (
    "/opt/brave-bin/brave --ozone-platform=wayland "
    '--password-store=gnome-libsecret --enable-hardware-overlays=""'
)


_ADDRESSES = iter(lambda c=[0]: c.__setitem__(0, c[0] + 1) or f"0x{c[0]:x}", None)


def client(cls, pid=1234, **kw):
    base = {
        "class": cls, "pid": pid, "mapped": True, "title": "",
        "address": next(_ADDRESSES), "grouped": [],
        "workspace": {"id": 2, "name": "2"}, "at": [0, 0], "size": [800, 600],
        "floating": False, "pinned": False, "fullscreen": 0, "monitor": 0,
    }
    base.update(kw)
    return base


class UnflattenArgv(unittest.TestCase):
    def test_splits_chromium_blob(self):
        self.assertEqual(
            hsr.unflatten_argv([BRAVE_BLOB])[0], "/opt/brave-bin/brave")
        self.assertIn("--ozone-platform=wayland", hsr.unflatten_argv([BRAVE_BLOB]))

    def test_leaves_normal_argv_alone(self):
        argv = ["/usr/bin/nautilus", "--new-window"]
        self.assertEqual(hsr.unflatten_argv(argv), argv)

    def test_leaves_unrunnable_path_with_spaces_alone(self):
        """A real path containing spaces must not be split into pieces."""
        argv = ["/home/alex/My Apps/thing"]
        self.assertEqual(hsr.unflatten_argv(argv), argv)

    def test_handles_empty(self):
        self.assertEqual(hsr.unflatten_argv([]), [])


class TerminalArgv(unittest.TestCase):
    def test_joins_flag_ending_in_equals(self):
        """ghostty parses only --key=value; a separated argument is ignored."""
        self.assertEqual(
            hsr.terminal_argv("ghostty", "--working-directory=", "/tmp"),
            ["ghostty", "--working-directory=/tmp"])

    def test_keeps_separated_flag_separate(self):
        self.assertEqual(hsr.terminal_argv("kitty", "-d", "/tmp"),
                         ["kitty", "-d", "/tmp"])


class IsReplayable(unittest.TestCase):
    def setUp(self):
        """The mount path must be a real executable, or the test would pass
        merely because the file is missing rather than because of the rule."""
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        prefix = os.path.join(self.dir.name, ".mount_")
        p = mock.patch.object(hsr, "APPIMAGE_MOUNT_PREFIX", prefix)
        p.start()
        self.addCleanup(p.stop)
        mount = prefix + "JoplinbhOjmd"
        os.makedirs(mount)
        self.mounted = os.path.join(mount, "joplin")
        self.elsewhere = os.path.join(self.dir.name, "joplin")
        for path in (self.mounted, self.elsewhere):
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(path, 0o755)

    def test_rejects_appimage_mount_path(self):
        self.assertTrue(os.access(self.mounted, os.X_OK))
        self.assertFalse(hsr.is_replayable(self.mounted))

    def test_accepts_the_same_binary_outside_the_mount(self):
        self.assertTrue(hsr.is_replayable(self.elsewhere))

    def test_rejects_dbus_service_activation(self):
        self.assertFalse(
            hsr.is_replayable("/usr/bin/gnome-mines --gapplication-service"))

    def test_rejects_missing_binary(self):
        self.assertFalse(hsr.is_replayable("/nonexistent/binary --flag"))

    def test_accepts_real_binary(self):
        self.assertTrue(hsr.is_replayable("/bin/sh -c true"))

    def test_rejects_unparseable(self):
        self.assertFalse(hsr.is_replayable('unbalanced "quote'))


class DesktopLookup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(hsr, "DESKTOP_DIRS", (self.dir.name,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, body):
        with open(os.path.join(self.dir.name, name), "w") as f:
            f.write(body)

    def test_matches_startupwmclass_through_appimage_prefix(self):
        self.write("Joplin.desktop", "[Desktop Entry]\n"
                   "Exec=/home/alex/Applications/Joplin.AppImage %u\n"
                   "StartupWMClass=Joplin\n")
        self.assertEqual(hsr.desktop_command("appimagekit-joplin"),
                         "/home/alex/Applications/Joplin.AppImage")

    def test_matches_on_filename(self):
        self.write("org.gnome.Mines.desktop",
                   "[Desktop Entry]\nExec=gnome-mines\n")
        self.assertEqual(hsr.desktop_command("org.gnome.Mines"), "gnome-mines")

    def test_ignores_keys_outside_desktop_entry_section(self):
        self.write("thing.desktop", "[Desktop Entry]\nExec=real-binary\n"
                   "[Desktop Action new]\nExec=wrong-binary\n")
        self.assertEqual(hsr.desktop_command("thing"), "real-binary")

    def test_returns_none_when_nothing_matches(self):
        self.write("other.desktop", "[Desktop Entry]\nExec=other\n")
        self.assertIsNone(hsr.desktop_command("nosuchclass"))


class RelaunchCommand(unittest.TestCase):
    """The end-to-end save path, with /proc and the desktop dirs faked."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch.object(hsr, "DESKTOP_DIRS", (self.dir.name,))
        p.start()
        self.addCleanup(p.stop)

    def write_desktop(self, name, body):
        with open(os.path.join(self.dir.name, name), "w") as f:
            f.write(body)

    def test_brave_is_unflattened_and_gets_restore_flag(self):
        with mock.patch.object(hsr, "proc_cmdline", return_value=[BRAVE_BLOB]):
            cmd = hsr.relaunch_command(client("brave-browser"))
        self.assertTrue(cmd.startswith("/opt/brave-bin/brave "))
        self.assertIn("--ozone-platform=wayland", cmd)
        self.assertTrue(cmd.endswith("--restore-last-session"))

    def test_appimage_falls_back_to_desktop_entry(self):
        """The running binary exists, so only the mount prefix can reject it."""
        prefix = os.path.join(self.dir.name, ".mount_")
        mount = prefix + "JoplinXXXXXX"
        os.makedirs(mount)
        running = os.path.join(mount, "joplin")
        with open(running, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(running, 0o755)
        self.write_desktop("Joplin.desktop", "[Desktop Entry]\n"
                           "Exec=/home/alex/Applications/Joplin.AppImage %u\n"
                           "StartupWMClass=Joplin\n")
        with mock.patch.object(hsr, "APPIMAGE_MOUNT_PREFIX", prefix), \
             mock.patch.object(hsr, "proc_cmdline", return_value=[running]):
            cmd = hsr.relaunch_command(client("appimagekit-joplin"))
        self.assertEqual(cmd, "/home/alex/Applications/Joplin.AppImage")

    def test_dbus_activation_falls_back_to_desktop_entry(self):
        self.write_desktop("org.gnome.Mines.desktop",
                           "[Desktop Entry]\nExec=gnome-mines\n")
        with mock.patch.object(
                hsr, "proc_cmdline",
                return_value=["/usr/bin/gnome-mines", "--gapplication-service"]):
            cmd = hsr.relaunch_command(client("org.gnome.Mines"))
        self.assertEqual(cmd, "gnome-mines")

    def test_unrecoverable_window_is_dropped(self):
        with mock.patch.object(hsr, "proc_cmdline", return_value=None):
            self.assertIsNone(hsr.relaunch_command(client("mystery")))

    def test_terminal_reopens_in_shell_cwd(self):
        with mock.patch.object(hsr, "find_descendant", side_effect=[None, 99]), \
             mock.patch.object(hsr, "proc_cwd", return_value="/home/alex/src"):
            cmd = hsr.relaunch_command(client("com.mitchellh.ghostty"))
        self.assertEqual(cmd, "ghostty --working-directory=/home/alex/src")

    def test_terminal_relaunches_its_tui(self):
        with mock.patch.object(hsr, "find_descendant", return_value=77), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["nvim"]), \
             mock.patch.object(hsr, "proc_cwd", return_value="/etc"):
            cmd = hsr.relaunch_command(client("com.mitchellh.ghostty"))
        self.assertEqual(cmd, "ghostty --working-directory=/etc -e nvim")


class AddRestoreFlag(unittest.TestCase):
    def test_not_added_twice(self):
        once = hsr.add_restore_flag("brave", "brave-browser")
        self.assertEqual(hsr.add_restore_flag(once, "brave-browser"), once)

    def test_left_alone_for_other_apps(self):
        self.assertEqual(hsr.add_restore_flag("nautilus", "org.gnome.Nautilus"),
                         "nautilus")


class SaveGuard(unittest.TestCase):
    """omarchy closes every window before poweroff; a daemon tick landing in
    that gap must not blank the snapshot the power menu just took."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.session = os.path.join(self.dir.name, "session.json")
        for name, value in (("STATE_DIR", self.dir.name),
                            ("SESSION_FILE", self.session)):
            p = mock.patch.object(hsr, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_empty_save_keeps_existing_session(self):
        with open(self.session, "w") as f:
            json.dump({"saved_at": 1, "windows": [{"class": "code"}]}, f)
        with mock.patch.object(hsr, "hyprctl_json", return_value=[]):
            self.assertIsNone(hsr.save())
        with open(self.session) as f:
            self.assertEqual(len(json.load(f)["windows"]), 1)

    def test_empty_save_writes_when_no_session_yet(self):
        with mock.patch.object(hsr, "hyprctl_json", return_value=[]):
            self.assertEqual(hsr.save(), 0)
        with open(self.session) as f:
            self.assertEqual(json.load(f)["windows"], [])

    def test_second_window_of_one_process_is_not_respawned(self):
        two = [client("brave-browser", pid=7, workspace={"id": 1, "name": "1"}),
               client("brave-browser", pid=7, workspace={"id": 2, "name": "2"})]
        with mock.patch.object(hsr, "hyprctl_json", return_value=two), \
             mock.patch.object(hsr, "proc_cmdline", return_value=[BRAVE_BLOB]):
            self.assertEqual(hsr.save(), 2)
        with open(self.session) as f:
            spawns = [w["spawn"] for w in json.load(f)["windows"]]
        self.assertEqual(spawns, [True, False])

    def test_focused_workspace_is_recorded(self):
        """Group restore moves focus, so it has to know where to put it back."""
        with mock.patch.object(hsr, "active_workspace", return_value=4), \
             mock.patch.object(hsr, "hyprctl_json",
                               return_value=[client("code")]), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["/bin/sh"]):
            hsr.save()
        with open(self.session) as f:
            self.assertEqual(json.load(f)["focused_workspace"], 4)

    def test_excluded_class_is_not_saved(self):
        with mock.patch.object(hsr, "EXCLUDE_CLASSES", {"jobbot-search"}), \
             mock.patch.object(hsr, "hyprctl_json",
                               return_value=[client("jobbot-search")]), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["/bin/sh"]):
            hsr.save()
        with open(self.session) as f:
            self.assertEqual(json.load(f)["windows"], [])


class LuaEmission(unittest.TestCase):
    def test_named_workspace(self):
        self.assertEqual(hsr.ws_selector({"id": 4, "name": "code"}), "name:code")

    def test_numbered_workspace(self):
        self.assertEqual(hsr.ws_selector({"id": 4, "name": "4"}), "4")

    def test_special_workspace(self):
        self.assertEqual(hsr.ws_selector({"id": -98, "name": "special:magic"}),
                         "special:magic")

    def test_long_string_escapes_its_own_closer(self):
        emitted = hsr.lua_long_str("echo ]]")
        self.assertTrue(emitted.startswith("[=["))
        self.assertTrue(emitted.endswith("]=]"))

    def test_quote_in_workspace_name_is_escaped(self):
        self.assertEqual(hsr.lua_str("it's"), "'it\\'s'")

    def test_floating_window_carries_geometry(self):
        rules = hsr.exec_rules({
            "workspace": {"id": 2, "name": "2"}, "floating": True,
            "at": [10, 20], "size": [300, 400], "pinned": False})
        self.assertIn("float = true", rules)
        self.assertIn("move = {10, 20}", rules)
        self.assertIn("size = {300, 400}", rules)
        self.assertIn("'2 silent'", rules)

    def test_tiled_window_has_no_geometry(self):
        rules = hsr.exec_rules({
            "workspace": {"id": 2, "name": "2"}, "floating": False,
            "at": [10, 20], "size": [300, 400], "pinned": False})
        self.assertNotIn("float", rules)
        self.assertNotIn("move", rules)


if __name__ == "__main__":
    unittest.main()


def saved_window(cls, ws=2, spawn=True, floating=False, at=(0, 0),
                 size=(800, 600), pinned=False, fullscreen=0, cmd=None,
                 monitor_name=""):
    return {
        "class": cls, "title": "", "workspace": {"id": ws, "name": str(ws)},
        "at": list(at), "size": list(size), "floating": floating,
        "pinned": pinned, "fullscreen": fullscreen, "monitor": 0,
        "monitor_name": monitor_name,
        "cmd": cmd or f"/usr/bin/{cls}", "spawn": spawn,
    }


class RestoreHarness(unittest.TestCase):
    """restore() reaches the compositor through dispatch(), lua_eval() and
    current_clients(), so the whole pass is observable. The first two both
    send Lua, and are recorded in one list so their order is too."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.session = os.path.join(self.dir.name, "session.json")
        self.disabled = os.path.join(self.dir.name, "disabled")
        for name, value in (("STATE_DIR", self.dir.name),
                            ("SESSION_FILE", self.session),
                            ("DISABLE_FLAG", self.disabled)):
            p = mock.patch.object(hsr, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(hsr.time, "sleep")
        p.start()
        self.addCleanup(p.stop)
        # restore() reports what it could not do; a test that cares about a
        # report captures stderr itself, the rest keep it out of the run
        p = mock.patch.object(hsr.sys, "stderr", io.StringIO())
        p.start()
        self.addCleanup(p.stop)
        # one monitor by default: the placement pass stays a no-op
        p = mock.patch.object(hsr, "hyprctl_json", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def write_session(self, windows, focused_workspace=7):
        with open(self.session, "w") as f:
            json.dump({"saved_at": 1, "windows": windows,
                       "focused_workspace": focused_workspace}, f)

    def run_restore(self, clients_sequence, sweep_timeout=0):
        """Returns every line of Lua restore() sent, dispatched or evaluated.

        The sweep polls until it runs out of time, so the clock is faked (one
        second per reading) and the last client view repeats forever. Without
        both, a window that never turns up hangs or exhausts the mock.
        """
        views = list(clients_sequence)

        def next_view():
            return views.pop(0) if len(views) > 1 else views[0]

        clock = iter(lambda c=[1000.0]: c.__setitem__(0, c[0] + 1) or c[0], None)

        sent = []
        with mock.patch.object(hsr, "SWEEP_TIMEOUT", sweep_timeout), \
             mock.patch.object(hsr, "SPAWN_STAGGER", 0), \
             mock.patch.object(hsr.time, "time", side_effect=lambda: next(clock)), \
             mock.patch.object(hsr, "current_clients", side_effect=next_view), \
             mock.patch.object(hsr, "dispatch", side_effect=sent.append), \
             mock.patch.object(hsr, "lua_eval", side_effect=sent.append):
            hsr.restore()
        return sent


class RestoreGuards(RestoreHarness):
    def test_disable_flag_stops_restore(self):
        self.write_session([saved_window("code")])
        open(self.disabled, "w").close()
        self.assertEqual(self.run_restore([{}]), [])

    def test_missing_session_file(self):
        self.assertEqual(self.run_restore([{}]), [])

    def test_malformed_session_file(self):
        with open(self.session, "w") as f:
            f.write("{not json")
        self.assertEqual(self.run_restore([{}]), [])

    def test_empty_window_list(self):
        self.write_session([])
        self.assertEqual(self.run_restore([{}]), [])

    def test_aborts_when_desktop_already_populated(self):
        """The double-restore guard: more than 3 windows means a real session."""
        self.write_session([saved_window("code")])
        busy = {f"0x{i}": {"class": "x", "workspace": {"id": 1}} for i in range(4)}
        with mock.patch.object(hsr.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(self.run_restore([busy]), [])
        self.assertIn("aborting", err.getvalue())

    def test_proceeds_when_only_a_few_windows_open(self):
        self.write_session([saved_window("code")])
        few = {f"0x{i}": {"class": "x", "workspace": {"id": 1}} for i in range(3)}
        self.assertEqual(len(self.run_restore([few])), 1)


class RestoreSpawning(RestoreHarness):
    def test_only_spawn_entries_are_launched(self):
        """Extra windows of one process are left to the sweep."""
        self.write_session([saved_window("brave-browser", spawn=True),
                            saved_window("brave-browser", spawn=False)])
        self.assertEqual(len(self.run_restore([{}])), 1)

    def test_tiled_windows_spawn_before_floating(self):
        self.write_session([
            saved_window("floaty", ws=1, floating=True, at=(0, 0)),
            saved_window("tiled", ws=1, floating=False, at=(500, 0)),
        ])
        emitted = self.run_restore([{}])
        self.assertIn("tiled", emitted[0])
        self.assertIn("floaty", emitted[1])

    def test_workspaces_spawn_in_order(self):
        self.write_session([saved_window("second", ws=5),
                            saved_window("first", ws=1)])
        emitted = self.run_restore([{}])
        self.assertIn("first", emitted[0])
        self.assertIn("second", emitted[1])

    def test_dispatch_carries_silent_workspace_and_command(self):
        self.write_session([saved_window("code", ws=3, cmd="/usr/share/code/code")])
        emitted = self.run_restore([{}])[0]
        self.assertIn("[[/usr/share/code/code]]", emitted)
        self.assertIn("'3 silent'", emitted)

    def test_command_containing_long_bracket_is_escaped(self):
        self.write_session([saved_window("odd", cmd="sh -c ]]")])
        emitted = self.run_restore([{}])[0]
        self.assertIn("[=[sh -c ]]]=]", emitted)


class RestoreSweep(RestoreHarness):
    """Single-instance apps ignore the spawn rules, so their windows come up
    unplaced and have to be moved afterwards."""

    def test_misplaced_window_is_moved(self):
        self.write_session([saved_window("brave-browser", ws=4)])
        landed = {"0xaa": {"class": "brave-browser", "workspace": {"id": 1},
                           "at": [0, 0], "floating": False}}
        emitted = self.run_restore([{}, landed], sweep_timeout=5)
        moves = [e for e in emitted if "window.move" in e and "address:0xaa" in e]
        self.assertTrue(moves)
        self.assertIn("'4'", moves[0])

    def test_correctly_placed_window_is_left_alone(self):
        self.write_session([saved_window("brave-browser", ws=4)])
        landed = {"0xaa": {"class": "brave-browser", "workspace": {"id": 4},
                           "at": [0, 0], "floating": False}}
        emitted = self.run_restore([{}, landed], sweep_timeout=5)
        self.assertEqual([e for e in emitted if "address:0xaa" in e], [])

    def test_window_present_before_restore_is_not_touched(self):
        self.write_session([saved_window("brave-browser", ws=4)])
        already = {"0xbb": {"class": "brave-browser", "workspace": {"id": 1},
                            "at": [0, 0], "floating": False}}
        emitted = self.run_restore([already, already], sweep_timeout=5)
        self.assertEqual([e for e in emitted if "address:0xbb" in e], [])

    def test_pairing_prefers_the_entry_on_the_same_workspace(self):
        """Two saved windows of one class: match the one that fits."""
        self.write_session([saved_window("brave-browser", ws=1, at=(0, 0)),
                            saved_window("brave-browser", ws=9, at=(1, 0))])
        landed = {"0xaa": {"class": "brave-browser", "workspace": {"id": 9},
                           "at": [0, 0], "floating": False}}
        emitted = self.run_restore([{}, landed], sweep_timeout=5)
        self.assertEqual([e for e in emitted if "address:0xaa" in e], [])


class FixWindow(unittest.TestCase):
    def emit(self, win, address="0xaa"):
        with mock.patch.object(hsr, "dispatch") as dispatched:
            hsr.fix_window(win, address)
        return [c.args[0] for c in dispatched.call_args_list]

    def test_floating_window_regains_position_and_size(self):
        emitted = self.emit(saved_window("x", floating=True, at=(10, 20),
                                         size=(300, 400)))
        joined = " ".join(emitted)
        self.assertIn("window.float", joined)
        self.assertIn("x = 10, y = 20", joined)
        self.assertIn("x = 300, y = 400", joined)

    def test_pinned_window_is_repinned(self):
        self.assertTrue(any("window.pin" in e
                            for e in self.emit(saved_window("x", pinned=True))))

    def test_fullscreen_bit_selects_fullscreen_mode(self):
        emitted = " ".join(self.emit(saved_window("x", fullscreen=2)))
        self.assertIn("mode = 'fullscreen'", emitted)

    def test_maximized_state_is_not_reported_as_fullscreen(self):
        emitted = " ".join(self.emit(saved_window("x", fullscreen=1)))
        self.assertIn("mode = 'maximized'", emitted)

    def test_tiled_plain_window_only_moves(self):
        emitted = self.emit(saved_window("x"))
        self.assertEqual(len(emitted), 1)
        self.assertIn("window.move", emitted[0])


class CurrentClients(unittest.TestCase):
    def collect(self, clients, exclude=frozenset()):
        with mock.patch.object(hsr, "EXCLUDE_CLASSES", set(exclude)), \
             mock.patch.object(hsr, "hyprctl_json", return_value=clients):
            return hsr.current_clients()

    def test_skips_unmapped_and_classless_and_excluded(self):
        got = self.collect([
            {"address": "0x1", "class": "keep", "mapped": True},
            {"address": "0x2", "class": "gone", "mapped": False},
            {"address": "0x3", "class": "", "mapped": True},
            {"address": "0x4", "class": "skipme", "mapped": True},
        ], exclude={"skipme"})
        self.assertEqual(list(got), ["0x1"])


class FindDescendant(unittest.TestCase):
    def test_finds_a_grandchild(self):
        tree = {1: [2], 2: [3], 3: []}
        with mock.patch.object(hsr, "proc_children", side_effect=lambda p: tree.get(p, [])), \
             mock.patch.object(hsr, "proc_comm", side_effect=lambda p: {3: "nvim"}.get(p)):
            self.assertEqual(hsr.find_descendant(1, {"nvim"}), 3)

    def test_stops_at_max_depth(self):
        tree = {1: [2], 2: [3], 3: [4], 4: []}
        with mock.patch.object(hsr, "proc_children", side_effect=lambda p: tree.get(p, [])), \
             mock.patch.object(hsr, "proc_comm", side_effect=lambda p: {4: "nvim"}.get(p)):
            self.assertIsNone(hsr.find_descendant(1, {"nvim"}, max_depth=2))

    def test_returns_none_when_absent(self):
        with mock.patch.object(hsr, "proc_children", return_value=[]):
            self.assertIsNone(hsr.find_descendant(1, {"nvim"}))


class QuitGracefully(unittest.TestCase):
    """Chromium only keeps its tabs if it exits cleanly, so shutdown has to
    signal it and wait rather than let the compositor kill it."""

    def test_signals_only_session_keeping_apps(self):
        clients = [client("brave-browser", pid=11),
                   client("com.mitchellh.ghostty", pid=22),
                   client("org.gnome.Nautilus", pid=33)]
        with mock.patch.object(hsr, "hyprctl_json", return_value=clients), \
             mock.patch.object(hsr.os, "kill") as killed, \
             mock.patch.object(hsr.os.path, "exists", return_value=False):
            hsr.quit_gracefully(timeout=0)
        self.assertEqual([c.args[0] for c in killed.call_args_list], [11])

    def test_reports_apps_that_outlive_the_timeout(self):
        with mock.patch.object(hsr, "hyprctl_json",
                               return_value=[client("brave-browser", pid=11)]), \
             mock.patch.object(hsr.os, "kill"), \
             mock.patch.object(hsr.time, "sleep"), \
             mock.patch.object(hsr.os.path, "exists", return_value=True), \
             mock.patch.object(hsr.time, "time", side_effect=[0, 1, 2]):
            self.assertEqual(hsr.quit_gracefully(timeout=1), {11})

    def test_reports_nothing_when_everything_exits(self):
        with mock.patch.object(hsr, "hyprctl_json",
                               return_value=[client("brave-browser", pid=11)]), \
             mock.patch.object(hsr.os, "kill"), \
             mock.patch.object(hsr.time, "sleep"), \
             mock.patch.object(hsr.os.path, "exists", return_value=False), \
             mock.patch.object(hsr.time, "time", side_effect=[0, 1, 2]):
            self.assertEqual(hsr.quit_gracefully(timeout=5), set())

    def test_a_process_that_is_already_gone_is_not_reported(self):
        with mock.patch.object(hsr, "hyprctl_json",
                               return_value=[client("brave-browser", pid=11)]), \
             mock.patch.object(hsr.os, "kill", side_effect=ProcessLookupError), \
             mock.patch.object(hsr.time, "sleep"), \
             mock.patch.object(hsr.time, "time", side_effect=[0, 1, 2]):
            self.assertEqual(hsr.quit_gracefully(timeout=5), set())


def grouped_clients(*specs):
    """specs: (class, pid, group_key) — windows sharing a group_key are one
    Hyprland group, which reports every member's address on each member."""
    made = [client(cls, pid=pid) for cls, pid, _ in specs]
    by_group = {}
    for win, (_, _, key) in zip(made, specs):
        if key is not None:
            by_group.setdefault(key, []).append(win["address"])
    for win, (_, _, key) in zip(made, specs):
        win["grouped"] = list(by_group.get(key, [])) if key is not None else []
    return made


class SecondWindowOfOneProcess(SaveGuard):
    """VS Code and Nautilus reopen nothing by themselves, so every window of
    theirs has to be launched. Browsers are the exception."""

    def spawn_flags(self, clients):
        with mock.patch.object(hsr, "hyprctl_json", return_value=clients), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["/bin/sh"]):
            hsr.save()
        with open(self.session) as f:
            return [w["spawn"] for w in json.load(f)["windows"]]

    def test_every_window_of_an_ordinary_app_is_launched(self):
        two = [client("code", pid=7), client("code", pid=7)]
        self.assertEqual(self.spawn_flags(two), [True, True])

    def test_nautilus_second_window_is_launched(self):
        two = [client("org.gnome.Nautilus", pid=7),
               client("org.gnome.Nautilus", pid=7)]
        self.assertEqual(self.spawn_flags(two), [True, True])

    def test_browser_is_still_launched_once_per_process(self):
        """Brave restores its own windows; launching again would duplicate."""
        two = [client("brave-browser", pid=7), client("brave-browser", pid=7)]
        self.assertEqual(self.spawn_flags(two), [True, False])

    def test_separate_processes_are_each_launched(self):
        two = [client("brave-browser", pid=7), client("brave-browser", pid=8)]
        self.assertEqual(self.spawn_flags(two), [True, True])


class GroupCapture(SaveGuard):
    def saved(self, clients):
        with mock.patch.object(hsr, "hyprctl_json", return_value=clients), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["/bin/sh"]):
            hsr.save()
        with open(self.session) as f:
            return json.load(f)["windows"]

    def test_group_membership_is_recorded(self):
        made = grouped_clients(("code", 1, "a"), ("brave-browser", 2, "a"),
                               ("org.gnome.Mines", 3, None))
        groups = [w["group"] for w in self.saved(made)]
        self.assertEqual(groups[0], groups[1])
        self.assertIsNone(groups[2])

    def test_two_groups_get_distinct_ids(self):
        made = grouped_clients(("code", 1, "a"), ("brave-browser", 2, "a"),
                               ("org.kde.kdenlive", 3, "b"), ("foot", 4, "b"))
        groups = [w["group"] for w in self.saved(made)]
        self.assertEqual(groups[0], groups[1])
        self.assertEqual(groups[2], groups[3])
        self.assertNotEqual(groups[0], groups[2])

    def test_a_group_of_one_is_not_a_group(self):
        made = grouped_clients(("code", 1, "a"))
        self.assertIsNone(self.saved(made)[0]["group"])


class DispatchReporting(unittest.TestCase):
    """Every placement is a dispatch, so a dispatcher missing on the running
    Hyprland must not fail silently."""

    def run_dispatch(self, stdout):
        done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        with mock.patch.object(hsr.subprocess, "run", return_value=done), \
             mock.patch.object(hsr.sys, "stderr", io.StringIO()) as err:
            return hsr.dispatch("hl.dsp.whatever()"), err.getvalue()

    def test_accepted_dispatch_is_quiet(self):
        ok, err = self.run_dispatch("ok\n")
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_rejected_dispatch_is_reported(self):
        ok, err = self.run_dispatch("error: attempt to call a table value\n")
        self.assertFalse(ok)
        self.assertIn("dispatch rejected", err)
        self.assertIn("hl.dsp.whatever()", err)

    def test_empty_reply_is_reported(self):
        ok, err = self.run_dispatch("")
        self.assertFalse(ok)
        self.assertIn("no reply", err)


class Refocus(unittest.TestCase):
    def test_uses_the_focus_dispatcher(self):
        """hl.dsp.workspace is a table of operations, not a switch call."""
        with mock.patch.object(hsr, "dispatch") as dispatched:
            hsr.refocus(7)
        self.assertEqual(dispatched.call_args.args[0],
                         "hl.dsp.focus({ workspace = '7' })")

    def test_no_saved_workspace_does_nothing(self):
        with mock.patch.object(hsr, "dispatch") as dispatched:
            hsr.refocus(None)
        dispatched.assert_not_called()


class LastShutdownCopy(unittest.TestCase):
    """session.json is overwritten shortly after the next login, so the
    shutdown copy is what a post-reboot comparison has to read."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.session = os.path.join(self.dir.name, "session.json")
        self.copy = os.path.join(self.dir.name, "last-shutdown.json")
        for name, value in (("STATE_DIR", self.dir.name),
                            ("SESSION_FILE", self.session),
                            ("LAST_SHUTDOWN_FILE", self.copy)):
            p = mock.patch.object(hsr, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_shutdown(self, clients):
        with mock.patch.object(hsr, "hyprctl_json", return_value=clients), \
             mock.patch.object(hsr, "active_workspace", return_value=1), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["/bin/sh"]), \
             mock.patch.object(hsr, "quit_gracefully", return_value=set()), \
             mock.patch.object(hsr.sys, "argv", ["x", "shutdown"]), \
             mock.patch.object(hsr.sys, "stdout", io.StringIO()):
            hsr.main()

    def test_shutdown_keeps_a_copy_of_the_snapshot(self):
        self.run_shutdown([client("code"), client("foot")])
        with open(self.copy) as f:
            kept = json.load(f)["windows"]
        self.assertEqual(sorted(w["class"] for w in kept), ["code", "foot"])

    def test_copy_matches_the_snapshot_exactly(self):
        self.run_shutdown([client("code")])
        with open(self.session) as a, open(self.copy) as b:
            self.assertEqual(a.read(), b.read())

    def test_plain_save_does_not_touch_the_copy(self):
        """Only a clean exit updates it; the 60-second daemon must not."""
        with mock.patch.object(hsr, "hyprctl_json", return_value=[client("code")]), \
             mock.patch.object(hsr, "active_workspace", return_value=1), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["/bin/sh"]):
            hsr.save()
        self.assertFalse(os.path.exists(self.copy))


class ChromiumClasses(unittest.TestCase):
    """XWayland reports Chromium; native Wayland chromium or chromium-browser.
    All of them keep their tabs only if quit cleanly and relaunched with the
    restore flag."""

    def test_every_chromium_class_is_session_keeping(self):
        for cls in ("Chromium", "chromium", "chromium-browser"):
            with self.subTest(cls=cls):
                cmd = hsr.add_restore_flag("/usr/lib/chromium/chromium", cls)
                self.assertTrue(cmd.endswith("--restore-last-session"))
                self.assertIn(cls, hsr.SESSION_KEEPING_CLASSES)


class ExcludedAtRestore(RestoreHarness):
    def test_excluded_class_in_the_snapshot_is_not_launched(self):
        """The exclusion list can grow after the snapshot was taken, and an
        autostarted app relaunched from an old snapshot comes up twice."""
        self.write_session([saved_window("omacal", ws=1), saved_window("code", ws=2)])
        with mock.patch.object(hsr, "EXCLUDE_CLASSES", {"omacal"}):
            emitted = self.run_restore([{}])
        launched = [e for e in emitted if "exec_cmd" in e]
        self.assertEqual(len(launched), 1)
        self.assertIn("code", launched[0])


class SweepLeavesGroupsAlone(RestoreHarness):
    def test_a_grouped_window_is_never_moved(self):
        """Moving one member moves the whole group."""
        self.write_session([saved_window("foot", ws=5)])
        emitted = self.run_restore([
            {},
            {"0xf": {"class": "foot", "workspace": {"id": 1},
                     "grouped": ["0xf", "0xg"]}},
        ], sweep_timeout=2)
        self.assertFalse(any("window.move" in e for e in emitted))


class SweepPlacesLoneSelfGroupedWindow(RestoreHarness):
    """A lone window can report itself as a group of one; the sweep must still
    move it. Only a real group of two or more is left alone."""

    def test_group_of_one_is_still_moved(self):
        self.write_session([saved_window("org.gnome.Mines", ws=17)])
        emitted = self.run_restore([
            {},
            {"0xb": {"class": "org.gnome.Mines", "workspace": {"id": 1},
                     "grouped": ["0xb"]}},
        ], sweep_timeout=2)
        self.assertTrue(any("window.move" in e and "0xb" in e for e in emitted))


def saved_window_m(ws, monitor_name):
    return dict(saved_window("x", ws=ws), monitor_name=monitor_name)


class MonitorCapture(SaveGuard):
    def test_monitor_name_is_recorded_from_the_id_map(self):
        def fake(cmd):
            return {
                "clients": [client("code", monitor=1)],
                "monitors": [{"id": 0, "name": "eDP-1"}, {"id": 1, "name": "DP-9"}],
                "activeworkspace": {"id": 2},
            }[cmd]
        with mock.patch.object(hsr, "hyprctl_json", side_effect=fake), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["/bin/sh"]):
            hsr.save()
        with open(self.session) as f:
            self.assertEqual(json.load(f)["windows"][0]["monitor_name"], "DP-9")

    def test_unknown_monitor_id_records_an_empty_name(self):
        def fake(cmd):
            return {"clients": [client("code", monitor=9)],
                    "monitors": [{"id": 0, "name": "eDP-1"}],
                    "activeworkspace": {"id": 2}}[cmd]
        with mock.patch.object(hsr, "hyprctl_json", side_effect=fake), \
             mock.patch.object(hsr, "proc_cmdline", return_value=["/bin/sh"]):
            hsr.save()
        with open(self.session) as f:
            self.assertEqual(json.load(f)["windows"][0]["monitor_name"], "")


class WorkspaceMonitorPlacement(unittest.TestCase):
    """Workspaces bind to no monitor here, so a restored workspace is moved
    onto the monitor it was saved on. One move per workspace."""

    TWO = [{"id": 0, "name": "eDP-1"}, {"id": 1, "name": "DP-9"}]

    def emit(self, windows, monitors, live_ws):
        view = {f"0x{i}": {"class": "x", "workspace": {"id": ws}}
                for i, ws in enumerate(live_ws)}
        with mock.patch.object(hsr, "hyprctl_json", return_value=monitors), \
             mock.patch.object(hsr, "current_clients", return_value=view), \
             mock.patch.object(hsr, "dispatch") as d:
            n = hsr.place_workspaces_on_monitors(windows)
        return n, [c.args[0] for c in d.call_args_list]

    def test_each_workspace_moves_once_to_its_saved_monitor(self):
        wins = [saved_window_m(17, "eDP-1"), saved_window_m(19, "DP-9"),
                saved_window_m(17, "eDP-1")]
        n, e = self.emit(wins, self.TWO, [17, 19])
        self.assertEqual(n, 2)
        self.assertIn("hl.dsp.workspace.move({ workspace = '17', monitor = 'eDP-1' })", e)
        self.assertIn("hl.dsp.workspace.move({ workspace = '19', monitor = 'DP-9' })", e)

    def test_single_monitor_is_a_no_op(self):
        self.assertEqual(self.emit([saved_window_m(17, "eDP-1")],
                                   [{"id": 0, "name": "eDP-1"}], [17]), (0, []))

    def test_window_with_no_saved_monitor_is_skipped(self):
        self.assertEqual(self.emit([saved_window_m(17, "")], self.TWO, [17]), (0, []))

    def test_workspace_with_no_live_window_is_skipped(self):
        self.assertEqual(self.emit([saved_window_m(17, "eDP-1")], self.TWO, []), (0, []))

    def test_special_workspace_is_skipped(self):
        self.assertEqual(self.emit([saved_window_m(-99, "eDP-1")], self.TWO, [-99]), (0, []))


class MonitorPlacementInRestore(RestoreHarness):
    def run_multi(self, windows, view):
        self.write_session(windows, focused_workspace=17)
        with mock.patch.object(hsr, "SWEEP_TIMEOUT", 0), \
             mock.patch.object(hsr, "SPAWN_STAGGER", 0), \
             mock.patch.object(hsr.time, "time", side_effect=range(1, 500)), \
             mock.patch.object(hsr, "hyprctl_json",
                               return_value=[{"id": 0, "name": "eDP-1"},
                                             {"id": 1, "name": "DP-9"}]), \
             mock.patch.object(hsr, "current_clients", return_value=view), \
             mock.patch.object(hsr, "dispatch") as d:
            hsr.restore()
        return [c.args[0] for c in d.call_args_list]

    def test_workspace_moved_to_monitor_then_focus_restored(self):
        emitted = self.run_multi(
            [dict(saved_window("code", ws=17), monitor_name="eDP-1")],
            {"0xa": {"class": "code", "workspace": {"id": 17}}})
        move = "hl.dsp.workspace.move({ workspace = '17', monitor = 'eDP-1' })"
        self.assertIn(move, emitted)
        self.assertEqual(emitted[-1], "hl.dsp.focus({ workspace = '17' })")
        self.assertLess(emitted.index(move), len(emitted) - 1)

    def test_multi_monitor_restores_focus_even_without_groups(self):
        emitted = self.run_multi(
            [dict(saved_window("code", ws=17), monitor_name="eDP-1")],
            {"0xa": {"class": "code", "workspace": {"id": 17}}})
        self.assertTrue(any("hl.dsp.focus({ workspace =" in e for e in emitted))




def live_window(cls, ws=2, at=(0, 0), size=(800, 600), members=0):
    """A client as hyprctl reports it."""
    return {"class": cls, "workspace": {"id": ws}, "at": list(at),
            "size": list(size), "grouped": ["0x0"] * members}


class LuaEvalReporting(unittest.TestCase):
    """Groups are built through the Lua API now, so a rejected eval must not
    pass silently any more than a rejected dispatch."""

    def run_eval(self, stdout):
        done = subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
        with mock.patch.object(hsr.subprocess, "run", return_value=done), \
             mock.patch.object(hsr.sys, "stderr", io.StringIO()) as err:
            return hsr.lua_eval("hl.get_window('address:0xa')"), err.getvalue()

    def test_accepted_eval_is_quiet(self):
        self.assertEqual(self.run_eval("ok\n"), (True, ""))

    def test_rejected_eval_is_reported(self):
        ok, err = self.run_eval("error: attempt to index a nil value\n")
        self.assertFalse(ok)
        self.assertIn("eval rejected", err)
        self.assertIn("hl.get_window('address:0xa')", err)

    def test_empty_reply_is_reported(self):
        ok, err = self.run_eval("")
        self.assertFalse(ok)
        self.assertIn("no reply", err)


class BuildGroups(unittest.TestCase):
    """group:add puts an existing window into an existing group outright, so
    the groups are assembled at the end from whatever the sweep placed. No
    focus, no direction, and no lock holding a half-built group shut."""

    def emit(self, placed):
        with mock.patch.object(hsr, "dispatch") as dispatched, \
             mock.patch.object(hsr, "lua_eval") as evaluated:
            built = hsr.build_groups(placed)
        return built, [c.args[0] for c in dispatched.call_args_list], \
            [c.args[0] for c in evaluated.call_args_list]

    @staticmethod
    def member(group, ws=2):
        return dict(saved_window("x", ws=ws), group=group)

    def test_the_first_member_anchors_and_the_rest_are_added_to_it(self):
        built, dispatched, evaluated = self.emit(
            [(self.member(0), "0xa"), (self.member(0), "0xb"), (self.member(0), "0xc")])
        self.assertEqual(built, 1)
        self.assertEqual(dispatched,
                         ["hl.dsp.group.toggle({ window = 'address:0xa' })"])
        self.assertEqual(evaluated, [
            "hl.get_window('address:0xa').group:add(hl.get_window('address:0xb'))",
            "hl.get_window('address:0xa').group:add(hl.get_window('address:0xc'))"])

    def test_a_group_down_to_one_window_is_not_built(self):
        """Nothing to tab it with. A group of one is also what used to be
        left locked, because the unlock pass walked straight past it."""
        self.assertEqual(self.emit([(self.member(0), "0xa")]), (0, [], []))

    def test_windows_that_were_in_no_group_are_ignored(self):
        self.assertEqual(
            self.emit([(saved_window("x"), "0xa"), (saved_window("y"), "0xb")]),
            (0, [], []))

    def test_each_saved_group_is_built(self):
        built, dispatched, evaluated = self.emit(
            [(self.member(0), "0xa"), (self.member(0), "0xb"),
             (self.member(1), "0xc"), (self.member(1), "0xd")])
        self.assertEqual((built, len(dispatched), len(evaluated)), (2, 2, 2))

    def test_no_window_is_focused_and_no_group_is_locked(self):
        """Both were mechanisms of the old build, and both caused bugs: a
        lock left behind refused every window for the rest of the session."""
        _, dispatched, evaluated = self.emit(
            [(self.member(0), "0xa"), (self.member(0), "0xb")])
        self.assertFalse(any("lock" in lua or "focus" in lua
                             for lua in dispatched + evaluated))


class LaunchedInParallel(RestoreHarness):
    """Every window is launched before any of them is waited for, so a slow
    app overlaps with the others instead of holding up the queue."""

    def test_group_members_carry_their_own_workspace_rule(self):
        """They used to be launched bare, one at a time, so that a focused
        group would absorb each one as it opened."""
        self.write_session([dict(saved_window("code", ws=2), group=0),
                            dict(saved_window("foot", ws=2), group=0)])
        launches = [e for e in self.run_restore([{}]) if "exec_cmd" in e]
        self.assertEqual(len(launches), 2)
        self.assertTrue(all("workspace = '2 silent'" in e for e in launches))

    def test_nothing_is_placed_or_grouped_until_everything_is_launched(self):
        windows = [dict(saved_window("code", ws=2), group=0),
                   dict(saved_window("foot", ws=2), group=0),
                   saved_window("ghostty", ws=5)]
        self.write_session(windows)
        settled = {"0xa": live_window("code", ws=2), "0xb": live_window("foot", ws=2),
                   "0xg": live_window("ghostty", ws=1)}
        emitted = self.run_restore([{}, settled], sweep_timeout=5)
        last_launch = max(i for i, e in enumerate(emitted) if "exec_cmd" in e)
        after = [e for e in emitted[last_launch + 1:] if "window.move" in e or "group" in e]
        self.assertTrue(after)
        self.assertFalse(any("window.move" in e or "group" in e
                             for e in emitted[:last_launch]))

    def test_a_browser_is_still_launched_once_per_process(self):
        """Its other windows come back from its own session; a second launch
        would duplicate them."""
        self.write_session([dict(saved_window("brave-browser", ws=2), group=0),
                            dict(saved_window("brave-browser", ws=5, spawn=False), group=1)])
        emitted = self.run_restore([{}])
        self.assertEqual(len([e for e in emitted if "exec_cmd" in e]), 1)

    def test_a_window_that_never_turns_up_is_reported(self):
        self.write_session([saved_window("ghost", ws=2)])
        with mock.patch.object(hsr.sys, "stderr", io.StringIO()) as err:
            self.run_restore([{}], sweep_timeout=3)
        self.assertIn("no window turned up for ghost", err.getvalue())


class ReportedBug(RestoreHarness):
    """Two Brave windows, one in each of two groups. Brave reopens them from
    its own session, so they arrive loose and in no particular order — which
    is now no different from any other window, because groups are built from
    what the sweep placed rather than from what was launched into them."""

    def test_both_browser_windows_end_up_in_their_group(self):
        self.write_session([
            dict(saved_window("code", ws=2), group=0),
            dict(saved_window("brave-browser", ws=2), group=0),
            dict(saved_window("foot", ws=5), group=1),
            dict(saved_window("brave-browser", ws=5, spawn=False), group=1)])
        settled = {"0xc": live_window("code", ws=2),
                   "0xb1": live_window("brave-browser", ws=2),
                   "0xf": live_window("foot", ws=5),
                   "0xb2": live_window("brave-browser", ws=5)}
        emitted = self.run_restore([{}, settled], sweep_timeout=5)
        adds = [e for e in emitted if "group:add" in e]
        self.assertEqual(len(adds), 2)
        self.assertTrue(any("0xb1" in e for e in adds))
        self.assertTrue(any("0xb2" in e for e in adds))

    def test_a_member_that_never_arrives_does_not_stop_the_group(self):
        """Workspace 19 on the laptop: kdenlive never turned up, and what was
        left was locked and unjoinable. Now the rest still tab together."""
        self.write_session([dict(saved_window("org.gnome.Mines", ws=19), group=1),
                            dict(saved_window("chromium-browser", ws=19), group=1),
                            dict(saved_window("org.kde.kdenlive", ws=19), group=1)])
        settled = {"0xm": live_window("org.gnome.Mines", ws=19),
                   "0xc": live_window("chromium-browser", ws=19)}
        with mock.patch.object(hsr.sys, "stderr", io.StringIO()) as err:
            emitted = self.run_restore([{}, settled], sweep_timeout=5)
        self.assertEqual(len([e for e in emitted if "group:add" in e]), 1)
        self.assertIn("no window turned up for org.kde.kdenlive", err.getvalue())
        self.assertFalse(any("lock" in e for e in emitted))
