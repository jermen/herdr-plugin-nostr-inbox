#!/usr/bin/env python3
"""Install the Nostr inbox plugin: link it, bind the popup key and add the
unread indicator to Herdr's tab-bar status area."""

import argparse
import difflib
import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN_ID = "jermen.nostr-inbox"
KEY_BLOCK = ("# >>> nostr-inbox keybinding block", "# <<< nostr-inbox keybinding block")
BAR_BLOCK = ("# >>> nostr-inbox tab bar block", "# <<< nostr-inbox tab bar block")
# Installed before the rename to nostr-inbox; replaced on install, removed on uninstall.
LEGACY_PLUGIN_ID = "jermen.nostr-index"
LEGACY_BLOCKS = (
    ("# >>> nostr-index keybinding block", "# <<< nostr-index keybinding block"),
    ("# >>> nostr-index tab bar block", "# <<< nostr-index tab bar block"),
)


class InstallError(Exception):
    pass


def herdr_config_path(explicit=None):
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("HERDR_CONFIG_PATH"):
        return Path(os.environ["HERDR_CONFIG_PATH"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "herdr" / "config.toml"


def backup_root():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "herdr-nostr-inbox" / "backups"


def toml_string(value):
    # JSON string escapes are a subset of TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


def status_command(python=None):
    return f"{shlex.quote(python or sys.executable)} {shlex.quote(str(ROOT / 'nostr_inbox.py'))} status"


def key_block(key):
    start, end = KEY_BLOCK
    return (
        f"{start}\n"
        "[[keys.command]]\n"
        f"key = {toml_string(key)}\n"
        'type = "plugin_action"\n'
        f'command = "{PLUGIN_ID}.open"\n'
        'description = "Nostr messages"\n'
        f"{end}\n"
    )


def bar_block(command, interval, timeout):
    start, end = BAR_BLOCK
    return (
        f"{start}\n"
        "tab_bar_right = [\n"
        f'  {{ type = "command", command = {toml_string(command)}, '
        f"interval_seconds = {interval}, timeout_seconds = {timeout} }},\n"
        "]\n"
        f"{end}\n"
    )


def remove_block(text, block):
    start, end = block
    kept, inside = [], False
    for line in text.splitlines(keepends=True):
        if line.strip() == start:
            inside = True
        elif inside and line.strip() == end:
            inside = False
        elif not inside:
            kept.append(line)
    if inside:
        raise InstallError(f"unterminated managed block '{start}'; fix the config by hand")
    return "".join(kept)


def strip_managed(text):
    for block in (KEY_BLOCK, BAR_BLOCK, *LEGACY_BLOCKS):
        text = remove_block(text, block)
    return text


def bound_keys(parsed):
    keys = parsed.get("keys", {})
    used = set()
    for name, value in keys.items():
        if name == "command":
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                used.add(item)
    for command in keys.get("command", []):
        if isinstance(command, dict) and isinstance(command.get("key"), str):
            used.add(command["key"])
    return used


def candidate(text, key="prefix+m", command=None, interval=120, timeout=60):
    """The config with both managed blocks (re)inserted; refuses to touch
    settings it does not own."""
    command = command or status_command()
    text = strip_managed(text)
    # Trailing blank lines are normalized so that reinstalling is idempotent.
    text = text.rstrip("\n") + "\n" if text.strip() else ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise InstallError(f"the current config is not valid TOML: {err}") from err
    if "tab_bar_right" in parsed.get("ui", {}):
        raise InstallError(
            "ui.tab_bar_right is already set; add this entry to it by hand:\n  "
            f'{{ type = "command", command = {toml_string(command)}, interval_seconds = {interval}, timeout_seconds = {timeout} }}'
        )
    if key in bound_keys(parsed):
        raise InstallError(f"{key} is already bound in the config; choose another key with --key")

    lines = text.splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines) if line.split("#", 1)[0].strip() == "[ui]"), None)
    if header is None:
        # A [ui] header after [ui.*] sub-tables is valid TOML.
        text += ("\n" if text else "") + "[ui]\n" + bar_block(command, interval, timeout)
    else:
        lines.insert(header + 1, bar_block(command, interval, timeout))
        text = "".join(lines)
    text += "\n" + key_block(key)

    result = tomllib.loads(text)
    entries = result.get("ui", {}).get("tab_bar_right", [])
    if not entries or entries[-1].get("command") != command:
        raise InstallError("the generated config does not contain the status entry; not writing it")
    if not any(c.get("command") == f"{PLUGIN_ID}.open" for c in result.get("keys", {}).get("command", [])):
        raise InstallError("the generated config does not contain the key binding; not writing it")
    return text


def herdr(*args, check=False):
    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not binary:
        raise InstallError("herdr was not found on PATH")
    proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise InstallError(f"herdr {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def linked_root(config_path, plugin_id=PLUGIN_ID):
    registry = config_path.parent / "plugins.json"
    try:
        for plugin in json.loads(registry.read_text()):
            if plugin.get("plugin_id") == plugin_id:
                return plugin.get("plugin_root")
    except (OSError, ValueError, AttributeError):
        pass
    return None


def remove_legacy_plugin(config_path):
    """Unlink the pre-rename plugin id and stop its watcher. The watcher is
    stopped through its lock, because the old checkout may already lack the
    script its stop action would run."""
    if not linked_root(config_path, LEGACY_PLUGIN_ID):
        return
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    for lock in (Path(base) / "herdr" / "plugins" / LEGACY_PLUGIN_ID).glob("watch-*.lock"):
        with open(lock) as handle:
            try:
                # Acquiring the lock means no watcher holds it.
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                continue
            except BlockingIOError:
                pass
            try:
                os.kill(int(handle.read().strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
    herdr("plugin", "unlink", LEGACY_PLUGIN_ID, check=True)
    print(f"unlinked {LEGACY_PLUGIN_ID}")


def write_config(path, before, after):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_root() / stamp
    backup.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, backup / "config.toml")
    # Replace the file a symlinked config (e.g. from a dotfiles repo) points to.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.nostr-inbox.tmp")
    temp.write_text(after, encoding="utf-8")
    if target.exists():
        shutil.copymode(target, temp)
    os.replace(temp, target)
    return backup


def show_diff(path, before, after):
    diff = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                fromfile=str(path), tofile=f"{path} (new)")
    text = "".join(diff)
    print(text if text else f"{path}: no change")


def install(args):
    path = herdr_config_path(args.config)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    after = candidate(before, key=args.key, interval=args.interval, timeout=args.timeout)
    show_diff(path, before, after)
    current = linked_root(path)
    if args.dry_run:
        if not args.no_link and linked_root(path, LEGACY_PLUGIN_ID):
            print(f"would unlink {LEGACY_PLUGIN_ID}")
        if not args.no_link and current != str(ROOT):
            print(f"would link {ROOT}" + (f" (replacing {current})" if current else ""))
        print("dry run: nothing changed")
        return 0
    if after != before:
        print(f"backup: {write_config(path, before, after)}")
    if not args.no_link:
        remove_legacy_plugin(path)
        if current and current != str(ROOT):
            herdr("plugin", "action", "invoke", "stop", "--plugin", PLUGIN_ID)
            herdr("plugin", "unlink", PLUGIN_ID, check=True)
        if current != str(ROOT):
            herdr("plugin", "link", str(ROOT), check=True)
            print(f"linked {ROOT}")
        herdr("plugin", "action", "invoke", "start", "--plugin", PLUGIN_ID)
    if herdr("server", "reload-config").returncode == 0:
        print("server config reloaded")
    print("In the Herdr UI use the global menu -> reload config, then press "
          f"{args.key} for the inbox.")
    return 0


def uninstall(args):
    path = herdr_config_path(args.config)
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    after = strip_managed(before)
    show_diff(path, before, after)
    if args.dry_run:
        print("dry run: nothing changed")
        return 0
    if after != before:
        print(f"backup: {write_config(path, before, after)}")
    if not args.no_link:
        remove_legacy_plugin(path)
    if not args.no_link and linked_root(path):
        herdr("plugin", "action", "invoke", "stop", "--plugin", PLUGIN_ID)
        herdr("plugin", "unlink", PLUGIN_ID, check=True)
        print(f"unlinked {PLUGIN_ID}")
    herdr("server", "reload-config")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Herdr config.toml (default: $HERDR_CONFIG_PATH or ~/.config/herdr/config.toml)")
    parser.add_argument("--key", default="prefix+m", help="key that opens the inbox (default: prefix+m)")
    parser.add_argument("--interval", type=int, default=120, help="seconds between unread-count refreshes")
    parser.add_argument("--timeout", type=int, default=60, help="seconds before a refresh is abandoned")
    parser.add_argument("--no-link", action="store_true", help="only edit the config (e.g. on a separate UI computer)")
    parser.add_argument("--dry-run", action="store_true", help="show the changes without applying them")
    parser.add_argument("--uninstall", action="store_true", help="remove the managed config blocks and unlink the plugin")
    args = parser.parse_args(argv)
    if not 1 <= args.interval <= 31536000 or not 1 <= args.timeout <= 3600:
        parser.error("--interval must be 1-31536000 and --timeout 1-3600 seconds")
    try:
        return uninstall(args) if args.uninstall else install(args)
    except InstallError as err:
        print(f"install: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
