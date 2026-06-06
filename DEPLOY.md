# Deploying the TF2 PUG bot 24/7

The bot only runs while its process is alive. To keep it up around the clock you
need an always-on Linux host plus a process manager that restarts it on crash
and on server reboot. This guide uses a small cloud VM + `systemd`.

## 0. Push the current code first (from a personal machine)

The work laptop's push hook only allows `gitlab.com/shopback/*`, so commit and
push the latest code from a **personal machine**:

```bash
git add -A && git commit -m "deploy" && git push origin main
```

The server will `git pull` from GitHub — that's unaffected by the work-laptop
hook (the hook only blocks *pushes* from that machine).

## 1. Get a host

- **Free, forever:** Oracle Cloud **Always Free** — an Ampere A1 (ARM) instance,
  up to 4 cores / 24 GB RAM, no expiry. Pick the **Singapore** region for low
  latency. A credit card is required for identity check but you won't be charged
  on Always Free resources. (Capacity for A1 can be spotty; retry "Create" if it
  says "out of host capacity".)
- **Paid, simplest:** any small VPS (e.g. a 1 vCPU / 1 GB box) for a few dollars
  a month. This bot's footprint is tiny; the smallest tier is plenty.

Create the VM with Ubuntu and add your SSH key, then SSH in.

## 2. Install Python + clone

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone https://github.com/sonnnayyyf/pug-bot-tf2.git
cd pug-bot-tf2
python3 -m venv .venv
.venv/bin/pip install -U "discord.py>=2.3" python-dotenv
```

## 3. Create the .env (on the server only — never commit it)

```bash
cat > .env <<'EOF'
DISCORD_TOKEN=your-bot-token-here
TEST_GUILD_ID=your-server-id        # instant slash-command sync for one guild
PUG_CHANNEL_ID=your-channel-id      # lock the bot to one channel (optional)
EOF
```

Leave `PUG_CHANNEL_ID` out to let the bot work in every channel. To lock it to a
single channel, turn on Discord Developer Mode (User Settings > Advanced), then
right-click the channel > Copy Channel ID and paste it in.

Leave `PUG_DEBUG` out (debug commands stay off in production).

Quick sanity check before daemonising:

```bash
.venv/bin/python bot.py      # should print "Logged in as ..."; Ctrl-C to stop
```

## 4. Run it under systemd

Copy `tf2pug.service` to systemd, editing `User` and the two paths inside if you
didn't clone to `/home/ubuntu/pug-bot-tf2`:

```bash
sudo cp tf2pug.service /etc/systemd/system/tf2pug.service
sudo systemctl daemon-reload
sudo systemctl enable --now tf2pug      # start now + on every boot
systemctl status tf2pug                 # check it's running
journalctl -u tf2pug -f                 # live logs
```

`Restart=always` brings it back if it crashes; `enable` brings it back after a
reboot. That's your 24/7.

## 5. Discord portal checklist (one-time)

In the Developer Portal → your app → Bot:
- Enable **Message Content Intent** (for `++` / `--` / `!ar`).
- Enable **Server Members Intent** (for username + skill-role lookups).
- Make sure the **Puggers** ping role is "Allow anyone to @mention this role,"
  or give the bot the *Mention All Roles* permission, or `/promote` won't notify.

## 6. Updating the code after launch

```bash
# on a personal machine: edit, then
git push origin main
# on the server:
cd pug-bot-tf2 && git pull && sudo systemctl restart tf2pug
```

**Restarting no longer wipes state.** The bot snapshots its state (queue, live
match, immunity, auto-ready, next queue) to a local SQLite file (`pug.db`, beside
`bot.py`) every few seconds and on graceful shutdown, and reloads it on boot. So
a `systemctl restart` or a crash resumes the active game where it left off — a
ready check re-arms with a fresh 2:00 window, a draft re-posts the board, a live
game and its next queue come back intact. `pug.db` is gitignored; it lives only
on the server. (To wipe state deliberately, stop the bot and delete `pug.db`.)

- **Behaviour-only changes** (logic, wording): just pull + restart.
- **New or renamed slash commands:** the bot re-syncs on startup, so a restart
  registers them — instantly with `TEST_GUILD_ID` set, up to ~1h globally.
