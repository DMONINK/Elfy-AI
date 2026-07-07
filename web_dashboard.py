"""
Web dashboard for Elfy.

Runs as an aiohttp app in the *same* asyncio event loop as the Discord
bot (see main.py) — not a separate thread/process — so it can read live
bot state (bot.guilds, active AI sessions) directly and safely `await`
bot calls (e.g. updating presence) with no cross-thread synchronization.

Routes:
  GET  /                        overview stats
  GET  /servers                 every server Elfy's in: name, icon, members, chat channel
  GET  /users                   who's talking to her — DMs and servers, shown separately
  GET  /conversation/{id}       full logged transcript for one channel
  GET  /vips, /vips/new,
       /vips/edit/{user_id}     VIP roster + add/edit forms
  POST /vips/save               create or update a VIP
  POST /vips/delete             remove a VIP
  POST /vips/reset-greeting     re-arm a VIP's one-time greeting
  GET  /settings                every dashboard-editable bot setting
  POST /settings                save settings (applies immediately, no restart)
  POST /settings/reset          revert every setting to its built-in default
  GET/POST /login, POST /logout password gate

Auth: a single shared password (the DASHBOARD_PASSWORD env var/Secret).
This dashboard can show private DMs and change how the bot behaves, so
if no password is configured it fails CLOSED — every route shows setup
instructions instead of serving anything, rather than defaulting to open.
"""
import html
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import discord
from aiohttp import web

import conversation_log
import dashboard_settings
import vip_users
from storage import ChatDataManager

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
_SESSION_COOKIE = "elfy_dashboard_session"
_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# In-memory session store (token -> expiry). Resets on restart, same as
# everything else process-local here — fine for a single-operator tool.
_sessions: dict = {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + _SESSION_TTL_SECONDS
    return token


def _session_valid(token: Optional[str]) -> bool:
    if not token or token not in _sessions:
        return False
    if _sessions[token] < time.time():
        del _sessions[token]
        return False
    return True


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    if not DASHBOARD_PASSWORD:
        return _page(
            "Setup required",
            '<div class="card">'
            "<h1>One step left</h1>"
            "<p>The dashboard needs a password before it'll serve anything — "
            "it can show private DMs and change how Elfy behaves, so it "
            "never runs open to the internet.</p>"
            "<p>Add a <code>DASHBOARD_PASSWORD</code> value in your Replit "
            "Secrets (or <code>.env.development</code> for local dev), then "
            "restart the bot.</p>"
            "</div>",
            authed=False,
        )

    if request.path == "/login":
        return await handler(request)

    if not _session_valid(request.cookies.get(_SESSION_COOKIE)):
        raise web.HTTPFound("/login")

    return await handler(request)


# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #0F0E13;
  --surface: #17151D;
  --surface-2: #1E1B27;
  --border: #2A2733;
  --text: #F2F0F7;
  --text-muted: #9C97AC;
  --accent: #9B8CFF;
  --accent-soft: rgba(155, 140, 255, 0.14);
  --accent-2: #FF8FB1;
  --success: #6FCF97;
  --danger: #FF6B6B;
  --radius: 12px;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  background-image: radial-gradient(circle at 15% 0%, rgba(155,140,255,0.08), transparent 45%);
  color: var(--text);
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  font-size: 15px;
  line-height: 1.55;
  min-height: 100vh;
}
h1, h2, h3, .brand, .brand-lg { font-family: 'Sora', system-ui, sans-serif; font-weight: 600; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 0 0 14px; color: var(--text); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
code { background: var(--surface-2); padding: 2px 6px; border-radius: 5px; font-size: 0.9em; }
.text-muted { color: var(--text-muted); }
.small { font-size: 0.82rem; }

.sparkle { display: inline-block; color: var(--accent); animation: twinkle 2.4s ease-in-out infinite; }
@keyframes twinkle { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(0.82); } }
@media (prefers-reduced-motion: reduce) { .sparkle { animation: none; } }

.topnav {
  display: flex; align-items: center; flex-wrap: wrap; gap: 10px 20px;
  padding: 14px 24px; border-bottom: 1px solid var(--border);
  background: rgba(23, 21, 29, 0.7); backdrop-filter: blur(8px);
  position: sticky; top: 0; z-index: 10;
}
.brand { font-size: 1.05rem; margin-right: auto; white-space: nowrap; }
.nav-links { display: flex; flex-wrap: wrap; gap: 4px; }
.nav-link {
  color: var(--text-muted); padding: 7px 12px; border-radius: 8px; font-size: 0.92rem;
}
.nav-link:hover { color: var(--text); text-decoration: none; background: var(--surface-2); }
.nav-link.active { color: var(--text); background: var(--accent-soft); }
.logout-form { margin: 0; }

.container { max-width: 980px; margin: 0 auto; padding: 28px 20px 80px; }
.login-wrap { min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
.login-card {
  width: 100%; max-width: 360px; background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 32px 28px;
}
.brand-lg { font-size: 1.3rem; margin-bottom: 6px; }

.card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 22px 24px; margin-bottom: 20px;
}
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 6px; }

.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin: 20px 0 28px; }
.stat-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px 20px;
}
.stat-value { font-family: 'Sora', system-ui, sans-serif; font-size: 1.8rem; font-weight: 700; color: var(--text); }
.stat-label { color: var(--text-muted); font-size: 0.86rem; margin-top: 2px; }

.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); }
table.data-table { width: 100%; border-collapse: collapse; min-width: 480px; }
table.data-table th {
  text-align: left; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--text-muted); padding: 12px 16px; background: var(--surface-2); white-space: nowrap;
}
table.data-table td { padding: 12px 16px; border-top: 1px solid var(--border); vertical-align: middle; }
table.data-table tbody tr:hover { background: var(--surface-2); }
.cell-identity { display: flex; align-items: center; gap: 10px; }
.cell-actions { display: flex; gap: 8px; align-items: center; white-space: nowrap; }

.avatar { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: var(--surface-2); }
.avatar-sm { width: 26px; height: 26px; }
.avatar-fallback {
  display: flex; align-items: center; justify-content: center; background: var(--accent-soft);
  color: var(--accent); font-weight: 700; font-size: 0.8em;
}

.badge {
  display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 0.74rem;
  font-weight: 600; letter-spacing: 0.02em; background: var(--surface-2); color: var(--text-muted);
}
.badge-dm { background: rgba(255, 143, 177, 0.15); color: var(--accent-2); }

.btn {
  display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 9px;
  padding: 9px 16px; font-size: 0.92rem; font-weight: 600; cursor: pointer; background: var(--surface-2);
  color: var(--text); font-family: inherit;
}
.btn:hover { border-color: var(--accent); text-decoration: none; }
.btn-primary { background: var(--accent); border-color: var(--accent); color: #16131F; }
.btn-primary:hover { filter: brightness(1.08); }
.btn-danger { background: transparent; border-color: var(--danger); color: var(--danger); }
.btn-danger:hover { background: rgba(255, 107, 107, 0.1); }
.btn-ghost { background: transparent; }
.btn-sm { padding: 6px 12px; font-size: 0.82rem; }
.btn-block { width: 100%; justify-content: center; }
.inline-form { display: inline; margin: 0; }

.field { margin-bottom: 16px; }
.field label { display: block; font-size: 0.86rem; font-weight: 600; margin-bottom: 6px; color: var(--text); }
.field input[type="text"], .field input[type="password"], .field input[type="number"],
.field textarea, .field select {
  width: 100%; background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; color: var(--text); font-family: inherit; font-size: 0.94rem;
}
.field textarea { resize: vertical; }
.field input:focus, .field textarea:focus, .field select:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.field .hint { color: var(--text-muted); font-size: 0.8rem; margin: 6px 0 0; }
.field-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px 16px; }
.form-card { max-width: 640px; }
.actions-row { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 4px; }

.flash { padding: 12px 16px; border-radius: 9px; margin-bottom: 18px; font-size: 0.92rem; }
.flash-success { background: rgba(111, 207, 151, 0.12); color: var(--success); border: 1px solid rgba(111, 207, 151, 0.3); }
.flash-error { background: rgba(255, 107, 107, 0.12); color: var(--danger); border: 1px solid rgba(255, 107, 107, 0.3); }

.empty-state {
  padding: 36px 20px; text-align: center; color: var(--text-muted); border: 1px dashed var(--border);
  border-radius: var(--radius); font-size: 0.92rem;
}

.transcript { display: flex; flex-direction: column; gap: 14px; }
.msg { display: flex; gap: 10px; max-width: 640px; }
.msg-bot { margin-left: auto; flex-direction: row-reverse; }
.msg-body {
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 10px 14px;
}
.msg-bot .msg-body { background: var(--accent-soft); border-color: transparent; }
.msg-meta { font-size: 0.78rem; margin-bottom: 3px; }
.msg-bot .msg-meta { text-align: right; }
.msg-text { white-space: pre-wrap; word-break: break-word; }

@media (max-width: 640px) {
  .container { padding: 20px 14px 60px; }
  .topnav { padding: 12px 16px; }
  .card { padding: 18px; }
  .msg { max-width: 92%; }
}
"""


def _page(title: str, body: str, active: str = "", authed: bool = True) -> web.Response:
    nav_html = ""
    if authed:
        nav_items = [
            ("/", "Overview", "overview"),
            ("/servers", "Servers", "servers"),
            ("/users", "Users", "users"),
            ("/vips", "VIPs", "vips"),
            ("/settings", "Settings", "settings"),
        ]
        links = "".join(
            f'<a href="{href}" class="nav-link{" active" if key == active else ""}">{label}</a>'
            for href, label, key in nav_items
        )
        nav_html = (
            '<nav class="topnav">'
            '<div class="brand"><span class="sparkle">✦</span> Elfy Control Room</div>'
            f'<div class="nav-links">{links}</div>'
            '<form method="post" action="/logout" class="logout-form">'
            '<button type="submit" class="btn btn-ghost btn-sm">Log out</button>'
            "</form>"
            "</nav>"
        )

    doc = (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)} · Elfy Control Room</title>"
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">'
        f"<style>{_CSS}</style>"
        f"</head><body>{nav_html}<main class=\"container\">{body}</main></body></html>"
    )
    return web.Response(text=doc, content_type="text/html")


def _stat_card(value: str, label: str) -> str:
    return (
        f'<div class="stat-card"><div class="stat-value">{html.escape(value)}</div>'
        f'<div class="stat-label">{html.escape(label)}</div></div>'
    )


def _empty_state(message: str) -> str:
    return f'<div class="empty-state">{html.escape(message)}</div>'


def _format_relative(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "—"
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 2592000:
        return f"{int(seconds // 86400)}d ago"
    return dt.strftime("%b %d, %Y")


def _format_time(iso_ts: Optional[str]) -> str:
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    return dt.strftime("%b %d, %Y, %I:%M %p UTC")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

def _login_body(error: bool = False) -> str:
    error_html = (
        '<div class="flash flash-error">Wrong password — try again.</div>' if error else ""
    )
    return (
        '<div class="login-wrap"><div class="login-card">'
        '<div class="brand-lg"><span class="sparkle">✦</span> Elfy Control Room</div>'
        '<p class="text-muted">Enter the dashboard password to continue.</p>'
        f"{error_html}"
        '<form method="post" action="/login">'
        '<div class="field"><label for="password">Password</label>'
        '<input type="password" id="password" name="password" autofocus required></div>'
        '<button type="submit" class="btn btn-primary btn-block">Log in</button>'
        "</form></div></div>"
    )


async def handle_login_get(request: web.Request) -> web.Response:
    return _page("Log in", _login_body(), authed=False)


async def handle_login_post(request: web.Request) -> web.Response:
    data = await request.post()
    password = str(data.get("password", ""))
    if DASHBOARD_PASSWORD and secrets.compare_digest(password, DASHBOARD_PASSWORD):
        resp = web.HTTPFound("/")
        resp.set_cookie(
            _SESSION_COOKIE, _new_session(),
            max_age=_SESSION_TTL_SECONDS, httponly=True, samesite="Lax",
        )
        return resp
    return _page("Log in", _login_body(error=True), authed=False)


async def handle_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(_SESSION_COOKIE)
    if token:
        _sessions.pop(token, None)
    resp = web.HTTPFound("/login")
    resp.del_cookie(_SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

async def handle_home(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    dm_convos = conversation_log.list_dm_conversations()
    guild_convos = conversation_log.list_guild_conversations()

    body = (
        "<h1>Overview</h1>"
        '<p class="text-muted">A snapshot of Elfy right now.</p>'
        '<div class="stat-grid">'
        + _stat_card(str(len(bot.guilds)), "Servers")
        + _stat_card(str(conversation_log.total_distinct_users()), "People who've talked to her")
        + _stat_card(str(len(dm_convos)), "DM conversations")
        + _stat_card(str(len(guild_convos)), "Active server channels")
        + _stat_card(str(conversation_log.total_message_count()), "Messages logged")
        + _stat_card(str(len(vip_users.list_vips())), "VIPs configured")
        + "</div>"
        '<div class="card"><h2>Quick links</h2>'
        '<p><a href="/servers">See every server Elfy\'s in →</a></p>'
        '<p><a href="/users">See who\'s talking to her, and read conversations →</a></p>'
        '<p><a href="/vips">Manage VIP friends and their personas →</a></p>'
        '<p><a href="/settings">Tune personality, generation, and safety settings →</a></p>'
        "</div>"
    )
    return _page("Overview", body, active="overview")


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------

async def handle_servers(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    chat_channel_manager = request.app["chat_channel_manager"]

    rows = []
    for guild in sorted(bot.guilds, key=lambda g: g.name.lower()):
        if guild.icon:
            icon_html = f'<img class="avatar" src="{html.escape(str(guild.icon.url))}" alt="">'
        else:
            icon_html = f'<div class="avatar avatar-fallback">{html.escape(guild.name[:1].upper())}</div>'

        chat_channel_id = chat_channel_manager.get_channel(guild.id)
        channel_obj = guild.get_channel(chat_channel_id) if chat_channel_id else None
        if channel_obj is not None:
            channel_html = f'<a href="/conversation/{channel_obj.id}">#{html.escape(channel_obj.name)}</a>'
        elif chat_channel_id is not None:
            channel_html = '<span class="text-muted">channel not found</span>'
        else:
            channel_html = '<span class="text-muted">not set — /setchat</span>'

        chatting = conversation_log.distinct_users_for_guild(guild.id)
        member_count = guild.member_count if guild.member_count is not None else "—"
        rows.append(
            "<tr>"
            f'<td><div class="cell-identity">{icon_html}<span>{html.escape(guild.name)}</span></div></td>'
            f"<td>{member_count}</td>"
            f"<td>{channel_html}</td>"
            f"<td>{chatting}</td>"
            "</tr>"
        )

    if rows:
        table = (
            '<div class="table-wrap"><table class="data-table"><thead><tr>'
            "<th>Server</th><th>Members</th><th>Chat channel</th><th>Chatting with Elfy</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        table = _empty_state("Elfy isn't in any servers yet.")

    count = len(bot.guilds)
    body = (
        "<h1>Servers</h1>"
        f'<p class="text-muted">{count} server{"s" if count != 1 else ""} total.</p>'
        f"{table}"
    )
    return _page("Servers", body, active="servers")


# ---------------------------------------------------------------------------
# Users (DMs + server conversations)
# ---------------------------------------------------------------------------

def _conversation_row(conv: dict, is_dm: bool) -> str:
    names = conv.get("participant_names", {})
    if is_dm:
        pids = conv.get("participant_ids", [])
        pid = pids[0] if pids else None
        name = names.get(str(pid), "Unknown user")
        who_html = f'<span class="badge badge-dm">DM</span> {html.escape(name)}'
    else:
        server = html.escape(conv.get("guild_name") or "Unknown server")
        chan = html.escape(conv.get("channel_name") or "unknown")
        count = len(conv.get("participant_ids", []))
        who_html = (
            f"<div>{server} · #{chan}</div>"
            f'<div class="text-muted small">{count} participant{"s" if count != 1 else ""}</div>'
        )
    return (
        "<tr>"
        f"<td>{who_html}</td>"
        f'<td>{conv.get("message_count", 0)}</td>'
        f'<td>{_format_relative(conv.get("last_active"))}</td>'
        f'<td><a class="btn btn-ghost btn-sm" href="/conversation/{conv["channel_id"]}">View →</a></td>'
        "</tr>"
    )


async def handle_users(request: web.Request) -> web.Response:
    dm_convos = conversation_log.list_dm_conversations()
    guild_convos = conversation_log.list_guild_conversations()

    dm_table = (
        _empty_state("No one's DM'd Elfy yet.") if not dm_convos else
        '<div class="table-wrap"><table class="data-table"><thead><tr>'
        "<th>User</th><th>Messages</th><th>Last active</th><th></th>"
        "</tr></thead><tbody>"
        + "".join(_conversation_row(c, True) for c in dm_convos)
        + "</tbody></table></div>"
    )
    guild_table = (
        _empty_state("No server conversations logged yet.") if not guild_convos else
        '<div class="table-wrap"><table class="data-table"><thead><tr>'
        "<th>Channel</th><th>Messages</th><th>Last active</th><th></th>"
        "</tr></thead><tbody>"
        + "".join(_conversation_row(c, False) for c in guild_convos)
        + "</tbody></table></div>"
    )

    body = (
        "<h1>Users</h1>"
        '<p class="text-muted">Everyone talking to Elfy — DMs and servers kept separate, since a DM is one person\'s private conversation and a server channel is shared.</p>'
        '<div class="stat-grid">'
        + _stat_card(str(conversation_log.total_distinct_users()), "Distinct people")
        + _stat_card(str(len(dm_convos)), "DM conversations")
        + _stat_card(str(len(guild_convos)), "Server channels active")
        + "</div>"
        "<h2>Direct messages</h2>"
        f"{dm_table}"
        '<h2 style="margin-top:28px">Server conversations</h2>'
        '<p class="text-muted">Shared channels — multiple people can appear in the same conversation.</p>'
        f"{guild_table}"
    )
    return _page("Users", body, active="users")


async def handle_conversation(request: web.Request) -> web.Response:
    try:
        channel_id = int(request.match_info["channel_id"])
    except (KeyError, ValueError):
        raise web.HTTPFound("/users")

    meta = conversation_log.get_channel_meta(channel_id)
    if meta is None:
        body = '<p><a href="/users">← Back to Users</a></p>' + _empty_state(
            "No log found for this channel."
        )
        return _page("Conversation", body, active="users")

    if meta.get("is_dm"):
        names = list(meta.get("participant_names", {}).values())
        label = f"DM with {html.escape(names[0] if names else 'Unknown user')}"
    else:
        label = (
            f'{html.escape(meta.get("guild_name") or "Unknown server")} '
            f'· #{html.escape(meta.get("channel_name") or "unknown")}'
        )

    transcript = conversation_log.get_transcript(channel_id)
    entries_html = []
    for entry in transcript:
        avatar = html.escape(entry.get("author_avatar") or "")
        author_name = html.escape(entry.get("author_name") or "")
        entries_html.append(
            '<div class="msg msg-user">'
            f'<img class="avatar avatar-sm" src="{avatar}" alt="">'
            '<div class="msg-body">'
            f'<div class="msg-meta"><strong>{author_name}</strong> '
            f'<span class="text-muted small">{_format_time(entry.get("timestamp"))}</span></div>'
            f'<div class="msg-text">{html.escape(entry.get("user_text", ""))}</div>'
            "</div></div>"
            '<div class="msg msg-bot">'
            '<div class="avatar avatar-sm avatar-fallback">E</div>'
            '<div class="msg-body">'
            '<div class="msg-meta"><strong>Elfy</strong></div>'
            f'<div class="msg-text">{html.escape(entry.get("bot_text", ""))}</div>'
            "</div></div>"
        )

    transcript_html = (
        f'<div class="transcript">{"".join(entries_html)}</div>'
        if entries_html else _empty_state("No messages logged yet.")
    )
    count = len(transcript)
    body = (
        '<p><a href="/users">← Back to Users</a></p>'
        f"<h1>{label}</h1>"
        f'<p class="text-muted">{count} logged exchange{"s" if count != 1 else ""} '
        "(most recent 300 are kept per channel).</p>"
        f"{transcript_html}"
    )
    return _page("Conversation", body, active="users")


# ---------------------------------------------------------------------------
# VIPs
# ---------------------------------------------------------------------------

def _flash_from_query(request: web.Request) -> str:
    """
    Build a flash banner from the ?ok=...  / ?err=... query param a VIP
    write route (save/delete/reset-greeting below) redirected with. Those
    routes redirect (Post/Redirect/Get, so refreshing the page doesn't
    resubmit the form) rather than rendering directly like Settings does,
    so this is how they carry a real success/error message across that
    redirect instead of the list just silently reloading either way.
    """
    ok = request.query.get("ok")
    err = request.query.get("err")
    if ok:
        return f'<div class="flash flash-success">{html.escape(ok)}</div>'
    if err:
        return f'<div class="flash flash-error">{html.escape(err)}</div>'
    return ""


async def handle_vips(request: web.Request) -> web.Response:
    flash = _flash_from_query(request)
    vips = vip_users.list_vips()
    rows = []
    for uid, v in sorted(vips.items(), key=lambda kv: kv[1]["name"].lower()):
        greeted_badge = (
            '<span class="badge">greeted</span>' if vip_users.has_been_greeted(uid)
            else '<span class="badge badge-dm">not yet greeted</span>'
        )
        name = html.escape(v["name"])
        rows.append(
            "<tr>"
            f"<td><strong>{name}</strong><div class=\"text-muted small\">{uid}</div></td>"
            f'<td>{html.escape(v["relationship"])}</td>'
            f"<td>{greeted_badge}</td>"
            '<td><div class="cell-actions">'
            f'<a class="btn btn-ghost btn-sm" href="/vips/edit/{uid}">Edit</a>'
            '<form method="post" action="/vips/reset-greeting" class="inline-form">'
            f'<input type="hidden" name="user_id" value="{uid}">'
            '<button type="submit" class="btn btn-ghost btn-sm">Re-arm greeting</button>'
            "</form>"
            f'<form method="post" action="/vips/delete" class="inline-form" '
            f"onsubmit=\"return confirm('Remove {name} as a VIP?');\">"
            f'<input type="hidden" name="user_id" value="{uid}">'
            '<button type="submit" class="btn btn-danger btn-sm">Remove</button>'
            "</form></div></td>"
            "</tr>"
        )

    table = (
        '<div class="table-wrap"><table class="data-table"><thead><tr>'
        "<th>Name</th><th>Relationship</th><th>Greeting</th><th></th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    ) if rows else _empty_state("No VIPs configured yet.")

    body = (
        '<div class="page-head"><h1>VIPs</h1>'
        '<div style="display:flex;gap:.5rem;align-items:center;">'
        '<form method="post" action="/vips/sync-from-code" class="inline-form" '
        "onsubmit=\"return confirm('Sync from vip_users.py? This overwrites the live VIP list with whatever is in the code file.');\">"
        '<button type="submit" class="btn btn-ghost">⟳ Sync from code</button>'
        "</form>"
        '<a class="btn btn-primary" href="/vips/new">+ Add VIP</a>'
        "</div></div>"
        '<p class="text-muted">Elfy gives each VIP a custom relationship, private personality note, '
        "and a one-time greeting the first time they ever message her.</p>"
        f"{flash}"
        f"{table}"
    )
    return _page("VIPs", body, active="vips")


def _vip_form_body(
    heading: str,
    user_id: str = "",
    user_id_readonly: bool = False,
    name: str = "",
    relationship: str = "",
    personality_note: str = "",
    greeting: str = "",
) -> str:
    readonly_attr = " readonly" if user_id_readonly else ""
    return (
        '<p><a href="/vips">← Back to VIPs</a></p>'
        f"<h1>{html.escape(heading)}</h1>"
        '<form method="post" action="/vips/save" class="card form-card">'
        '<div class="field"><label for="user_id">Discord user ID</label>'
        f'<input type="text" id="user_id" name="user_id" value="{html.escape(user_id)}"{readonly_attr} '
        'required inputmode="numeric" pattern="[0-9]+">'
        '<p class="hint">Discord Settings → Advanced → Developer Mode, then right-click their name → Copy User ID.</p></div>'
        '<div class="field"><label for="name">Name</label>'
        f'<input type="text" id="name" name="name" value="{html.escape(name)}" required></div>'
        '<div class="field"><label for="relationship">Relationship</label>'
        f'<input type="text" id="relationship" name="relationship" value="{html.escape(relationship)}" '
        'placeholder="e.g. best friend, boyfriend, little sister" required></div>'
        '<div class="field"><label for="personality_note">Personality note</label>'
        f'<textarea id="personality_note" name="personality_note" rows="7" required>{html.escape(personality_note)}</textarea>'
        '<p class="hint">Private context only Elfy sees — shapes how she talks to this specific person.</p></div>'
        '<div class="field"><label for="greeting">One-time greeting</label>'
        f'<textarea id="greeting" name="greeting" rows="2" required>{html.escape(greeting)}</textarea>'
        '<p class="hint">Sent once, the first time this person ever messages Elfy.</p></div>'
        '<button type="submit" class="btn btn-primary">Save VIP</button>'
        "</form>"
    )


async def handle_vip_new(request: web.Request) -> web.Response:
    return _page("Add VIP", _vip_form_body("Add VIP"), active="vips")


async def handle_vip_edit(request: web.Request) -> web.Response:
    try:
        user_id = int(request.match_info["user_id"])
    except (KeyError, ValueError):
        raise web.HTTPFound("/vips")

    vip = vip_users.get_vip(user_id)
    if vip is None:
        raise web.HTTPFound("/vips")

    body = _vip_form_body(
        f"Edit {vip['name']}",
        user_id=str(user_id),
        user_id_readonly=True,
        name=vip["name"],
        relationship=vip["relationship"],
        personality_note=vip["personality_note"],
        greeting=vip["greeting"],
    )
    return _page(f"Edit {vip['name']}", body, active="vips")


async def handle_vip_save(request: web.Request) -> web.Response:
    """
    Create or update a VIP.

    NOTE: this used to redirect back to /vips unconditionally with no
    success/error message at all — the save itself worked, but with zero
    visible confirmation either way, a real save and a silent failure
    looked identical, which is exactly what made this feel like "adding a
    VIP does nothing." It now reports what actually happened.
    """
    data = await request.post()
    raw_id = str(data.get("user_id", "")).strip()
    name = str(data.get("name", "")).strip()

    if not raw_id.isdigit():
        raise web.HTTPFound("/vips?" + urlencode({
            "err": "Couldn't save — Discord user ID must be numeric."
        }))

    try:
        vip_users.save_vip(
            int(raw_id),
            name=name,
            relationship=str(data.get("relationship", "")),
            personality_note=str(data.get("personality_note", "")),
            greeting=str(data.get("greeting", "")),
        )
    except Exception as e:
        print(f"[dashboard] Failed to save VIP {raw_id}: {e}")
        raise web.HTTPFound("/vips?" + urlencode({
            "err": f"Couldn't save {name or raw_id} — something went wrong on my end."
        }))

    raise web.HTTPFound("/vips?" + urlencode({"ok": f"Saved {name or raw_id} as a VIP."}))


async def handle_vip_delete(request: web.Request) -> web.Response:
    data = await request.post()
    raw_id = str(data.get("user_id", "")).strip()
    if not raw_id.isdigit():
        raise web.HTTPFound("/vips?" + urlencode({"err": "Couldn't remove — invalid user ID."}))

    try:
        existing = vip_users.get_vip(int(raw_id))
        name = existing["name"] if existing else raw_id
        vip_users.delete_vip(int(raw_id))
    except Exception as e:
        print(f"[dashboard] Failed to delete VIP {raw_id}: {e}")
        raise web.HTTPFound("/vips?" + urlencode({
            "err": f"Couldn't remove {raw_id} — something went wrong on my end."
        }))

    raise web.HTTPFound("/vips?" + urlencode({"ok": f"Removed {name} as a VIP."}))


async def handle_vip_sync_from_code(request: web.Request) -> web.Response:
    """Overwrite live Replit DB VIP config with _DEFAULT_VIP_USERS from vip_users.py."""
    ns: dict = {}
    stub_cm = type("CM", (), {
        "load_vip_config": staticmethod(lambda: None),
        "save_vip_config": staticmethod(lambda *_: None),
        "load_vip_greeted": staticmethod(lambda: []),
    })
    ns["ChatDataManager"] = stub_cm
    try:
        with open("vip_users.py", encoding="utf-8") as f:
            src = f.read()
        exec(compile(src, "vip_users.py", "exec"), ns)
        defaults: dict = ns["_DEFAULT_VIP_USERS"]
        vip_config = {str(k): v for k, v in defaults.items()}
        vip_users.ChatDataManager.save_vip_config(vip_config)
        vip_users._vip_config = None
        print(f"[dashboard] Synced {len(vip_config)} VIPs from code to Replit DB.")
        raise web.HTTPFound("/vips?" + urlencode({"ok": f"Synced {len(vip_config)} VIPs from vip_users.py successfully."}))
    except web.HTTPFound:
        raise
    except Exception as e:
        print(f"[dashboard] Sync from code failed: {e}")
        raise web.HTTPFound("/vips?" + urlencode({"err": f"Sync failed: {e}"}))


async def handle_vip_reset_greeting(request: web.Request) -> web.Response:
    data = await request.post()
    raw_id = str(data.get("user_id", "")).strip()
    if not raw_id.isdigit():
        raise web.HTTPFound("/vips?" + urlencode({"err": "Couldn't re-arm greeting — invalid user ID."}))

    try:
        existing = vip_users.get_vip(int(raw_id))
        name = existing["name"] if existing else raw_id
        vip_users.reset_greeting(int(raw_id))
    except Exception as e:
        print(f"[dashboard] Failed to reset greeting for {raw_id}: {e}")
        raise web.HTTPFound("/vips?" + urlencode({
            "err": f"Couldn't re-arm {raw_id}'s greeting — something went wrong on my end."
        }))

    raise web.HTTPFound("/vips?" + urlencode({"ok": f"{name}'s one-time greeting is re-armed."}))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SAFETY_LABELS = {
    "BLOCK_NONE": "Don't block anything",
    "BLOCK_ONLY_HIGH": "Block only high-risk content",
    "BLOCK_MEDIUM_AND_ABOVE": "Block medium & high-risk content",
    "BLOCK_LOW_AND_ABOVE": "Block low, medium & high-risk (strictest)",
}


def _safety_select(field_name: str, current: str) -> str:
    options = "".join(
        f'<option value="{t}"{" selected" if t == current else ""}>{_SAFETY_LABELS[t]}</option>'
        for t in dashboard_settings.SAFETY_THRESHOLDS
    )
    return f'<select id="{field_name}" name="{field_name}">{options}</select>'


def _settings_body(flash: str = "") -> str:
    s = dashboard_settings.get_all()
    return (
        "<h1>Settings</h1>"
        '<p class="text-muted">Changes apply immediately — no restart needed.</p>'
        f"{flash}"
        '<form method="post" action="/settings">'

        '<div class="card"><h2>Presence</h2>'
        '<div class="field"><label for="bot_activity">Activity status</label>'
        f'<input type="text" id="bot_activity" name="bot_activity" value="{html.escape(str(s["bot_activity"]))}">'
        '<p class="hint">Shows as "Playing &lt;this&gt;" under Elfy\'s name on Discord.</p></div>'
        "</div>"

        '<div class="card"><h2>Personality</h2>'
        '<div class="field"><label for="bot_personality">Elfy\'s personality / system prompt</label>'
        f'<textarea id="bot_personality" name="bot_personality" rows="8">{html.escape(str(s["bot_personality"]))}</textarea>'
        '<p class="hint">Applies to brand-new conversations, or any channel reset with /forget. '
        "Conversations already in progress keep their current personality.</p></div>"
        '<div class="field"><label for="welcome_instruction">Welcome-message instruction</label>'
        f'<textarea id="welcome_instruction" name="welcome_instruction" rows="5">{html.escape(str(s["welcome_instruction"]))}</textarea>'
        '<p class="hint">Guides the greeting Elfy generates when someone new joins a server.</p></div>'
        '<div class="field"><label for="elfy_appearance">Elfy\'s appearance (for image generation)</label>'
        f'<textarea id="elfy_appearance" name="elfy_appearance" rows="4">{html.escape(str(s["elfy_appearance"]))}</textarea>'
        '<p class="hint">Fixed physical description (hair, face, vibe) prepended whenever someone asks for '
        "a picture of Elfy herself, so she looks like the same character every time — only the outfit/scene "
        "changes per request.</p></div>"
        "</div>"

        '<div class="card"><h2>Chat generation</h2><div class="field-row">'
        '<div class="field"><label for="chat_temperature">Temperature</label>'
        f'<input type="number" step="0.01" min="0" max="2" id="chat_temperature" name="chat_temperature" value="{s["chat_temperature"]}"></div>'
        '<div class="field"><label for="chat_top_p">Top P</label>'
        f'<input type="number" step="0.01" min="0" max="1" id="chat_top_p" name="chat_top_p" value="{s["chat_top_p"]}"></div>'
        '<div class="field"><label for="chat_top_k">Top K</label>'
        f'<input type="number" step="1" min="1" max="100" id="chat_top_k" name="chat_top_k" value="{s["chat_top_k"]}"></div>'
        '<div class="field"><label for="chat_max_output_tokens">Max reply length (tokens)</label>'
        f'<input type="number" step="1" min="1" max="8192" id="chat_max_output_tokens" name="chat_max_output_tokens" value="{s["chat_max_output_tokens"]}"></div>'
        "</div><p class=\"hint\">Applies immediately to every conversation already in progress, not just new ones.</p></div>"

        '<div class="card"><h2>Image generation</h2><div class="field-row">'
        '<div class="field"><label for="image_temperature">Temperature</label>'
        f'<input type="number" step="0.01" min="0" max="2" id="image_temperature" name="image_temperature" value="{s["image_temperature"]}"></div>'
        '<div class="field"><label for="image_top_p">Top P</label>'
        f'<input type="number" step="0.01" min="0" max="1" id="image_top_p" name="image_top_p" value="{s["image_top_p"]}"></div>'
        '<div class="field"><label for="image_top_k">Top K</label>'
        f'<input type="number" step="1" min="1" max="100" id="image_top_k" name="image_top_k" value="{s["image_top_k"]}"></div>'
        '<div class="field"><label for="image_max_output_tokens">Max prompt tokens</label>'
        f'<input type="number" step="1" min="1" max="8192" id="image_max_output_tokens" name="image_max_output_tokens" value="{s["image_max_output_tokens"]}"></div>'
        "</div></div>"

        '<div class="card"><h2>Reply shape</h2><div class="field-row">'
        '<div class="field"><label for="max_reply_lines">Max reply lines</label>'
        f'<input type="number" step="1" min="1" max="20" id="max_reply_lines" name="max_reply_lines" value="{s["max_reply_lines"]}"></div>'
        '<div class="field"><label for="max_message_length">Max message length (characters)</label>'
        f'<input type="number" step="1" min="500" max="2000" id="max_message_length" name="max_message_length" value="{s["max_message_length"]}"></div>'
        "</div></div>"

        '<div class="card"><h2>Content safety</h2><div class="field-row">'
        f'<div class="field"><label for="safety_harassment">Harassment</label>{_safety_select("safety_harassment", s["safety_harassment"])}</div>'
        f'<div class="field"><label for="safety_hate_speech">Hate speech</label>{_safety_select("safety_hate_speech", s["safety_hate_speech"])}</div>'
        f'<div class="field"><label for="safety_sexually_explicit">Sexual content</label>{_safety_select("safety_sexually_explicit", s["safety_sexually_explicit"])}</div>'
        f'<div class="field"><label for="safety_dangerous_content">Dangerous content</label>{_safety_select("safety_dangerous_content", s["safety_dangerous_content"])}</div>'
        "</div></div>"

        '<div class="card"><h2>Access</h2>'
        '<div class="field"><label for="owner_ids">Owner Discord user IDs</label>'
        f'<input type="text" id="owner_ids" name="owner_ids" value="{html.escape(str(s["owner_ids"]))}" placeholder="comma-separated">'
        "<p class=\"hint\">These users can run every slash command on every server, regardless of permissions.</p></div>"
        "</div>"

        '<div class="actions-row">'
        '<button type="submit" class="btn btn-primary">Save settings</button>'
        '<button type="submit" formaction="/settings/reset" formnovalidate class="btn btn-ghost" '
        'onclick="return confirm(\'Reset every setting back to its default? This can\\\'t be undone.\');">'
        "Reset to defaults</button>"
        "</div></form>"
    )


async def handle_settings_get(request: web.Request) -> web.Response:
    return _page("Settings", _settings_body(), active="settings")


async def _apply_live_settings(request: web.Request) -> str:
    """
    Push freshly-saved settings out to the running bot: rebuild active AI
    sessions with the new generation/safety config, and update Discord
    presence if the activity text changed.

    The setting itself is already saved to storage by the time this runs
    (see handle_settings_post/handle_settings_reset) — only the *live push*
    can fail here. Previously ai_service.refresh_active_sessions() wasn't
    wrapped at all, so if it ever raised, the whole request crashed with a
    raw 500 instead of the settings page — even though the save had already
    succeeded. Returns a short description of anything that didn't apply
    live, or "" if everything applied cleanly, so the caller can show an
    accurate message either way instead of a blanket "success."
    """
    problems = []

    ai_service = request.app["ai_service"]
    try:
        ai_service.refresh_active_sessions()
    except Exception as e:
        print(f"[dashboard] Failed to refresh active AI sessions: {e}")
        problems.append("existing conversations (they'll pick it up after a restart)")

    bot = request.app["bot"]
    try:
        await bot.change_presence(activity=discord.Game(dashboard_settings.get("bot_activity")))
    except Exception as e:
        print(f"[dashboard] Failed to update presence: {e}")
        problems.append("Discord activity status")

    return ", ".join(problems)


async def handle_settings_post(request: web.Request) -> web.Response:
    data = await request.post()
    changes = {}
    errors = []
    for key in dashboard_settings.DEFAULTS:
        if key not in data:
            continue
        try:
            changes[key] = dashboard_settings.coerce(key, data[key])
        except (ValueError, TypeError):
            errors.append(key)

    if errors:
        flash = (
            '<div class="flash flash-error">Couldn\'t save — invalid value for: '
            f'{html.escape(", ".join(errors))}. Nothing was changed.</div>'
        )
        return _page("Settings", _settings_body(flash=flash), active="settings")

    # If the appearance description itself changed, the cached reference
    # portrait (see ai_service.generate_character_image) no longer matches
    # it — drop it so the next "picture of Elfy" request bootstraps a fresh
    # one from the new description instead of reusing the old look.
    if "elfy_appearance" in changes and changes["elfy_appearance"] != dashboard_settings.get("elfy_appearance"):
        ChatDataManager.delete_elfy_reference_image()

    dashboard_settings.update(changes)
    live_issues = await _apply_live_settings(request)

    if live_issues:
        flash = (
            '<div class="flash flash-error">Saved — but couldn\'t immediately refresh: '
            f"{html.escape(live_issues)}.</div>"
        )
    else:
        flash = '<div class="flash flash-success">Settings saved and applied.</div>'
    return _page("Settings", _settings_body(flash=flash), active="settings")


async def handle_settings_reset(request: web.Request) -> web.Response:
    ChatDataManager.delete_elfy_reference_image()
    dashboard_settings.reset_to_defaults()
    live_issues = await _apply_live_settings(request)

    if live_issues:
        flash = (
            '<div class="flash flash-error">Reset to defaults — but couldn\'t immediately refresh: '
            f"{html.escape(live_issues)}.</div>"
        )
    else:
        flash = '<div class="flash flash-success">Every setting reset to its default.</div>'
    return _page("Settings", _settings_body(flash=flash), active="settings")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_dashboard_app(bot, ai_service, chat_channel_manager, threads_manager) -> web.Application:
    app = web.Application(middlewares=[_auth_middleware])
    app["bot"] = bot
    app["ai_service"] = ai_service
    app["chat_channel_manager"] = chat_channel_manager
    app["threads_manager"] = threads_manager

    app.router.add_get("/login", handle_login_get)
    app.router.add_post("/login", handle_login_post)
    app.router.add_post("/logout", handle_logout)

    app.router.add_get("/", handle_home)
    app.router.add_get("/servers", handle_servers)
    app.router.add_get("/users", handle_users)
    app.router.add_get("/conversation/{channel_id}", handle_conversation)

    app.router.add_get("/vips", handle_vips)
    app.router.add_get("/vips/new", handle_vip_new)
    app.router.add_get("/vips/edit/{user_id}", handle_vip_edit)
    app.router.add_post("/vips/save", handle_vip_save)
    app.router.add_post("/vips/delete", handle_vip_delete)
    app.router.add_post("/vips/sync-from-code", handle_vip_sync_from_code)
    app.router.add_post("/vips/reset-greeting", handle_vip_reset_greeting)

    app.router.add_get("/settings", handle_settings_get)
    app.router.add_post("/settings", handle_settings_post)
    app.router.add_post("/settings/reset", handle_settings_reset)

    return app
