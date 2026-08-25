# Changelog

## Per user Claude session storage, generated file replies and Reddit 5xx handling

#### claude.py
- ⭐ Session store — every session a user runs is kept in a new `claude_session_store` table (`name`, `summary`, `created_at`, `last_used`, `expired_at`) so it can be nick named, resumed or deleted later; `claude_session` stays as the *active* session pointer.
	- Added a unique per user index on `name` (unnamed sessions are ignored by it) and a `PRAGMA table_info` migration that patches `expired_at` into older databases.
	- Added `set_active_session`, `clear_active_session`, `get_sessions` and `delete_session` helpers; `run_claude` no longer writes the session row itself.
- ⭐ `/claude resume` — makes a saved session active again, by nick name (with autocomplete) or raw session ID.
- ⭐ `/claude name` — nick names the active session with an optional summary; rejects a name already taken by another session.
- ⭐ `/claude sessions` — paginated list of saved sessions showing the active marker, last used, file count/size and summary; `named_only` flag to filter.
	- Falls back to a quoted snippet of the session's *opening* ask when no summary was set, so an unnamed session still says what it is tied to — a session drifts over many turns, the first prompt is the more stable label.
	- `get_sessions` pulls it with a correlated subquery on `claude_history.session_id` (no new column); `_prompt_snippet` strips the bracketed notes we append ourselves, collapses whitespace and trims to `SESSION_SNIPPET_SIZE` (140).
- ⭐ `/claude delete` — drops the store row and `rmtree`s the session workspace, with a `keep_files` flag.
- ⭐ `/claude plan` — runs with `--permission-mode plan`; `run_claude` and `ClaudeView` now take a `permission_mode` string instead of the `allow_edits` bool (`/claude ask` maps its flag to `acceptEdits`).
- ⛔ `/claude reset` → ⭐ `/claude clear`; it only drops the active pointer now, the session itself stays resumable.
- ⭐ Per user, per session file layout under `EXTENSIONS_ROOT`:
	- Generated files live in `.claude_asks/<user_id>/<session slug>/`, attachments in `.claude_attachments/<user_id>/`; slug is the first 8 chars of the session ID (`SESSION_SLUG_SIZE`).
	- New sessions pre-generate a UUID and pass `--session-id` so the workspace path is known before the run; `_move_workspace` follows (and merges) if the CLI hands back a different ID, such as a fork.
	- Every prompt is suffixed with `OUTPUT_DIR_NOTICE` pointing at that session's workspace only.
	- Added `session_slug`, `session_dir`, `attachments_dir`, `_dir_snapshot`, `_new_files` and `_dir_stats` helpers; `save_attachment` now takes a `user_id`.
- ⭐ File replies — the workspace is snapshotted before/after each run and anything new or touched rides along as an attachment (`ClaudeResult.files`).
	- `collect_reply_files`/`_build_reply_files` cap replies at `MAX_REPLY_FILES` (8) and `MAX_REPLY_FILE_SIZE` (8MB); oversized files are listed in a `Generated Files:` embed field instead.
	- Responses longer than `FULL_RESPONSE_THRESHOLD` (two chunks) also attach `claude_response.md`.
- ⭐ Session retention — added `prune_loop`, a daily `tasks.loop` registered in `bot.task_loops` that also runs on cog load.
	- Age cutoff of 30 days by default, read from Claude Code's own `cleanupPeriodDays` when set (`_session_max_age`) — past that the CLI has dropped the transcript so the session cannot be resumed anyway.
	- Unnamed aged out sessions are deleted with their files; nick named ones keep the row (`expired_at`) and files, show ⚠️ *expired* in `/claude sessions`, and `/claude resume` explains rather than failing.
	- Count cap of `MAX_SESSIONS_PER_USER` (20) unnamed sessions per user, oldest `last_used` rotated out first; nick named sessions are exempt from the cap but not from age.
	- `_prune_attachments` now walks recursively and cleans up empty user directories; `_prune_empty_workspaces` does the same for `.claude_asks/`, touching only the numeric per user dirs.
- ⭐ `/claude history conversation:True` — sends a plain text `claude_conversation.txt` transcript, oldest first.
	- ⛔ Dropped the `history_type` filter parameter; every entry is returned and the type is still shown per row.
- Factored the shared prompt flow out of `ask` into `dispatch_prompt` (used by `ask` and `plan`) and hoisted the model list into `MODEL_CHOICES`.
- ⚠️ `resolve_session` told you "not both" when you gave it *neither* a `name` nor a `session_id`; the two cases now have their own messages.
- `cog_unload` un-registers `prune_loop` from `bot.task_loops` so a reload no longer stacks duplicates.
- Cleanup pass — dropped a stray `reveal_type` import and the vestigial `query`/`params` build-up left in `history` by the removed type filter, replaced the `paths[:10]` magic number in `collect_reply_files` with `MAX_REPLY_FILES`, and reworded the skipped-file note since it also covers files past the per reply cap, not just oversized ones.
	- Refreshed comments/docstrings that no longer matched: `ATTACHMENT_MAX_AGE` (pruned by the loop now, not on cog load), the `ClaudeCog` session paragraph (`--session-id` for new, `--resume` to continue) and the `dispatch_prompt` attachment directory.

#### reddit.py
- ⚠️ Fixed `asyncprawcore.exceptions.ServerError` escaping both exception handlers and aborting a whole media handler run — it is a `ResponseException`, *not* a `RequestException`, so it needed its own clause.
	- `process_subreddit_submissions` returns its partial results and lets the next cycle retry.
	- `check_subreddit` returns `503` to abort the cycle, since a Reddit-side 5xx means the API is likely degraded rather than that one sub being bad.

#### .gitignore
- Ignored `reddit.json` and any `*.claude*` path.

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
