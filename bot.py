"""
Discord layer for the TF2 PUG bot — slash commands + ++/-- and !ar shortcuts.

Run:  DISCORD_TOKEN=xxx [TEST_GUILD_ID=123] python bot.py   (discord.py >= 2.3)

All game logic is in pug_state.py. This file maps Discord I/O + timing onto it.
Display: rosters show server display names in `code boxes` (no pings); the
auto-ready confirmation is a green "Success" embed. Real @mentions are kept
only where a ping is the point (the player on the clock, match cancellation).
"""

import os
import time
import json
import asyncio
import random
import difflib
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from storage import Store

from pug_state import (PugState, Phase, QUEUE_SIZE,
                       DEFAULT_AR_SECONDS, AR_COMMAND_SECONDS,
                       MAX_AR_SECONDS, READY_CHECK_SECONDS, TIMEOUT_SECONDS,
                       ELO_START)

# Load DISCORD_TOKEN / TEST_GUILD_ID / PUG_DEBUG from a local .env file if
# python-dotenv is installed; otherwise fall back to shell environment vars.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ADMIN_ROLE = "PUG Admin"

# Role that /promote pings to rally players (matched case-insensitively against
# the server's role names). Edit to match your server's role exactly.
PUG_PING_ROLE = "Puggers"
PROMOTE_COOLDOWN = 120          # seconds between /promote pings (anti-spam)

# Skill-division roles shown next to unpicked players during the draft, so
# captains know who they're picking. Role names are matched case-insensitively
# against each member's server roles; first match wins (so list best→worst).
# Edit these to match your server's actual role names exactly.
SKILL_ROLES = [
    ("div 1", "Div 1"),
    ("div 2", "Div 2"),
    ("div 3", "Div 3"),
]

# If set, the bot ONLY responds in this one channel; commands anywhere else are
# ignored (text) or get a quiet "wrong channel" notice (slash). This stops people
# queueing in random channels. Set it via the PUG_CHANNEL_ID env var — get the ID
# from Discord with Developer Mode on: right-click the channel > Copy Channel ID.
# Leave unset to allow the bot in every channel (old behaviour).
# PUGs run in these channels. Each listed channel is an INDEPENDENT lobby with
# its own queue / ready check / draft / live game. Set PUG_CHANNEL_ID (or the
# alias PUG_CHANNEL_IDS) to a comma- or space-separated list of channel IDs, e.g.
# PUG_CHANNEL_ID="111111111111 222222222222". One ID = single lobby (old
# behaviour). Unset = a single lobby that responds in every channel.
# Get IDs with Developer Mode on: right-click a channel > Copy Channel ID.
def _parse_channel_ids(raw):
    out = []
    for part in re.split(r"[,\s]+", (raw or "").strip()):
        if part:
            try:
                out.append(int(part))
            except ValueError:
                pass
    return out

PUG_CHANNEL_IDS = _parse_channel_ids(
    os.environ.get("PUG_CHANNEL_ID") or os.environ.get("PUG_CHANNEL_IDS") or "")

# Channel where players post/find the server connect string. Shown on the live
# teams embed so people know where to go. Set CONNECT_CHANNEL_ID (channel ID) to
# make it a clickable #mention; left unset, a plain "#connect-string" is shown.
CONNECT_CHANNEL_ID = int(os.environ["CONNECT_CHANNEL_ID"]) if os.environ.get("CONNECT_CHANNEL_ID") else None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

import contextvars
from lobby import reconcile, blocking_cid

# ---------- lobby registry (one independent game per configured channel) ----------
SHARED_IMMUNITY = {}             # med immunity is shared across lobbies (per-player)
SHARED_STATS = {}                # lifetime {uid: {"games","capt","w","l","d","elo"}}, shared across lobbies
_SINGLE = None                   # lobby key used when no channels are configured
lobbies = {}                     # lobby_key -> PugState
_ready_views = {}                # lobby_key -> ReadyView | None
_lobby_channels = {}             # lobby_key -> discord channel (for posting / resume)
_resume_keys = {}                # lobby_key -> channel_id to resume in (from snapshot)
_last_promote = {}               # lobby_key -> monotonic time of last /promote ping


def _new_state():
    return PugState(immunity=SHARED_IMMUNITY, stats=SHARED_STATS)


# Pre-create the configured lobbies (or a single shared one if none configured).
if PUG_CHANNEL_IDS:
    for _cid in PUG_CHANNEL_IDS:
        lobbies[_cid] = _new_state()
else:
    lobbies[_SINGLE] = _new_state()

_cur_key = contextvars.ContextVar("cur_lobby_key", default=_SINGLE)


def lobby_key_for(channel_id):
    """Channel id -> its lobby key, or False if it isn't a PUG channel."""
    if PUG_CHANNEL_IDS:
        return channel_id if channel_id in PUG_CHANNEL_IDS else False
    return _SINGLE                # single shared lobby, responds in any channel


def _enter(channel):
    """Bind the active lobby for this task from a channel. Returns its PugState,
    or None if the channel isn't a PUG channel."""
    key = lobby_key_for(channel.id)
    if key is False:
        return None
    _cur_key.set(key)
    _lobby_channels[key] = channel
    return lobbies[key]


class _LobbyProxy:
    """Forwards attribute access to the current task's lobby PugState, so every
    existing `pug.…` call site operates on the right channel's game without
    threading a state parameter through dozens of helpers. Async-safe: the key
    lives in a ContextVar, so concurrent interactions in different channels never
    clobber each other."""
    __slots__ = ()

    def __getattr__(self, name):
        return getattr(lobbies[_cur_key.get()], name)


pug = _LobbyProxy()


def cross_block_msg(uid):
    """If `uid` is committed to a game in a DIFFERENT lobby, return a refusal
    message (they can't queue anywhere until that game ends). Else None."""
    if len(lobbies) < 2:
        return None
    home = blocking_cid(lobbies, uid, _cur_key.get())
    if home is None:
        return None
    ch = _lobby_channels.get(home)
    where = ch.mention if ch is not None else "another channel"
    return f"You're in the game in {where} — finish it before queuing here."


async def after_commit(home_channel, home_guild):
    """Enforce one-game-at-a-time after any change that may commit a player:
    pull committed players out of every OTHER lobby, notify those channels, and
    re-render them (a mid-check pull backfills and may re-fire that check)."""
    if len(lobbies) < 2:
        return
    changed, _notes = reconcile(lobbies)
    for key, uids in changed.items():
        ch = _lobby_channels.get(key)
        if ch is None:
            continue
        old = _ready_views.get(key)          # tear down a now-stale ready check
        if old is not None:
            if old.deadline:
                old.deadline.cancel()
            old.stop()
            _ready_views[key] = None
            try:
                await old.message.edit(content="*(queue changed — re-checking)*", view=None)
            except (discord.HTTPException, AttributeError):
                pass
        token = _cur_key.set(key)
        try:
            names = ", ".join(name_box(ch.guild, u) for u in uids)
            await ch.send(f"{names} got pulled into a game in another channel — "
                          "removed from this queue.")
            await render_active(ch, ch.guild)
        finally:
            _cur_key.reset(token)

# ---------- persistence ----------
store = Store(os.environ.get("PUG_DB", "pug.db"))
_last_saved = None           # last snapshot we wrote (skip redundant writes)
_resumed = False             # one-shot guard (on_ready can fire on reconnects)


def persist():
    """Write a snapshot of every lobby (+ shared immunity + the channel each game
    lives in) if it changed. Cheap and called on a timer, so a missed handler
    can never lose much."""
    global _last_saved
    payload = {
        "v": 2,
        "immunity": SHARED_IMMUNITY,
        "stats": SHARED_STATS,
        "fake_names": FAKE_NAMES,
        "lobbies": {
            str(key): {
                "state": st.to_dict(),
                "channel_id": (_lobby_channels[key].id
                               if _lobby_channels.get(key) is not None else None),
            }
            for key, st in lobbies.items()
        },
    }
    blob = json.dumps(payload, sort_keys=True)
    if blob != _last_saved and store.save(payload):
        _last_saved = blob


# ---------- name + display helpers ----------
FAKE_NAMES = {}   # debug-only: fake player id -> label


def display_name(guild, uid) -> str:
    if uid in FAKE_NAMES:
        return FAKE_NAMES[uid]
    member = guild.get_member(uid) if guild else None
    return member.name if member else str(uid)   # .name = Discord username, ignores nicknames


def name_box(guild, uid) -> str:
    """Server display name in an inline code box — shows the name, no ping."""
    return f"`{display_name(guild, uid)}`"


def player_tag(guild, uid) -> str:
    """For embeds: a real player renders as an @mention (purple, but embeds
    don't fire notifications); a debug bot renders as its code-box label."""
    if uid in FAKE_NAMES:
        return f"`{FAKE_NAMES[uid]}`"
    return f"<@{uid}>"


def skill_label(guild, uid) -> str:
    """A player's skill-division tag (e.g. 'Div 1') from their server roles.
    Returns '' for bots or members with no matching role."""
    member = guild.get_member(uid) if guild else None
    if not member:
        return ""
    have = {r.name.lower() for r in member.roles}
    for key, label in SKILL_ROLES:
        if key in have:
            return label
    return ""


def fuzzy_unpicked(name, guild):
    """Best-matching unpicked player for a typed name. Tries exact, then
    prefix, then substring, then difflib similarity. Returns uid or None."""
    q = name.strip().lower()
    pairs = [(u, display_name(guild, u).lower()) for u in pug.unpicked()]
    if not pairs:
        return None
    for u, n in pairs:                       # exact
        if n == q:
            return u
    pre = [u for u, n in pairs if n.startswith(q)]
    if pre:
        return pre[0]
    sub = [u for u, n in pairs if q in n]
    if sub:
        return sub[0]
    match = difflib.get_close_matches(q, [n for _, n in pairs], n=1, cutoff=0.4)
    if match:
        return next(u for u, n in pairs if n == match[0])
    return None


def is_admin(member) -> bool:
    perms = getattr(member, "guild_permissions", None)
    if perms and perms.administrator:
        return True
    return any(r.name == ADMIN_ROLE for r in getattr(member, "roles", []))


def fmt_dur(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _ago(ts) -> str:
    """A Discord relative timestamp (renders as e.g. '5 minutes ago' per viewer)."""
    try:
        return f"<t:{int(ts)}:R>"
    except (TypeError, ValueError):
        return "earlier"


def success_embed(ar_seconds: int) -> discord.Embed:
    return discord.Embed(
        title="Success",
        description=(f"During next {fmt_dur(ar_seconds)} your match "
                     "participation will be confirmed automatically."),
        color=discord.Color.green(),
    )


def queue_display(guild) -> str:
    ids = pug.queue_ids()
    if not ids:
        return "Queue empty. /add or ++ to join."
    roster = " / ".join(name_box(guild, u) for u in ids)
    return f"**6v6** ({len(ids)}/{QUEUE_SIZE}) | {roster}"


def next_queue_display(guild) -> str:
    ids = pug.next_queue_ids()
    if not ids:
        return "Next queue is empty."
    roster = " / ".join(name_box(guild, u) for u in ids)
    return f"**Next queue** ({len(ids)}/{QUEUE_SIZE}) | {roster}"


def ready_menu(guild) -> str:
    # plain-text message (not an embed), so these mentions DO notify — that's
    # intended: the 12 get pinged once when the check starts. Editing the
    # message as people ready up does not re-ping.
    ready, not_ready = pug.ready_status()
    r = " / ".join(player_tag(guild, u) for u in ready) or "—"
    n = " / ".join(player_tag(guild, u) for u in not_ready) or "—"
    return ("**Ready check!** Click **Ready** within "
            f"{fmt_dur(READY_CHECK_SECONDS)} or you're dropped.\n"
            f"✅ Ready ({len(ready)}/{QUEUE_SIZE}): {r}\n"
            f"⌛ Waiting: {n}")


def team_roster(guild, color) -> str:
    """A team's bracketed roster line. The first slot is always the captain, so
    'empty' in that slot means the team currently has no captain (e.g. after a
    /match put moved one out). With no captain and no picks it reads '[ empty ]',
    same as a fresh team at the start of the draft."""
    capt = pug.capt_of.get(color)
    picks = pug.team[color]
    capt_str = player_tag(guild, capt) if capt else "empty"
    inside = (" / ".join([capt_str] + [player_tag(guild, u) for u in picks])
              if picks else capt_str)
    return f"[ {inside} ]"


def draft_embed(guild) -> discord.Embed:
    dot = {"RED": "🔴", "BLU": "🔵"}
    lines = []
    for color in ("RED", "BLU"):
        lines.append(f"{dot[color]} **{color}** ⟨{len(pug.team[color])}⟩")
        lines.append(team_roster(guild, color))
    lines.append("")
    if not pug._both_capts_set:
        caps = " and ".join(player_tag(guild, u) for u in pug.captains)
        word = "captain" if len(pug.captains) == 1 else "captains"
        lines.append(f"{caps} have been rolled as {word}")
    up = pug.unpicked()
    if up:
        lines.append("**Unpicked:**")
        for u in up:
            tag = player_tag(guild, u)
            skill = skill_label(guild, u)
            imm = pug.immunity.get(u)
            extra = f" · {skill}" if skill else ""
            extra += f" — **IMMUNE: x{imm}**" if imm else ""
            lines.append(tag + extra)
    lines.append("—")
    if not pug._both_capts_set:
        lines.append("Type **/capfor red** or **/capfor blu** to captain a team "
                     "(anyone may volunteer). **/capoff** to step down.")
    elif pug.phase is Phase.PICKING:
        picker = pug._current_picker()
        if picker:
            lines.append(f"{player_tag(guild, picker)} to pick — /pick @player")
        else:
            lines.append("That side has no captain — /capfor red or /capfor blu to take it.")
    return discord.Embed(title="6v6 is now on the draft stage!",
                         description="\n".join(lines),
                         color=discord.Color.blurple())


def final_embed(guild) -> discord.Embed:
    dot = {"RED": "🔴", "BLU": "🔵"}
    lines = []
    for color in ("RED", "BLU"):
        lines.append(f"{dot[color]} **{color}**")
        lines.append(team_roster(guild, color))
    lines.append("—")
    connect = f"<#{CONNECT_CHANNEL_ID}>" if CONNECT_CHANNEL_ID else "#connect-string"
    lines.append(f"➡️ Head to {connect} to join the server.")
    lines.append("When it's over: **/match report red** or **/match report blu**.")
    lines.append("Admins: **/match cancel** to void a game with no result.")
    return discord.Embed(title="Teams set — GLHF!",
                         description="\n".join(lines),
                         color=discord.Color.green())


def results_embed(guild, mid, detail) -> discord.Embed:
    """The post-report card: which team won and each player's Elo before → after.
    `detail` is PugState.last_result (in-memory, int-keyed). Deliberately NOT a
    monospace code block — those wrap badly on mobile; a plain list reads cleaner."""
    winner = detail["winner"]
    elos = detail["elos"]                       # {uid: [before, after]}
    dots = {"RED": "🔴", "BLU": "🔵"}

    def line(u):
        before, after = elos[u]
        return f"`{display_name(guild, u)}` {before} → **{after}**  ({after - before:+d})"

    def field(side):
        return "\n".join(line(u) for u in detail[side.lower()]) or "—"

    colors = {"RED": discord.Color.red(), "BLU": discord.Color.blue()}
    head = f"Match #{mid} — " if mid is not None else ""
    if winner == "DRAW":
        embed = discord.Embed(title=f"{head}Draw 🤝", color=discord.Color.greyple())
        order = ("RED", "BLU")
    else:
        embed = discord.Embed(title=f"{head}{winner} win 🏆",
                              color=colors.get(winner, discord.Color.green()))
        order = ("RED", "BLU") if winner == "RED" else ("BLU", "RED")   # winner on top
    for side in order:
        trophy = "  🏆" if side == winner else ""
        embed.add_field(name=f"{dots[side]} {side}{trophy}", value=field(side), inline=False)
    return embed


# ---------- ready-check UI ----------
class ReadyView(discord.ui.View):
    def __init__(self, channel, guild, key=_SINGLE):
        super().__init__(timeout=None)   # we run our own hard ready deadline
        self.channel = channel
        self.guild = guild
        self.key = key                   # which lobby this check belongs to
        self.message = None
        self.deadline = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # button clicks don't flow through the command tree, so bind the lobby here
        _cur_key.set(self.key)
        return True

    async def start(self):
        self.message = await self.channel.send(ready_menu(self.guild), view=self)
        self.deadline = asyncio.create_task(self._expire())

    async def refresh(self):
        """Re-render the ready menu (e.g. after someone armed auto-ready via command)."""
        if self.message:
            try:
                await self.message.edit(content=ready_menu(self.guild), view=self)
            except discord.HTTPException:
                pass

    async def finish_draft(self):
        """All players ready -> close the check and post the draft board."""
        if self.deadline:
            self.deadline.cancel()
        self.stop()
        _ready_views[self.key] = None
        if self.message:
            try:
                await self.message.edit(content="**All players ready!**", view=None)
            except discord.HTTPException:
                pass
        await self.channel.send(embed=draft_embed(self.guild))

    async def _expire(self):
        _cur_key.set(self.key)
        try:
            await asyncio.sleep(READY_CHECK_SECONDS)
        except asyncio.CancelledError:
            return
        if pug.phase is not Phase.READY_CHECK:
            return
        dropped = pug.resolve_ready_check()
        self.stop()
        _ready_views[self.key] = None
        names = " / ".join(name_box(self.guild, u) for u in dropped) or "nobody"
        await self.message.edit(
            content=f"**Ready check failed.** Dropped (expire time ran off): {names}",
            view=None)
        await render_active(self.channel, self.guild)
        await after_commit(self.channel, self.guild)

    @discord.ui.button(label="Ready", style=discord.ButtonStyle.success, emoji="✅")
    async def ready_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, sig = pug.mark_ready(interaction.user.id)
        if not ok:
            await interaction.response.send_message(sig, ephemeral=True)
            return
        if pug.phase is Phase.PICKING:
            if self.deadline:
                self.deadline.cancel()
            self.stop()
            _ready_views[self.key] = None
            await interaction.response.edit_message(content="**All players ready!**", view=None)
            await self.channel.send(embed=draft_embed(self.guild))
        else:
            await interaction.response.edit_message(content=ready_menu(self.guild), view=self)
        await after_commit(self.channel, self.guild)

    @discord.ui.button(label="Abort", style=discord.ButtonStyle.danger, emoji="✖️")
    async def abort_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, sig, dropped = pug.abort_ready_check(interaction.user.id)
        if not ok:
            await interaction.response.send_message(sig, ephemeral=True)
            return
        if self.deadline:
            self.deadline.cancel()
        self.stop()
        _ready_views[self.key] = None
        await interaction.response.edit_message(
            content=f"**Ready check aborted** — {name_box(self.guild, interaction.user.id)} "
                    "left the queue.",
            view=None)
        await render_active(self.channel, self.guild)
        await after_commit(self.channel, self.guild)


async def launch_ready_check(channel, guild):
    key = _cur_key.get()
    view = ReadyView(channel, guild, key)
    _ready_views[key] = view
    await view.start()


async def render_active(channel, guild):
    """Show whatever the active slot is now (used after state changes that may
    promote the next queue or re-fire a ready check)."""
    if pug.phase is Phase.READY_CHECK:
        await launch_ready_check(channel, guild)
    elif pug.phase is Phase.PICKING:
        await channel.send(embed=draft_embed(guild))
    elif pug.phase is Phase.LIVE:
        await channel.send(embed=final_embed(guild))
    else:
        await channel.send(queue_display(guild))


async def announce_after_add(channel, guild, ar, confirm, reprint=True):
    """Render the right thing after a successful add, given the new phase.
    `confirm` is an async fn taking (content=None, embed=None).
    reprint=False (a re-arm of someone already queued) shows only the embed."""
    if pug.phase is Phase.QUEUING:
        await confirm(embed=success_embed(ar))
        if reprint:
            await channel.send(queue_display(guild))
    elif pug.phase is Phase.READY_CHECK:
        await confirm(content="Queue full — ready check started!")
        await launch_ready_check(channel, guild)
    elif pug.phase is Phase.PICKING:                  # everyone was auto-ready
        await confirm(content="Queue full — everyone auto-ready!")
        await channel.send(embed=draft_embed(guild))


# ---------- client ----------
class PugTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # central choke point: every slash command flows through here, so this is
        # where we bind the lobby for this channel (so `pug` resolves correctly)
        # and enforce the channel lock. Text commands bind in on_message.
        key = lobby_key_for(interaction.channel_id) if interaction.channel_id else False
        if key is False:
            where = " ".join(f"<#{c}>" for c in PUG_CHANNEL_IDS) or "the PUG channel"
            await interaction.response.send_message(
                f"PUGs only run in {where} — use the bot there.", ephemeral=True)
            return False
        _cur_key.set(key)
        if interaction.channel is not None:
            _lobby_channels[key] = interaction.channel
        return True


class PugClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = PugTree(self)

    async def setup_hook(self):
        global _last_saved
        # restore any persisted state before we start taking commands
        saved = store.load()
        if saved and saved.get("lobbies"):
            SHARED_IMMUNITY.clear()
            SHARED_IMMUNITY.update(
                {int(k): int(v) for k, v in (saved.get("immunity") or {}).items()})
            SHARED_STATS.clear()
            SHARED_STATS.update(
                {int(k): {"games": int(v.get("games", 0)), "capt": int(v.get("capt", 0)),
                          "w": int(v.get("w", 0)), "l": int(v.get("l", 0)),
                          "d": int(v.get("d", 0)), "elo": int(v.get("elo", ELO_START))}
                 for k, v in (saved.get("stats") or {}).items()})
            for k_str, lob in saved["lobbies"].items():
                key = _SINGLE if k_str == "None" else int(k_str)
                if key not in lobbies:
                    continue                       # config changed; ignore unknown lobby
                lobbies[key].load_dict(lob.get("state", {}))
                cid = lob.get("channel_id")
                if cid is not None:
                    _resume_keys[key] = cid
            FAKE_NAMES.update({int(k): v for k, v in (saved.get("fake_names") or {}).items()})
            _last_saved = json.dumps(saved, sort_keys=True)
        gid = os.environ.get("TEST_GUILD_ID")
        if gid:
            guild = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)          # register in the guild (instant)
            # remove any stale GLOBAL registrations so they don't duplicate the guild ones
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()

    async def on_ready(self):
        global _resumed
        if not timeout_sweep.is_running():
            timeout_sweep.start()
        if not autosave.is_running():
            autosave.start()
        if not _resumed:                 # on_ready can re-fire on reconnects
            _resumed = True
            await self._resume()
        print(f"Logged in as {self.user}")

    async def _resume(self):
        """After a restart, re-show (and for a ready check, re-arm) whatever game
        was in progress in each lobby, in the channel it was happening in."""
        for key, cid in list(_resume_keys.items()):
            st = lobbies.get(key)
            if st is None or st.phase is Phase.IDLE:
                continue
            channel = self.get_channel(cid)
            if channel is None:
                try:
                    channel = await self.fetch_channel(cid)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue
            _lobby_channels[key] = channel
            _cur_key.set(key)
            guild = channel.guild
            try:
                if pug.phase is Phase.QUEUING and pug.queue_ids():
                    await channel.send("♻️ **Bot restarted** — queue restored.\n"
                                       + queue_display(guild))
                elif pug.phase is Phase.READY_CHECK:
                    await channel.send("♻️ **Bot restarted** — resuming the ready check "
                                       "(fresh 2:00 window).")
                    await launch_ready_check(channel, guild)
                elif pug.phase is Phase.PICKING:
                    await channel.send("♻️ **Bot restarted** — draft resumed.",
                                       embed=draft_embed(guild))
                elif pug.phase is Phase.LIVE:
                    await channel.send("♻️ **Bot restarted** — game still live.",
                                       embed=final_embed(guild))
                if pug.next_queue_ids() and pug.phase is not Phase.QUEUING:
                    await channel.send(next_queue_display(guild))
            except discord.HTTPException:
                pass

    async def on_member_join(self, member):
        """Give every new (human) member the rally ping role so /promote reaches
        them. Needs the Server Members intent (on) plus Manage Roles, and the
        bot's top role must sit ABOVE the Puggers role — otherwise Discord refuses
        the assignment (caught and ignored here)."""
        if member.bot:
            return
        role = discord.utils.get(member.guild.roles, name=PUG_PING_ROLE)
        if role is None:
            return
        try:
            await member.add_roles(role, reason="Auto-assign Puggers on join")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def close(self):
        persist()                        # final flush on graceful shutdown
        store.close()
        await super().close()

    async def on_message(self, message):
        if message.author.bot:
            return
        if _enter(message.channel) is None:
            return                       # ignore ++/--/!ar outside a PUG channel
        text = message.content.strip().lower()
        if text in ("++", "!ar"):
            uid = message.author.id
            blocked = cross_block_msg(uid)
            if blocked and not (pug.phase is Phase.READY_CHECK and uid in pug.queue):
                await message.channel.send(blocked)
                return
            await self._text_add(message, DEFAULT_AR_SECONDS if text == "++" else AR_COMMAND_SECONDS)
        elif text == "--":
            ok, msg = pug.remove(message.author.id)
            if not ok:
                await message.channel.send(msg)
            elif pug.slot_busy:                  # they left the NEXT queue (a game is on)
                await message.channel.send(next_queue_display(message.guild))
            else:
                await message.channel.send(queue_display(message.guild))

    async def _text_add(self, message, ar):
        uid = message.author.id
        in_check = pug.phase is Phase.READY_CHECK and uid in pug.queue
        busy = pug.slot_busy
        already = uid in pug.queue or uid in pug.next_queue
        ok, msg = pug.add(uid, ar)
        if not ok:
            await message.channel.send(msg)
            return
        if in_check:                              # armed auto-ready mid ready-check
            await self._after_ready_arm(message.channel, message.guild, ar,
                                        lambda **k: message.channel.send(**k))
            await after_commit(message.channel, message.guild)
            return
        async def confirm(content=None, embed=None):
            await message.channel.send(content=content, embed=embed)
        if busy:                                  # joined / re-armed the next queue
            await confirm(embed=success_embed(ar))
            if not already:
                await message.channel.send(next_queue_display(message.guild))
        else:
            await announce_after_add(message.channel, message.guild, ar, confirm,
                                     reprint=not already)
        await after_commit(message.channel, message.guild)

    async def _after_ready_arm(self, channel, guild, ar, reply):
        """Shared rendering after a player arms auto-ready during a ready check:
        they're confirmed now; if that completed the check, launch the draft,
        otherwise refresh the ready menu so they show as ready."""
        view = _ready_views.get(_cur_key.get())
        if pug.phase is Phase.PICKING:            # they were the last needed
            if view:
                await view.finish_draft()
            else:
                await channel.send(embed=draft_embed(guild))
            await reply(content=f"Auto-ready set ({fmt_dur(ar)}) — you're confirmed, draft starting.")
        else:
            if view:
                await view.refresh()
            await reply(content=f"Auto-ready set ({fmt_dur(ar)}) — you're readied now, and "
                                "auto-confirmed if the ready check restarts.")


client = PugClient()


# ---------- slash: joining ----------
async def _slash_add(interaction: discord.Interaction, ar: int):
    uid = interaction.user.id
    in_check = pug.phase is Phase.READY_CHECK and uid in pug.queue
    blocked = cross_block_msg(uid)
    if blocked and not in_check:
        await interaction.response.send_message(blocked, ephemeral=True)
        return
    busy = pug.slot_busy
    already = uid in pug.queue or uid in pug.next_queue
    ok, msg = pug.add(uid, ar)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    if in_check:                              # armed auto-ready mid ready-check
        replied = {"v": False}
        async def reply(content=None):
            replied["v"] = True
            await interaction.response.send_message(content, ephemeral=True)
        await client._after_ready_arm(interaction.channel, interaction.guild, ar, reply)
        await after_commit(interaction.channel, interaction.guild)
        return
    async def confirm(content=None, embed=None):
        await interaction.response.send_message(content=content, embed=embed)
    if busy:                                  # joined / re-armed the next queue
        await confirm(embed=success_embed(ar))
        if not already:
            await interaction.channel.send(next_queue_display(interaction.guild))
    else:
        await announce_after_add(interaction.channel, interaction.guild, ar, confirm,
                                 reprint=not already)
    await after_commit(interaction.channel, interaction.guild)


@client.tree.command(name="add", description="Join the queue (2-min auto-ready).")
async def add_cmd(interaction: discord.Interaction):
    await _slash_add(interaction, DEFAULT_AR_SECONDS)


@client.tree.command(name="ar", description="Join with a 15-min auto-ready window.")
async def ar_cmd(interaction: discord.Interaction):
    await _slash_add(interaction, AR_COMMAND_SECONDS)


@client.tree.command(name="auto-ready", description="Join with a custom auto-ready window (max 30 min).")
@app_commands.describe(minutes="Minutes to stay auto-ready (capped at 30)")
async def auto_ready_cmd(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 30]):
    await _slash_add(interaction, minutes * 60)


@client.tree.command(name="leave", description="Leave the queue.")
async def leave_cmd(interaction: discord.Interaction):
    ok, msg = pug.remove(interaction.user.id)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
    elif pug.slot_busy:                          # they left the NEXT queue (a game is on)
        await interaction.response.send_message(next_queue_display(interaction.guild))
    else:
        await interaction.response.send_message(queue_display(interaction.guild))


@client.tree.command(name="aroff", description="Turn off your auto-ready (ready up manually).")
async def aroff_cmd(interaction: discord.Interaction):
    ok, msg = pug.clear_auto_ready(interaction.user.id)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    embed = discord.Embed(
        title="Success",
        description="Auto-ready is now off — you'll need to ready up manually when the queue fills.",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@client.tree.command(name="capoff", description="Step down as captain so someone else can take it.")
async def capoff_cmd(interaction: discord.Interaction):
    ok, msg = pug.uncap(interaction.user.id)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(embed=draft_embed(interaction.guild))


@client.tree.command(name="ready", description="Confirm you're ready (fallback for the button).")
async def ready_cmd(interaction: discord.Interaction):
    ok, sig = pug.mark_ready(interaction.user.id)
    if not ok:
        await interaction.response.send_message(sig, ephemeral=True)
        return
    if pug.phase is Phase.PICKING:
        await interaction.response.send_message("All players ready!")
        await interaction.channel.send(embed=draft_embed(interaction.guild))
    else:
        await interaction.response.send_message("You're ready ✅", ephemeral=True)


# ---------- slash: draft ----------
@client.tree.command(name="capfor", description="Volunteer to captain a side (RED/BLU).")
@app_commands.describe(team="Which side to captain")
@app_commands.choices(team=[
    app_commands.Choice(name="RED", value="red"),
    app_commands.Choice(name="BLU", value="blu"),
])
async def capfor_cmd(interaction: discord.Interaction, team: app_commands.Choice[str]):
    ok, msg = pug.capfor(interaction.user.id, team.value)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(embed=draft_embed(interaction.guild))


@client.tree.command(name="pick", description="Draft a player (current picker only).")
@app_commands.describe(player="Who to pick")
async def pick_cmd(interaction: discord.Interaction, player: discord.Member):
    ok, msg = pug.pick(interaction.user.id, player.id)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    embed = final_embed(interaction.guild) if pug.phase is Phase.LIVE else draft_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)


# ---------- slash: subs ----------
@client.tree.command(name="subme", description="Request a substitute for your spot (draft stage only).")
async def subme_cmd(interaction: discord.Interaction):
    ok, msg = pug.request_sub(interaction.user.id)
    await interaction.response.send_message(msg, ephemeral=not ok)


@client.tree.command(name="subfor", description="Sub in for someone who asked (draft stage only).")
@app_commands.describe(player="(optional) whose spot to take")
async def subfor_cmd(interaction: discord.Interaction, player: Optional[discord.Member] = None):
    ok, msg = pug.sub_for(interaction.user.id, player.id if player else None)
    await interaction.response.send_message(msg, ephemeral=not ok)


# ---------- slash: match admin ----------
match_group = app_commands.Group(name="match", description="Match controls.")


@match_group.command(name="report", description="Report the result of the live game (captain or admin).")
@app_commands.describe(winner="Which team won, or draw")
@app_commands.choices(winner=[
    app_commands.Choice(name="RED win", value="red"),
    app_commands.Choice(name="BLU win", value="blu"),
    app_commands.Choice(name="Draw / Tie", value="draw"),
])
async def match_report_cmd(interaction: discord.Interaction,
                           winner: app_commands.Choice[str]):
    ok, msg, pinged = pug.match_report(interaction.user.id, is_admin(interaction.user),
                                       winner=winner.value)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    # a recorded game is written to the append-only audit log and tagged with its id
    result = pug.last_result
    mid = (store.log_event("match", {**result, "channel_id": interaction.channel.id})
           if result else None)
    embed = results_embed(interaction.guild, mid, result)
    embed.set_footer(text="Queue open — /add to join.")
    await interaction.response.send_message(embed=embed)
    # a next queue may have just been promoted into the active slot
    if pug.queue_ids():
        await render_active(interaction.channel, interaction.guild)
    await after_commit(interaction.channel, interaction.guild)


@match_group.command(name="cancel", description="Admin: end a match with NO result — void a live game or scrap a forming one.")
async def match_cancel_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    ok, msg, pinged = pug.match_cancel(interaction.user.id, is_admin=True)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    mentions = ", ".join(f"<@{u}>" for u in pinged)   # cancellation SHOULD ping the players
    await interaction.response.send_message(f"{mentions} {msg}" if mentions else msg)
    if pug.queue_ids():
        await render_active(interaction.channel, interaction.guild)
    await after_commit(interaction.channel, interaction.guild)


@match_group.command(name="put", description="Admin: move a player onto a team, a captain slot, or the bench.")
@app_commands.describe(player="Player to move", team="Target: a team, a captain slot, or the bench")
@app_commands.choices(team=[
    app_commands.Choice(name="RED", value="red"),
    app_commands.Choice(name="BLU", value="blu"),
    app_commands.Choice(name="Captain RED", value="capt_red"),
    app_commands.Choice(name="Captain BLU", value="capt_blu"),
    app_commands.Choice(name="Bench", value="bench"),
])
async def match_put_cmd(interaction: discord.Interaction,
                        player: discord.Member, team: app_commands.Choice[str]):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    ok, msg = pug.match_put(player.id, team.value)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    embed = final_embed(interaction.guild) if pug.phase is Phase.LIVE else draft_embed(interaction.guild)
    await interaction.response.send_message(embed=embed)


@match_group.command(name="start", description="Admin: force a fully-set draft to go live (e.g. after arranging teams with /match put).")
async def match_start_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    ok, msg = pug.match_start(interaction.user.id, is_admin=True)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(embed=final_embed(interaction.guild))


@match_group.command(name="log", description="Show the most recent reported matches.")
async def match_log_cmd(interaction: discord.Interaction):
    events = store.recent_events(limit=10, kind="match")
    if not events:
        await interaction.response.send_message("No matches recorded yet.")
        return
    lines = []
    for e in events:
        d = e["data"]
        win = d.get("winner", "?")
        dot = {"RED": "🔴", "BLU": "🔵"}.get(win, "🤝")
        label = "Draw" if win == "DRAW" else f"{win} win"
        lines.append(f"**#{e['id']}** {dot} {label} · Δ{d.get('delta', 0)} · "
                     f"{_ago(e['ts'])}")
    await interaction.response.send_message(
        "🗒️ **Recent matches** (use `/match info <id>` for detail)\n" + "\n".join(lines))


@match_group.command(name="info", description="Show the detail of one recorded match by its id.")
@app_commands.describe(match_id="The match number (see /match log)")
async def match_info_cmd(interaction: discord.Interaction, match_id: int):
    e = store.get_event(match_id)
    if e is None or e.get("kind") != "match":
        await interaction.response.send_message(f"No match #{match_id}.", ephemeral=True)
        return
    d = e["data"]
    elos = d.get("elos", {})

    def side(color):
        out = []
        for uid in d.get(color.lower(), []):
            ba = elos.get(str(uid)) or elos.get(uid)        # keys are strings after JSON
            tag = name_box(interaction.guild, uid)
            if ba:
                out.append(f"{tag} ({ba[0]}→{ba[1]})")
            else:
                out.append(tag)
        return ", ".join(out) or "—"

    win = d.get("winner", "?")
    dot = {"RED": "🔴", "BLU": "🔵", "DRAW": "🤝"}
    label = "**Draw**" if win == "DRAW" else f"**{win} win**"
    lines = [f"**Match #{e['id']}** · {dot.get(win, '')} {label} · "
             f"swing Δ{d.get('delta', 0)} · {_ago(e['ts'])}",
             f"🔴 RED: {side('RED')}",
             f"🔵 BLU: {side('BLU')}"]
    await interaction.response.send_message("\n".join(lines))


@match_group.command(name="fix", description="Admin: correct a misreported match (auto-recomputes everyone's Elo + W/L/D).")
@app_commands.describe(match_id="The match number (see /match log)", result="The correct result")
@app_commands.choices(result=[
    app_commands.Choice(name="RED win", value="red"),
    app_commands.Choice(name="BLU win", value="blu"),
    app_commands.Choice(name="Draw / Tie", value="draw"),
])
async def match_fix_cmd(interaction: discord.Interaction,
                        match_id: int, result: app_commands.Choice[str]):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    e = store.get_event(match_id)
    if e is None or e.get("kind") != "match":
        await interaction.response.send_message(f"No match #{match_id}.", ephemeral=True)
        return
    d = e["data"]
    old_winner = d.get("winner")
    new_winner = result.value.upper()
    if new_winner == old_winner:
        label = "a draw" if old_winner == "DRAW" else f"a {old_winner} win"
        await interaction.response.send_message(
            f"Match #{match_id} is already recorded as {label}.", ephemeral=True)
        return
    elos = d.get("elos", {})
    red = [int(u) for u in d.get("red", [])]
    blu = [int(u) for u in d.get("blu", [])]
    before = {}
    for u in red + blu:
        ba = elos.get(str(u)) or elos.get(u)
        if not ba:
            await interaction.response.send_message(
                f"Match #{match_id} is missing rating data — can't auto-fix.", ephemeral=True)
            return
        before[u] = ba[0]
    detail = pug.correct_match(red, blu, before, old_winner, new_winner)
    store.log_event("correction", {"match_id": match_id, "from": old_winner,
                                   "to": new_winner, "by": interaction.user.id})
    embed = results_embed(interaction.guild, match_id, detail)
    was = "Draw" if old_winner == "DRAW" else f"{old_winner} win"
    now = "Draw" if new_winner == "DRAW" else f"{new_winner} win"
    embed.title = f"Match #{match_id} corrected — {now} (was {was})"
    embed.set_footer(text="Elo and W/L/D updated automatically.")
    await interaction.response.send_message(embed=embed)


client.tree.add_command(match_group)


@client.tree.command(name="reset", description="Admin: unstick a frozen pick (re-roll captains).")
async def reset_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    pug.admin_reset()
    if pug.phase is Phase.PICKING:
        await interaction.response.send_message(embed=draft_embed(interaction.guild))
    else:
        await interaction.response.send_message(queue_display(interaction.guild))


@client.tree.command(name="forceadd", description="Admin: add one or more players to the queue (e.g. after a restart).")
@app_commands.describe(players="Mention the players to add, e.g. @a @b @c")
async def forceadd_cmd(interaction: discord.Interaction, players: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    ids = [int(m) for m in re.findall(r"<@!?(\d+)>", players)]
    if not ids:
        await interaction.response.send_message(
            "Mention at least one player, e.g. /forceadd players:@a @b", ephemeral=True)
        return
    busy = pug.slot_busy
    added, skipped = [], []
    for uid in ids:
        if cross_block_msg(uid):                   # committed in another lobby -> skip
            skipped.append(uid)
            continue
        ok, _ = pug.add(uid)                       # default 2-min auto-ready
        if ok:
            added.append(uid)
        if not busy and pug.phase not in (Phase.IDLE, Phase.QUEUING):
            break                                  # active queue just filled
    note = ""
    if skipped:
        names = ", ".join(name_box(interaction.guild, u) for u in skipped)
        note = f"\nSkipped (in a game elsewhere): {names}"
    if busy:                                        # slot occupied -> went to next queue
        await interaction.response.send_message(
            f"Added {len(added)} to the next queue.{note}\n{next_queue_display(interaction.guild)}")
    elif pug.phase is Phase.READY_CHECK:
        await interaction.response.send_message(f"Added {len(added)} — queue full, ready check:{note}")
        await launch_ready_check(interaction.channel, interaction.guild)
    elif pug.phase is Phase.PICKING:
        await interaction.response.send_message(embed=draft_embed(interaction.guild))
    else:
        await interaction.response.send_message(queue_display(interaction.guild) + note)
    await after_commit(interaction.channel, interaction.guild)


immunity_group = app_commands.Group(name="immunity", description="Admin: manage med immunity.")


@immunity_group.command(name="show", description="Show who currently has med immunity.")
async def immunity_show_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    imm = pug.immunity_list()
    if not imm:
        await interaction.response.send_message("No one has med immunity.", ephemeral=True)
        return
    lines = [f"{name_box(interaction.guild, u)} — x{n}" for u, n in imm.items()]
    await interaction.response.send_message("**Med immunity:**\n" + "\n".join(lines))


@immunity_group.command(name="set", description="Set a player's med immunity to an exact number of games.")
@app_commands.describe(player="Player", games="Games of immunity (0 to remove)")
async def immunity_set_cmd(interaction: discord.Interaction,
                           player: discord.Member, games: app_commands.Range[int, 0, 20]):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await interaction.response.send_message(pug.set_immunity(player.id, games)[1])


@immunity_group.command(name="add", description="Add (or subtract) med immunity by an amount.")
@app_commands.describe(player="Player", games="Amount to add (use a negative number to subtract)")
async def immunity_add_cmd(interaction: discord.Interaction,
                           player: discord.Member, games: app_commands.Range[int, -20, 20]):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await interaction.response.send_message(pug.add_immunity(player.id, games)[1])


@immunity_group.command(name="clear", description="Remove a player's med immunity.")
@app_commands.describe(player="Player")
async def immunity_clear_cmd(interaction: discord.Interaction, player: discord.Member):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await interaction.response.send_message(pug.clear_immunity(player.id)[1])


client.tree.add_command(immunity_group)


@client.tree.command(name="clear", description="Admin: clear the queue.")
async def clear_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await interaction.response.send_message(pug.admin_clear()[1])


# ---------- slash: info ----------
@client.tree.command(name="captstat", description="Top 10 players by times rolled as captain.")
async def captstat_cmd(interaction: discord.Interaction):
    rows = [(uid, rec) for uid, rec in SHARED_STATS.items()
            if uid not in FAKE_NAMES and rec.get("capt", 0) > 0]
    rows.sort(key=lambda r: (-r[1].get("capt", 0), -r[1].get("games", 0)))
    if not rows:
        await interaction.response.send_message(
            "No captain stats yet — play some games first!")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"{medals.get(i, f'{i}.')} {name_box(interaction.guild, uid)} — "
             f"**{rec['capt']}**× captain ({rec.get('games', 0)} games)"
             for i, (uid, rec) in enumerate(rows[:10], 1)]
    await interaction.response.send_message("🏅 **Most-rolled captains**\n" + "\n".join(lines))


@client.tree.command(name="stat", description="Show a player's Elo, record, and games.")
@app_commands.describe(player="(optional) whose stats to show — defaults to you")
async def stat_cmd(interaction: discord.Interaction, player: Optional[discord.Member] = None):
    member = player or interaction.user
    rec = SHARED_STATS.get(member.id) or {}
    g = rec.get("games", 0)
    if g == 0:
        empty = discord.Embed(
            title=f"📊 {display_name(interaction.guild, member.id)}",
            description="Hasn't finished any games yet.",
            color=discord.Color.light_grey())
        await interaction.response.send_message(embed=empty)
        return
    c = rec.get("capt", 0)
    w, l, dr = rec.get("w", 0), rec.get("l", 0), rec.get("d", 0)
    elo = rec.get("elo", ELO_START)
    decided = w + l
    wr = f"{round(100 * w / decided)}%" if decided else "—"
    capt_val = f"{c}  ·  {round(100 * c / g)}% of games" if c else "—"

    # Elo rank among everyone who's played (bots excluded)
    ranked = sorted(
        (u for u, r in SHARED_STATS.items()
         if u not in FAKE_NAMES and r.get("games", 0) > 0),
        key=lambda u: (-SHARED_STATS[u].get("elo", ELO_START),
                       -SHARED_STATS[u].get("games", 0)))
    rank = ranked.index(member.id) + 1 if member.id in ranked else None

    embed = discord.Embed(
        title=f"📊 {display_name(interaction.guild, member.id)}",
        color=discord.Color.blurple())
    if member.id not in FAKE_NAMES:
        embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Elo", value=f"**{elo}**", inline=True)
    embed.add_field(name="Record", value=f"{w}W – {l}L – {dr}D", inline=True)
    embed.add_field(name="Win rate", value=wr, inline=True)
    embed.add_field(name="Games", value=str(g), inline=True)
    embed.add_field(name="Captain", value=capt_val, inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)   # keep the 3-col grid tidy
    if rank is not None:
        embed.set_footer(text=f"Elo rank #{rank} of {len(ranked)}")
    await interaction.response.send_message(embed=embed)


elo_group = app_commands.Group(name="elo", description="Ratings.")


@elo_group.command(name="top", description="Top 10 players by Elo rating.")
async def elo_top_cmd(interaction: discord.Interaction):
    rows = [(uid, rec) for uid, rec in SHARED_STATS.items()
            if uid not in FAKE_NAMES and rec.get("games", 0) > 0]
    rows.sort(key=lambda r: (-r[1].get("elo", ELO_START), -r[1].get("games", 0)))
    if not rows:
        await interaction.response.send_message(
            "No rated games yet — play some games first!")
        return
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"{medals.get(i, f'{i}.')} {name_box(interaction.guild, uid)} — "
             f"**{rec.get('elo', ELO_START)}** Elo "
             f"({rec.get('w', 0)}W–{rec.get('l', 0)}L)"
             for i, (uid, rec) in enumerate(rows[:10], 1)]
    await interaction.response.send_message("📈 **Top Elo**\n" + "\n".join(lines))


@elo_group.command(name="set", description="Admin: set a player's Elo to an exact value.")
@app_commands.describe(player="Player", rating="New Elo rating")
async def elo_set_cmd(interaction: discord.Interaction,
                      player: discord.Member, rating: app_commands.Range[int, 0, 5000]):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    ok, msg, before, after = pug.set_elo(player.id, rating)
    store.log_event("elo_adjust", {"mode": "set", "uid": player.id,
                                   "by": interaction.user.id,
                                   "before": before, "after": after})
    await interaction.response.send_message(msg)


@elo_group.command(name="add", description="Admin: nudge a player's Elo up or down.")
@app_commands.describe(player="Player", amount="Amount to add (use a negative number to subtract)")
async def elo_add_cmd(interaction: discord.Interaction,
                      player: discord.Member, amount: app_commands.Range[int, -2000, 2000]):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    ok, msg, before, after = pug.add_elo(player.id, amount)
    store.log_event("elo_adjust", {"mode": "add", "uid": player.id,
                                   "by": interaction.user.id, "amount": int(amount),
                                   "before": before, "after": after})
    await interaction.response.send_message(msg)


client.tree.add_command(elo_group)


@client.tree.command(name="tosscoin", description="Flip a coin — heads or tails.")
async def tosscoin_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"🪙 **{random.choice(('Heads', 'Tails'))}**")


@client.tree.command(name="promote", description="Ping the pugger role with how many more players are needed.")
async def promote_cmd(interaction: discord.Interaction):
    if pug.slot_busy:
        await interaction.response.send_message(
            "A game is already forming or live — nothing to promote.", ephemeral=True)
        return
    needed = QUEUE_SIZE - len(pug.queue_ids())
    if needed <= 0:
        await interaction.response.send_message("The queue is already full.", ephemeral=True)
        return
    key = _cur_key.get()
    remaining = PROMOTE_COOLDOWN - (time.monotonic() - _last_promote.get(key, 0.0))
    if remaining > 0:
        await interaction.response.send_message(
            f"`/promote` is on cooldown — try again in {int(remaining) + 1}s.", ephemeral=True)
        return
    _last_promote[key] = time.monotonic()
    role = discord.utils.find(
        lambda r: r.name.lower() == PUG_PING_ROLE.lower(), interaction.guild.roles)
    mention = role.mention if role else f"@{PUG_PING_ROLE}"
    await interaction.response.send_message(
        f"{mention} There are **{needed}** more player(s) required to start the 6v6 pug. "
        "Type `/add` or `++` to join!",
        allowed_mentions=discord.AllowedMentions(roles=True))


@client.tree.command(name="queue", description="Show the queue, or teams if a game is on.")
async def queue_cmd(interaction: discord.Interaction):
    g = interaction.guild
    nxt = f"\n\n{next_queue_display(g)}" if pug.next_queue_ids() else ""
    if pug.phase is Phase.PICKING:
        await interaction.response.send_message(embed=draft_embed(g),
                                                content=(next_queue_display(g) if nxt else None))
    elif pug.phase is Phase.LIVE:
        await interaction.response.send_message(embed=final_embed(g),
                                                content=(next_queue_display(g) if nxt else None))
    else:
        await interaction.response.send_message(queue_display(g) + nxt)


@client.tree.command(name="commands", description="Show the command list.")
async def commands_cmd(interaction: discord.Interaction):
    text = (
        "**TF2 PUG — commands**\n"
        "`/add` or `++` — join (2-min auto-ready)\n"
        "`/ar` or `!ar` — join (15-min auto-ready)\n"
        "`/auto-ready <min>` — join (custom, max 30 min)\n"
        "`/leave` or `--` — leave the queue\n"
        "`/aroff` — turn off your auto-ready\n"
        "`/ready` — confirm in a ready check (or click the button)\n"
        "`/queue` — show queue or teams\n"
        "`/promote` — ping the pugger role for more players\n"
        "`/tosscoin` — flip a coin (heads/tails)\n"
        "`/captstat` — top 10 most-rolled captains · `/elo top` — top 10 by rating\n"
        "`/stat [@user]` — a player's Elo, W/L, win rate + captaincies\n"
        "`/capfor red|blu` — volunteer to captain · `/capoff` — step down\n"
        "`/pick @user` — draft a player\n"
        "`/subme` — request a sub · `/subfor` — sub in (draft stage)\n"
        "`/match report red|blu|draw` — report the result of the live game (captain/admin)\n"
        "`/match cancel` — admin: end a match with no result (void a live game or scrap a forming one)\n"
        "`/match log` · `/match info <id>` — browse recorded matches\n"
        "`/match fix <id> red|blu|draw` — admin: correct a misreported match\n"
        "`/match put @player red|blu|capt red|capt blu|bench` — admin: move a player to a team, a captain slot, or the bench\n"
        "`/match start` — admin: force a fully-set draft to go live\n"
        "`/immunity show|set|add|clear` — admin: manage med immunity\n"
        "`/elo set|add @player` — admin: correct a rating\n"
        "`/reset` · `/clear` · `/forceadd` — admin\n"
        "\n*While a game is live, new joins line up in the **next queue** and start once it's reported.*"
    )
    await interaction.response.send_message(text, ephemeral=True)


# ---------- background ----------
@tasks.loop(minutes=1)
async def timeout_sweep():
    for key, st in list(lobbies.items()):
        ch = _lobby_channels.get(key)
        _cur_key.set(key)
        dropped = st.sweep_timeouts()
        if dropped and ch:
            names = ", ".join(f"<@{uid}>" for uid in dropped)   # real ping so they're notified
            hours = TIMEOUT_SECONDS // 3600
            await ch.send(f"{names} were removed from all queues (idle {hours}h).")


@tasks.loop(seconds=5)
async def autosave():
    persist()


# ---------- debug harness (only registered when PUG_DEBUG=1) ----------
DEBUG = os.environ.get("PUG_DEBUG") == "1"

if DEBUG:
    FAKE_BASE = 900000

    def _next_fake_id():
        """Smallest fake id not already in use (active queue, next queue, or
        named), so repeated /fill calls never collide with bots already added."""
        used = set(pug.queue) | set(pug.next_queue) | set(FAKE_NAMES)
        fid = FAKE_BASE
        while fid in used:
            fid += 1
        return fid

    @client.tree.command(name="fill",
                         description="DEBUG: add fake players (to the next queue if a game is forming/live).")
    @app_commands.describe(count="How many fake players to add (1-12)",
                           ready="Auto-confirm them (True) or leave them waiting (False)")
    async def fill_cmd(interaction: discord.Interaction,
                       count: app_commands.Range[int, 1, 12], ready: bool = True):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        ar = AR_COMMAND_SECONDS if ready else -1   # -1 => already expired => "waiting"
        target_next = pug.slot_busy                # game forming/live -> bots go to the NEXT queue
        added = 0
        for _ in range(count):
            fid = _next_fake_id()
            FAKE_NAMES[fid] = f"Bot{fid - FAKE_BASE + 1}"
            ok, _ = pug.add(fid, ar)
            if not ok:                             # queue/next queue full -> undo the label and stop
                FAKE_NAMES.pop(fid, None)
                break
            added += 1
            if not target_next and pug.phase is not Phase.QUEUING:
                break                              # active queue just filled -> don't spill into next
        if target_next:
            await interaction.response.send_message(
                f"Filled {added} bot(s) into the **next queue** "
                f"({len(pug.next_queue)}/{QUEUE_SIZE}).")
        elif pug.phase is Phase.READY_CHECK:
            await interaction.response.send_message(f"Filled {added} bots — ready check:")
            await launch_ready_check(interaction.channel, interaction.guild)
        elif pug.phase is Phase.PICKING:
            await interaction.response.send_message(embed=draft_embed(interaction.guild))
        else:
            await interaction.response.send_message(queue_display(interaction.guild))
        await after_commit(interaction.channel, interaction.guild)

    @client.tree.command(name="forceready", description="DEBUG: mark everyone ready (skip the timer).")
    async def forceready_cmd(interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if pug.phase is not Phase.READY_CHECK:
            await interaction.response.send_message("No ready check active.", ephemeral=True)
            return
        for u in pug.queue_ids():
            pug.mark_ready(u)
        if pug.phase is Phase.PICKING:
            await interaction.response.send_message(embed=draft_embed(interaction.guild))
        else:
            await interaction.response.send_message(ready_menu(interaction.guild))

    @client.tree.command(name="autopick", description="DEBUG: let the bots play their draft turns.")
    async def autopick_cmd(interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if pug.phase is not Phase.PICKING:
            await interaction.response.send_message("Not in the draft stage.", ephemeral=True)
            return
        # claim sides for any FAKE captains; leave a human captain to /capfor themselves
        for cap in pug.captains:
            if cap in FAKE_NAMES and cap not in pug.capt_of.values():
                pug.capfor(cap, "red" if "RED" not in pug.capt_of else "blu")
        # auto-pick only while a FAKE captain is on the clock
        while (pug.phase is Phase.PICKING and pug._both_capts_set
               and pug._current_picker() in FAKE_NAMES):
            pool = pug.unpicked()
            if not pool:
                break
            pug.pick(pug._current_picker(), random.choice(pool))
        if pug.phase is Phase.LIVE:
            await interaction.response.send_message(embed=final_embed(interaction.guild))
        else:
            await interaction.response.send_message(embed=draft_embed(interaction.guild))

    @client.tree.command(name="botcap", description="DEBUG: make one bot claim a captain slot (single step).")
    @app_commands.describe(team="(optional) which side the bot should take")
    @app_commands.choices(team=[
        app_commands.Choice(name="RED", value="red"),
        app_commands.Choice(name="BLU", value="blu"),
    ])
    async def botcap_cmd(interaction: discord.Interaction,
                         team: Optional[app_commands.Choice[str]] = None):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if pug.phase is not Phase.PICKING:
            await interaction.response.send_message("Not in the draft stage.", ephemeral=True)
            return
        if pug._both_capts_set:
            await interaction.response.send_message("Both sides already have captains.", ephemeral=True)
            return
        # prefer a rolled bot captain; otherwise any bot in the match
        candidates = [c for c in pug.captains if c in FAKE_NAMES and c not in pug.capt_of.values()]
        if not candidates:
            candidates = [u for u in pug.queue_ids()
                          if u in FAKE_NAMES and u not in pug.capt_of.values()]
        if not candidates:
            await interaction.response.send_message("No bot available to captain.", ephemeral=True)
            return
        color = team.value if team else ("red" if "RED" not in pug.capt_of else "blu")
        pug.capfor(candidates[0], color)
        await interaction.response.send_message(embed=draft_embed(interaction.guild))

    @client.tree.command(name="botpick", description="DEBUG: the on-clock bot captain picks ONE player.")
    async def botpick_cmd(interaction: discord.Interaction):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if pug.phase is not Phase.PICKING or not pug._both_capts_set:
            await interaction.response.send_message("Not in the picking stage yet.", ephemeral=True)
            return
        picker = pug._current_picker()
        if picker not in FAKE_NAMES:
            await interaction.response.send_message(
                f"It's <@{picker}>'s turn — use /pick.", ephemeral=True)
            return
        pool = pug.unpicked()
        if not pool:
            await interaction.response.send_message("No players left to pick.", ephemeral=True)
            return
        pug.pick(picker, random.choice(pool))
        if pug.phase is Phase.LIVE:
            await interaction.response.send_message(embed=final_embed(interaction.guild))
        else:
            await interaction.response.send_message(embed=draft_embed(interaction.guild))

    @client.tree.command(name="pickname", description="DEBUG: pick by (fuzzy) name — for picking bots you can't @mention.")
    @app_commands.describe(name="Part of the player's name (e.g. 'bot3', or 'micha' for Michael)")
    async def pickname_cmd(interaction: discord.Interaction, name: str):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if pug.phase is not Phase.PICKING or not pug._both_capts_set:
            await interaction.response.send_message("Not in the picking stage yet.", ephemeral=True)
            return
        target = fuzzy_unpicked(name, interaction.guild)
        if target is None:
            await interaction.response.send_message(
                f"No unpicked player matches “{name}”.", ephemeral=True)
            return
        # pick as the caller -> the turn guard rejects if it isn't their turn
        ok, msg = pug.pick(interaction.user.id, target)
        if not ok:
            await interaction.response.send_message(msg, ephemeral=True)
            return
        if pug.phase is Phase.LIVE:
            await interaction.response.send_message(embed=final_embed(interaction.guild))
        else:
            await interaction.response.send_message(embed=draft_embed(interaction.guild))


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN env var first.")
    client.run(token)
