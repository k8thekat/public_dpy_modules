"""Copyright (C) 2021-2025 Katelynn Cadwallader.

This file is part of Kuma Kuma Bear, a Discord Bot.

Kuma Kuma Bear is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3, or (at your option)
any later version.

Kuma Kuma Bear is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
License for more details.

You should have received a copy of the GNU General Public License
along with Kuma Kuma Bear; see the file COPYING.  If not, write to the Free
Software Foundation, 51 Franklin Street - Fifth Floor, Boston, MA
02110-1301, USA.

"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Self, Unpack, reveal_type

import discord
from discord import app_commands

from utils import KumaCog as Cog, KumaEmbed, KumaView

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from kuma_kuma import Kuma_Kuma
    from utils._types import EmbedParams
    from utils.ui import ViewParams

LOGGER = logging.getLogger()

# All subprocess calls are locked to this directory — no path traversal possible.
PROJECT_ROOT: Path = Path(__file__).parent.parent
# Discord attachments are saved here so the CLI can read them without leaving the project.
ATTACHMENTS_DIR: Path = PROJECT_ROOT.joinpath(".claude_attachments")
CLAUDE_ICON: Path = PROJECT_ROOT.joinpath("resources", "claude_icon.png")

CLAUDE_TIMEOUT: int = 900
CHUNK_SIZE: int = 3800
MAX_ATTACHMENT_SIZE: int = 25 * 1024 * 1024  # 25MB
ATTACHMENT_MAX_AGE: int = 86400  # Saved attachments older than this (seconds) are removed on cog load.
PROGRESS_INTERVAL: float = 4.0  # Minimum seconds between progress edits to stay under Discord rate limits.

SESSION_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS claude_session (
    user_id INTEGER PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL
)"""

HISTORY_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS claude_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_id TEXT,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_usd REAL,
    type TEXT NOT NULL DEFAULT 'ask',
    created_at REAL NOT NULL
)"""

YES_NO_CUES: tuple[str, ...] = (
    "shall i",
    "should i",
    "would you like",
    "do you want",
    "want me to",
    "proceed",
    "continue",
    "confirm",
    "apply",
)


async def _is_owner(interaction: discord.Interaction) -> bool:
    return await interaction.client.is_owner(interaction.user)  # type: ignore[arg-type]


def _is_yes_no_question(text: str) -> bool:
    """Determine if the response ends in a yes/no style question worth offering quick-reply buttons for.

    Checks that the last non-empty line ends with a `?` and contains a common confirmation cue.
    """
    lines: list[str] = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) == 0:
        return False
    last: str = lines[-1].lower()
    return last.endswith("?") and any(cue in last for cue in YES_NO_CUES)


def _chunk_response(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Split a response into embed-sized chunks, preferring natural break points.

    Splits preferentially at code-fence boundaries, then double newlines, then
    single newlines, falling back to a hard cut only if none are found in the
    second half of the window.
    """
    chunks: list[str] = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        window = text[:size]
        split = size
        for marker in ("```", "\n\n", "\n"):
            idx = window.rfind(marker)
            if idx > size // 2:
                split = idx
                break
        chunks.append(text[:split].rstrip())
        text = text[split:].lstrip()
    return [c for c in chunks if c]


@dataclass
class ClaudeResult:
    """The outcome of a single Claude Code CLI invocation.

    Attributes
    ----------
    text: :class:`str`
        The response text; empty when :attr:`error` is set.
    cost_usd: :class:`Optional[float]`
        The API cost reported by Claude Code, if available.
    error: :class:`Optional[str]`
        A user-displayable error message; `None` on success.

    """

    text: str = field(default="")
    cost_usd: Optional[float] = field(default=None)
    error: Optional[str] = field(default=None)


class ClaudeEmbed(KumaEmbed):
    """Embed displaying a single chunk of a Claude Code response.

    Parameters
    ----------
    cog: :class:`KumaCog`
        The parent Cog, passed through to :class:`KumaEmbed`.
    prompt: :class:`str`
        The original prompt, displayed as a field (truncated to 200 chars).
    chunk: :class:`str`
        The response text slice to display in the embed description.
    model: :class:`str`
        The Claude model used, displayed in the embed title.
    cost_usd: :class:`Optional[float]`
        The API cost reported by Claude Code, displayed in the footer if provided.
    **kwargs: :class:`Unpack[EmbedParams]`
        Any additional keyword arguments forwarded to :class:`KumaEmbed`.

    """

    def __init__(
        self,
        cog: Cog,
        *,
        prompt: str,
        chunk: str,
        model: str,
        cost_usd: Optional[float] = None,
        **kwargs: Unpack[EmbedParams],
    ) -> None:
        kwargs.setdefault("title", f"Claude Code — `{model}`")
        kwargs.setdefault("color", discord.Color.blurple())
        kwargs.setdefault("description", chunk)
        super().__init__(cog=cog, **kwargs)

        self.avatar_icon = discord.File(CLAUDE_ICON)
        self.set_author(name="Claude Code")

        short_prompt = f"`{prompt[:200]}{'...' if len(prompt) > 200 else ''}`"
        self.add_field(name="Prompt:", value=short_prompt, inline=False)
        if cost_usd is not None:
            self.set_footer(text=f"**Cost:** ${cost_usd:.4f} | Kuma Kuma Bear")


class ClaudeReplyModal(discord.ui.Modal, title="Reply to Claude Code"):
    """Free-form reply box dispatched by the Reply button on :class:`ClaudeView`."""

    reply: discord.ui.TextInput[Self] = discord.ui.TextInput(
        label="Reply",
        style=discord.TextStyle.paragraph,
        placeholder="Your reply to Claude...",
        max_length=4000,
    )

    def __init__(self, *, view: ClaudeView) -> None:
        super().__init__()
        self.view: ClaudeView = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.view._quick_reply(interaction=interaction, reply=self.reply.value, disable_buttons=False)  # noqa: SLF001


class ClaudeView(KumaView):
    """Paginated view for Claude Code responses.

    Always offers a Reply button that opens a :class:`ClaudeReplyModal` to continue the
    CLI session free-form. Adds Yes/No quick-reply buttons when the response ends in a
    yes/no style question; pressing one resumes the CLI session with that reply and sends
    the new response as a fresh followup with its own view.

    Parameters
    ----------
    model: :class:`str`
        The Claude model to use for quick replies.
    is_question: :class:`bool`, optional
        Whether to show the Yes/No quick-reply buttons, by default `False`.
    allow_edits: :class:`bool`, optional
        Passed through to :meth:`ClaudeCog.run_claude` on quick replies, by default `False`.
    **kwargs: :class:`Unpack[ViewParams]`
        Any additional keyword arguments forwarded to :class:`KumaView`.

    """

    cog: ClaudeCog

    def __init__(self, *, model: str, is_question: bool = False, allow_edits: bool = False, **kwargs: Unpack[ViewParams]) -> None:
        super().__init__(**kwargs)
        self.model: str = model
        self.allow_edits: bool = allow_edits
        # Decorator buttons are not tracked by add_item; extend manually.
        self.components.append(self.reply_callback)
        if is_question:
            self.components.extend([self.yes_callback, self.no_callback])
        else:
            self.remove_item(item=self.yes_callback)
            self.remove_item(item=self.no_callback)

    async def _quick_reply(
        self, interaction: discord.Interaction, reply: str, *, history_type: str = "free_reply", disable_buttons: bool = True
    ) -> None:
        """Resumes the CLI session with `reply`, showing a processing message that is edited into the response.

        Parameters
        ----------
        interaction: :class:`discord.Interaction`
            The component or modal-submit interaction to respond through.
        reply: :class:`str`
            The reply to send to the CLI.
        history_type: :class:`str`, optional
            The interaction type tag for history storage, by default ``'free_reply'``.
        disable_buttons: :class:`bool`, optional
            Disable the Yes/No buttons on the dispatching message; only valid for
            component interactions, by default `True`.

        """
        await interaction.response.defer(ephemeral=True)
        if disable_buttons:
            self.yes_callback.disabled = True
            self.no_callback.disabled = True
            await interaction.edit_original_response(view=self)

        # Let the user know we picked up their reply while the CLI session resumes.
        processing: discord.WebhookMessage = await interaction.followup.send(
            content=f"{self.cog.emoji_table.kuma_tea} Working on your reply...",
            ephemeral=True,
            wait=True,
        )

        git_before: str = await self.cog.git_status()
        res: ClaudeResult = await self.cog.run_claude(
            prompt=reply,
            model=self.model,
            user_id=self.owner.id,
            allow_edits=self.allow_edits,
        )
        if res.error is not None:
            await processing.edit(content=res.error)
            return

        await self.cog.save_history(
            user_id=self.owner.id,
            prompt=reply,
            response=res.text,
            model=self.model,
            cost_usd=res.cost_usd,
            history_type=history_type,
        )

        embeds: list[ClaudeEmbed] = self.cog.build_embeds(prompt=reply, response=res.text, model=self.model, cost_usd=res.cost_usd)
        git_after: str = await self.cog.git_status()
        if git_after != git_before:
            self.cog.append_git_changes(embeds=embeds, changes=git_after)

        view = ClaudeView(
            model=self.model,
            is_question=_is_yes_no_question(res.text),
            allow_edits=self.allow_edits,
            owner=self.owner,
            cog=self.cog,
            embeds=embeds,
            timeout=None,
        )
        first: ClaudeEmbed = embeds[0]
        await processing.edit(content=None, embed=first, attachments=first.attachments, view=view)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green, row=1)
    async def yes_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        await self._quick_reply(interaction=interaction, reply="Yes", history_type="yes_reply")

    @discord.ui.button(label="No", style=discord.ButtonStyle.red, row=1)
    async def no_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        await self._quick_reply(interaction=interaction, reply="No", history_type="no_reply")

    @discord.ui.button(label="Reply...", style=discord.ButtonStyle.blurple, row=1)
    async def reply_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        await interaction.response.send_modal(ClaudeReplyModal(view=self))


class ClaudeCog(Cog, name="Claude"):
    """Cog providing a Discord slash command interface to the Claude Code CLI.

    All commands are restricted to bot owners. The subprocess is always run
    with :attr:`PROJECT_ROOT` as its working directory so file operations
    stay inside the Kuma_Kuma project. Sessions persist per Discord user via
    the CLI `--resume` flag and survive restarts through the `claude_session`
    table; Discord attachments are saved to :attr:`ATTACHMENTS_DIR` so the
    CLI can read them.
    """

    claude = app_commands.Group(name="claude", description="Claude Code CLI integration.")

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)
        self._sessions: dict[int, str] = {}

    async def cog_load(self) -> None:
        """Creates the session table, loads stored sessions and prunes stale attachments.

        Removes any saved attachments older than :attr:`ATTACHMENT_MAX_AGE`.
        """
        async with self.bot.pool.acquire() as conn:
            await conn.execute(SESSION_SETUP_SQL)
            await conn.execute(HISTORY_SETUP_SQL)
            rows = await conn.fetchall("""SELECT user_id, session_id FROM claude_session""")
            self._sessions = {entry["user_id"]: entry["session_id"] for entry in rows}

        ATTACHMENTS_DIR.mkdir(exist_ok=True)
        cutoff: float = time.time() - ATTACHMENT_MAX_AGE
        for entry in ATTACHMENTS_DIR.iterdir():
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()

    async def git_status(self) -> str:
        """Returns the short-format git status of :attr:`PROJECT_ROOT`; empty string when the tree is clean or git fails."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=PROJECT_ROOT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (TimeoutError, FileNotFoundError):
            return ""
        return stdout.decode().strip()

    def append_git_changes(self, *, embeds: list[ClaudeEmbed], changes: str) -> None:
        """Adds a `Working Tree Changes:` field with the git status to the final embed."""
        embeds[-1].add_field(name="Working Tree Changes:", value=f"```\n{changes[:1000]}\n```", inline=False)

    async def save_history(
        self,
        *,
        user_id: int,
        prompt: str,
        response: str,
        model: str,
        cost_usd: Optional[float] = None,
        history_type: str = "ask",
    ) -> None:
        """Persists a prompt/response exchange to the ``claude_history`` table."""
        session_id: Optional[str] = self._sessions.get(user_id)
        async with self.bot.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO claude_history(user_id, session_id, prompt, response, model, cost_usd, type, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                user_id,
                session_id,
                prompt,
                response,
                model,
                cost_usd,
                history_type,
                time.time(),
            )

    async def run_claude(
        self,
        *,
        prompt: str,
        model: str,
        user_id: int,
        allow_edits: bool = False,
        progress: Optional[Callable[[str], Coroutine[Any, Any, None]]] = None,
    ) -> ClaudeResult:
        """Runs the Claude Code CLI with the prompt, resuming the user's session if one exists.

        Parameters
        ----------
        prompt: :class:`str`
            The prompt to send to Claude Code.
        model: :class:`str`
            The Claude model to use.
        user_id: :class:`int`
            The Discord user ID; used to resume and store CLI sessions.
        allow_edits: :class:`bool`, optional
            Run with `--permission-mode acceptEdits` so file edits are auto-approved, by default `False`.
        progress: :class:`Optional[Callable[[str], Coroutine[Any, Any, None]]]`, optional
            An async callback invoked with a short status string as the CLI uses tools, by default `None`.

        Returns
        -------
        :class:`ClaudeResult`
            The response text and cost, or a user-displayable error message.

        """
        args: list[str] = ["claude", "-p", prompt, "--model", model, "--output-format", "stream-json", "--verbose"]
        if allow_edits:
            args += ["--permission-mode", "acceptEdits"]
        if user_id in self._sessions:
            args += ["--resume", self._sessions[user_id]]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
                cwd=PROJECT_ROOT,
            )
        except FileNotFoundError:
            return ClaudeResult(error=f"`claude` was not found on PATH. Is Claude Code installed? {self.emoji_table.kuma_shock}")

        assert proc.stdout is not None and proc.stderr is not None  # noqa: PT018, S101
        stderr_task: asyncio.Task[bytes] = asyncio.create_task(proc.stderr.read())
        data: Optional[dict] = None

        try:
            async with asyncio.timeout(delay=CLAUDE_TIMEOUT):
                while line := await proc.stdout.readline():
                    try:
                        event: dict = json.loads(line.decode().strip() or "{}")
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") == "assistant" and progress is not None:
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "tool_use":
                                await progress(f"`{block.get('name', 'tool')}`")
                    elif event.get("type") == "result":
                        data = event
                await proc.wait()

        except TimeoutError:
            proc.kill()
            stderr_task.cancel()
            return ClaudeResult(error=f"Claude Code timed out after {CLAUDE_TIMEOUT} seconds. {self.emoji_table.kuma_head_clench}")

        stderr: str = (await stderr_task).decode().strip()
        if data is None:
            err_block: str = f"\n```\n{stderr[:1000]}\n```" if stderr else ""
            return ClaudeResult(error=f"Claude Code exited without a result. {self.emoji_table.kuma_crying}{err_block}")

        if data.get("is_error"):
            return ClaudeResult(
                error=f"Claude returned an error. {self.emoji_table.kuma_sad}\n```\n{data.get('result', 'Unknown error')}\n```",
            )

        if session_id := data.get("session_id"):
            self._sessions[user_id] = session_id
            async with self.bot.pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO claude_session(user_id, session_id) VALUES(?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET session_id = excluded.session_id""",
                    user_id,
                    session_id,
                )

        text: str = str(data.get("result", "")).strip()
        if not text:
            return ClaudeResult(error=f"Claude returned an empty response. {self.emoji_table.kuma_shrug}")
        return ClaudeResult(text=text, cost_usd=data.get("total_cost_usd"))

    def build_embeds(self, *, prompt: str, response: str, model: str, cost_usd: Optional[float] = None) -> list[ClaudeEmbed]:
        """Chunks the response text and builds a :class:`ClaudeEmbed` per chunk with page number footers.

        Parameters
        ----------
        prompt: :class:`str`
            The prompt that generated the response, displayed as a field on each embed.
        response: :class:`str`
            The full response text to chunk.
        model: :class:`str`
            The Claude model used, displayed in the embed titles.
        cost_usd: :class:`Optional[float]`, optional
            The API cost, displayed in the final embed's footer, by default `None`.

        Returns
        -------
        :class:`list[ClaudeEmbed]`
            The built embeds; always at least one.

        """
        chunks: list[str] = _chunk_response(response)
        embeds: list[ClaudeEmbed] = [
            ClaudeEmbed(
                cog=self,
                prompt=prompt,
                chunk=chunk,
                model=model,
                cost_usd=cost_usd if i == len(chunks) - 1 else None,
            )
            for i, chunk in enumerate(chunks)
        ]
        if len(embeds) > 1:
            for i, embed in enumerate(embeds):
                cost_str = f" | **Cost:** ${cost_usd:.4f}" if cost_usd is not None and i == len(embeds) - 1 else ""
                embed.set_footer(text=f"{i + 1}/{len(embeds)}{cost_str} | Kuma Kuma Bear")
        return embeds

    async def save_attachment(self, *, attachment: discord.Attachment) -> Path:
        """Saves a Discord attachment into :attr:`ATTACHMENTS_DIR` with a sanitized, timestamped filename.

        Parameters
        ----------
        attachment: :class:`discord.Attachment`
            The Discord attachment to save.

        Returns
        -------
        :class:`Path`
            The path the attachment was saved to.

        """
        ATTACHMENTS_DIR.mkdir(exist_ok=True)
        name: str = re.sub(r"[^\w.\-]", "_", attachment.filename)
        path: Path = ATTACHMENTS_DIR.joinpath(f"{int(time.time())}-{name}")
        await attachment.save(fp=path)
        return path

    @claude.command(name="ask", description="Send a prompt to Claude Code in the Kuma_Kuma project directory.")
    @app_commands.describe(
        prompt="The prompt to send to Claude Code.",
        model="The Claude model to use. Defaults to sonnet.",
        attachment="An optional file to share with Claude Code (saved inside the project).",
        allow_edits="Auto-approve file edits inside the project directory. Defaults to False.",
    )
    @app_commands.choices(
        model=[
            app_commands.Choice(name="Sonnet 4.6 (default)", value="claude-sonnet-4-6"),
            app_commands.Choice(name="Opus 4.8", value="claude-opus-4-8"),
            app_commands.Choice(name="Haiku 4.5", value="claude-haiku-4-5-20251001"),
            app_commands.Choice(name="Fable 5", value="claude-fable-5"),
        ]
    )
    @app_commands.check(_is_owner)
    async def ask(
        self,
        interaction: discord.Interaction,
        prompt: str,
        model: str = "claude-sonnet-4-6",
        attachment: Optional[discord.Attachment] = None,
        allow_edits: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if attachment is not None:
            if attachment.size > MAX_ATTACHMENT_SIZE:
                await interaction.edit_original_response(
                    content=f"That file is too big for me to carry ({attachment.size / 1024 / 1024:.1f}MB); "
                    f"the limit is {MAX_ATTACHMENT_SIZE // 1024 // 1024}MB. {self.emoji_table.kuma_pout}",
                )
                return
            path: Path = await self.save_attachment(attachment=attachment)
            prompt = f"{prompt}\n\n[The user attached a file, saved at `{path.relative_to(PROJECT_ROOT)}`]"

        last_update: float = 0.0

        async def on_progress(status: str) -> None:
            nonlocal last_update
            now: float = time.monotonic()
            if now - last_update < PROGRESS_INTERVAL:
                return
            last_update = now
            try:
                await interaction.edit_original_response(content=f"{self.emoji_table.kuma_tea} Working on it... {status}")
            except discord.HTTPException:
                pass

        git_before: str = await self.git_status()
        res: ClaudeResult = await self.run_claude(
            prompt=prompt,
            model=model,
            user_id=interaction.user.id,
            allow_edits=allow_edits,
            progress=on_progress,
        )
        if res.error is not None:
            await interaction.edit_original_response(content=res.error)
            return

        await self.save_history(
            user_id=interaction.user.id,
            prompt=prompt,
            response=res.text,
            model=model,
            cost_usd=res.cost_usd,
            history_type="ask",
        )

        embeds: list[ClaudeEmbed] = self.build_embeds(prompt=prompt, response=res.text, model=model, cost_usd=res.cost_usd)
        git_after: str = await self.git_status()
        if git_after != git_before:
            self.append_git_changes(embeds=embeds, changes=git_after)

        view = ClaudeView(
            model=model,
            is_question=_is_yes_no_question(res.text),
            allow_edits=allow_edits,
            owner=interaction.user,
            cog=self,
            embeds=embeds,
            timeout=None,
        )
        first: ClaudeEmbed = embeds[0]
        await interaction.edit_original_response(content=None, embed=first, attachments=first.attachments, view=view)

    @claude.command(name="history", description="View your Claude Code interaction history.")
    @app_commands.describe(
        limit="Number of entries to show (default 10, max 50).",
        history_type="Filter by interaction type.",
        export="Send results as a JSON file instead of embeds.",
    )
    @app_commands.choices(
        history_type=[
            app_commands.Choice(name="All (default)", value="all"),
            app_commands.Choice(name="Ask", value="ask"),
            app_commands.Choice(name="Yes reply", value="yes_reply"),
            app_commands.Choice(name="No reply", value="no_reply"),
            app_commands.Choice(name="Free reply", value="free_reply"),
        ]
    )
    @app_commands.check(_is_owner)
    async def history(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] = 10,
        history_type: str = "all",
        export: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        query: str = """SELECT * FROM claude_history WHERE user_id = ?"""
        params: list[int | str] = [interaction.user.id]
        if history_type != "all":
            query += " AND type = ?"
            params.append(history_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with self.bot.pool.acquire() as conn:
            rows = await conn.fetchall(query, *params)

        if not rows:
            await interaction.edit_original_response(
                content=f"No history found. {self.emoji_table.kuma_shrug}",
            )
            return

        if export:
            entries: list[dict[str, object]] = [
                {
                    "prompt": row["prompt"],
                    "response": row["response"],
                    "model": row["model"],
                    "cost_usd": row["cost_usd"],
                    "type": row["type"],
                    "created_at": datetime.fromtimestamp(row["created_at"], tz=UTC).isoformat(),
                }
                for row in rows
            ]
            data: str = json.dumps(entries, indent=2)
            buf: io.BytesIO = io.BytesIO(data.encode())
            file: discord.File = discord.File(fp=buf, filename="claude_history.json")
            await interaction.edit_original_response(
                content=f"Here's your history export! {self.emoji_table.kuma_happy}",
                attachments=[file],
            )
            return

        embeds: list[KumaEmbed] = []
        for row in rows:
            ts: str = f"<t:{int(row['created_at'])}:R>"
            prompt_preview: str = row["prompt"][:200] + ("..." if len(row["prompt"]) > 200 else "")
            response_preview: str = row["response"][:4090] + ("..." if len(row["response"]) > 4090 else "")
            embed = KumaEmbed(
                cog=self,
                title=f"Claude History — `{row['model']}`",
                color=discord.Color.greyple(),
                description=response_preview,
            )
            embed.add_field(name="Prompt:", value=f"`{prompt_preview}`", inline=False)
            embed.add_field(name="Type:", value=f"`{row['type']}`", inline=True)
            embed.add_field(name="When:", value=ts, inline=True)
            if row["cost_usd"] is not None:
                embed.add_field(name="Cost:", value=f"${row['cost_usd']:.4f}", inline=True)
            embeds.append(embed)

        if len(embeds) > 1:
            for i, embed in enumerate(embeds):
                embed.set_footer(text=f"{i + 1}/{len(embeds)} | Kuma Kuma Bear")

        view = KumaView(
            owner=interaction.user,
            cog=self,
            embeds=embeds,
            timeout=None,
        )
        first: KumaEmbed = embeds[0]
        await interaction.edit_original_response(content=None, embed=first, attachments=first.attachments, view=view)

    @claude.command(name="reset", description="Clear your Claude Code conversation history and start a new session.")
    @app_commands.check(_is_owner)
    async def reset(self, interaction: discord.Interaction) -> None:
        had_session: bool = self._sessions.pop(interaction.user.id, None) is not None
        if had_session:
            async with self.bot.pool.acquire() as conn:
                await conn.execute("""DELETE FROM claude_session WHERE user_id = ?""", interaction.user.id)
            msg = f"Session cleared, we have a clean slate! Next `/claude ask` starts fresh. {self.emoji_table.kuma_happy}"
        else:
            msg = f"No active session to clear. {self.emoji_table.kuma_shrug}"
        await interaction.response.send_message(content=msg, ephemeral=True)


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103
    await bot.add_cog(ClaudeCog(bot=bot))
