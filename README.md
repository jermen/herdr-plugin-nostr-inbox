# Herdr Nostr Inbox

Nostr messages in Herdr — an unread count in the tab bar, an inbox popup on
`prefix+m`, and the two message-state dimensions of
[`nostr-agent`](https://github.com/jermen/nostr-agent-comms). Python 3.9+
(installer 3.11+), Linux/macOS, Herdr 0.9.1+. No pip/npm dependencies.

```text
 Messages · 2 unread · relays 3/3
 ● alice                                              2026-09-27 21:03
   Restart db02
   PUBLIC: UNREAD       PRIVATE: -
 ○ codex                                              2026-09-27 20:41
   Review completed
   PUBLIC: IN PROGRESS  PRIVATE: IN PROGRESS   DMDOX-330
```

## How it works

The plugin is only a presentation layer. It runs `nostr-agent` and shows its
JSON; it contains no Nostr protocol, crypto, relay discovery or identity code,
and stores no messages, cursors or state. Nostr relays are the store: every
refresh rebuilds the inbox from them, so another workstation with the same
Nostr identity shows the same inbox and state.

- **Unread indicator** — a `command` entry in Herdr's tab-bar status area runs
  `nostr_inbox.py status` every 120 seconds: `✉ 3` for three incoming messages
  whose public state is `unread`, nothing when there are none, `✉ 3*` when some
  inbox relay did not answer (the count may be low), `✉ ?` when no relay
  answered or `nostr-agent` is missing.
- **Inbox popup** — the `open` action (bound to `prefix+m`) opens a Herdr popup
  running `nostr_inbox.py tui`.
- **Toasts** — a startup hook starts one watcher per Herdr server. It polls
  every 5 minutes and shows a Herdr notification for unread messages that
  arrive while it runs. Its set of announced messages lives in memory only;
  messages older than the watcher are never announced, so a restart or a relay
  that comes back does not replay old messages.

Herdr 0.9.1 has no plugin-owned sidebar sections; when it gets them, a native
**Messages** section below **Agents** can replace the popup without changing
`nostr-agent` or its state protocol.

## Message state

Each message has two independent states, both stored as encrypted Nostr events
by `nostr-agent`:

- **Public** — shared with the correspondent: `unread`, `read`, `in progress`,
  `done`. Forward-only. Visible to the sender, like a read receipt.
- **Private** — visible only to you: `-` (never opened), `read`, `todo`,
  `in progress`, `done`, optionally with a ticket such as `DMDOX-330` that
  stays attached through later private changes until cleared.

Opening an unread message marks it `read` in both dimensions (never
downgrading a more advanced state). Nothing else changes state implicitly.

## Keys

| Key | Action |
| --- | --- |
| `↑`/`↓`, `j`/`k` | select a message; scroll in the message view |
| `enter`, `o` | open the message (marks it read) |
| `r` | reply (`$VISUAL`/`$EDITOR` if set, otherwise one line), confirmed with `y` |
| `p` then `r`/`i`/`d` | public state: read, in progress, done |
| `s` then `r`/`t`/`i`/`d` | private state: read, todo, in progress, done |
| `t` | set a ticket: private in progress with that ticket |
| `T` | clear the ticket, keeping the private state |
| `a` | show or hide your sent messages (their public state comes from the recipient) |
| `g`, `F5` | reload from the relays |
| `esc`, `q` | back to the list; quit |

Each action runs `nostr-agent`, which re-reads the relays first, so it can take
a few seconds; the footer shows progress, relay warnings and errors. Message
text is untrusted: terminal control characters are stripped before display, and
nothing in a message triggers an action.

## Install

Requires the `nostr-agent` CLI with a configured identity
(see [nostr-agent-comms](https://github.com/jermen/nostr-agent-comms)); check
with `nostr-agent inbox count --json`. On the machine running Herdr:

```sh
herdr plugin install jermen/herdr-plugin-nostr-inbox --yes
herdr plugin action invoke configure --plugin jermen.nostr-inbox
```

`configure` runs `install.py --no-link` inside the installed checkout; the
result is logged by `herdr plugin log list --plugin jermen.nostr-inbox`.
Reinstalling with `herdr plugin install` updates the checkout in place, and the
status entry keeps pointing to it. For development, or to pass options, run the
installer from a clone instead (`herdr plugin uninstall jermen.nostr-inbox`
first); it then links that clone:

```sh
./install.sh --dry-run   # show the config diff, change nothing
./install.sh
```

The installer backs up `config.toml` under
`$XDG_STATE_HOME/herdr-nostr-inbox/backups/`, inserts two managed blocks —
the `tab_bar_right` status entry under `[ui]` and a `[[keys.command]]` binding
for `prefix+m` — links the plugin (unless it runs from the installed checkout),
starts the watcher and reloads the server
config. Then use Herdr's global menu → **reload config** in the UI. It refuses
to overwrite an existing `ui.tab_bar_right` or a key that is already bound and
prints the entry to add by hand instead. Options: `--key`, `--interval`,
`--timeout`, `--config PATH`, `--no-link` (config only: for the installed
checkout or a separate UI computer) and `--uninstall` (removes the blocks and
unlinks; backups remain). Running it again is idempotent; after moving a linked
clone, run it again to relink and rewrite the status command path.
The plugin was called `jermen.nostr-index` before; installing replaces that
id's config blocks, stops its watcher and unlinks it.

Config: `~/.config/herdr/plugins/config/jermen.nostr-inbox/config.json`
(optional; see `config.example.json`).

- `nostr_agent`: path to the CLI; default `nostr-agent` on `PATH`, then
  `~/.local/bin/nostr-agent`.
- `limit`: gift wraps per relay passed as `--limit`; default: the CLI's own.
- `command_timeout`: seconds before a `nostr-agent` call is abandoned (90).
- `icon`, `show_zero`, `mark_partial`: indicator text; `show_zero` keeps `✉ 0`
  visible, `mark_partial: false` drops the `*`.
- `toast`, `toast_sound` (`none`, `done`, `request`), `poll_seconds`: watcher.
- `show_sent`: start the popup with sent messages shown.

## Verify

```sh
python3 -B -m unittest discover -s tests -v
python3 nostr_inbox.py status              # the indicator text
python3 nostr_inbox.py tui                 # the popup UI in this terminal
herdr plugin action invoke open --plugin jermen.nostr-inbox
herdr plugin log list --plugin jermen.nostr-inbox
```

The tests use a fake `nostr-agent`: CLI arguments and error handling, the
indicator, escaping of hostile message text, wide-character layout, the
watcher's announcement rule, idempotent and refusing config edits, and a
pseudo-terminal run of the popup that opens a message, changes both states,
sets and clears a ticket and replies. Watcher output goes to `watch.log` in the
plugin state directory (`~/.local/state/herdr/plugins/jermen.nostr-inbox/`).
