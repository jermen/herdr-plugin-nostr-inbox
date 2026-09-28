import os
import pty
import select
import signal
import sys
import time
import unittest

from helpers import ROOT, FakeAgent, message

import inbox_tui as tui


class RenderTest(unittest.TestCase):
    def test_entry_shows_both_state_dimensions_and_ticket_within_width(self):
        entry = message("1" * 64, public_state="in_progress", private_state="in_progress", ticket_id="DMDOX-330")
        lines = tui.entry_lines(entry, 60)
        self.assertEqual(len(lines), 3)
        self.assertIn("alice", lines[0])
        self.assertTrue(lines[0].startswith("○"))
        self.assertIn("Restart db02", lines[1])
        self.assertIn("PUBLIC: IN PROGRESS", lines[2])
        self.assertIn("PRIVATE: IN PROGRESS", lines[2])
        self.assertIn("DMDOX-330", lines[2])
        for line in lines:
            self.assertLessEqual(tui.display_width(line), 60)

    def test_unread_marker_and_missing_private_state(self):
        lines = tui.entry_lines(message("1" * 64), 60)
        self.assertTrue(lines[0].startswith("●"))
        self.assertIn("PRIVATE: -", lines[2])

    def test_wide_and_hostile_text_stays_inside_the_width(self):
        hostile = message("1" * 64, content="\x1b[2J漢字" * 30 + "\x07", alias="\x1b]0;x\x07名前" * 5)
        for width in (20, 33, 80):
            for line in tui.entry_lines(hostile, width) + tui.detail_lines(hostile, width):
                self.assertLessEqual(tui.display_width(line), width, repr(line))
                self.assertNotIn("\x1b", line)
                self.assertNotIn("\x07", line)

    def test_wrap_keeps_indentation_blank_lines_and_breaks_long_words(self):
        lines = tui.wrap("first line\n\n    indented code\n" + "x" * 25, 12)
        self.assertEqual(lines, ["first line", "", "    indented", "code", "x" * 12, "x" * 12, "x"])
        self.assertEqual(tui.wrap("\u6f22\u5b57\u6f22", 4), ["\u6f22\u5b57", "\u6f22"])

    def test_detail_names_sender_states_and_content(self):
        entry = message("1" * 64, private_state="todo", ticket_id="DMDOX-330", subject="Topic")
        lines = tui.detail_lines(entry, 80)
        text = "\n".join(lines)
        self.assertIn("From: alice", text)
        self.assertIn("Subject: Topic", text)
        self.assertIn("Public: UNREAD   Private: TODO   Ticket: DMDOX-330", text)
        self.assertEqual(lines[-1], "Restart db02")

    def test_summary_counts_unread_and_relays(self):
        messages = [message("1" * 64), message("2" * 64, public_state="read")]
        relays = [{"ok": True}, {"ok": False}]
        self.assertEqual(tui.summary(messages, relays, False), "Messages · 1 unread · relays 1/2")


class PopupSmokeTest(unittest.TestCase):
    """Drives the curses UI in a pseudo-terminal against the fake CLI."""

    def setUp(self):
        self.fake = FakeAgent()
        self.addCleanup(self.fake.close)
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update({"TERM": "xterm-256color", "LINES": "30", "COLUMNS": "100"})
            os.environ.pop("VISUAL", None)
            os.environ.pop("EDITOR", None)
            os.chdir(ROOT)
            os.execv(sys.executable, [sys.executable, "-B", "nostr_inbox.py", "tui"])
        self.pid, self.fd = pid, fd
        self.output = b""
        self.addCleanup(self.kill)

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(self.fd)

    def read_until(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            ready, _, _ = select.select([self.fd], [], [], 0.1)
            if ready:
                try:
                    self.output += os.read(self.fd, 65536)
                except OSError:
                    break
        self.fail(f"timed out; calls={self.fake.calls()} output tail={self.output[-400:]!r}")

    def send(self, keys):
        os.write(self.fd, keys.encode())

    def test_open_state_ticket_and_reply_go_through_the_cli(self):
        first, second = "1" * 64, "2" * 64
        self.read_until(lambda: b"codex" in self.output and b"alice" in self.output)
        self.assertEqual(self.fake.calls()[0], ["inbox", "--all"])
        self.assertNotIn(b"Thanks", self.output)  # sent messages are hidden by default

        self.send("j\r")  # the second-newest entry is alice's unread message
        self.read_until(lambda: ["message", "open", first] in self.fake.calls())
        self.send("st")
        self.read_until(lambda: ["state", "private", first, "todo"] in self.fake.calls())
        self.send("pi")
        self.read_until(lambda: ["state", "public", first, "in-progress"] in self.fake.calls())
        self.send("tDMDOX-331\r")
        self.read_until(lambda: ["state", "private", first, "in-progress", "--ticket", "DMDOX-331"] in self.fake.calls())
        self.send("rOn it\ry")
        self.read_until(lambda: self.fake.data().get("replies"))
        self.assertEqual(self.fake.data()["replies"], [{"to": first, "text": "On it"}])
        self.send("q")  # back to the list
        self.send("kT")  # clear codex's ticket, keeping its private state
        self.read_until(lambda: ["state", "private", second, "in-progress", "--clear-ticket"] in self.fake.calls())
        self.send("q")
        _, status = os.waitpid(self.pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        data = {m["id"]: m for m in self.fake.data()["messages"]}
        self.assertEqual((data[first]["public_state"], data[first]["private_state"], data[first]["ticket_id"]),
                         ("in_progress", "in_progress", "DMDOX-331"))
        self.assertIsNone(data[second]["ticket_id"])


if __name__ == "__main__":
    unittest.main()
