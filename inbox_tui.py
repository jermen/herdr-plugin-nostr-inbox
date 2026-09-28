#!/usr/bin/env python3
"""Popup inbox: list, read and answer Nostr messages and set their state.

Every change goes through nostr-agent; the UI only holds the current answer
in memory while it is open.
"""

import curses
import locale
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unicodedata

import nostr_inbox as core

STATE_LABELS = {
    "unread": "UNREAD",
    "read": "READ",
    "todo": "TODO",
    "in_progress": "IN PROGRESS",
    "done": "DONE",
    None: "-",
}
PUBLIC_KEYS = {"r": "read", "i": "in_progress", "d": "done"}
PRIVATE_KEYS = {"r": "read", "t": "todo", "i": "in_progress", "d": "done"}
LIST_HELP = "enter open  i paste ref  r reply  p public  s private  t ticket  T clear ticket  a sent  g reload  q quit"
DETAIL_HELP = "esc back  i paste ref  r reply  p public  s private  t ticket  T clear ticket  j/k scroll  q back"
ENTRY_HEIGHT = 4


def char_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text):
    return sum(char_width(char) for char in text)


def take_width(text, width):
    """Longest prefix that fits into width cells (at least one character)."""
    used = 0
    for index, char in enumerate(text):
        used += char_width(char)
        if used > width:
            return text[: max(index, 1)]
    return text


def clip(text, width):
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    return take_width(text, width - 1) + "…"


def pad(text, width):
    text = clip(text, width)
    return text + " " * max(width - display_width(text), 0)


def clean_line(line):
    """One line of untrusted text with terminal controls removed; keeps indentation."""
    kept = []
    for char in line.replace("\t", "    "):
        if unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp", "Co", "Cs"):
            continue
        kept.append(char)
    return "".join(kept).rstrip()


def wrap_line(line, width):
    if not line:
        return [""]
    lines, current = [], ""
    for token in re.split(r"(\s+)", line):
        if not token:
            continue
        if display_width(current) + display_width(token) <= width:
            current += token
            continue
        if token.isspace():
            lines.append(current.rstrip())
            current = ""
            continue
        if current.strip():
            lines.append(current.rstrip())
        current = ""
        while display_width(token) > width:
            piece = take_width(token, width)
            lines.append(piece)
            token = token[len(piece):]
        current = token
    lines.append(current.rstrip())
    return lines


def wrap(text, width):
    width = max(width, 1)
    lines = []
    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        lines.extend(wrap_line(clean_line(raw), width))
    return lines


def state_label(state):
    return STATE_LABELS.get(state, core.printable(state, 16).upper())


def format_time(timestamp):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(timestamp)))
    except (TypeError, ValueError, OverflowError):
        return ""


def correspondent(message):
    if message.get("direction") == "out":
        recipients = message.get("recipients") or []
        target = recipients[0][:12] + "…" if recipients else "?"
        return f"→ {target}"
    return core.sender_label(message)


def is_unread(message):
    return message.get("direction") == "in" and message.get("public_state") == "unread"


def entry_lines(message, width):
    """The three lines of one list entry."""
    date = format_time(message.get("created_at"))
    marker = "●" if is_unread(message) else "○"
    first = pad(f"{marker} {correspondent(message)}", max(width - len(date) - 1, 0)) + " " + date
    second = "  " + clip(core.headline(message) or "(empty)", width - 2)
    states = f"  PUBLIC: {state_label(message.get('public_state')):<12} PRIVATE: {state_label(message.get('private_state'))}"
    if message.get("ticket_id"):
        states += f"   {core.printable(message['ticket_id'], 64)}"
    return [clip(first, width), second, clip(states, width)]


def detail_lines(message, width):
    who = "To" if message.get("direction") == "out" else "From"
    header = [f"{who}: {correspondent(message) if who == 'To' else core.sender_label(message)}"]
    if who == "From":
        header.append(f"      {message.get('sender_npub', '')}")
    header.append(f"Date: {format_time(message.get('created_at'))}")
    if message.get("subject"):
        header.append(f"Subject: {core.printable(message['subject'])}")
    if message.get("reply_to"):
        header.append(f"Reply to: {message['reply_to'][:16]}…")
    state = f"Public: {state_label(message.get('public_state'))}   Private: {state_label(message.get('private_state'))}"
    if message.get("ticket_id"):
        state += f"   Ticket: {core.printable(message['ticket_id'], 64)}"
    header.append(state)
    lines = [clip(line, width) for line in header]
    if message.get("ref"):
        # Wrapped rather than clipped, so it can still be copied by hand.
        lines.extend(wrap("Ref: " + core.printable(message["ref"]), width))
    lines.append("─" * max(width, 0))
    return lines + wrap(message.get("content"), width)


def cursor(visible):
    try:
        curses.curs_set(1 if visible else 0)
    except curses.error:
        pass


def summary(messages, relays, show_sent):
    unread = sum(1 for message in messages if is_unread(message))
    parts = ["Messages", f"{unread} unread"]
    if relays:
        ok = sum(1 for relay in relays if relay.get("ok"))
        parts.append(f"relays {ok}/{len(relays)}")
    if show_sent:
        parts.append("with sent")
    return " · ".join(parts)


class App:
    def __init__(self, screen, config, config_error=None):
        self.screen = screen
        self.config = config
        self.messages = []
        self.relays = []
        self.show_sent = bool(config.get("show_sent"))
        self.selected = 0
        self.top = 0
        self.mode = "list"
        self.detail_id = None
        self.detail_top = 0
        self.notice = config_error or ""
        self.notice_error = bool(config_error)
        self.loaded = False

    # Data

    def visible(self):
        shown = [m for m in self.messages if self.show_sent or m.get("direction") == "in"]
        return sorted(shown, key=lambda m: (m.get("created_at") or 0, m.get("id")), reverse=True)

    def current(self):
        if self.mode == "detail":
            return next((m for m in self.messages if m["id"] == self.detail_id), None)
        shown = self.visible()
        return shown[self.selected] if 0 <= self.selected < len(shown) else None

    def replace(self, message):
        if not message:
            return
        self.messages = [message if m["id"] == message["id"] else m for m in self.messages]

    def set_notice(self, text, error=False):
        self.notice = core.printable(text, 300)
        self.notice_error = error

    def call(self, busy_text, fn, *args, **kwargs):
        self.set_notice(busy_text)
        self.draw()
        try:
            return fn(self.config, *args, **kwargs)
        except core.AgentError as err:
            self.set_notice(str(err), error=True)
            return None

    def load(self):
        result = self.call("Loading messages from Nostr relays…", core.inbox, include_sent=True)
        if result is None:
            return
        self.messages = result.get("messages", [])
        self.relays = result.get("relay_status", [])
        self.loaded = True
        self.selected = min(self.selected, max(len(self.visible()) - 1, 0))
        failed = [r for r in self.relays if not r.get("ok")]
        notes = []
        if failed:
            notes.append(f"{len(failed)} relay(s) unreachable, list may be incomplete")
        if result.get("errors"):
            notes.append(f"{len(result['errors'])} invalid event(s) ignored")
        if result.get("truncated"):
            notes.append("older messages beyond the limit are not shown")
        self.set_notice("; ".join(notes) if notes else "Up to date", error=bool(failed))

    # Actions

    def open_current(self):
        message = self.current()
        if not message:
            return
        self.mode = "detail"
        self.detail_id = message["id"]
        self.detail_top = 0
        if message.get("direction") != "in" or (message.get("public_state") != "unread" and message.get("private_state")):
            self.set_notice("")
            return
        result = self.call("Marking as read…", core.open_message, message["id"])
        if result:
            self.replace(result.get("message"))
            warnings = result.get("warnings") or []
            self.set_notice(warnings[0] if warnings else "Marked as read", error=bool(warnings))

    def change_state(self, scope, state, ticket=None, clear_ticket=False):
        message = self.current()
        if not message:
            return
        result = self.call(f"Setting {scope} state…", core.set_state, scope, message["id"], state,
                           ticket=ticket, clear_ticket=clear_ticket)
        if not result:
            return
        self.replace(result.get("message"))
        warnings = result.get("warnings") or []
        if warnings:
            self.set_notice(warnings[0], error=True)
        elif result.get("changed"):
            self.set_notice(f"{scope.capitalize()} state: {state_label(result.get('state'))}")
        else:
            self.set_notice("No change")

    def choose_state(self, scope):
        message = self.current()
        if not message:
            return
        if scope == "public" and message.get("direction") != "in":
            self.set_notice("Public state of a sent message is set by its recipient", error=True)
            return
        keys = PUBLIC_KEYS if scope == "public" else PRIVATE_KEYS
        choices = "  ".join(f"[{key}] {state_label(state).lower()}" for key, state in keys.items())
        key = self.ask_key(f"{scope.capitalize()} state: {choices}  esc cancels")
        if key in keys:
            self.change_state(scope, keys[key])
        else:
            self.set_notice("")

    def set_ticket(self):
        if not self.current():
            return
        ticket = self.read_line("Ticket (sets private in progress): ")
        if ticket:
            self.change_state("private", "in_progress", ticket=ticket.strip())

    def clear_ticket(self):
        message = self.current()
        if not message:
            return
        if not message.get("ticket_id"):
            self.set_notice("No ticket to clear")
            return
        self.change_state("private", message.get("private_state") or "read", clear_ticket=True)

    def reply(self):
        message = self.current()
        if not message:
            return
        text = self.compose()
        if text is None or not text.strip():
            self.set_notice("Reply cancelled")
            return
        target = correspondent(message)
        if self.ask_key(f"Send reply to {target}? [y/n]") not in ("y", "Y"):
            self.set_notice("Reply cancelled")
            return
        result = self.call("Sending reply…", core.reply, message["id"], text)
        if result:
            accepted = [r for r in result.get("recipient_relays", []) if r.get("ok")]
            self.set_notice(f"Reply sent; accepted by {len(accepted)} of {len(result.get('recipient_relays', []))} relays")

    def paste_ref(self):
        """Pastes the message reference into the prompt under the popup.
        Returns True when the popup should close."""
        message = self.current()
        if not message:
            return False
        ref = message.get("ref")
        if not ref:
            self.set_notice("This nostr-agent gives no message references; update it", error=True)
            return False
        pane = core.prompt_pane()
        if not pane:
            self.set_notice(f"No prompt pane known; reference: {ref}", error=True)
            return False
        try:
            core.paste_to_pane(pane, ref + " ")
        except (core.AgentError, OSError, subprocess.SubprocessError) as err:
            self.set_notice(f"Paste failed ({err}); reference: {ref}", error=True)
            return False
        return True

    def compose(self):
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            return self.read_line("Reply: ")
        curses.def_prog_mode()
        curses.endwin()
        try:
            # A private directory; the draft is deleted as soon as it is read.
            with tempfile.TemporaryDirectory(prefix="nostr-reply-") as directory:
                path = os.path.join(directory, "reply.txt")
                with open(path, "w", encoding="utf-8"):
                    pass
                subprocess.run(shlex.split(editor) + [path], check=False)
                with open(path, encoding="utf-8") as draft:
                    return draft.read().rstrip("\n")
        except (OSError, ValueError) as err:
            self.set_notice(f"Editor failed: {err}", error=True)
            return None
        finally:
            curses.reset_prog_mode()
            self.screen.clear()

    # Input

    def ask_key(self, prompt):
        self.set_notice(prompt)
        self.draw()
        key = self.screen.get_wch()
        return key if isinstance(key, str) and key != "\x1b" else None

    def read_line(self, prompt):
        text = ""
        cursor(True)
        try:
            while True:
                height, width = self.screen.getmaxyx()
                self.set_notice("")
                self.draw()
                line = prompt + text
                visible = line[-(width - 2):] if display_width(line) > width - 2 else line
                self.put(height - 2, 0, pad(visible, width - 1), curses.A_BOLD)
                self.screen.move(height - 2, min(display_width(visible), width - 2))
                key = self.screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return text
                if key == "\x1b":
                    return None
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    text = text[:-1]
                elif key == "\x15":
                    text = ""
                elif isinstance(key, str) and key.isprintable():
                    text += key
        finally:
            cursor(False)

    # Drawing

    def put(self, y, x, text, attr=0):
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            # Writing the bottom-right cell raises after drawing it.
            pass

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 8 or width < 30:
            self.put(0, 0, clip("Window too small", width - 1))
            self.screen.refresh()
            return
        self.put(0, 0, pad(" " + summary(self.messages, self.relays, self.show_sent), width - 1), curses.A_REVERSE)
        body_top, body_height = 1, height - 3
        if self.mode == "detail" and self.current():
            self.draw_detail(body_top, body_height, width)
        else:
            self.mode = "list"
            self.draw_list(body_top, body_height, width)
        notice_attr = curses.A_BOLD | (curses.color_pair(1) if self.notice_error and curses.has_colors() else 0)
        self.put(height - 2, 0, pad(" " + self.notice, width - 1), notice_attr)
        self.put(height - 1, 0, pad(" " + (DETAIL_HELP if self.mode == "detail" else LIST_HELP), width - 1), curses.A_DIM)
        self.screen.refresh()

    def draw_list(self, top, height, width):
        shown = self.visible()
        if not shown:
            text = "No messages" if self.loaded else "Nothing loaded yet; press g to retry"
            self.put(top + 1, 2, clip(text, width - 3), curses.A_DIM)
            return
        self.selected = max(0, min(self.selected, len(shown) - 1))
        per_page = max((height + 1) // ENTRY_HEIGHT, 1)
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + per_page:
            self.top = self.selected - per_page + 1
        for slot, message in enumerate(shown[self.top:self.top + per_page]):
            y = top + slot * ENTRY_HEIGHT
            selected = self.top + slot == self.selected
            for offset, line in enumerate(entry_lines(message, width - 2)):
                attr = curses.A_REVERSE if selected else 0
                if offset == 0 and is_unread(message):
                    attr |= curses.A_BOLD
                if offset == 2 and not selected:
                    attr |= curses.A_DIM
                if y + offset < top + height:
                    self.put(y + offset, 1, pad(line, width - 2), attr)

    def draw_detail(self, top, height, width):
        lines = detail_lines(self.current(), width - 2)
        self.detail_top = max(0, min(self.detail_top, max(len(lines) - height, 0)))
        for row, line in enumerate(lines[self.detail_top:self.detail_top + height]):
            self.put(top + row, 1, line)

    # Loop

    def handle(self, key):
        if key == curses.KEY_RESIZE:
            return True
        shown = self.visible()
        detail = self.mode == "detail"
        if key in ("q", "\x1b"):
            if detail:
                self.mode = "list"
                self.set_notice("")
                return True
            return False
        if key in ("j", curses.KEY_DOWN):
            if detail:
                self.detail_top += 1
            else:
                self.selected = min(self.selected + 1, max(len(shown) - 1, 0))
        elif key in ("k", curses.KEY_UP):
            if detail:
                self.detail_top = max(self.detail_top - 1, 0)
            else:
                self.selected = max(self.selected - 1, 0)
        elif key in (curses.KEY_NPAGE, " "):
            if detail:
                self.detail_top += self.screen.getmaxyx()[0] - 4
            else:
                self.selected = min(self.selected + 3, max(len(shown) - 1, 0))
        elif key == curses.KEY_PPAGE:
            if detail:
                self.detail_top = max(self.detail_top - (self.screen.getmaxyx()[0] - 4), 0)
            else:
                self.selected = max(self.selected - 3, 0)
        elif key in ("\n", "\r", "o", curses.KEY_ENTER) and not detail:
            self.open_current()
        elif key == "i":
            return not self.paste_ref()
        elif key == "r":
            self.reply()
        elif key == "p":
            self.choose_state("public")
        elif key == "s":
            self.choose_state("private")
        elif key == "t":
            self.set_ticket()
        elif key == "T":
            self.clear_ticket()
        elif key == "a" and not detail:
            self.show_sent = not self.show_sent
            self.selected = 0
        elif key in ("g", curses.KEY_F5):
            self.load()
        return True

    def run(self):
        cursor(False)
        if curses.has_colors():
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, -1)
        self.draw()
        self.load()
        while True:
            self.draw()
            if not self.handle(self.screen.get_wch()):
                return 0


def main():
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    config_error = None
    try:
        config = core.load_config()
    except core.AgentError as err:
        config, config_error = dict(core.DEFAULTS), str(err)
    return curses.wrapper(lambda screen: App(screen, config, config_error).run())


if __name__ == "__main__":
    sys.exit(main())
