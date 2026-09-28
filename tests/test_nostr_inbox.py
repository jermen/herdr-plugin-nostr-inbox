import json
import os
import unittest

from helpers import FakeAgent, message

import nostr_inbox as core


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAgent()
        self.addCleanup(self.fake.close)
        self.config = core.load_config()

    def test_inbox_returns_cli_json_and_adds_limit_only_when_configured(self):
        result = core.inbox(self.config, include_sent=True)
        self.assertEqual(len(result["messages"]), 3)
        core.inbox(dict(self.config, limit=500))
        self.assertEqual(self.fake.calls(), [["inbox", "--all"], ["inbox", "--limit", "500"]])

    def test_state_changes_use_cli_spelling_and_ticket_options(self):
        message_id = "1" * 64
        core.set_state(self.config, "private", message_id, "in_progress", ticket="DMDOX-330")
        core.set_state(self.config, "private", message_id, "todo", clear_ticket=True)
        core.set_state(self.config, "public", message_id, "in_progress")
        self.assertEqual(self.fake.calls(), [
            ["state", "private", message_id, "in-progress", "--ticket", "DMDOX-330"],
            ["state", "private", message_id, "todo", "--clear-ticket"],
            ["state", "public", message_id, "in-progress"],
        ])
        with self.assertRaises(core.AgentError):
            core.set_state(self.config, "public", message_id, "todo")

    def test_cli_errors_surface_the_json_error(self):
        with self.assertRaisesRegex(core.AgentError, "forward-only"):
            core.set_state(self.config, "public", "2" * 64, "read")

    def test_reply_passes_text_on_stdin(self):
        core.reply(self.config, "1" * 64, "on it")
        self.assertEqual(self.fake.data()["replies"], [{"to": "1" * 64, "text": "on it"}])

    def test_missing_cli_is_reported(self):
        with self.assertRaisesRegex(core.AgentError, "cannot run"):
            core.inbox(dict(self.config, nostr_agent="/nonexistent/nostr-agent"))


class StatusTest(unittest.TestCase):
    def status(self, **kwargs):
        config = kwargs.pop("config", None)
        fake = FakeAgent(config=config, **kwargs)
        self.addCleanup(fake.close)
        return core.status_text(core.load_config())

    def test_unread_count(self):
        self.assertEqual(self.status(), "✉ 1")

    def test_zero_is_hidden_unless_configured(self):
        read = [message("1" * 64, public_state="read")]
        self.assertEqual(self.status(messages=read), "")
        self.assertEqual(self.status(messages=read, config={"show_zero": True}), "✉ 0")

    def test_partial_relay_answer_is_marked(self):
        relays = [{"relay": "wss://one", "ok": True}, {"relay": "wss://two", "ok": False}]
        self.assertEqual(self.status(relay_status=relays), "✉ 1*")
        self.assertEqual(self.status(messages=[], relay_status=relays), "✉ 0*")
        self.assertEqual(self.status(relay_status=relays, config={"mark_partial": False}), "✉ 1")

    def test_failure_is_a_question_mark(self):
        os.environ["FAKE_AGENT_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_AGENT_FAIL")
        self.assertEqual(self.status(), "✉ ?")

    def test_status_subcommand_survives_a_broken_config(self):
        fake = FakeAgent()
        self.addCleanup(fake.close)
        (fake.config_dir / "config.json").write_text("{broken")
        from io import StringIO
        from unittest import mock
        with mock.patch("sys.stdout", new=StringIO()) as out:
            self.assertEqual(core.main(["status"]), 0)
        self.assertEqual(out.getvalue().strip(), "✉ ?")


class TextTest(unittest.TestCase):
    def test_printable_removes_terminal_controls(self):
        text = "\x1b[31mred\x1b]0;evil title\x07 ‮flip‬\nnext"
        cleaned = core.printable(text)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\x07", cleaned)
        self.assertNotIn("‮", cleaned)
        self.assertEqual(cleaned, "[31mred]0;evil title flip next")
        self.assertEqual(core.printable("abcdef", 4), "abc…")

    def test_headline_prefers_subject_then_first_line(self):
        self.assertEqual(core.headline(message("1" * 64, content="\n\n  first\nsecond")), "first")
        self.assertEqual(core.headline(message("1" * 64, subject="Topic")), "Topic")

    def test_sender_label_falls_back_to_short_npub(self):
        label = core.sender_label(message("1" * 64, alias=None))
        self.assertTrue(label.startswith("npub1aaaa"))
        self.assertLessEqual(len(label), 20)


class PromptPasteTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAgent()
        self.addCleanup(self.fake.close)

    def context(self, value):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = value
        self.addCleanup(os.environ.pop, "HERDR_PLUGIN_CONTEXT_JSON", None)

    def test_prompt_pane_comes_from_the_popup_context(self):
        self.context(json.dumps({"workspace_id": "w1", "focused_pane_id": "w1:p2", "focused_pane_agent": "claude"}))
        self.assertEqual(core.prompt_pane(), "w1:p2")
        for broken in ("", "{", "[]", json.dumps({"focused_pane_id": ""}), json.dumps({"focused_pane_id": 3})):
            self.context(broken)
            self.assertIsNone(core.prompt_pane(), broken)

    def test_paste_types_the_text_without_enter(self):
        core.paste_to_pane("w1:p2", "nostr:nevent1abc ")
        self.assertEqual(self.fake.herdr_calls(), [["pane", "send-text", "w1:p2", "nostr:nevent1abc "]])
        os.environ["FAKE_HERDR_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_HERDR_EXIT")
        with self.assertRaises(core.AgentError):
            core.paste_to_pane("w1:p2", "x")


class WatcherTest(unittest.TestCase):
    def test_only_new_unread_incoming_messages_after_start_are_announced(self):
        messages = [
            message("1" * 64, created_at=100),
            message("2" * 64, created_at=300),
            message("3" * 64, created_at=300, public_state="read"),
            message("4" * 64, created_at=300, direction="out"),
            message("5" * 64, created_at=300),
        ]
        fresh = core.new_unread(messages, announced={"5" * 64}, since=200)
        self.assertEqual([m["id"] for m in fresh], ["2" * 64])

    def test_config_ignores_unknown_keys_and_rejects_non_objects(self):
        fake = FakeAgent()
        self.addCleanup(fake.close)
        path = fake.config_dir / "config.json"
        path.write_text(json.dumps({"poll_seconds": 60, "unknown": 1}))
        config = core.load_config()
        self.assertEqual(config["poll_seconds"], 60)
        self.assertNotIn("unknown", config)
        path.write_text("[]")
        with self.assertRaises(core.AgentError):
            core.load_config()

    def test_stop_without_a_running_watcher_is_harmless(self):
        fake = FakeAgent()
        self.addCleanup(fake.close)
        self.assertEqual(core.stop_watcher(), 0)


if __name__ == "__main__":
    unittest.main()
