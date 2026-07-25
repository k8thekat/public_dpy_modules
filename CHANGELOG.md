# Changelog

## Spam-detection re-write, new AutoMod/Claude/Ollama/Reddit cogs and hot-reloadable extensions

#### __init__.py
- Replaced the static extension snapshot with a `discover_extensions()` helper that re-runs `iter_modules` so files added after import (including `extensions/private/`) are picked up.

#### automod.py
- ⭐ New cog. Mention-spam protection built on Discord's native AutoMod with custom escalation logic and stricter handling for recently-joined "new" members.

#### claude.py
- ⭐ New cog. Bridges Discord and the Claude Code CLI — send prompts, receive chunked embed responses, and continue conversations via a Reply button/modal. All subprocess calls are locked to the project directory.

#### ollama.py
- ⭐ New cog. Owner-only slash commands for chatting with a local Ollama instance over its REST API (`localhost:11434`); responses chunked into embeds.

#### reddit.py
- ⭐ New cog (replaces `_reddit_cog.py`). Scrapes subreddits for images and posts via webhooks with hash-based duplicate validation (`reddit_array.bin` / `reddit.json`) and an optional compiled helper that degrades to hash-only checks.

#### moderator.py
- Reworked the duplicate-attachment/spam detection:
	- Switched from a single-URL `re.search` to `re.findall`, hashing every URL in a message.
	- Added a `urls` property to `MessageRecords` alongside the truncated `hashes` (both rotate the 2 most recent entries).
	- Added a persistent global banned-hash table (`moderator_hashes.json`) with `_load_banned_hashes`/`_save_banned_hashes`; a match instantly trips the spam limit, and a banned user's hashes are merged into the table.
	- AutoMod ban embeds now list the offending attachment URLs.
- Made `reload` re-scan and hot-load newly added extensions (loading vs reloading as needed) and report the count of newly loaded modules.
- Tightened `set_mod_settings` error handling around `sqlite3.DatabaseError`.
- Bumped `settings`/`set` permissions from `manage_messages` to `administrator`; `settings set` now seeds defaults for new guilds and replies ephemerally.
- Added stickers/admin short-circuits and clearer debug logging to the attachment check.
- Reworked replies across `sync`, `prefix`, `trust` and `who_is` to use `KumaEmoji` styling.

#### gatekeeper.py
- Standardized all failure replies with `KumaEmoji` styling (`kuma_sad`, `kuma_hmm`, `kuma_crying`, `kuma_wow`, etc.) and cleaner formatting.
- Removed stale suggestion comments; fixed an "applcation" typo.

#### utility.py
- Added a `yoink` command/`YoinkView` flow to steal emojis/stickers from a message and upload them to a guild chosen via a select menu.
- Added a `CUSTOM_EMOJI_PATTERN`, `KumaEmbed`/`KumaView` imports and related typing.
- Minor cleanup to `get_latest_commits` (removed stale TODO, added `noqa`).

#### ffxiv.py
- Dropped the unused `KumaCommandTree` import.

#### README.MD
- Rewrote the overview: documented `iter_modules` auto-discovery, the `_`-prefix skip convention and the `private/` path, and added per-file sections for every current and archived cog.
