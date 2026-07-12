#!/usr/bin/env python3
"""
Shmoozy — a dead-simple native chat app for Claude Code on Arch/Linux.

A thin GTK4 + libadwaita front-end over the `claude` CLI. Messages stream back
token-by-token, conversations keep their context (via --resume), and replies
render as markdown. No API key to manage: it drives the `claude` binary you
already have, so it uses your existing Claude Code auth.

The Linux-native counterpart to Chatty (the macOS SwiftUI app).
"""

import json
import os
import re
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Gdk, Pango  # noqa: E402

APP_ID = "com.lubabs770.Shmoozy"
CONFIG_PATH = os.path.join(
    GLib.get_user_config_dir(), "shmoozy", "config.json"
)

# Theme cycle: light → dark → oled (pure black). The header button shows the
# icon of the *current* mode and advances to the next on click.
THEME_MODES = ["light", "dark", "oled"]
THEME_ICONS = {
    "light": "weather-clear-symbolic",
    "dark": "weather-clear-night-symbolic",
    "oled": "display-brightness-symbolic",
}

# Applied on top of libadwaita's dark scheme for OLED: recolor the backgrounds
# to pure black so unlit pixels stay off.
OLED_CSS = b"""
@define-color window_bg_color #000000;
@define-color view_bg_color #000000;
@define-color headerbar_bg_color #000000;
@define-color card_bg_color #101010;
@define-color popover_bg_color #0a0a0a;
@define-color dialog_bg_color #000000;
"""

# ── claude CLI wiring ──────────────────────────────────────────────────────
# Resolve the claude binary once; extend if you move installs around.
CLAUDE_CANDIDATES = [
    os.path.expanduser("~/.local/bin/claude"),
    "/usr/bin/claude",
    "/usr/local/bin/claude",
]


def resolve_claude():
    for c in CLAUDE_CANDIDATES:
        if os.access(c, os.X_OK):
            return c
    return "claude"


CLAUDE_PATH = resolve_claude()

# Directory claude runs in (what it can see/touch). Defaults to home.
WORKING_DIRECTORY = os.path.expanduser("~")

# How tool-use permissions are handled. There's no interactive approval in this
# UI, so anything stricter than "bypassPermissions" means tool calls (web
# search, file reads, bash) get auto-denied mid-turn.
#   "bypassPermissions" – all tools run, no prompts (can edit files / run bash
#                         in WORKING_DIRECTORY). Point it at a scratch folder.
#   "default"           – conversation only; tool calls are declined
#   "acceptEdits"       – auto-approves edits, still declines bash/search
PERMISSION_MODE = "bypassPermissions"

# Marker injected into claude's system prompt every turn so Claude can tell it's
# being driven by Shmoozy and switch to "just chat" mode. Reference it from
# CLAUDE.md, e.g.: "If the system prompt contains `client: shmoozy`, you're in
# the Shmoozy app — just chat, reply in markdown."
CLIENT_MARKER = "client: shmoozy"


# ── markdown → Pango markup ────────────────────────────────────────────────
# GtkLabel speaks Pango markup, not HTML/markdown. This is a small, forgiving
# converter covering the common cases. During streaming, partial text can be
# unbalanced (an open ``` or **), so the caller falls back to plain text when
# set_markup rejects the result.

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![\*_])[\*_]([^\*_\n]+)[\*_](?![\*_])")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _inline(text):
    """Apply inline markdown to an already Pango-escaped string."""
    text = _INLINE_CODE.sub(
        lambda m: f'<tt>{m.group(1)}</tt>', text
    )
    text = _LINK.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text
    )
    text = _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    return text


def md_to_pango(md):
    out = []
    in_code = False
    code_buf = []

    for raw in md.split("\n"):
        fence = raw.lstrip()
        if fence.startswith("```"):
            if in_code:
                body = GLib.markup_escape_text("\n".join(code_buf))
                out.append(
                    f'<tt><span background="#00000018">{body}</span></tt>'
                )
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(raw)
            continue

        esc = GLib.markup_escape_text(raw)

        # Headings
        h = re.match(r"^(#{1,6})\s+(.*)$", raw)
        if h:
            content = _inline(GLib.markup_escape_text(h.group(2)))
            size = ("xx-large", "x-large", "large", "large", "medium",
                    "medium")[len(h.group(1)) - 1]
            out.append(f'<span size="{size}" weight="bold">{content}</span>')
            continue

        # Blockquote
        bq = re.match(r"^\s*>\s?(.*)$", raw)
        if bq:
            content = _inline(GLib.markup_escape_text(bq.group(1)))
            out.append(
                f'<span foreground="#888888">▏ </span>'
                f'<i>{content}</i>'
            )
            continue

        # Bullet list
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
        if bullet:
            indent = " " * len(bullet.group(1))
            content = _inline(GLib.markup_escape_text(bullet.group(2)))
            out.append(f"{indent}• {content}")
            continue

        # Numbered list
        num = re.match(r"^(\s*)(\d+)\.\s+(.*)$", raw)
        if num:
            indent = " " * len(num.group(1))
            content = _inline(GLib.markup_escape_text(num.group(3)))
            out.append(f"{indent}{num.group(2)}. {content}")
            continue

        out.append(_inline(esc))

    # Unterminated code fence while streaming: show what we have so far.
    if in_code and code_buf:
        body = GLib.markup_escape_text("\n".join(code_buf))
        out.append(f'<tt><span background="#00000018">{body}</span></tt>')

    return "\n".join(out)


# ── config ─────────────────────────────────────────────────────────────────
def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
    except OSError:
        pass


# ── message bubble ─────────────────────────────────────────────────────────
class Bubble(Gtk.Box):
    def __init__(self, is_user):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.is_user = is_user
        self.raw = ""
        self._revert_id = 0

        self.label = Gtk.Label(xalign=0, yalign=0)
        self.label.set_wrap(True)
        self.label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.label.set_selectable(True)
        self.label.set_max_width_chars(64)
        self.label.add_css_class("bubble")
        self.label.add_css_class("user" if is_user else "assistant")

        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_top(4)
        self.set_margin_bottom(4)
        self.set_halign(Gtk.Align.END if is_user else Gtk.Align.START)

        self.copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
        self.copy_btn.set_tooltip_text("Copy to clipboard")
        self.copy_btn.add_css_class("flat")
        self.copy_btn.add_css_class("copy-btn")
        self.copy_btn.set_halign(Gtk.Align.END if is_user else Gtk.Align.START)
        self.copy_btn.connect("clicked", self.on_copy)

        self.append(self.label)
        self.append(self.copy_btn)

    def set_text(self, text):
        self.raw = text
        if self.is_user:
            self.label.set_text(text)
            return
        markup = md_to_pango(text)
        try:
            self.label.set_markup(markup)
        except GLib.GError:
            self.label.set_text(text)

    def on_copy(self, _btn):
        self.get_clipboard().set(self.raw)
        self.copy_btn.set_icon_name("object-select-symbolic")
        if self._revert_id:
            GLib.source_remove(self._revert_id)
        self._revert_id = GLib.timeout_add(1200, self._revert_icon)

    def _revert_icon(self):
        self.copy_btn.set_icon_name("edit-copy-symbolic")
        self._revert_id = 0
        return False


# ── main window ────────────────────────────────────────────────────────────
class ShmoozyWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Shmoozy")
        self.set_default_size(560, 760)

        self.session_id = None
        self.thinking = False
        self.current_bubble = None
        self.stderr_buf = ""

        self.cfg = load_config()
        self.theme_mode = self.cfg.get("theme", "dark")
        if self.theme_mode not in THEME_MODES:
            self.theme_mode = "dark"
        self._oled_on = False
        self.oled_provider = Gtk.CssProvider()
        self.oled_provider.load_from_data(OLED_CSS)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Shmoozy", subtitle=WORKING_DIRECTORY)
        header.set_title_widget(title)

        self.theme_btn = Gtk.Button(icon_name=THEME_ICONS[self.theme_mode])
        self.theme_btn.set_tooltip_text("Theme: light / dark / OLED")
        self.theme_btn.connect("clicked", self.on_toggle_theme)
        header.pack_end(self.theme_btn)

        new_btn = Gtk.Button(icon_name="edit-clear-all-symbolic")
        new_btn.set_tooltip_text("New conversation")
        new_btn.connect("clicked", self.on_new_chat)
        header.pack_start(new_btn)

        toolbar.add_top_bar(header)

        # scrolling message list
        self.msg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.msg_box.set_margin_top(8)
        self.msg_box.set_margin_bottom(8)
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.msg_box)

        # input row
        self.input = Gtk.TextView()
        self.input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.input.add_css_class("input-area")
        self.input.set_top_margin(8)
        self.input.set_bottom_margin(8)
        self.input.set_left_margin(10)
        self.input.set_right_margin(10)
        keyctl = Gtk.EventControllerKey()
        keyctl.connect("key-pressed", self.on_key)
        self.input.add_controller(keyctl)

        input_scroll = Gtk.ScrolledWindow()
        input_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        input_scroll.set_min_content_height(44)
        input_scroll.set_max_content_height(160)
        input_scroll.set_child(self.input)
        input_scroll.set_hexpand(True)
        input_scroll.add_css_class("input-frame")

        self.send_btn = Gtk.Button(icon_name="go-up-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.add_css_class("circular")
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.connect("clicked", lambda *_: self.send())

        input_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        input_row.set_margin_start(12)
        input_row.set_margin_end(12)
        input_row.set_margin_top(6)
        input_row.set_margin_bottom(12)
        input_row.append(input_scroll)
        input_row.append(self.send_btn)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.scroller)
        content.append(input_row)
        toolbar.set_content(content)

        self.set_content(toolbar)
        self.load_css()
        self.apply_theme(self.theme_mode)
        self.input.grab_focus()

    # ── theme ──
    def apply_theme(self, mode):
        sm = Adw.StyleManager.get_default()
        sm.set_color_scheme(
            Adw.ColorScheme.FORCE_LIGHT if mode == "light"
            else Adw.ColorScheme.FORCE_DARK
        )
        self.set_oled(mode == "oled")
        self.theme_btn.set_icon_name(THEME_ICONS[mode])

    def set_oled(self, on):
        display = Gdk.Display.get_default()
        if on and not self._oled_on:
            Gtk.StyleContext.add_provider_for_display(
                display, self.oled_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
            )
            self._oled_on = True
        elif not on and self._oled_on:
            Gtk.StyleContext.remove_provider_for_display(
                display, self.oled_provider
            )
            self._oled_on = False

    def on_toggle_theme(self, *_):
        i = THEME_MODES.index(self.theme_mode)
        self.theme_mode = THEME_MODES[(i + 1) % len(THEME_MODES)]
        self.apply_theme(self.theme_mode)
        self.cfg["theme"] = self.theme_mode
        save_config(self.cfg)

    def load_css(self):
        css = b"""
        .bubble { padding: 9px 13px; border-radius: 14px; }
        .assistant { background: @card_bg_color; }
        .user {
            background: @accent_bg_color;
            color: @accent_fg_color;
        }
        .input-frame {
            border-radius: 20px;
            background: @card_bg_color;
        }
        .input-area, .input-area text { background: transparent; }
        .copy-btn {
            min-height: 22px;
            min-width: 22px;
            padding: 0 4px;
            opacity: 0.5;
        }
        .copy-btn:hover { opacity: 1; }
        .copy-btn image {
            -gtk-icon-size: 14px;
            color: #9a9a9a;
        }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    # ── input handling ──
    def on_key(self, ctrl, keyval, keycode, state):
        shift = state & Gdk.ModifierType.SHIFT_MASK
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not shift:
            self.send()
            return True
        return False

    def on_new_chat(self, *_):
        if self.thinking:
            return
        self.session_id = None
        child = self.msg_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.msg_box.remove(child)
            child = nxt
        self.input.grab_focus()

    def add_bubble(self, is_user, text=""):
        b = Bubble(is_user)
        b.set_text(text)
        self.msg_box.append(b)
        self.scroll_to_bottom()
        return b

    def scroll_to_bottom(self):
        def _do():
            adj = self.scroller.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        GLib.idle_add(_do)

    def get_input_text(self):
        buf = self.input.get_buffer()
        start, end = buf.get_bounds()
        return buf.get_text(start, end, True)

    # ── the actual send / stream ──
    def send(self):
        if self.thinking:
            return
        prompt = self.get_input_text().strip()
        if not prompt:
            return
        self.input.get_buffer().set_text("")
        self.add_bubble(True, prompt)

        self.thinking = True
        self.send_btn.set_sensitive(False)
        self.current_bubble = self.add_bubble(False, "")
        self.stream(prompt)

    def stream(self, prompt):
        args = [
            CLAUDE_PATH,
            "-p", prompt,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--permission-mode", PERMISSION_MODE,
            "--append-system-prompt", CLIENT_MARKER,
        ]
        if self.session_id:
            args += ["--resume", self.session_id]

        launcher = Gio.SubprocessLauncher.new(
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
        )
        launcher.set_cwd(WORKING_DIRECTORY)
        launcher.setenv(
            "PATH",
            os.path.expanduser("~/.local/bin")
            + ":/usr/local/bin:/usr/bin:/bin",
            True,
        )
        self.stderr_buf = ""
        try:
            proc = launcher.spawnv(args)
        except GLib.GError as e:
            self.current_bubble.set_text(
                f"Failed to launch claude at {CLAUDE_PATH}: {e.message}"
            )
            self.finish_turn()
            return

        self.proc = proc
        out = Gio.DataInputStream.new(proc.get_stdout_pipe())
        err = Gio.DataInputStream.new(proc.get_stderr_pipe())
        self.read_line(out)
        self.read_err(err)
        proc.wait_async(None, self.on_proc_done)

    def read_line(self, stream):
        stream.read_line_async(
            GLib.PRIORITY_DEFAULT, None, self.on_line, None
        )

    def on_line(self, stream, res, _data):
        try:
            line, _len = stream.read_line_finish_utf8(res)
        except GLib.GError:
            return
        if line is None:
            return  # EOF; on_proc_done wraps up
        self.handle_line(line)
        self.read_line(stream)

    def read_err(self, stream):
        stream.read_line_async(
            GLib.PRIORITY_DEFAULT, None, self.on_err_line, None
        )

    def on_err_line(self, stream, res, _data):
        try:
            line, _len = stream.read_line_finish_utf8(res)
        except GLib.GError:
            return
        if line is None:
            return
        self.stderr_buf += line + "\n"
        self.read_err(stream)

    def handle_line(self, line):
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            return

        sid = event.get("session_id")
        if sid:
            self.session_id = sid

        if event.get("type") == "stream_event":
            ev = event.get("event") or {}
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self.current_bubble.set_text(
                        self.current_bubble.raw + delta["text"]
                    )
                    self.scroll_to_bottom()

    def on_proc_done(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.GError:
            pass
        status = proc.get_exit_status() if proc.get_if_exited() else -1
        if not self.current_bubble.raw:
            if status != 0:
                msg = self.stderr_buf.strip() or f"claude exited {status}"
                self.current_bubble.set_text(f"⚠ {msg}")
            else:
                self.current_bubble.set_text("(no response)")
        self.finish_turn()

    def finish_turn(self):
        self.thinking = False
        self.send_btn.set_sensitive(True)
        self.input.grab_focus()


class ShmoozyApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = ShmoozyWindow(self)
        win.present()


def main():
    app = ShmoozyApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
