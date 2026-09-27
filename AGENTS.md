# AGENTS.md — TF2 PUG Bot

Context for AI agents working in this repo. Keep it current when the architecture
changes. For a full feature/command tour, see [README.md](README.md).

## What this is

A Discord bot that runs 6v6 TF2 pickup games: queue → ready check → captain draft
→ live game → report. State lives in memory and is snapshotted to SQLite so a
restart resumes the active game. The bot can run **multiple fully isolated Discord
servers** from one process.

## Files

| File | Role |
|---|---|
| [pug_state.py](pug_state.py) | Pure game state machine. **No `discord` import.** One `PugState` class; every command is a method returning `(ok, msg)`. Guild-agnostic and unit-testable. |
| [bot.py](bot.py) | The Discord layer: slash commands, `++`/`--`/`!ar` text shortcuts, buttons, timers, rendering. Holds no game rules — it maps Discord I/O onto `PugState`. |
| [lobby.py](lobby.py) | Pure cross-lobby coordination (one-game-at-a-time). Operates on a `{key: PugState}` map, no Discord import. |
| [storage.py](storage.py) | Tiny SQLite layer: one-row JSON snapshot + append-only `events` audit log. |

**Golden rule:** game rules go in `pug_state.py`; Discord/rendering goes in
`bot.py`. Don't leak `discord` into the state layer.

## Testing

Each pure module has a smoke suite in its `__main__` (run with the `py` launcher
on this Windows box — bare `python` is a Store shim):

```
py pug_state.py   # cases A–EE: full rule set
py lobby.py       # cross-lobby one-game-at-a-time
py storage.py     # snapshot + audit log
```

`bot.py` needs `discord.py` (installed) but a real token to actually run; validate
edits with `py -m py_compile bot.py` and, for logic, by importing `bot` under a
fake env and poking helpers (see how the multi-guild wiring is exercised).

## Multi-server architecture (per-guild isolation)

Every Discord server the bot serves is **fully isolated**: its own SQLite file,
stats, med immunity, backup channel, rally-ping role, and rule flags. This is
modeled by `GuildCtx` in `bot.py`.

- **`DEFAULT_CTX`** — server 1 / the original, configured by the classic env vars
  (`PUG_DB`, `PUG_CHANNEL_ID`, `BACKUP_CHANNEL_ID`, `CONNECT_CHANNEL_ID`,
  `PUG_PING_ROLE`). Elo on, no tiers. Left untouched when adding more servers.
- **`PUG_GUILDS`** — a JSON list env var, one object per extra server. Keys:
  `guild_id`, `channels` (each an independent lobby), `db`, `backup_channel_id`,
  `connect_channel_id`, `ping_role`, `admin_roles` (role names granting admin,
  besides the Administrator perm), `tiered` (bool), `elo` (bool). Its channels
  are auto-added to the locked channel set.

Routing:

- `lobbies` maps a lobby key (channel id, or `_SINGLE`) → `PugState`.
- `_cur_key` (a `ContextVar`) holds the active lobby for the current async task;
  the `pug` proxy resolves to `lobbies[_cur_key.get()]`.
- `ctx()` → the `GuildCtx` for the current lobby; `ctx_for_key(key)` for a
  specific one; `guild_lobbies(ctx)` → that server's lobby subset.
- A lobby's `PugState.immunity`/`.stats` point at its `GuildCtx` dicts, so those
  are shared **within** a server, isolated **between** servers.

Cross-server rules are scoped per `GuildCtx`:

- `reconcile()` and `cross_block_msg()` operate on `guild_lobbies(ctx)`, so
  one-game-at-a-time is enforced within a server only — never across servers.
- Persistence is per store: `persist()` / `setup_hook` load / `close()` iterate
  `all_ctxs`, each reading/writing its own DB file.
- `is_admin()` checks the current guild's `admin_roles` (default `["PUG Admin"]`)
  or the Administrator permission.

## Server-2 rule flags

- **Tiered draft (`tiered: true`)** — a hard restriction derived from Discord
  roles: a higher tier can't be picked while any lower-tier player is unpicked.
  See `TIER_ROLES` / `tier_of()` / `tier_block()`. Tier 1 = Seed/Sprout
  (unrestricted), Tier 2 = Tree, Tier 3 = Elder Tree. A player with none or
  multiple tier roles falls back to `TIER_FALLBACK` (Tier 1). Enforced in
  `pick_cmd` (and the debug `pickname`). Server 1 has no concept of tiers.
- **Elo off (`elo: false`)** — W/L/D, win rate, and captain counts stay; only the
  Elo number/ranking is dropped. `pug_state.py` still computes Elo internally
  (it's guild-agnostic), so `bot.py` snapshots and **restores** ratings around
  `match_report` / `match fix` to freeze them, hides Elo on the draft board,
  go-live card, `/stat`, and match log/info, and disables `/elo top|bottom|set|add`.

## Conventions

- Tunable rules live as constants at the top of `pug_state.py`; Discord-facing
  config (roles, tiers, cooldowns, guild config) at the top of `bot.py`.
- Keep changes minimal and match the existing terse, single-line comment style.
- Don't create markdown docs for changes unless asked.
