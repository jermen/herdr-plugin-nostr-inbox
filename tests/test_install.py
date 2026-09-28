import shlex
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts the plugin root on sys.path)

import install

COMMAND = "/usr/bin/python3 '/opt/nostr index/nostr_inbox.py' status"


def apply(text, **kwargs):
    return install.candidate(text, command=COMMAND, **kwargs)


class CandidateTest(unittest.TestCase):
    def test_inserts_status_entry_under_existing_ui_and_appends_key(self):
        before = 'onboarding = false\n[ui]\nagent_panel_sort = "priority"\n\n[keys]\nprefix = "ctrl+a"\n'
        after = apply(before)
        parsed = tomllib.loads(after)
        self.assertEqual(parsed["ui"]["agent_panel_sort"], "priority")
        self.assertEqual(parsed["ui"]["tab_bar_right"], [
            {"type": "command", "command": COMMAND, "interval_seconds": 120, "timeout_seconds": 60},
        ])
        self.assertEqual(parsed["keys"]["command"], [{
            "key": "prefix+m", "type": "plugin_action", "command": "jermen.nostr-inbox.open",
            "description": "Nostr messages",
        }])
        self.assertEqual(parsed["keys"]["prefix"], "ctrl+a")

    def test_is_idempotent_and_removable(self):
        before = '[ui]\nagent_panel_sort = "priority"\n\n[ui.sidebar.agents]\nrow_gap = 0\n'
        once = apply(before)
        self.assertEqual(apply(once), once)
        self.assertEqual(tomllib.loads(install.strip_managed(once)), tomllib.loads(before))

    def test_replaces_blocks_installed_under_the_old_plugin_id(self):
        old = (
            'onboarding = false\n[ui]\n# >>> nostr-index tab bar block\n'
            'tab_bar_right = [\n  { type = "command", command = "python3 /old/nostr_index.py status", interval_seconds = 120, timeout_seconds = 60 },\n]\n'
            '# <<< nostr-index tab bar block\nagent_panel_sort = "priority"\n\n'
            '# >>> nostr-index keybinding block\n[[keys.command]]\nkey = "prefix+m"\ntype = "plugin_action"\n'
            'command = "jermen.nostr-index.open"\ndescription = "Nostr messages"\n# <<< nostr-index keybinding block\n'
        )
        after = apply(old)
        self.assertNotIn("nostr-index", after)
        parsed = tomllib.loads(after)
        self.assertEqual([c["command"] for c in parsed["keys"]["command"]], ["jermen.nostr-inbox.open"])
        self.assertEqual(parsed["ui"]["tab_bar_right"][0]["command"], COMMAND)
        self.assertEqual(parsed["ui"]["agent_panel_sort"], "priority")
        self.assertNotIn("nostr-index", install.strip_managed(old))

    def test_adds_ui_table_after_existing_sub_tables(self):
        before = "[ui.sidebar.agents]\nrow_gap = 0\n"
        parsed = tomllib.loads(apply(before))
        self.assertEqual(parsed["ui"]["sidebar"]["agents"]["row_gap"], 0)
        self.assertEqual(parsed["ui"]["tab_bar_right"][0]["command"], COMMAND)
        self.assertIn("prefix+m", install.bound_keys(parsed))

    def test_empty_config(self):
        parsed = tomllib.loads(apply("", key="prefix+alt+m", interval=60, timeout=30))
        self.assertEqual(parsed["ui"]["tab_bar_right"][0]["interval_seconds"], 60)
        self.assertEqual(parsed["keys"]["command"][0]["key"], "prefix+alt+m")

    def test_refuses_foreign_tab_bar_and_taken_keys(self):
        with self.assertRaisesRegex(install.InstallError, "tab_bar_right is already set"):
            apply('[ui]\ntab_bar_right = [{ type = "zoom" }]\n')
        with self.assertRaisesRegex(install.InstallError, "already bound"):
            apply('[keys]\nsettings = ["prefix+m", "f2"]\n')
        with self.assertRaisesRegex(install.InstallError, "already bound"):
            apply('[[keys.command]]\nkey = "prefix+m"\ntype = "shell"\ncommand = "true"\n')
        with self.assertRaisesRegex(install.InstallError, "not valid TOML"):
            apply("[ui\n")

    def test_status_command_quotes_paths_with_spaces_and_commas(self):
        with mock.patch.object(install, "ROOT", Path("/home/u/Documents/dmDOX, s.r.o./Dev/plugin")):
            command = install.status_command("/usr/bin/python3")
        self.assertEqual(command, "/usr/bin/python3 '/home/u/Documents/dmDOX, s.r.o./Dev/plugin/nostr_inbox.py' status")
        self.assertEqual(shlex.split(command)[1], "/home/u/Documents/dmDOX, s.r.o./Dev/plugin/nostr_inbox.py")
        parsed = tomllib.loads(install.candidate("", command="python3 'a, b.c/x y' status"))
        self.assertEqual(parsed["ui"]["tab_bar_right"][0]["command"], "python3 'a, b.c/x y' status")


if __name__ == "__main__":
    unittest.main()
