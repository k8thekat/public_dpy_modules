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

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

from utils import ROTATE_WINDOW, KumaCog as Cog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Row

    from kuma_kuma import Kuma_Kuma

LOGGER = logging.getLogger()

# The table lives with the cog rather than in core, as it is the Hints feature's own state and nothing
# else reads it. `userid` is the Discord ID.
HINTS_SETUP_SQL: str = """
CREATE TABLE IF NOT EXISTS user_hints (
    userid INTEGER NOT NULL,
    hint_key TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    retire_at INTEGER NOT NULL DEFAULT 3,
    dismissed INTEGER NOT NULL DEFAULT 0,
    last_seen REAL,
    PRIMARY KEY (userid, hint_key))
"""

# How many times a hint shows before it retires itself. Stored per row rather than read from here on
# every check, so `Remind me later` can raise one user's ceiling without touching anyone else's.
DEFAULT_RETIRE_AT: int = 3

# Seconds before a user can be offered another cog-wide hint. `retire_at` counts showings rather than
# time, so without a throttle a burst of commands spends a hint's whole allowance inside a minute and
# it retires having never been read. Matched to `KumaCog.rotate_pick`'s window, so a user who waits
# long enough to be offered another one is also into the next slot of the rotation.
COG_WIDE_COOLDOWN: float = float(ROTATE_WINDOW)


class HintStyle(StrEnum):
    """How a hint renders. Both sit inside a blockquote so the grey left bar marks it as an aside.

    BLOCK is the bold label with the body on its own line; INLINE puts both on one line.
    """

    BLOCK = "block"
    INLINE = "inline"


# INLINE by default; it stays readable once the buttons sit under it. BLOCK is opt-in per `Hint.style`.
DEFAULT_STYLE: HintStyle = HintStyle.INLINE
HINT_EMOJI: str = "kuma_peak"

# Outside the blockquote, so it reads as chrome rather than part of the hint.
HINT_FOOTER: str = "-# Turn these off any time with `/preferences`"
# Swapped in on the last showing. The only place a count reaches the user.
HINT_FINAL_FOOTER: str = "-# Last time you'll see this one."

# Components V2 caps a message at 40 components through the whole nested tree, and each hint costs 3.
# Measured: a full page with the paging buttons serialises to 35, nine would be 38 and ten would be 41.
HINTS_PER_PAGE: int = 8
PANEL_TIMEOUT: float = 300.0


class HintButton(discord.ui.Button):
    """A button that hands its press to the view that built it, via `action`.

    Both views are built imperatively, so there is no layout to declare with `@discord.ui.button`,
    and a plain Button has a no-op callback.
    """

    def __init__(
        self,
        *,
        action: str,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(label=label, style=style, emoji=emoji, disabled=disabled)
        self.action: str = action

    async def callback(self, interaction: discord.Interaction) -> None:
        """Hands the press to the owning view."""
        # `self.view` is set by discord.py when the item is added, at any nesting depth.
        view: Optional[Union[HintView, HintsPanel]] = self.view  # type: ignore[assignment]
        if view is None:
            return
        await view._dispatch(interaction=interaction, action=self.action)  # noqa: SLF001 - the view owns this button.


@dataclass(frozen=True)
class Hint:
    """One piece of advice a cog offers, declared in that cog's `__hints__`.

    Attributes
    ----------
    key: :class:`str`
        Stable identifier, stored in the database. Namespace it by cog (`ffxiv.watchlist`); changing
        it un-dismisses the hint for everyone.
    text: :class:`str`
        The advice. One sentence.
    label: :class:`str`
        Short name for the `/hints` panel.
    url: :class:`Optional[str]`
        Renders a `Learn more` link button when set.
    style: :class:`Optional[HintStyle]`
        Override the render for this hint alone.
    retire_at: :class:`int`
        Times to show it before retiring, by default :attr:`DEFAULT_RETIRE_AT`.
    cog_wide: :class:`bool`
        Offer this hint after any of the declaring cog's commands, by default False.

    """

    key: str
    text: str
    label: str
    url: Optional[str] = field(default=None)
    style: Optional[HintStyle] = field(default=None)
    retire_at: int = field(default=DEFAULT_RETIRE_AT)
    cog_wide: bool = field(default=False)
    """Offer this hint after *any* command the declaring cog owns, rather than only where a call site
    names it by key. `global` is a reserved word, hence the name."""


@dataclass
class HintRecord:
    """One user's ledger for one hint.

    `seen` is a fact (times shown) and `retire_at` is a preference (times wanted), so `Remind me
    later` raises the ceiling and never lowers `seen`. Decrementing it would make a snoozed view
    indistinguishable from a real one and corrupt the counts the panel reports.
    """

    seen: int = field(default=0)
    retire_at: int = field(default=DEFAULT_RETIRE_AT)
    dismissed: bool = field(default=False)

    @property
    def retired(self) -> bool:
        """Whether this hint has shown as many times as it is allowed to."""
        return self.seen >= self.retire_at

    @property
    def active(self) -> bool:
        """Whether the hint would still be shown."""
        return not self.dismissed and not self.retired

    @property
    def status(self) -> str:
        """The small-text line describing this row on the panel."""
        if self.dismissed:
            return "Dismissed"
        if self.retired:
            return f"Seen {self.seen} of {self.retire_at} — retired"
        return f"Seen {self.seen} of {self.retire_at}"


def render_hint(*, hint: Hint, emoji: str, style: HintStyle, final: bool) -> str:
    """Builds the message content for a hint. `final` swaps the footer for the last-showing one."""
    footer: str = HINT_FINAL_FOOTER if final else HINT_FOOTER
    if style is HintStyle.BLOCK:
        # Every line needs its own `>` or the quote ends at the first newline.
        body: str = "\n".join(f"> {line}" for line in hint.text.splitlines())
        return f"> **Hint:** {emoji}\n{body}\n{footer}"

    # Inline only has the one line, so fold a multi-line hint rather than let it break out of the quote.
    text: str = " ".join(hint.text.split())
    return f"> **Hint:** {text} {emoji}\n{footer}"


class HintView(discord.ui.View):
    """The buttons under a hint. Deliberately small; the footer already points at `/preferences`.

    `Remind me later` only appears on the last scheduled showing. Before then there is nothing to
    put off. Only `user_id` may press any of them.
    """

    def __init__(self, *, cog: HintsCog, hint: Hint, user_id: int, final: bool) -> None:
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog: HintsCog = cog
        self.hint: Hint = hint
        self.user_id: int = user_id

        self.add_item(HintButton(action="got_it", label="Got it", style=discord.ButtonStyle.success))
        if final:
            self.add_item(HintButton(action="later", label="Remind me later"))
        if hint.url is not None:
            # A link button fires no interaction, so it needs no handler.
            self.add_item(discord.ui.Button(label="Learn more", style=discord.ButtonStyle.link, url=hint.url))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects anyone the hint was not shown to."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                content=f"That hint isn't yours! {self.cog.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return False
        return True

    async def _dispatch(self, *, interaction: discord.Interaction, action: str) -> None:
        """Applies a press, then retires the view."""
        if action == "got_it":
            await self.cog.dismiss(user_id=self.user_id, key=self.hint.key)
            note: str = f"Got it — you won't see that one again. {self.cog.emoji_table.kuma_happy}"
        elif action == "later":
            await self.cog.remind_later(user_id=self.user_id, key=self.hint.key)
            note = f"Sure — I'll show that one once more. {self.cog.emoji_table.kuma_peak}"
        else:
            return

        # One press settles it either way, so take the buttons off.
        self.stop()
        await interaction.response.edit_message(view=None)
        await interaction.followup.send(content=note, ephemeral=True)


class HintsPanel(discord.ui.LayoutView):
    """The `/hints` panel: every hint the bot knows, and whether it is on for you.

    Not persistent, unlike `SessionPanel` in `claude.py`; the custom IDs encode the hint key so they
    cannot be pre-registered at startup.

    .. warning::
        A Components V2 message cannot carry `content` or `embeds`.

    """

    def __init__(self, *, cog: HintsCog, user_id: int, entries: list[tuple[Hint, HintRecord]], page: int = 0) -> None:
        super().__init__(timeout=PANEL_TIMEOUT)
        self.cog: HintsCog = cog
        self.user_id: int = user_id
        self.entries: list[tuple[Hint, HintRecord]] = entries
        self.pages: int = max(1, -(-len(entries) // HINTS_PER_PAGE))
        self.page: int = max(0, min(page, self.pages - 1))

        container: discord.ui.Container = discord.ui.Container(accent_colour=discord.Color.blurple())
        container.add_item(discord.ui.TextDisplay(f"## {cog.emoji_table.kuma_peak} Your Hints"))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        start: int = self.page * HINTS_PER_PAGE
        window: list[tuple[Hint, HintRecord]] = entries[start : start + HINTS_PER_PAGE]
        if not window:
            container.add_item(discord.ui.TextDisplay("-# No cog has registered a hint yet."))
        for hint, record in window:
            container.add_item(
                discord.ui.Section(
                    f"**{hint.label}**\n-# {record.status}",
                    accessory=self._toggle(hint=hint, record=record),
                ),
            )

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {self._summary()}"))
        self.add_item(container)

        # Outside the container: these act *on* the panel rather than being settings of it, and the
        # container's border is what makes that read.
        self.add_item(self._actions())

    def _summary(self) -> str:
        """Returns the counts line under the list."""
        active: int = sum(1 for _, record in self.entries if record.active)
        dismissed: int = sum(1 for _, record in self.entries if record.dismissed)
        page: str = f" · page {self.page + 1} of {self.pages}" if self.pages > 1 else ""
        return f"{len(self.entries)} hints · {active} active, {dismissed} dismissed{page}"

    def _toggle(self, *, hint: Hint, record: HintRecord) -> HintButton:
        """Returns the on/off accessory for one hint."""
        on: bool = not record.dismissed
        return HintButton(
            action=f"toggle:{hint.key}",
            label="On" if on else "Off",
            emoji="✔️" if on else "✖️",
            style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
        )

    def _actions(self) -> discord.ui.ActionRow:
        """Returns the row of panel-wide actions."""
        row: discord.ui.ActionRow = discord.ui.ActionRow()
        if self.pages > 1:
            row.add_item(HintButton(action="prev", label="Prev", disabled=self.page == 0))
            row.add_item(HintButton(action="next", label="Next", disabled=self.page >= self.pages - 1))
        row.add_item(HintButton(action="all_on", label="Enable all", emoji="✔️", style=discord.ButtonStyle.success))
        row.add_item(HintButton(action="all_off", label="All off", emoji="✖️"))
        row.add_item(HintButton(action="reset", label="Reset", emoji="🔄", style=discord.ButtonStyle.danger))
        return row

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects anyone but the person the panel was opened for."""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                content=f"That panel isn't yours! {self.cog.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return False
        return True

    async def _dispatch(self, *, interaction: discord.Interaction, action: str) -> None:
        """Applies a press and re-renders the panel in place."""
        page: int = self.page

        if action.startswith("toggle:"):
            await self.cog.toggle(user_id=self.user_id, key=action.removeprefix("toggle:"))
        elif action == "all_on":
            await self.cog.set_all(user_id=self.user_id, dismissed=False)
        elif action == "all_off":
            await self.cog.set_all(user_id=self.user_id, dismissed=True)
        elif action == "reset":
            await self.cog.reset(user_id=self.user_id)
        elif action == "prev":
            page -= 1
        elif action == "next":
            page += 1
        else:
            return

        entries: list[tuple[Hint, HintRecord]] = await self.cog.ledger(user_id=self.user_id)
        await interaction.response.edit_message(view=HintsPanel(cog=self.cog, user_id=self.user_id, entries=entries, page=page))


class HintsCog(Cog, name="Hints"):
    """Shows a cog's hints to a user a few times, then stops.

    A cog offers hints by declaring `__hints__` on itself::

        class FFXIV(Cog):
            __hints__ = (
                Hint(
                    key="ffxiv.watchlist",
                    label="Watchlist",
                    text="Track price drops on any item with `/ffxiv watchlist add`.",
                    url="https://github.com/k8thekat/Kuma_Kuma",
                ),
            )

    and shows one with :meth:`send`. The registry is walked from the loaded cogs on each call, so a
    hint arrives with its cog and leaves with it, with nothing to keep in step.
    """

    async def cog_load(self) -> None:
        """Creates the ledger table."""
        async with self.bot.pool.acquire() as conn:
            await conn.execute(HINTS_SETUP_SQL)

    def registry(self) -> Iterator[Hint]:
        """Yields every hint declared by a currently loaded cog."""
        for cog in self.bot.cogs.values():
            for hint in getattr(cog, "__hints__", ()):
                if isinstance(hint, Hint):
                    yield hint

    def find(self, key: str) -> Optional[Hint]:
        """Returns the hint registered under `key`, or `None` when no loaded cog declares it."""
        return next((hint for hint in self.registry() if hint.key == key), None)

    @staticmethod
    def declared_cog_wide(cog: Optional[commands.Cog]) -> list[Hint]:
        """Returns the cog-wide hints a cog declares, in declaration order.

        Read off the cog rather than through :meth:`registry`, which yields every loaded cog's hints
        with nothing left to say which one they came from.

        Parameters
        ----------
        cog: :class:`Optional[commands.Cog]`
            The cog whose command was just run; `None` for a command belonging to no cog.

        Returns
        -------
        :class:`list[Hint]`
            The cog-wide hints, empty when there are none or the cog is `None`.

        """
        if cog is None:
            return []
        return [hint for hint in getattr(cog, "__hints__", ()) if isinstance(hint, Hint) and hint.cog_wide]

    def owner_of(self, command: Union[app_commands.Command[Any, ..., Any], app_commands.ContextMenu]) -> Optional[commands.Cog]:
        """Returns the cog that owns an app command, through the bot's command index.

        Via :attr:`Kuma_Kuma.command_owners` rather than `app_commands.Command.binding`, which holds
        the same answer but is absent from that class's documented attributes.

        Parameters
        ----------
        command: :class:`Union[app_commands.Command, app_commands.ContextMenu]`
            The command that just completed.

        Returns
        -------
        :class:`Optional[commands.Cog]`
            The owning cog, or `None` when the command belongs to none.

        """
        # The index holds top level names, so a group's child has to be asked about by its root; a
        # `ContextMenu` has no `root_parent` at all and is already its own root.
        root: Union[app_commands.Command[Any, ..., Any], app_commands.ContextMenu, app_commands.Group]
        root = getattr(command, "root_parent", None) or command
        cog_name: Optional[str] = self.bot.command_owners.get(root.name)
        return None if cog_name is None else self.bot.get_cog(cog_name)

    async def last_hint_at(self, *, user_id: int) -> Optional[float]:
        """Returns when a user was last shown any hint, or `None` when they never have been.

        Reads `MAX(last_seen)`, a column the ledger has always written and nothing has ever read.

        Parameters
        ----------
        user_id: :class:`int`
            The Discord ID of the user.

        Returns
        -------
        :class:`Optional[float]`
            A UNIX timestamp, or `None` when this user has no rows.

        """
        async with self.bot.pool.acquire() as conn:
            row: Optional[Row] = await conn.fetchone(
                """SELECT MAX(last_seen) AS last_seen FROM user_hints WHERE userid = ?""",
                user_id,
            )
        if row is None or row["last_seen"] is None:
            return None
        return float(row["last_seen"])

    async def record(self, *, user_id: int, hint: Hint) -> HintRecord:
        """Returns a user's row for a hint, defaulted from the hint when they have never seen it.

        A row is only written once the hint is shown, so the table records what happened.
        """
        async with self.bot.pool.acquire() as conn:
            row: Optional[Row] = await conn.fetchone(
                """SELECT seen, retire_at, dismissed FROM user_hints WHERE userid = ? AND hint_key = ?""",
                user_id,
                hint.key,
            )
        if row is None:
            return HintRecord(retire_at=hint.retire_at)
        return HintRecord(seen=row["seen"], retire_at=row["retire_at"], dismissed=bool(row["dismissed"]))

    async def ledger(self, *, user_id: int) -> list[tuple[Hint, HintRecord]]:
        """Returns every known hint paired with this user's row for it, for the panel."""
        return [(hint, await self.record(user_id=user_id, hint=hint)) for hint in self.registry()]

    async def _mark_seen(self, *, user_id: int, hint: Hint) -> None:
        """Increments a user's seen count, creating the row on first sight."""
        async with self.bot.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_hints (userid, hint_key, seen, retire_at, last_seen) VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT (userid, hint_key) DO UPDATE SET seen = seen + 1, last_seen = excluded.last_seen""",
                user_id,
                hint.key,
                hint.retire_at,
                time.time(),
            )

    async def dismiss(self, *, user_id: int, key: str) -> None:
        """Retires a hint for a user outright; the `Got it` path."""
        async with self.bot.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO user_hints (userid, hint_key, dismissed) VALUES (?, ?, 1)
                   ON CONFLICT (userid, hint_key) DO UPDATE SET dismissed = 1""",
                user_id,
                key,
            )

    async def remind_later(self, *, user_id: int, key: str) -> None:
        """Grants one more showing by raising the ceiling, never by lowering `seen`. See `HintRecord`."""
        async with self.bot.pool.acquire() as conn:
            await conn.execute(
                """UPDATE user_hints SET retire_at = retire_at + 1, dismissed = 0 WHERE userid = ? AND hint_key = ?""",
                user_id,
                key,
            )

    async def toggle(self, *, user_id: int, key: str) -> None:
        """Flips a hint on or off from the panel.

        Turning one back on clears `seen` too, or re-enabling a retired hint would do nothing visible
        and the button would read as broken.
        """
        async with self.bot.pool.acquire() as conn:
            row: Optional[Row] = await conn.fetchone(
                """SELECT dismissed FROM user_hints WHERE userid = ? AND hint_key = ?""",
                user_id,
                key,
            )
            if row is None:
                await conn.execute("""INSERT INTO user_hints (userid, hint_key, dismissed) VALUES (?, ?, 1)""", user_id, key)
                return
            if row["dismissed"]:
                await conn.execute(
                    """UPDATE user_hints SET dismissed = 0, seen = 0 WHERE userid = ? AND hint_key = ?""",
                    user_id,
                    key,
                )
                return
            await conn.execute("""UPDATE user_hints SET dismissed = 1 WHERE userid = ? AND hint_key = ?""", user_id, key)

    async def set_all(self, *, user_id: int, dismissed: bool) -> None:
        """Turns every known hint on or off for a user."""
        keys: list[str] = [hint.key for hint in self.registry()]
        async with self.bot.pool.acquire() as conn:
            for key in keys:
                await conn.execute(
                    """INSERT INTO user_hints (userid, hint_key, dismissed, seen) VALUES (?, ?, ?, 0)
                       ON CONFLICT (userid, hint_key) DO UPDATE SET dismissed = excluded.dismissed,
                       seen = CASE WHEN excluded.dismissed = 0 THEN 0 ELSE user_hints.seen END""",
                    user_id,
                    key,
                    int(dismissed),
                )

    async def reset(self, *, user_id: int) -> None:
        """Clears a user's ledger, so every hint is new again. `Enable all` only clears dismissals."""
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""DELETE FROM user_hints WHERE userid = ?""", user_id)

    async def preference(self, *, user: Union[discord.User, discord.Member], setting: str, default: bool) -> bool:
        """Reads one of a user's preferences, answering `default` when Preferences is not loaded.

        Via `get_cog` rather than an import, so the two cogs stay independently reloadable.
        """
        cog = self.bot.get_cog("Preferences")
        if cog is None:
            return default
        return await cog.enabled(user=user, setting=setting)  # type: ignore - the Preferences cog owns this.

    async def send(
        self,
        target: Union[discord.Interaction, commands.Context],
        key: str,
        *,
        style: Optional[HintStyle] = None,
    ) -> bool:
        """Shows a hint to whoever ran the command, if they are still due to see it.

        Safe to call unconditionally. A dismissed, retired or unregistered hint is a no-op and a
        Discord failure is swallowed; a hint must never be why a command's real reply fails.

        Parameters
        ----------
        target: :class:`Union[discord.Interaction, commands.Context]`
            The command being answered. Interactions get an ephemeral follow-up.
        key: :class:`str`
            The :attr:`Hint.key` to show.
        style: :class:`Optional[HintStyle]`, optional
            Override both the hint's own style and the default.

        Returns
        -------
        :class:`bool`
            Whether a hint was shown.

        """
        user: Union[discord.User, discord.Member] = target.user if isinstance(target, discord.Interaction) else target.author
        prepared: Optional[tuple[str, HintView]] = await self.prepare(user=user, key=key, style=style)
        if prepared is None:
            return False

        content, view = prepared
        try:
            if isinstance(target, discord.Interaction):
                await target.followup.send(content=content, view=view, ephemeral=True)
            else:
                await target.send(content=content, view=view)
        except discord.HTTPException as e:
            LOGGER.warning("<%s.send> | Failed to show the hint %r | Error: %s", __class__.__name__, key, e)
            return False
        return True

    async def send_into(
        self,
        destination: discord.abc.Messageable,
        key: str,
        *,
        user: Union[discord.User, discord.Member],
        style: Optional[HintStyle] = None,
    ) -> bool:
        """Shows a hint somewhere other than where a command was answered.

        :meth:`send` replies to the command, which is the right place for advice about the command. A
        hint about a *place* — a session post, a thread the command just made — belongs in that place
        instead, and the command's own reply is the wrong end of the room.

        The user has to be named because the destination cannot imply one: the gating, the seen count
        and the buttons are all per person. Posted normally rather than ephemerally, as there is no
        interaction to hang an ephemeral message off; only send into somewhere the user can already see.

        Parameters
        ----------
        destination: :class:`discord.abc.Messageable`
            Where to post; typically the thread the hint is about.
        key: :class:`str`
            The :attr:`Hint.key` to show.
        user: :class:`Union[discord.User, discord.Member]`
            Who the hint is for, and the only person its buttons will answer.
        style: :class:`Optional[HintStyle]`, optional
            Override both the hint's own style and the default.

        Returns
        -------
        :class:`bool`
            Whether a hint was shown.

        """
        prepared: Optional[tuple[str, HintView]] = await self.prepare(user=user, key=key, style=style)
        if prepared is None:
            return False

        content, view = prepared
        try:
            await destination.send(content=content, view=view)
        except discord.HTTPException as e:
            LOGGER.warning("<%s.send_into> | Failed to show the hint %r | Error: %s", __class__.__name__, key, e)
            return False
        return True

    async def offer_cog_wide(self, *, target: Union[discord.Interaction, commands.Context], cog: Optional[commands.Cog]) -> bool:
        """Offers one of a cog's cog-wide hints to whoever just ran one of its commands.

        Safe to call after any command. A cog with no cog-wide hint, a user inside the cooldown and a
        user who has already seen everything on offer are all no-ops.

        Parameters
        ----------
        target: :class:`Union[discord.Interaction, commands.Context]`
            The command being answered, handed straight to :meth:`send`.
        cog: :class:`Optional[commands.Cog]`
            The cog whose command was run.

        Returns
        -------
        :class:`bool`
            Whether a hint was shown.

        """
        declared: list[Hint] = self.declared_cog_wide(cog)
        if not declared:
            return False

        user: Union[discord.User, discord.Member] = target.user if isinstance(target, discord.Interaction) else target.author

        # Throttled before the per-hint checks, and against *any* hint rather than only the cog-wide
        # ones: someone who was just shown a hint by name does not want a second one underneath it.
        last_seen: Optional[float] = await self.last_hint_at(user_id=user.id)
        if last_seen is not None and (time.time() - last_seen) < COG_WIDE_COOLDOWN:
            return False

        # Filtered before the pick rather than after. Rotating first and then finding the chosen hint
        # retired would skip a turn and show nothing, on a cog that still had something to say.
        candidates: list[Hint] = [hint for hint in declared if (await self.record(user_id=user.id, hint=hint)).active]
        if not candidates:
            return False

        chosen: Hint = self.rotate_pick(candidates, offset=user.id)
        return await self.send(target, chosen.key)

    @commands.Cog.listener("on_command_completion")
    async def hint_after_command(self, context: commands.Context) -> None:
        """Offers the invoking cog's cog-wide hint after one of its prefix commands.

        .. warning::
            A hybrid command run as a slash dispatches `command_completion` **and**
            `app_command_completion`, so this fires for invocations
            :meth:`hint_after_app_command` also sees. `Context.interaction` is what tells the two
            apart, and the app listener is the one that owns that case.

        Parameters
        ----------
        context: :class:`commands.Context`
            The command that just succeeded.

        """
        if context.interaction is not None:
            return
        await self.offer_cog_wide(target=context, cog=context.cog)

    @commands.Cog.listener("on_app_command_completion")
    async def hint_after_app_command(
        self,
        interaction: discord.Interaction,
        command: Union[app_commands.Command[Any, ..., Any], app_commands.ContextMenu],
    ) -> None:
        """Offers the invoking cog's cog-wide hint after one of its app commands, hybrids included.

        Parameters
        ----------
        interaction: :class:`discord.Interaction`
            The interaction that ran the command; the hint follows up on it ephemerally.
        command: :class:`Union[app_commands.Command, app_commands.ContextMenu]`
            The command that just succeeded.

        """
        await self.offer_cog_wide(target=interaction, cog=self.owner_of(command))

    async def prepare(
        self,
        *,
        user: Union[discord.User, discord.Member],
        key: str,
        style: Optional[HintStyle] = None,
    ) -> Optional[tuple[str, HintView]]:
        """Gates one hint for one user and renders it, or returns `None` when they are not due it.

        Everything that decides *whether* a hint is shown lives here, so the senders only decide
        *where*. Counts the showing, so a caller that takes a rendering must actually try to send it.
        """
        hint: Optional[Hint] = self.find(key)
        if hint is None:
            LOGGER.warning("<%s.prepare> | No loaded cog declares the hint %r", __class__.__name__, key)
            return None

        if not await self.preference(user=user, setting="hints_enabled", default=True):
            return None

        record: HintRecord = await self.record(user_id=user.id, hint=hint)
        if not record.active:
            return None

        await self._mark_seen(user_id=user.id, hint=hint)
        # Counted *after* the increment: this showing is the one that reaches the ceiling.
        final: bool = record.seen + 1 >= record.retire_at
        # Argument, then the hint's own choice, then the user's preference, then the compact default.
        # The preference sits under the hint's so one that needs the room still gets it.
        if style is None and hint.style is None and await self.preference(user=user, setting="hint_style_block", default=False):
            style = HintStyle.BLOCK
        content: str = render_hint(
            hint=hint,
            emoji=self.emoji_table.kuma_peak,
            style=style or hint.style or DEFAULT_STYLE,
            final=final,
        )
        return content, HintView(cog=self, hint=hint, user_id=user.id, final=final)

    hints = app_commands.Group(
        name="hints",
        description="See and change which hints Kuma shows you.",
        guild_only=False,
    )

    @hints.command(name="list", description="Show every hint and whether it is on for you.")
    async def hints_list(self, interaction: discord.Interaction) -> None:
        """Opens the hints panel."""
        entries: list[tuple[Hint, HintRecord]] = await self.ledger(user_id=interaction.user.id)
        await interaction.response.send_message(
            view=HintsPanel(cog=self, user_id=interaction.user.id, entries=entries),
            ephemeral=True,
        )

    @hints.command(name="reset", description="Forget which hints you have seen, so they all show again.")
    async def hints_reset(self, interaction: discord.Interaction) -> None:
        """Clears the caller's ledger."""
        await self.reset(user_id=interaction.user.id)
        await interaction.response.send_message(
            content=f"Cleared your hints — you'll see them all again. {self.emoji_table.kuma_star_eye}",
            ephemeral=True,
        )


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103 # docstring
    await bot.add_cog(HintsCog(bot=bot))
