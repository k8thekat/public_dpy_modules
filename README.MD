# **Discord.py COGS/Extensions**
___
## Introduction
___
A collection of cogs I have created for Discord using [dpy](https://github.com/Rapptz/discord.py), loaded by [Kuma_Kuma](https://github.com/k8thekat/Kuma_Kuma).

Extensions in this directory are auto-discovered at startup via `iter_modules` (see `__init__.py`). Any file prefixed with `_` (e.g. `_eval_cog.py`, `_tag_cog.py`) is **skipped**, which is how disabled/archived cogs are kept around. The `extensions/private/` directory is appended to the search path, so private cogs load the same way.

Most cogs inherit from `utils.cog.KumaCog`, giving them shared access to the bot's database pool, cached HTTP session, `KumaEmoji` resources, and embed helpers.

___
## Extensions
___

### AutoMod Extension (`automod.py`):
- Mention-spam protection built on top of Discord's **native AutoMod** with custom escalation logic.
- Treats recently-joined members as "new" and applies stricter handling to them.

### Claude Extension (`claude.py`):
- Bridges Discord and the **Claude Code CLI** — send prompts, receive chunked embed responses, and continue conversations via a Reply button/modal.
- All subprocess calls are locked to the project directory (no path traversal).

### FFXIV Extension (`ffxiv.py`):
- Final Fantasy XIV item lookup and marketboard tooling backed by [moogles_intuition](https://github.com/k8thekat/moogles_intuition) (XIVDataMining, TeamCraft, GarlandTools, Universalis, FF14Angler).
- Per-user settings (datacenter/world) and item **watch lists** with price min/max stored in the database.
- Embeds using custom FFXIV application emojis.

### Gatekeeper Extension (`gatekeeper.py`):
- Manages CubeCoders **AMP** instances via [AMPAPI_Python](https://github.com/k8thekat/AMPAPI_Python).
- Server whitelisting (add/remove/on/off), console/chat channel bridging, and instance control from Discord.

### Moderator Extension (`moderator.py`):
- Support for `who_is` to look up Discord ID's if the Bot can locate the user.
- Support for `clear` to remove Discord Messages, with support of a Message ID to remove all messages up to that Message ID in history.
- Support for `trust` to add or remove Discord Members who become apart of the Owner IDs list of the bot.
- Support for `prefixes` where you can add/set and clear prefixes on a per Discord Guild basis.
- Support for `sync` command to sync commands to a specific Guild or Globally.
    - Also supports resetting a command tree for an entire Guild.
- Per-guild moderator settings and spam-message hash tracking.

### Reddit Extension (`reddit.py`):
- Scrapes specified subreddits for Images and posts them to a specific channel via webhooks.
    - Supports duplicate image validation in a hash db (`reddit_array.bin` / `reddit.json`).
    - Optional compiled helper for byte-level edge comparisons; degrades to hash-only checks when unavailable.

### REPL Extension (`repl_cog.py`):
- Basic developer extension to run python code inside Discord among other small features.
- Session-based REPL tied to a user/channel, plus source links to [public_dpy_modules](https://github.com/k8thekat/public_dpy_modules).

### Utility Extension (`utility.py`):
- General-purpose helper commands.
- `yoink` — steal emojis/stickers from messages and upload them to a guild you pick via a select menu.

___
## Disabled/Archived (prefixed with `_`)
___
- `_autorole_cog.py` — reaction/auto role assignment.
- `_eval_cog.py` — older code-eval cog (superseded by `repl_cog.py`).
- `_ffxiv_old.py` — previous FFXIV implementation, kept for reference.
- `_tag_cog.py` — tag/snippet storage.
