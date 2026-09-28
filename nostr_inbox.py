#!/usr/bin/env python3
"""Herdr Nostr inbox: unread indicator, toast watcher and popup launcher.

All Nostr work (relays, NIP-42, NIP-44/59, identity, message state) happens in
the nostr-agent CLI. This module only runs it and presents its JSON. It keeps
no message state: the watcher's set of already announced messages lives in
memory only.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

PLUGIN_ID = "jermen.nostr-inbox"
DEFAULTS = {
    "nostr_agent": None,
    "limit": None,
    "command_timeout": 90,
    "icon": "✉",
    "show_zero": False,
    "mark_partial": True,
    "toast": True,
    "toast_sound": "none",
    "poll_seconds": 300,
    "show_sent": False,
}
PUBLIC_STATES = ("read", "in_progress", "done")
PRIVATE_STATES = ("read", "todo", "in_progress", "done")


class AgentError(Exception):
    """nostr-agent is missing, failed, or returned unusable output."""


def herdr_config_dir():
    config_path = os.environ.get("HERDR_CONFIG_PATH")
    if config_path:
        return Path(config_path).expanduser().parent
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "herdr"


def plugin_config_dir():
    # Status-area commands run outside the plugin runtime, without Herdr's env.
    configured = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(configured) if configured else herdr_config_dir() / "plugins" / "config" / PLUGIN_ID


def plugin_state_dir():
    configured = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if configured:
        return Path(configured)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "herdr" / "plugins" / PLUGIN_ID


def load_config(path=None):
    path = Path(path) if path else plugin_config_dir() / "config.json"
    config = dict(DEFAULTS)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as err:
        raise AgentError(f"cannot read {path}: {err}") from err
    if not isinstance(raw, dict):
        raise AgentError(f"{path} must contain a JSON object")
    config.update({key: value for key, value in raw.items() if key in DEFAULTS})
    return config


def agent_command(config):
    configured = config.get("nostr_agent")
    if configured:
        return [os.path.expanduser(str(configured))]
    found = shutil.which("nostr-agent")
    if found:
        return [found]
    fallback = os.path.join(os.path.expanduser("~"), ".local", "bin", "nostr-agent")
    if os.access(fallback, os.X_OK):
        return [fallback]
    raise AgentError("nostr-agent not found; install nostr-agent-comms or set \"nostr_agent\" in config.json")


def run_agent(config, args, input_text=None):
    limit = config.get("limit")
    extra = ["--limit", str(limit)] if limit and args and args[0] in ("inbox", "message", "state", "reply") else []
    command = agent_command(config) + list(args) + extra + ["--json"]
    try:
        proc = subprocess.run(
            command,
            input=input_text if input_text is not None else "",
            capture_output=True,
            text=True,
            timeout=float(config.get("command_timeout") or DEFAULTS["command_timeout"]),
        )
    except FileNotFoundError as err:
        raise AgentError(f"cannot run {command[0]}: {err}") from err
    except subprocess.TimeoutExpired as err:
        raise AgentError(f"nostr-agent {' '.join(args[:2])} timed out") from err
    if proc.returncode != 0:
        message = proc.stderr.strip() or f"exit status {proc.returncode}"
        try:
            parsed = json.loads(proc.stderr)
            message = parsed.get("error") or message
        except ValueError:
            pass
        raise AgentError(message)
    try:
        return json.loads(proc.stdout)
    except ValueError as err:
        raise AgentError("nostr-agent returned invalid JSON") from err


def cli_state(state):
    return state.replace("_", "-")


def inbox(config, include_sent=False):
    return run_agent(config, ["inbox"] + (["--all"] if include_sent else []))


def unread_count(config):
    return run_agent(config, ["inbox", "count"])


def open_message(config, message_id):
    return run_agent(config, ["message", "open", message_id])


def set_state(config, scope, message_id, state, ticket=None, clear_ticket=False):
    allowed = PUBLIC_STATES if scope == "public" else PRIVATE_STATES
    if state not in allowed:
        raise AgentError(f"unknown {scope} state {state}")
    args = ["state", scope, message_id, cli_state(state)]
    if ticket:
        args += ["--ticket", ticket]
    if clear_ticket:
        args.append("--clear-ticket")
    return run_agent(config, args)


def reply(config, message_id, text):
    return run_agent(config, ["reply", message_id], input_text=text)


def partial(relay_status):
    return any(not relay.get("ok") for relay in relay_status or [])


def status_text(config):
    """One line for Herdr's tab-bar status area; empty hides the entry."""
    icon = str(config.get("icon") or DEFAULTS["icon"])
    try:
        result = unread_count(config)
        count = int(result["count"])
    except (AgentError, KeyError, TypeError, ValueError):
        return f"{icon} ?"
    marker = "*" if config.get("mark_partial", True) and partial(result.get("relay_status")) else ""
    if count == 0 and not marker and not config.get("show_zero"):
        return ""
    return f"{icon} {count}{marker}"


def printable(text, limit=None):
    """Untrusted message text without terminal controls, on one line."""
    cleaned = []
    for char in str(text or ""):
        category = unicodedata.category(char)
        if char in "\n\t\r":
            cleaned.append(" ")
        elif category in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs"):
            continue
        else:
            cleaned.append(char)
    line = " ".join("".join(cleaned).split())
    if limit and len(line) > limit:
        line = line[: max(limit - 1, 0)] + "…"
    return line


def sender_label(message):
    alias = message.get("sender_alias")
    if alias:
        return printable(alias, 40)
    npub = message.get("sender_npub") or ""
    return f"{npub[:12]}…{npub[-6:]}" if len(npub) > 20 else npub


def headline(message):
    subject = message.get("subject")
    if subject:
        return printable(subject)
    for line in str(message.get("content") or "").splitlines():
        if line.strip():
            return printable(line)
    return ""


def herdr_command():
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr"


def toast(config, message):
    title = f"Nostr: {sender_label(message)}"
    body = headline(message)
    command = [herdr_command(), "notification", "show", printable(title, 60)]
    if body:
        command += ["--body", printable(body, 120)]
    sound = config.get("toast_sound")
    if sound in ("done", "request"):
        command += ["--sound", sound]
    subprocess.run(command, capture_output=True, timeout=10, check=False)


def new_unread(messages, announced, since):
    """Unread incoming messages worth a toast: not yet announced by this
    process and not older than its start, so relays that come back later do
    not replay old messages."""
    fresh = []
    for message in messages:
        if message.get("direction") != "in" or message.get("public_state") != "unread":
            continue
        if message["id"] in announced or int(message.get("created_at") or 0) < since:
            continue
        fresh.append(message)
    return fresh


def lock_path():
    socket = os.environ.get("HERDR_SOCKET_PATH", "")
    digest = hashlib.sha256(socket.encode()).hexdigest()[:12]
    return plugin_state_dir() / f"watch-{digest}.lock"


def log(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}", flush=True)


def socket_alive(path):
    return bool(path) and os.path.exists(path)


def watch(poll_override=None):
    socket = os.environ.get("HERDR_SOCKET_PATH")
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    started = int(time.time())
    announced = set()
    seeded = False
    log(f"watching Nostr inbox for {socket or 'no socket'}")
    while socket is None or socket_alive(socket):
        try:
            config = load_config()
            if config.get("toast", True):
                messages = inbox(config)["messages"]
                if seeded:
                    for message in new_unread(messages, announced, started):
                        toast(config, message)
                        announced.add(message["id"])
                else:
                    # Messages already waiting at start are counted, not announced.
                    announced.update(m["id"] for m in messages)
                    seeded = True
            poll = poll_override or int(config.get("poll_seconds") or DEFAULTS["poll_seconds"])
        except (AgentError, OSError, subprocess.SubprocessError) as err:
            log(f"poll failed: {err}")
            poll = poll_override or DEFAULTS["poll_seconds"]
        deadline = time.time() + max(poll, 30)
        while time.time() < deadline:
            if socket is not None and not socket_alive(socket):
                break
            time.sleep(2)
    log("Herdr socket is gone; exiting")
    return 0


def start_watcher():
    state = plugin_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "watch.log", "a") as log_file:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "watch"],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
    return 0


def stop_watcher():
    path = lock_path()
    try:
        handle = open(path, "r")
    except OSError:
        return 0
    with handle:
        try:
            # Acquiring the lock proves no watcher holds it, so a stale pid
            # (possibly reused by another process) is never signalled.
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return 0
        except BlockingIOError:
            pass
        try:
            os.kill(int(handle.read().strip()), signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass
    return 0


def open_popup():
    command = [herdr_command(), "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "inbox"]
    proc = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout)
    return proc.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nostr_inbox.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print the tab-bar unread indicator")
    sub.add_parser("open", help="open the inbox popup")
    watch_parser = sub.add_parser("watch", help="run the toast watcher in the foreground")
    watch_parser.add_argument("--detach", action="store_true", help="start it in the background and return")
    watch_parser.add_argument("--poll", type=int, help=argparse.SUPPRESS)
    sub.add_parser("stop", help="stop the toast watcher")
    sub.add_parser("tui", help="run the inbox UI in this terminal")
    args = parser.parse_args(argv)

    if args.command == "status":
        try:
            config = load_config()
        except AgentError:
            print(f"{DEFAULTS['icon']} ?")
            return 0
        print(status_text(config))
        return 0
    if args.command == "open":
        return open_popup()
    if args.command == "watch":
        return start_watcher() if args.detach else watch(args.poll)
    if args.command == "stop":
        return stop_watcher()
    if args.command == "tui":
        import inbox_tui

        return inbox_tui.main()
    return 2


if __name__ == "__main__":
    sys.exit(main())
