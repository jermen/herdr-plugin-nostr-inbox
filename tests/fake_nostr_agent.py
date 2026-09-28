#!/usr/bin/env python3
"""Stand-in for the nostr-agent CLI in tests.

Serves the messages in $FAKE_AGENT_FIXTURE, applies state changes to that
file, and appends every invocation to $FAKE_AGENT_LOG.
"""

import json
import os
import sys

RANK = {"unread": 0, "read": 1, "in_progress": 2, "done": 3}


def fail(message):
    print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
    sys.exit(1)


def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    with open(os.environ["FAKE_AGENT_LOG"], "a") as log:
        log.write(json.dumps(args) + "\n")
    if os.environ.get("FAKE_AGENT_FAIL"):
        fail("relays unreachable")
    if "--limit" in args:
        index = args.index("--limit")
        del args[index:index + 2]
    path = os.environ["FAKE_AGENT_FIXTURE"]
    with open(path) as fixture:
        data = json.load(fixture)
    messages = data["messages"]
    relays = data.get("relay_status", [{"relay": "wss://one", "ok": True}])

    def save():
        with open(path, "w") as fixture:
            json.dump(data, fixture)

    def find(message_id):
        for message in messages:
            if message["id"] == message_id:
                return message
        fail(f"message {message_id} was not found")

    if args[:2] == ["inbox", "count"]:
        count = sum(1 for m in messages if m["direction"] == "in" and m["public_state"] == "unread")
        print(json.dumps({"ok": True, "count": count, "relay_status": relays}))
    elif args and args[0] == "inbox":
        shown = messages if "--all" in args else [m for m in messages if m["direction"] == "in"]
        print(json.dumps({"ok": True, "messages": shown, "relay_status": relays, "errors": [], "truncated": False}))
    elif args[:2] == ["message", "open"]:
        message = find(args[2])
        updates = []
        if message["public_state"] == "unread":
            message["public_state"] = "read"
            updates.append({"scope": "public", "state": "read", "changed": True})
        if message["private_state"] is None:
            message["private_state"] = "read"
            updates.append({"scope": "private", "state": "read", "changed": True})
        save()
        print(json.dumps({"ok": True, "changed": bool(updates), "updates": updates, "warnings": [], "message": message}))
    elif args and args[0] == "state":
        scope, message_id, state = args[1], args[2], args[3].replace("-", "_")
        message = find(message_id)
        if scope == "public":
            if RANK[state] < RANK[message["public_state"]]:
                fail("public state is forward-only")
            message["public_state"] = state
        else:
            message["private_state"] = state
            if "--ticket" in args:
                message["ticket_id"] = args[args.index("--ticket") + 1]
            if "--clear-ticket" in args:
                message["ticket_id"] = None
        save()
        print(json.dumps({"ok": True, "changed": True, "scope": scope, "state": state, "warnings": [], "message": message}))
    elif args and args[0] == "reply":
        text = sys.stdin.read()
        data.setdefault("replies", []).append({"to": args[1], "text": text})
        save()
        print(json.dumps({"ok": True, "message_id": "f" * 64, "recipient_relays": [{"url": "wss://one", "ok": True}]}))
    else:
        fail(f"unsupported fake command {args}")


if __name__ == "__main__":
    main()
