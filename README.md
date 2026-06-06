# TF2 PUG Bot

A Discord bot that organises **6v6 Team Fortress 2 pickup games (PUGs)** — queueing, auto-ready checks, captain drafting, and a medic-immunity rotation so the same people don't get stuck medding every game.

Built with [discord.py](https://github.com/Rapptz/discord.py) (slash commands + classic `++` shortcuts). State is kept in memory and snapshotted to a small SQLite file, so restarts resume the active game rather than wiping it.

---

## Features

- **Queue** with `++` / `/add`, live roster display, and a 2-hour idle timeout.
- **Auto-ready windows** — joining confirms your participation automatically for a window (2 min default, 15 min via `!ar`, or a custom value up to 30 min). No need to babysit the queue.
- **Ready check** when the queue fills: a 2-minute confirmation with **Ready** and **Abort** buttons. Anyone whose auto-ready window is still open is confirmed instantly; anyone who doesn't ready up in time is dropped. Using `!ar` / `/ar` during a check confirms you *and* re-arms your auto-ready, so you won't have to re-click if the check restarts.
- **Captain draft** — two captains are rolled (excluding medic-immune players), but anyone in the match can volunteer with `/capfor`, and captains can step down with `/capoff`. The draft board shows each unpicked player's skill division (from their server role) and immunity. When only one player is left, they're auto-assigned so the last captain skips a no-choice pick.
- **Snake-ish pick order** (`1-2-1-1-1-1-1-1-1`) that produces balanced 6v6 teams, with the captains as the two medics.
- **Medic immunity** — whoever captains/medics a game gets immunity for their next **2** games (so they won't be forced to med again), tracked per-player, capped, and non-stacking.
- **Substitutions** during the draft stage (`/subme` to request, `/subfor` to fill).
- **Next queue** — while a game is forming or live, new joins line up in a separate queue; when the active match is reported, that queue is promoted (and starts its own ready check if it's already full). One game forms/plays at a time, with the next lined up behind it.
- **Auto-report** — a live game with no manual `/match report` auto-ends after 50 minutes (configurable), so a forgotten report can't strand the queue overnight.
- **Persistence** — all state (queue, live match, immunity, auto-ready, next queue) is snapshotted to a local SQLite file (`pug.db`) and reloaded on boot, so a restart or crash resumes the active game instead of wiping it.
- **Single-channel lock** — set `PUG_CHANNEL_ID` and the bot only responds in that one channel; commands anywhere else are ignored.
- **Rally + coin toss** — `/promote` pings a configurable role with how many more players are needed (2-min cooldown); `/tosscoin` flips heads/tails for first pick or side.
- **Admin controls** — `/match report` (end a live game or cancel a forming one), `/match put` (move a player to a team or the bench, captains included), `/immunity` (manage med immunity), `/reset` (re-roll a stuck draft), `/clear` (clears the next queue mid-game, the active queue otherwise), and `/forceadd` (rebuild a queue, e.g. after a restart).
- **Debug harness** (opt-in) for testing the full flow solo without 12 humans.

---

## How a PUG flows

The bot is a small state machine. Each command only works in the phase where it makes sense, which is what keeps the edge cases (double picks, queueing onto the next game early, picking out of turn) from happening.

```
IDLE → QUEUING → READY_CHECK → PICKING → LIVE → (report) → IDLE
```

1. **QUEUING** — players `++` until the queue reaches 12.
2. **READY_CHECK** — everyone confirms (auto-ready or the Ready button) within 2 minutes; stragglers are dropped.
3. **PICKING** — two captains claim sides and draft the rest, alternating per the pick order.
4. **LIVE** — teams are set; the game is played, then reported to reopen the queue.

---

## Commands

### Players

| Command | What it does |
|---|---|
| `++` / `/add` | Join the queue (2-min auto-ready) |
| `!ar` / `/ar` | Join with a 15-min auto-ready window |
| `/auto-ready <min>` | Join with a custom auto-ready window (max 30 min) |
| `--` / `/leave` | Leave the queue |
| `/aroff` | Turn off your auto-ready (ready up manually) |
| `/ready` | Confirm in a ready check (or just click the button) |
| `/queue` | Show the queue, or the teams if a game is on |
| `/capfor red\|blu` | Volunteer to captain a side |
| `/capoff` | Step down as captain |
| `/pick @player` | Draft a player (current picker only) |
| `/subme` | Request a substitute (draft stage only) |
| `/subfor [@player]` | Sub in for someone (draft stage only) |
| `/promote` | Ping the rally role with how many more players are needed (2-min cooldown) |
| `/tosscoin` | Flip a coin — heads or tails |
| `/commands` | Show the command list |

### Admin

| Command | What it does |
|---|---|
| `/match report` | End a live game (captain/admin) or cancel a forming match (admin) |
| `/match put @player red\|blu\|bench` | Move a player to a team or the bench (captains included — they step down) |
| `/immunity show\|set\|add\|clear` | View or adjust med immunity |
| `/reset` | Unstick a frozen draft by re-rolling captains |
| `/clear` | Clear the next queue if a game is on, otherwise the active queue |
| `/forceadd @a @b …` | Add players to the queue by mention (e.g. after a restart) |

"Admin" means a member with Discord's **Administrator** permission, or the `PUG Admin` role.

### Debug (only when `PUG_DEBUG=1`, admin-only)

`/fill`, `/forceready`, `/autopick`, `/botcap`, `/botpick`, `/pickname` — fill the queue with fake players and step through the ready check and draft solo. Hidden entirely when `PUG_DEBUG` is unset.

---

## Architecture

Two files, deliberately separated:

- **`pug_state.py`** — all game logic, with **no `discord` import**. It's a plain `PugState` class: every command is a method that checks the phase/turn/identity guards and returns `(ok, msg)`. Because it has no Discord dependency, the entire rule set is unit-testable in isolation.
- **`bot.py`** — the Discord layer. It maps slash commands, buttons, and timers onto `PugState` calls and handles all rendering (embeds, mentions, ready-check buttons). It holds no game rules of its own.

This split is the point: UI changes (slash commands, embeds, name formatting) never touch the logic, and logic changes are verified by tests that never need a live bot.

---

## Setup

Requirements: **Python 3.10+** and **discord.py ≥ 2.3**.

```bash
pip install -U "discord.py>=2.3" python-dotenv
```

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications) → **Bot** → reset and copy the token.
2. Under **Bot → Privileged Gateway Intents**, enable **Message Content Intent** (for `++` / `!ar`) and **Server Members Intent** (so display names resolve).
3. **OAuth2 → URL Generator**: scope `bot`, permissions *Send Messages* + *Read Message History*. Open the URL to invite the bot.
4. Create a role named **`PUG Admin`** and assign it to your admins.
5. For `/promote` to actually notify, create the rally role (default name **`Puggers`**) and either make it mentionable ("Allow anyone to @mention this role") or give the bot the *Mention All Roles* permission.

---

## Running

Configuration is read from environment variables, loaded from a local `.env` file (via `python-dotenv`) or from your shell.

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | yes | Your bot token |
| `TEST_GUILD_ID` | no | A server ID for **instant** slash-command sync during development. Omit for global sync (can take ~1h to propagate). |
| `PUG_CHANNEL_ID` | no | Lock the bot to a single channel (by ID). Unset = responds everywhere. |
| `CONNECT_CHANNEL_ID` | no | Channel ID shown as a clickable link on the live teams embed ("head to #connect-string"). |
| `PUG_DB` | no | Path to the SQLite state file (default `pug.db`). |
| `PUG_DEBUG` | no | Set to `1` to enable the debug commands. |

Create a `.env` file in the project root:

```
DISCORD_TOKEN=your_actual_token_here
TEST_GUILD_ID=your_server_id_here
PUG_CHANNEL_ID=your_pug_channel_id      # optional
CONNECT_CHANNEL_ID=your_connect_channel_id   # optional
```

Then run:

```bash
python bot.py

# Enable the debug tools for a run:
PUG_DEBUG=1 python bot.py
```

**Never commit your token.** The `.env` file is git-ignored (see below).

A minimal `.gitignore`:

```
.env
pug.db
pug.db-journal
pug.db-wal
__pycache__/
*.pyc
.vscode/
.DS_Store
```

---

## Configuration

Tunable constants live at the top of `pug_state.py`:

| Constant | Default | Meaning |
|---|---|---|
| `QUEUE_SIZE` | 12 | Players needed to start (6v6) |
| `TIMEOUT_SECONDS` | 7200 | Idle drop while queuing (2 hours) |
| `IMMUNITY_GAMES` | 2 | Games of med-immunity granted after medding |
| `DEFAULT_AR_SECONDS` | 120 | Auto-ready window for `++` / `/add` |
| `AR_COMMAND_SECONDS` | 900 | Auto-ready window for `!ar` / `/ar` |
| `MAX_AR_SECONDS` | 1800 | Hard cap on `/auto-ready` |
| `READY_CHECK_SECONDS` | 120 | Ready-up window once the queue fills |
| `LIVE_AUTO_REPORT_SECONDS` | 3000 | Auto-end a live game with no report (50 min) |
| `PICK_ORDER` | `1-2-1-1-1-1-1-1-1` | Per-turn pick counts (RED first) |

The rally role name (`Puggers`), skill-division roles (`Div 1/2/3`), admin role (`PUG Admin`), and `/promote` cooldown are configurable near the top of `bot.py`.

---

## Testing

`pug_state.py` ships with a smoke-test suite (cases A–U) covering the full rule set — pick-order balance, the auto-ready / ready-check paths (including arming auto-ready mid-check), medic immunity (no-stacking and exclusion from the roll), subs, captain step-down, the abort flow, the next queue (hold/promote/merge), `/match put`, scoped `/clear`, last-player auto-assign, live auto-report, and a full JSON state round-trip for persistence:

```bash
python pug_state.py
```

It drives complete games (queue → ready → draft → live) without Discord, using an injectable clock to test the time-based logic deterministically.

---

## Limitations & roadmap

- **State persists across restarts** via the SQLite snapshot, but it's a single-instance design — one bot process, one game forming/live at a time (plus one next queue). It doesn't run concurrent games.
- **No server orchestration.** This bot organises the lobby on Discord only — it doesn't touch a game server (RCON, map changes, logs); players share the connect string manually.
- Possible future additions: match history and player stats (the snapshot makes this straightforward), per-player class preferences, a captain-selection timer, and match IDs.

---

## License

Choose one (MIT is a sensible default) and add a `LICENSE` file.
