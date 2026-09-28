import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE_AGENT = Path(__file__).resolve().parent / "fake_nostr_agent.py"
sys.path.insert(0, str(ROOT))

ALICE = "a" * 64
BOB = "b" * 64
SELF = "c" * 64


def message(message_id, sender=ALICE, content="Restart db02", created_at=1790000000, direction="in",
            public_state="unread", private_state=None, ticket_id=None, alias="alice", subject=None):
    return {
        "id": message_id,
        "sender": sender,
        "sender_npub": "npub1" + sender[:58],
        "sender_alias": alias,
        "recipients": [SELF if direction == "in" else BOB],
        "created_at": created_at,
        "created_at_iso": "",
        "reply_to": None,
        "subject": subject,
        "content": content,
        "direction": direction,
        "public_state": public_state,
        "private_state": private_state,
        "ticket_id": ticket_id,
    }


def fixture_messages():
    return [
        message("1" * 64, content="Restart db02\nsecond line", created_at=1790000000),
        message("2" * 64, sender=BOB, alias="codex", content="Review completed", created_at=1790000100,
                public_state="in_progress", private_state="in_progress", ticket_id="DMDOX-330"),
        message("3" * 64, sender=SELF, alias=None, content="Thanks", created_at=1790000200, direction="out",
                public_state="done"),
    ]


class FakeAgent:
    """Temporary plugin config dir wired to the fake nostr-agent."""

    def __init__(self, messages=None, relay_status=None, config=None):
        self.tmp = tempfile.TemporaryDirectory(prefix="nostr-inbox-test-")
        base = Path(self.tmp.name)
        FAKE_AGENT.chmod(FAKE_AGENT.stat().st_mode | stat.S_IXUSR)
        self.fixture = base / "fixture.json"
        self.log = base / "calls.log"
        self.log.touch()
        data = {"messages": messages if messages is not None else fixture_messages()}
        if relay_status is not None:
            data["relay_status"] = relay_status
        self.fixture.write_text(json.dumps(data))
        self.config_dir = base / "config"
        self.config_dir.mkdir()
        self.config = {"nostr_agent": str(FAKE_AGENT), **(config or {})}
        (self.config_dir / "config.json").write_text(json.dumps(self.config))
        self.env = {
            "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
            "HERDR_PLUGIN_STATE_DIR": str(base / "state"),
            "FAKE_AGENT_FIXTURE": str(self.fixture),
            "FAKE_AGENT_LOG": str(self.log),
        }
        self.saved = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def data(self):
        return json.loads(self.fixture.read_text())

    def close(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()
