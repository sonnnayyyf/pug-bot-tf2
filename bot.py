"""
Discord layer for the TF2 PUG bot — slash commands + ++/-- and !ar shortcuts.

Run:  DISCORD_TOKEN=xxx [TEST_GUILD_ID=123] python bot.py   (discord.py >= 2.3)

All game logic is in pug_state.py. This file maps Discord I/O + timing onto it.
Display: rosters show server display names in `code boxes` (no pings); the
auto-ready confirmation is a green "Success" embed. Real @mentions are kept
only where a ping is the point (the player on the clock, match cancellation).
"""

import os
import asyncio
import random
import difflib
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from pug_state import (PugState, Phase, QUEUE_SIZE,
                       DEFAULT_AR_SECONDS, AR_COMMAND_SECONDS,
                       MAX_AR_SECONDS, READY_CHECK_SECONDS, TIMEOUT_SECONDS)

# Load DISCORD_TOKEN / TEST_GUILD_ID / PUG_DEBUG from a local .env file if
# python-dotenv is installed; otherwise fall back to shell environment vars.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ADMIN_ROLE = "PUG Admin"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

pug = PugState()
_last_channel = None
_active_ready_view = None


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


def draft_embed(guild) -> discord.Embed:
    dot = {"RED": "🔴", "BLU": "🔵"}
    lines = []
    for color in ("RED", "BLU"):
        capt = pug.capt_of.get(color)
        members = ([capt] + pug.team[color]) if capt else pug.team[color]
        lines.append(f"{dot[color]} **{color}** ⟨{len(pug.team[color])}⟩")
        if members:
            lines.append("[ " + " / ".join(player_tag(guild, u) for u in members) + " ]")
        else:
            lines.append("[ empty ]")
    lines.append("")
    if not pug._both_capts_set:
        caps = " and ".join(player_tag(guild, u) for u in pug.captains)
        word = "captain" if len(pug.captains) == 1 else "captains"
        lines.append(f"{caps} have been rolled as {word}")
    up = pug.unpicked()
    if up:
        lines.append("**Unpicked:**")
        for u in up:
            imm = pug.immunity.get(u)
            lines.append(player_tag(guild, u) + (f" — **IMMUNE: x{imm}**" if imm else ""))
    lines.append("—")
    if not pug._both_capts_set:
        lines.append("Type **/capfor red** or **/capfor blu** to captain a team "
                     "(anyone may volunteer). **/capoff** to step down.")
    elif pug.phase is Phase.PICKING:
        lines.append(f"{player_tag(guild, pug._current_picker())} to pick — /pick @player")
    return discord.Embed(title="6v6 is now on the draft stage!",
                         description="\n".join(lines),
                         color=discord.Color.blurple())


def final_embed(guild) -> discord.Embed:
    dot = {"RED": "🔴", "BLU": "🔵"}
    lines = []
    for color in ("RED", "BLU"):
        members = [pug.capt_of.get(color)] + pug.team[color]
        lines.append(f"{dot[color]} **{color}**")
        lines.append("[ " + " / ".join(player_tag(guild, u) for u in members if u) + " ]")
    lines.append("—")
    lines.append("Admins: /match report to end or cancel.")
    return discord.Embed(title="Teams set — GLHF!",
                         description="\n".join(lines),
                         color=discord.Color.green())


# ---------- ready-check UI ----------
class ReadyView(discord.ui.View):
    def __init__(self, channel, guild):
        super().__init__(timeout=None)   # we run our own hard 60s deadline
        self.channel = channel
        self.guild = guild
        self.message = None
        self.deadline = None

    async def start(self):
        self.message = await self.channel.send(ready_menu(self.guild), view=self)
        self.deadline = asyncio.create_task(self._expire())

    async def _expire(self):
        try:
            await asyncio.sleep(READY_CHECK_SECONDS)
        except asyncio.CancelledError:
            return
        if pug.phase is not Phase.READY_CHECK:
            return
        dropped = pug.resolve_ready_check()
        self.stop()
        names = " / ".join(name_box(self.guild, u) for u in dropped) or "nobody"
        await self.message.edit(
            content=f"**Ready check failed.** Dropped (expire time ran off): {names}",
            view=None)
        await self.channel.send(queue_display(self.guild))

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
            await interaction.response.edit_message(content="**All players ready!**", view=None)
            await self.channel.send(embed=draft_embed(self.guild))
        else:
            await interaction.response.edit_message(content=ready_menu(self.guild), view=self)

    @discord.ui.button(label="Abort", style=discord.ButtonStyle.danger, emoji="✖️")
    async def abort_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, sig, dropped = pug.abort_ready_check(interaction.user.id)
        if not ok:
            await interaction.response.send_message(sig, ephemeral=True)
            return
        if self.deadline:
            self.deadline.cancel()
        self.stop()
        await interaction.response.edit_message(
            content=f"**Ready check aborted** — {name_box(self.guild, interaction.user.id)} "
                    "left the queue.",
            view=None)
        await self.channel.send(queue_display(self.guild))


async def launch_ready_check(channel, guild):
    global _active_ready_view
    _active_ready_view = ReadyView(channel, guild)
    await _active_ready_view.start()


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
class PugClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
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
        if not timeout_sweep.is_running():
            timeout_sweep.start()
        print(f"Logged in as {self.user}")

    async def on_message(self, message):
        global _last_channel
        if message.author.bot:
            return
        _last_channel = message.channel
        text = message.content.strip().lower()
        if text == "++":
            await self._text_add(message, DEFAULT_AR_SECONDS)
        elif text == "!ar":
            await self._text_add(message, AR_COMMAND_SECONDS)
        elif text == "--":
            ok, msg = pug.remove(message.author.id)
            await message.channel.send(queue_display(message.guild) if ok else msg)

    async def _text_add(self, message, ar):
        was_queued = message.author.id in pug.queue
        ok, msg = pug.add(message.author.id, ar)
        if not ok:
            await message.channel.send(msg)
            return
        async def confirm(content=None, embed=None):
            await message.channel.send(content=content, embed=embed)
        await announce_after_add(message.channel, message.guild, ar, confirm,
                                 reprint=not was_queued)


client = PugClient()


# ---------- slash: joining ----------
async def _slash_add(interaction: discord.Interaction, ar: int):
    global _last_channel
    _last_channel = interaction.channel
    was_queued = interaction.user.id in pug.queue
    ok, msg = pug.add(interaction.user.id, ar)
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    async def confirm(content=None, embed=None):
        await interaction.response.send_message(content=content, embed=embed)
    await announce_after_add(interaction.channel, interaction.guild, ar, confirm,
                             reprint=not was_queued)


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
    await interaction.response.send_message(
        queue_display(interaction.guild) if ok else msg, ephemeral=not ok)


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


@match_group.command(name="report", description="End a live game (captain/admin) or cancel a forming match (admin).")
async def match_report_cmd(interaction: discord.Interaction):
    ok, msg, pinged = pug.match_report(interaction.user.id, is_admin(interaction.user))
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    mentions = ", ".join(f"<@{u}>" for u in pinged)   # cancellation SHOULD ping
    await interaction.response.send_message(f"{mentions} {msg}" if mentions else msg)


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
    global _last_channel
    _last_channel = interaction.channel
    ids = [int(m) for m in re.findall(r"<@!?(\d+)>", players)]
    if not ids:
        await interaction.response.send_message(
            "Mention at least one player, e.g. /forceadd players:@a @b", ephemeral=True)
        return
    added = []
    for uid in ids:
        ok, _ = pug.add(uid)                       # default 2-min auto-ready
        if ok:
            added.append(uid)
        if pug.phase not in (Phase.IDLE, Phase.QUEUING):   # queue filled -> stop
            break
    if pug.phase is Phase.READY_CHECK:
        await interaction.response.send_message(f"Added {len(added)} — queue full, ready check:")
        await launch_ready_check(interaction.channel, interaction.guild)
    elif pug.phase is Phase.PICKING:
        await interaction.response.send_message(embed=draft_embed(interaction.guild))
    else:
        await interaction.response.send_message(queue_display(interaction.guild))


@client.tree.command(name="clear", description="Admin: clear the queue.")
async def clear_cmd(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("Admins only.", ephemeral=True)
        return
    await interaction.response.send_message(pug.admin_clear()[1])


# ---------- slash: info ----------
@client.tree.command(name="queue", description="Show the queue, or teams if a game is on.")
async def queue_cmd(interaction: discord.Interaction):
    g = interaction.guild
    if pug.phase is Phase.PICKING:
        await interaction.response.send_message(embed=draft_embed(g))
    elif pug.phase is Phase.LIVE:
        await interaction.response.send_message(embed=final_embed(g))
    else:
        await interaction.response.send_message(queue_display(g))


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
        "`/capfor red|blu` — volunteer to captain · `/capoff` — step down\n"
        "`/pick @user` — draft a player\n"
        "`/subme` — request a sub · `/subfor` — sub in (draft stage)\n"
        "`/match report` — end/cancel a match\n"
        "`/reset` · `/clear` · `/forceadd` — admin"
    )
    await interaction.response.send_message(text, ephemeral=True)


# ---------- background ----------
@tasks.loop(minutes=1)
async def timeout_sweep():
    dropped = pug.sweep_timeouts()
    if dropped and _last_channel:
        names = ", ".join(f"<@{uid}>" for uid in dropped)   # real ping so they're notified
        hours = TIMEOUT_SECONDS // 3600
        await _last_channel.send(f"{names} were removed from all queues (idle {hours}h).")


# ---------- debug harness (only registered when PUG_DEBUG=1) ----------
DEBUG = os.environ.get("PUG_DEBUG") == "1"

if DEBUG:
    FAKE_BASE = 900000

    @client.tree.command(name="fill", description="DEBUG: add fake players to the queue.")
    @app_commands.describe(count="How many fake players to add (1-12)",
                           ready="Auto-confirm them (True) or leave them waiting (False)")
    async def fill_cmd(interaction: discord.Interaction,
                       count: app_commands.Range[int, 1, 12], ready: bool = True):
        if not is_admin(interaction.user):
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        global _last_channel
        _last_channel = interaction.channel
        ar = AR_COMMAND_SECONDS if ready else -1   # -1 => already expired => "waiting"
        added = 0
        for i in range(count):
            fid = FAKE_BASE + i
            FAKE_NAMES[fid] = f"Bot{i+1}"
            ok, _ = pug.add(fid, ar)
            if ok:
                added += 1
            if pug.phase is not Phase.QUEUING:     # filled -> ready check / draft
                break
        if pug.phase is Phase.READY_CHECK:
            await interaction.response.send_message(f"Filled {added} bots — ready check:")
            await launch_ready_check(interaction.channel, interaction.guild)
        elif pug.phase is Phase.PICKING:
            await interaction.response.send_message(embed=draft_embed(interaction.guild))
        else:
            await interaction.response.send_message(queue_display(interaction.guild))

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
