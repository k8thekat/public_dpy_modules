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

import asyncio
import contextlib
import datetime
import json
import logging
import re
import sqlite3
from hashlib import sha256
from pathlib import Path
from pkgutil import ModuleInfo
from sqlite3 import Row
from typing import TYPE_CHECKING, Literal, Optional, TypedDict, Union, Unpack

import discord
from asqlite import Connection
from discord import Member, Message, User, app_commands
from discord.app_commands import Choice
from discord.ext import commands

import extensions
from kuma_kuma import DEFAULT_PREFIX, Kuma_Kuma, _get_prefix
from utils import (
    KumaCog as Cog,  # need to replace with your own Cog class
    KumaContext as Context,
    KumaGuildContext as GuildContext,
    reload_module_dependencies,
)
from utils._types import EmbedParams
from utils.embeds import KumaEmbed

if TYPE_CHECKING:
    from sqlite3 import Row

    import mystbin
    from asqlite import Cursor


BOT_NAME = "Kuma Kuma Bear"
# The markers `mod_on_thread_update` writes into a thread's title. Both are checked on either branch:
# a thread locked *and* archived in one edit dispatches the listener twice, and a title must never end
# up carrying both.
LOCKED_PREFIX: str = "[LOCKED] - "
CLOSED_PREFIX: str = "[CLOSED] - "
# Discord's ceiling on a thread name.
THREAD_TITLE_SIZE: int = 100
LOGGER = logging.getLogger()
HTTP_REGEX = r'https?://[^\s<>"{}|\\^`\[\]]+'

MODERATOR_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS moderator (
    id INTEGER PRIMARY KEY NOT NULL,
    serverid INTEGER NOT NULL,
    use_mystbin INT NOT NULL DEFAULT 0,
    spam_filter INT NOT NULL DEFAULT 0)
"""


class MessageRecords:
    # messages: list[discord.Message]
    count: int
    _hashes: list[str]
    """We are only going to store ``16`` characters of the full hash."""
    timestamp: datetime.datetime
    _urls: list[str]

    def __init__(self, count: int, timestamp: Optional[datetime.datetime] = None) -> None:
        # self.messages = messages
        self.count = count
        self._hashes = []
        self._urls = []
        # This is getting overwritten right after count == 1
        # I didn't want to set it to None; as then that's another logic check.
        if timestamp is None:
            timestamp = datetime.datetime.now(tz=datetime.UTC)
        self.timestamp = timestamp

    def __repr__(self) -> str:
        return f"Count: {self.count} | Hashes Len: {len(self.hashes)} | Timestamp: {self.timestamp}"

    @property
    def hashes(self) -> list[str]:
        """Users recent attachment hashes.

        .. note::
            The hashes are truncated to [:16] chars for efficiency.
            - You can re-hash the ``URLs`` entry and cross validate if needed.

        .. note::
            Rotates only 2 recent entries.

        Returns
        -------
        :class:`list[str]`
            A list of hashes.

        """
        return self._hashes

    @hashes.setter
    def hashes(self, value: str) -> None:
        if len(self.hashes) == 2:
            self.hashes.pop(0)
            self.hashes.append(value)
        else:
            self.hashes.append(value)

    @property
    def urls(self) -> list[str]:
        """Users recent URL attachments.

        .. note::
            Rotates only 2 recent entries.

        Returns
        -------
        :class:`list[str]`
            A list of URL strings.

        """
        return self._urls

    @urls.setter
    def urls(self, value: str) -> None:
        if len(self.urls) == 2:
            self.urls.pop(0)
            self.urls.append(value)
        else:
            self.urls.append(value)


class ModeratorSettings(TypedDict):
    id: int
    serverid: int
    use_mystbin: bool
    # online_player_count: bool
    spam_filter: bool


# Columns that are not a setting anyone changes, on top of `Cog.settings_excluded_keys`. Shared with
# the `settings set` choices below so the panel and the command can never offer different lists.
MOD_SETTINGS_EXCLUDED: frozenset[str] = frozenset({"online_player_count"})

# What each column means, for the panel. A column with no entry here still shows, it just shows
# without the explanation, so adding one to the table can never break this.
MOD_SETTING_SUMMARIES: dict[str, str] = {
    "use_mystbin": "Upload long pastes and logs to Mystbin instead of posting them in full.",
    "spam_filter": "Watch for repeated messages and duplicate attachments.",
}


def setting_label(key: str) -> str:
    """Turns a column name into the name the panel shows (`use_mystbin` -> `Use Mystbin`)."""
    return key.replace("_", " ").title()


class SettingButton(discord.ui.Button):
    """A button that hands its press to the panel that built it, via `action`.

    The panel is built imperatively - a row per column the database hands back - so there is no fixed
    layout to declare with `@discord.ui.button`, and a plain Button has a no-op callback.
    """

    def __init__(
        self,
        *,
        action: str,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: Optional[str] = None,
    ) -> None:
        super().__init__(label=label, style=style, emoji=emoji)
        self.action: str = action

    async def callback(self, interaction: discord.Interaction) -> None:
        """Hands the press to the owning panel."""
        # `self.view` is set by discord.py when the item is added, at any nesting depth.
        view: Optional[ModeratorSettingsPanel] = self.view  # type: ignore[assignment]
        if view is None:
            return
        await view._dispatch(interaction=interaction, action=self.action)  # noqa: SLF001 - the panel owns this button.


class ModeratorSettingsPanel(discord.ui.LayoutView):
    """A guild's Moderator settings, a `Section` per column with its on/off button beside it.

    The same panel as `preferences`, on the other side of the split - `settings` is about a place, so
    the header carries the guild's icon rather than a user's avatar. Only the admin who ran the command
    may press anything, not every admin who can see it.

    The rows come from the row the database handed back rather than a list kept here, so a column added
    to `moderator` shows up on its own; `MOD_SETTING_SUMMARIES` only decides whether it gets a line of
    explanation under its name.

    .. warning::
        A Components V2 message cannot carry `content` or `embeds`.

    """

    # The parameter's `Moderator` is quoted because the cog is defined below this and the file has no
    # `from __future__ import annotations`; the attribute's is not, as a body annotation never evaluates.
    def __init__(self, *, cog: "Moderator", guild: discord.Guild, owner: Member | User, settings: ModeratorSettings) -> None:
        # The panel dies with the message that carries it: every reply is sent with
        # `delete_after=self.message_timeout`, so taking the timeout from the same place means the
        # buttons never sit dead on a message still on screen, nor outlive one that is gone.
        super().__init__(timeout=cog.message_timeout)
        self.cog: Moderator = cog
        self.guild: discord.Guild = guild
        self.owner: Member | User = owner
        self.settings: ModeratorSettings = settings
        # `settings_excluded_keys` drops the bookkeeping columns (`id`, `serverid`); they are stored on
        # a guild but they are not one of its settings.
        skipped: frozenset[str] = Cog.settings_excluded_keys | MOD_SETTINGS_EXCLUDED
        self.options: list[str] = [key for key in settings.keys() if key not in skipped]  # noqa: SIM118 - It thinks it's a dict; when it's a sqlite3.Row Tuple object.

        header: str = f"## {cog.emoji_table.kuma_peak} {guild.name} Mod Settings\n-# These are this server's, not yours."
        container: discord.ui.Container = discord.ui.Container(accent_colour=discord.Color.blurple())
        # A guild is not required to have an icon, and `Thumbnail` needs a URL, so the header drops to
        # plain text rather than the accessory going missing out of a `Section`.
        if guild.icon is not None:
            container.add_item(discord.ui.Section(header, accessory=discord.ui.Thumbnail(media=guild.icon.url)))
        else:
            container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        for key in self.options:
            summary: str = MOD_SETTING_SUMMARIES.get(key, "")
            container.add_item(
                discord.ui.Section(
                    f"**{setting_label(key)}**" + (f"\n-# {summary}" if summary else ""),
                    accessory=self._toggle(key=key, on=bool(settings[key])),  # type: ignore - the key came off the row itself.
                ),
            )

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {self._summary()}"))
        self.add_item(container)

        # Outside the container: Reset acts *on* the panel rather than being a setting of it, and the
        # container's border is what makes that read.
        self.add_item(
            discord.ui.ActionRow().add_item(
                SettingButton(action="reset", label="Reset", emoji="🔄", style=discord.ButtonStyle.danger),
            ),
        )

    def _summary(self) -> str:
        """Returns the counts line under the list."""
        on: int = sum(1 for key in self.options if bool(self.settings[key]))  # type: ignore - the keys came off the row itself.
        return f"{len(self.options)} settings · {on} on, {len(self.options) - on} off"

    def _toggle(self, *, key: str, on: bool) -> SettingButton:
        """Returns the on/off accessory for one setting."""
        return SettingButton(
            action=f"toggle:{key}",
            label="On" if on else "Off",
            emoji="✔️" if on else "✖️",
            style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Rejects anyone but the admin the panel was opened for."""
        if interaction.user.id != self.owner.id:
            await interaction.response.send_message(
                content=f"That panel isn't yours! {self.cog.emoji_table.kuma_shrug}",
                ephemeral=True,
            )
            return False
        return True

    async def _dispatch(self, *, interaction: discord.Interaction, action: str) -> None:
        """Applies a press and re-renders the panel in place."""
        try:
            if action.startswith("toggle:"):
                key: str = action.removeprefix("toggle:")
                # The key came off our own row, but it travels through a custom ID to get back here,
                # so `set_mod_settings` validating it against the column list still earns its keep.
                updated: ModeratorSettings | None = await self.cog.set_mod_settings(
                    guild=self.guild,
                    setting=key,
                    value=not bool(self.settings[key]),  # type: ignore - see above.
                )
            elif action == "reset":
                updated = await self.cog.reset_mod_settings(guild=self.guild)
            else:
                return
        except (ConnectionError, ValueError, sqlite3.DatabaseError):
            updated = None

        if updated is None:
            # The panel on screen still shows what is stored, so leave it be and say so alongside.
            await interaction.response.send_message(
                content=f"We encountered an error saving that. {self.cog.emoji_table.kuma_crying}",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(
            view=ModeratorSettingsPanel(cog=self.cog, guild=self.guild, owner=self.owner, settings=updated),
        )


class AutoModEmbed(KumaEmbed):
    """Auto Moderation Embed.

    By default sets the ``Thumbnail`` for the :class:`KumaEmbed` to the :class:`discord.Member.avatar.url`

    .. note::
        - Default ``color`` is :meth:`discord.Color.red()`
        - The ``reason`` parameter is used for the Embed ``description``.


    """

    def __init__(
        self,
        mod_action: Literal["Ban", "Kick", "Timeout"],
        user: discord.Member | discord.User,
        guild: discord.Guild,
        reason: Optional[str] = None,
        *,
        cog: Cog,
        **kwargs: Unpack[EmbedParams],
    ) -> None:
        """Auto Mod __init__.

        Parameters
        ----------
        mod_action: :class:`str`
            eg. Ban, Kick, Timeout.
        user: :class:`discord.Member | discord.User`
            The Discord User or Member object.
        guild: :class:`discord.Guild`
            The Discord Guild.
        cog: :class:`Cog`
            The Cog using this embed.
        reason: :class:`Optional[str]`, optional
            The reason the User or Member had action taken against them, by default None.

        """
        kwargs["title"] = f"Auto-Mod | {guild}"

        if reason is not None:
            kwargs["description"] = reason

        if kwargs.get("color") is None:
            kwargs["color"] = discord.Color.red()

        super().__init__(cog=cog, **kwargs)
        self.add_field(name=f"__{self.cog.string_inflection(mod_action)} User__", value=user.display_name)

        if isinstance(user, discord.Member) and user.avatar is not None:
            self.set_thumbnail(url=user.avatar.url)


class Moderator(Cog):
    """Moderator type commands and functionality for Discord."""

    repo_url: str = "https://github.com/k8thekat/public_dpy_modules"
    guild_settings = dict[int, ModeratorSettings]  # key will be the guild ID -> guild settings

    CODEBLOCK_PATTERN: re.Pattern[str] = re.compile(
        pattern=r"`{3}(?P<LANG>\w+)?\n?(?P<CODE>(?:(?!`{3}).)+)\n?`{3}",
        flags=re.DOTALL | re.MULTILINE,
    )
    SPAM_LIMIT: int = 3
    # Allowed column names for UPDATE - guards against SQL injection via the setting parameter.
    _MOD_SETTING_COLUMNS: frozenset[str] = frozenset({"use_mystbin", "spam_filter"})
    spam_messages: dict[int, MessageRecords]

    # Global shorthand hash table - persisted across restarts.
    _banned_hash_file: Path = Path(__file__).parent.joinpath("moderator_hashes.json")
    banned_hashes: set[str]

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    async def cog_load(self) -> None:
        async with self.bot.pool.acquire() as conn:
            await conn.execute(MODERATOR_SETUP_SQL)
            # await self.migrate(conn=conn)
        self.spam_messages = {}
        self.banned_hashes = self._load_banned_hashes()

    def _load_banned_hashes(self) -> set[str]:
        """Loads the global ban hash table from `moderator_hashes.json`."""
        if not self._banned_hash_file.is_file():
            return set()
        try:
            data: list[str] = json.loads(self._banned_hash_file.read_text())
            return set(data)
        except (json.JSONDecodeError, OSError):
            LOGGER.exception("<%s.%s> | Failed to load banned hashes.", __class__.__name__, "_load_banned_hashes")
            return set()

    def _save_banned_hashes(self) -> None:
        """Saves the global ban hash table to `moderator_hashes.json`."""
        try:
            self._banned_hash_file.write_text(json.dumps(list(self.banned_hashes)))
        except OSError:
            LOGGER.exception("<%s.%s> | Failed to save banned hashes.", __class__.__name__, "_save_banned_hashes")

    async def get_mod_settings(self, guild: discord.Guild) -> ModeratorSettings | None:
        """Retrieves the Moderator Settings for the provided Discord guild.

        Parameters
        ----------
        guild: :class:`discord.Guild`
            The Discord guild to get Moderator settings for.

        Returns
        -------
        :class:`ModeratorSettings | None`
            The settings related to the Discord guild.

        Raises
        ------
        :exc:`ConnectionError`
            Raises a connection error if unable to connect to the Database for any reason.

        """
        try:
            async with self.bot.pool.acquire() as conn:
                res: ModeratorSettings | None = await conn.fetchone("""SELECT * FROM moderator WHERE serverid = ?""", guild.id)  # type: ignore - I know the dataset because of above.
                if res is None:
                    # Hand back the row we just made rather than falling out of the `try` with `None`;
                    # otherwise a guild's first `/settings` inserts the defaults and then reports an
                    # error, and only the second call shows anything.
                    return await self.set_mod_settings(guild=guild, default=True)
                return res
        except Exception as e:
            LOGGER.exception(
                "<%s.%s> | We encountered an error connecting to the database. | GuildID: %s",
                __class__.__name__,
                "get_mod_settings",
                guild.id,
                exc_info=e,
            )
            msg = "Unable to connect to the database."
            raise ConnectionError(msg) from None

    async def set_mod_settings(
        self,
        guild: discord.Guild,
        setting: str | None = None,
        value: bool = False,
        default: bool = False,
    ) -> ModeratorSettings | None:
        """Set or update Moderator specific settings for the provided Discord guild.

        Parameters
        ----------
        guild: :class:`discord.Guild`
            The Discord guild object.
        setting: :class:`str`, optional
            The column name in the moderator table to update (e.g. ``"use_mystbin"``).
            Must be one of :attr:`_MOD_SETTING_COLUMNS`. Required when ``default`` is False.
        value: :class:`bool`, optional
            The value to write to ``setting``, by default False.
        default: :class:`bool`, optional
            When True, inserts a new row with default values for the guild instead of
            updating an existing one. Use this for initial guild setup.

        Returns
        -------
        :class:`ModeratorSettings | None`
            The Discord guild specific Moderator settings.

        Raises
        ------
        :exc:`ValueError`
            If ``default`` is False and ``setting`` is None or not a valid column name.
        :exc:`sqlite3.DatabaseError`
            If the INSERT or UPDATE returns no row.
        :exc:`ConnectionError`
            If we are unable to connect to the Database.

        """
        if not default and (setting is None or setting not in self._MOD_SETTING_COLUMNS):
            msg = f"setting must be one of {self._MOD_SETTING_COLUMNS!r}, got {setting!r}."
            raise ValueError(msg)

        try:
            async with self.bot.pool.acquire() as conn:
                if default:
                    # `WHERE NOT EXISTS` rather than a bare INSERT, which is how one guild ended up
                    # with two rows. Written this way instead of `ON CONFLICT` because that needs a
                    # constraint to name, and `migrate` cannot add the index while a guild's rows
                    # still disagree - so this has to be correct without one.
                    data: ModeratorSettings | None = await conn.fetchone(
                        """INSERT INTO moderator(serverid) SELECT ?
                           WHERE NOT EXISTS (SELECT 1 FROM moderator WHERE serverid = ?) RETURNING *""",
                        guild.id,
                        guild.id,
                    )  # pyright: ignore[reportAssignmentType]
                    if data is None:
                        # The row was already there, so nothing was inserted and nothing returned.
                        data = await conn.fetchone("""SELECT * FROM moderator WHERE serverid = ? ORDER BY id""", guild.id)  # pyright: ignore[reportAssignmentType]
                else:
                    data = await conn.fetchone(
                        f"""UPDATE moderator SET {setting} = ? WHERE serverid = ? RETURNING *""",  # noqa: S608 - column name validated above
                        value,
                        guild.id,
                    )  # pyright: ignore[reportAssignmentType]

                # if data is None:
                return data

        except sqlite3.DatabaseError:
            LOGGER.exception(
                "<%s.%s> | We encountered an error %s a row in the database. | GuildID: %s",
                __class__.__name__,
                "set_mod_settings",
                "inserting" if default else "updating",
                guild.id,
            )
            msg = f"Unable to {'insert' if default else 'update'} a row in the database."
            raise sqlite3.DatabaseError(msg) from None

        except Exception as e:
            LOGGER.exception(
                "<%s.%s> | We encountered an error connecting to the database. | GuildID: %s",
                __class__.__name__,
                "set_mod_settings",
                guild.id,
                exc_info=e,
            )
            msg = "Unable to connect to the database."
            raise ConnectionError(msg) from None

    async def reset_mod_settings(self, guild: discord.Guild) -> ModeratorSettings | None:
        """Drop the Discord guild's row, then let :meth:`get_mod_settings` re-create it.

        Deleting rather than writing each column back by hand means a column added to `moderator`
        later comes back at whatever default it declares, with nothing to keep in step here.

        Parameters
        ----------
        guild: :class:`discord.Guild`
            The Discord guild object.

        Returns
        -------
        :class:`ModeratorSettings | None`
            The Discord guild specific Moderator settings, back at their defaults.

        Raises
        ------
        :exc:`ConnectionError`
            If we are unable to connect to the Database.

        """
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""DELETE FROM moderator WHERE serverid = ?""", guild.id)
        return await self.get_mod_settings(guild=guild)

    async def preference(self, *, user: Union[discord.User, discord.Member], setting: str, default: bool) -> bool:
        """Reads one of a user's preferences, answering `default` when Preferences is not loaded.

        Reached through `get_cog` rather than an import, so the two cogs stay independently
        reloadable - the same route :meth:`HintsCog.preference` takes.

        Parameters
        ----------
        user: :class:`Union[discord.User, discord.Member]`
            Whose preference to read.
        setting: :class:`str`
            The `user_settings` column name.
        default: :class:`bool`
            The answer when the Preferences cog is not loaded.

        Returns
        -------
        :class:`bool`
            The stored preference, or `default`.

        """
        cog = self.bot.get_cog("Preferences")
        if cog is None:
            return default
        return await cog.enabled(user=user, setting=setting)  # type: ignore - the Preferences cog owns this.

    @commands.Cog.listener(name="on_message")
    async def on_message_listener(self, message: discord.Message) -> None:
        # ignore ourselves and any other bot accounts.
        if message.author == self.bot.user or message.author.bot is True:
            return

        # ignore messages that start with a prefix.
        if message.content.startswith(tuple(await _get_prefix(bot=self.bot, message=message))):
            return
        # ignore messages not in a guild.
        if message.guild is None:
            return

        res: ModeratorSettings | None = await self.get_mod_settings(guild=message.guild)
        if res is None:
            return

        # Logic check for auto_mystbin.
        if (
            bool(res["use_mystbin"])
            and message.channel.type is not discord.ChannelType.news
            and len(message.content) > 1000
            and await self.preference(user=message.author, setting="auto_mystbin", default=True)
        ):
            await self._auto_on_mystbin(message=message)

        if bool(res["spam_filter"]):
            await self._duplicate_attachment_check(message)

    async def thread_rename_wanted(self, *, thread: discord.Thread) -> bool:
        """Whether the thread's owner wants Kuma Kuma marking their thread titles.

        The owner decides rather than whoever pressed lock, since it is their thread that ends up
        wearing the marker. An owner we cannot resolve answers with the preference's default: this
        runs on every thread update in every guild, which is no place to spend a member fetch.

        Parameters
        ----------
        thread: :class:`discord.Thread`
            The thread whose title would be rewritten.

        Returns
        -------
        :class:`bool`
            Whether to go ahead with the rename.

        """
        owner: Optional[Union[discord.Member, discord.User]] = thread.owner
        if owner is None and thread.owner_id is not None:
            owner = self.bot.get_user(thread.owner_id)
        if owner is None:
            return True
        return await self.preference(user=owner, setting="thread_rename", default=True)

    @commands.Cog.listener(name="on_thread_update")
    async def mod_on_thread_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        """Marks a thread's title with `[LOCKED]` or `[CLOSED]` when it becomes either.

        .. note::
            Threads Kuma Kuma owns are skipped outright.

        Parameters
        ----------
        before: :class:`discord.abc.GuildChannel`
            The channel before the update.
        after: :class:`discord.abc.GuildChannel`
            The channel after the update.

        """
        if not isinstance(before, discord.Thread) or not isinstance(after, discord.Thread):
            return

        if self.bot.is_me(after):
            return

        if before.permissions_for(before.guild.me).manage_threads is False:
            return

        # Either marker already present on either side means there is nothing left to do - and since
        # the `archived` branch below re-opens the thread to rename it, which dispatches this listener
        # all over again, this is also what stops it recursing.
        markers: tuple[str, ...] = (LOCKED_PREFIX.lower(), CLOSED_PREFIX.lower())
        if before.name.lower().startswith(markers) or after.name.lower().startswith(markers):
            return

        if not await self.thread_rename_wanted(thread=after):
            return

        if before.locked is False and after.locked is True:
            await after.edit(name=f"{LOCKED_PREFIX}{after.name}"[:THREAD_TITLE_SIZE])
            LOGGER.info(
                "<%s.%s> | Marked a locked Thread. | Guild ID: %s | Thread ID: %s | Name: %s",
                __class__.__name__,
                "mod_on_thread_update",
                before.guild.id,
                before.id,
                after.name,
            )
            return

        if before.archived is False and after.archived is True:
            # An archived thread refuses edits, so it has to be re-opened to be renamed, then sent
            # back to archived in the same call.
            reopened: discord.Thread = await after.edit(archived=False)
            await reopened.edit(name=f"{CLOSED_PREFIX}{after.name}"[:THREAD_TITLE_SIZE], archived=True)
            LOGGER.info(
                "<%s.%s> | Marked an archived Thread. | Guild ID: %s | Thread ID: %s | Name: %s",
                __class__.__name__,
                "mod_on_thread_update",
                before.guild.id,
                before.id,
                after.name,
            )

    async def _auto_on_mystbin(self, message: Message) -> None:
        """Converts a `discord.Message` into a Mystbin URL.

        Parameters
        ----------
        message: :class:`Message`
            The Discord Message to be converted.

        """
        content: str = message.content
        assert message.guild  # noqa: S101 # we are force checking this function earlier to make sure we are in a guild only.
        files: list[tuple[str, str]] = []

        should_upload_to_bin: bool = False

        for idx, match in enumerate(iterable=self.CODEBLOCK_PATTERN.finditer(string=content), start=1):
            language: str = match.group("LANG") or "python"
            filename: str = f"File-{idx}.{language}"
            file_content: str = match.group("CODE")

            files.append((filename, file_content))
            content = content.replace(match.group(), f"`[{filename}]`")

            should_upload_to_bin = should_upload_to_bin or len(file_content) > 1100
        if should_upload_to_bin:
            paste: mystbin.Paste = await self.bot.loghandler.create_paste(files=files, session=self.bot.session)

            myst_embed = discord.Embed(
                color=discord.Color.green(),
                description=f"{content}\n\nHey {message.author.mention}, {BOT_NAME} moved your codeblock(s) to `Mystbin`",
                timestamp=discord.utils.utcnow(),
            )
            myst_embed.set_author(name=message.author.name, icon_url=message.author.display_avatar.url)
            myst_embed.add_field(name="", value=paste.url)
            myst_embed.set_footer(text="Generated by `auto_on_mystbin`")

            await message.channel.send(embed=myst_embed)

            if message.channel.permissions_for(message.guild.me).manage_messages:
                await message.delete()

    async def _duplicate_attachment_check(self, message: discord.Message) -> None:
        """Checks an incoming message for duplicate attachments or URLs and bans the author if spam is detected.

        Uses sha256 to compare a shortened 16 char hash of recent attachments, after 2 different hashes the array is truncated.

        - After 3 minutes since the first duplicate, we reset the timestamp.

        Parameters
        ----------
        message: :class:`discord.Message`
            The Discord message to inspect.

        """
        # If we are in a Guild and the Guild member has admin, ignore.
        if isinstance(message.author, User) or message.guild is None:
            LOGGER.debug(
                "<%s.%s> | Duplicate Attachment Check Failed | Author Type: %s | Guild: %s",
                __class__.__name__,
                "duplicate_attachment_check",
                type(message.author),
                message.guild,
            )
            return

        if message.stickers:
            LOGGER.debug(
                "<%s.%s> | Duplicate Attachment Check Failed | User: %s | Stickers: %s",
                __class__.__name__,
                "duplicate_attachment_check",
                message.author,
                len(message.stickers),
            )
            return

        if message.author.guild_permissions.administrator is True:
            LOGGER.debug(
                "<%s.%s> | Duplicate Attachment Check Failed | User: %s | Admin: %s",
                __class__.__name__,
                "duplicate_attachment_check",
                message.author,
                message.author.guild_permissions.administrator,
            )
            return

        check_attachments: bool = False
        check_content: bool = False

        if len(message.attachments) != 0:
            # LOGGER.info("User sent an Attachment")
            check_attachments = True

        urls: list[str] = re.findall(HTTP_REGEX, message.content)
        if urls:
            check_content = True
            # LOGGER.info("User sent Content URL(s). | URLs: %s", urls)

        if check_attachments or check_content:
            LOGGER.debug(
                "<%s.%s> | Author: %s | Author Type: %s | Author Admin: %s | Message Guild: %s | Msg Attachment Count: %s",
                __class__.__name__,
                "duplicate_attachment_check",
                message.author,
                type(message.author),
                message.author.guild_permissions.administrator,
                message.guild,
                len(message.attachments),
            )

        record: MessageRecords = self.spam_messages.get(message.author.id, MessageRecords(count=0))
        # LOGGER.info("User: %s | Record: %s", message.author, record)

        if check_attachments:
            for cur_attachment in message.attachments:
                compare = await self._hash_parse(author=message.author, record=record, url=cur_attachment.url)
                if compare:
                    LOGGER.warning(
                        "<%s.%s> | User sent a duplicate Attachment. | User: %s | Guild ID: %s | Attachment URL: %s",
                        __class__.__name__,
                        "_duplicate_attachment_check",
                        message.author,
                        message.guild.id,
                        cur_attachment.url,
                    )
        if check_content:
            for url in urls:
                compare = await self._hash_parse(author=message.author, record=record, url=url)
                if compare:
                    LOGGER.warning(
                        "<%s.%s> | User sent duplicate Content URL. | User: %s | Guild ID: %s | URL: %s",
                        __class__.__name__,
                        "_duplicate_attachment_check",
                        message.author,
                        message.guild.id,
                        url,
                    )

        # If the users count breaks SPAM LIMIT, we try to ban the user.
        if record.count >= self.SPAM_LIMIT:
            cur_time: datetime.datetime = datetime.datetime.now(tz=datetime.UTC)
            # If they triggered the SPAM LIMIT and it's been over 1 minute since the first "dupe";
            # ignore the count, reset the counter to 1 (as it's still a "dupe") and adjust the timestamp for a new 1 minute window.
            if cur_time - record.timestamp > datetime.timedelta(minutes=1):
                LOGGER.info(
                    "<%s.%s> | Reset Users Spam Record Count and Timestamp. | User: %s",
                    __class__.__name__,
                    "duplicate_attachment_check",
                    message.author,
                )
                record.count = 1
                record.timestamp = cur_time
                self.spam_messages.update({message.author.id: record})
                return

            user_guild: discord.Guild | None = self.bot.get_guild(message.guild.id)
            if user_guild is None:
                LOGGER.warning(
                    "<%s.%s> | Failed to lookup a Message Guild ID | Guild ID: %s | Message: %s",
                    __class__.__name__,
                    "_duplicate_attachment_check",
                    message.guild.id,
                    message,
                )
                return

            try:
                await user_guild.ban(user=message.author, reason="Spam or Hacked account")
                LOGGER.info(
                    "<%s.%s> | Banned User for Spam/Duplicate Images. | User/Member: %s",
                    __class__.__name__,
                    "_duplicate_attachment_check",
                    message.author,
                )
                # Append the banned user's hashes to the global banned hash table.
                self.banned_hashes.update(record.hashes)
                await asyncio.to_thread(self._save_banned_hashes)
            # In case we do not have permissions in the server we are in.
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.exception(
                    "<%s.%s> | Failed to Ban a User for Spam | User/Member: %s",
                    __class__.__name__,
                    "_duplicate_attachment_check",
                    message.author,
                )
                return

            # Only send a message if we don't trigger the except.
            embed = AutoModEmbed(cog=self, mod_action="Ban", user=message.author, guild=user_guild, reason="Spam/Duplicate messages.")
            embed.add_field(name="__Attachments__", value="\n>".join(list(record.urls)))
            await self.bot.owner.send(embed=embed, silent=True)
        # LOGGER.info("User Record: %s", record)

    async def _hash_parse(self, author: discord.Member, record: MessageRecords, url: str) -> bool:
        data: bytes | None = await self.get_request(url=url)
        if data is None:
            return False
        # Use the bytes from the get request, truncate to 16 chars and compare against existing hashes.
        res = sha256(data).hexdigest()[:16]

        # Check against the global banned hash table first.
        if res in self.banned_hashes:
            LOGGER.warning(
                "<%s.%s> | Hash matched a entry of our global banned hash table. | User: %s | Hash: %s",
                __class__.__name__,
                "_hash_parse",
                author,
                res,
            )
            record.count = self.SPAM_LIMIT
            self.spam_messages.update({author.id: record})
            return True

        duplicate: bool = res in record.hashes
        if not duplicate:
            # LOGGER.info("Added a Hash to the User")
            record.hashes = res
            record.urls = url
        else:
            # LOGGER.info("Found a duplicate Hash for the User")
            record.count += 1
            if record.count == 1:
                record.timestamp = datetime.datetime.now(tz=datetime.UTC)

        # Always persist so new hashes survive to the next message.
        self.spam_messages.update({author.id: record})
        return duplicate

    @commands.command(name="reload", help="Reloads all extensions unless specified.")
    @commands.is_owner()
    async def reload(self, context: Context, args: Optional[str] = None) -> None:
        await context.typing(ephemeral=True)
        _flag = False
        try:
            # Re-scan the extensions directory so new files are picked up without a restart.
            current_extensions: list[ModuleInfo] = extensions.discover_extensions()
            extensions.EXTENSIONS = current_extensions

            name = "UNK"
            new_count = 0
            for extension in current_extensions:
                if isinstance(extension, ModuleInfo):
                    name = extension.name.split(".")[1]
                    is_new = extension.name not in self.bot.extensions

                    # If any additional args; attempt to find a match.
                    if args is not None and args.lower() in extension.name.lower():
                        if is_new:
                            await self.bot.load_extension(name=extension.name)
                        else:
                            # Refresh utils.* dependencies so the extension re-imports fresh code.
                            reload_module_dependencies(extension.name)
                            await self.bot.reload_extension(name=extension.name)
                        LOGGER.info("Loaded %sextension: %s", "module " if extension.ispkg else "", extension.name)
                        _flag = True
                        break

                    # else we have no args; reload/load each module each iteration.
                    if args is None:
                        if is_new:
                            await self.bot.load_extension(name=extension.name)
                            new_count += 1
                        else:
                            # Refresh utils.* dependencies so the extension re-imports fresh code.
                            reload_module_dependencies(extension.name)
                            await self.bot.reload_extension(name=extension.name)
                        LOGGER.info("Loaded %sextension: %s", "module " if extension.ispkg else "", extension.name)

            # `help_command` is an instance built once at startup, so reloading `utils.help` alone
            # leaves the old object still answering. Rebound here so a reload picks up help edits
            # rather than needing a full restart to test one.
            with contextlib.suppress(Exception):
                self.bot.refresh_help_command()

            if _flag:
                await context.send(
                    content=f"Reloaded the `{name}` extension. {self.emoji_table.kuma_happy}",
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )
                return

            await context.send(
                content=(
                    f"Reloaded all {len(current_extensions)} extensions. {self.emoji_table.kuma_star_eye}"
                    + (f" ({new_count} newly loaded)" if new_count else "")
                ),
                ephemeral=True,
                delete_after=self.message_timeout,
            )
        except Exception as e:
            LOGGER.exception("<%s.%s> | We encountered an error executing %s", __class__.__name__, "reload", context.command, exc_info=e)
            await context.send(
                content=f"We encountered an error reloading... {self.emoji_table.kuma_crying}\n{e}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

    @commands.is_owner()
    @commands.command(name="restart", help=f"Restarts the entire {BOT_NAME} process.", hidden=True)
    async def restart(self, context: Context) -> None:
        """Restarts the whole bot, not just the extensions."""
        LOGGER.info(
            "<%s.%s> | Restart requested by %s (%s).",
            __class__.__name__,
            "restart",
            context.author.name,
            context.author.id,
        )
        # `on_command_completion` normally clears the invocation, but it runs after we are already
        # closed and its HTTP call would just raise; do it here while the connection is still up.
        with contextlib.suppress(discord.HTTPException):
            await context.message.delete()

        # No `delete_after` - the task that would do the deleting dies with the event loop. This is
        # the last thing said before the process is replaced, so it stays up.
        await context.send(content=f"Be right back... {self.emoji_table.kuma_tea}", track=True)
        await self.bot.restart()

    @commands.command(name="sync", help=f"Sync the {BOT_NAME} commands to the guild.")
    @commands.is_owner()
    @commands.guild_only()
    async def sync(self, context: GuildContext, local: bool = True, reset: bool = False) -> Message:
        """Push the application command tree to Discord."""
        # `local` (default) syncs this guild only; `local=False` syncs globally, which Discord can
        # take up to an hour to roll out. `reset` clears the commands from that scope before syncing.
        await context.typing(ephemeral=True)
        scope: Optional[discord.Guild] = context.guild if local is True else None
        where: str = f"to {context.guild.name}" if local is True else "globally"

        if reset is True:
            self.bot.tree.clear_commands(guild=scope)
            synced: list[app_commands.AppCommand] = await self.bot.tree.sync(guild=scope)
            LOGGER.info(
                "<%s.%s> | Commands reset and sync'd. Clear your client cache (ctrl+F5). | Where: %s | Count: %s | By: %s",
                __class__.__name__,
                "sync",
                where,
                len(synced),
                context.author.name,
            )
            return await context.send(
                content=f"**WARNING** Reset `{self.bot.user.name}s` commands {where}. {self.emoji_table.kuma_bleh}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        if local is True:
            self.bot.tree.copy_global_to(guild=context.guild)

        synced = await self.bot.tree.sync(guild=scope)
        LOGGER.info(
            "<%s.%s> | Commands sync'd. | Where: %s | Count: %s | By: %s",
            __class__.__name__,
            "sync",
            where,
            len(synced),
            context.author.name,
        )
        # A global sync is not instant, so it says so rather than implying the commands are live.
        note: str = "" if local is True else "\n-# Discord can take up to an hour to roll a global sync out."
        return await context.send(
            content=f"Sync'd {len(synced)} of `{self.bot.user.name}s` commands {where}. {self.emoji_table.kuma_happy}{note}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @commands.group(name="prefix", invoke_without_command=True)
    @commands.guild_only()
    async def prefix(self, context: GuildContext) -> Message:
        """Returns a list of the current prefixes for the current guild."""
        async with self.bot.pool.acquire() as conn:
            res: list[Row] = await conn.fetchall("""SELECT prefix FROM prefix WHERE serverid = ?""", context.guild.id)
            if len(res) > 0:
                prefixes = "\n".join([entry["prefix"] for entry in res])
                return await context.send(
                    content=f"**Current Prefixes:** {self.emoji_table.kuma_peak}\n{prefixes}",
                    delete_after=self.message_timeout,
                    ephemeral=True,
                )
            return await context.send(
                content=f"No prefixes set for this server. {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
                ephemeral=True,
            )

    @prefix.command(name="add", help=f"Add a prefix to {BOT_NAME}", aliases=["prea", "pa"])
    @commands.is_owner()
    @commands.guild_only()
    async def add_prefix(self, context: GuildContext, prefix: str) -> Message:
        cleaned: str = prefix.lstrip()
        if not cleaned:
            return await context.send(
                content=f"A prefix needs to be something. {self.emoji_table.kuma_hmm}",
                delete_after=self.message_timeout,
                ephemeral=True,
            )

        # `prefix` has no UNIQUE constraint, so the same prefix could be stored any number of times.
        if cleaned in await self.bot.guild_prefixes(guild_id=context.guild.id):
            return await context.send(
                content=f"`{cleaned}` is already a prefix here. {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
                ephemeral=True,
            )

        async with self.bot.pool.acquire() as conn:
            await conn.execute("""INSERT INTO prefix(serverid, prefix) VALUES(?, ?)""", context.guild.id, cleaned)

        # The cache is keyed per guild and never expires on its own, so every write has to say so.
        self.bot.invalidate_prefixes(context.guild.id)
        return await context.send(
            content=f"Added the prefix `{cleaned}` for {context.guild.name}. {self.emoji_table.kuma_happy}",
            delete_after=self.message_timeout,
            ephemeral=True,
        )

    @prefix.command(name="delete", help=f"Delete a prefix from {BOT_NAME} for a guild.", aliases=["pred", "pd"])
    @commands.is_owner()
    @commands.guild_only()
    async def delete_prefix(self, context: GuildContext, prefix: str) -> Message:
        async with self.bot.pool.acquire() as conn:
            cur: Cursor = await conn.execute(
                """DELETE FROM prefix WHERE serverid = ? AND prefix = ?""",
                context.guild.id,
                prefix.lstrip(),
            )
            removed: int = cur.get_cursor().rowcount

        self.bot.invalidate_prefixes(context.guild.id)
        if removed == 0:
            return await context.send(
                content=f"`{prefix.lstrip()}` isn't a prefix here. {self.emoji_table.kuma_shrug}",
                delete_after=self.message_timeout,
            )
        return await context.send(
            content=f"Removed the prefix `{prefix.lstrip()}`. {self.emoji_table.kuma_chuckle}",
            delete_after=self.message_timeout,
        )

    @prefix.command(name="clear", help=f"Clear all prefixes for {BOT_NAME} in a guild.", aliases=["prec", "pc"])
    @commands.is_owner()
    @commands.guild_only()
    async def clear_prefix(self, context: GuildContext) -> Message:
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""DELETE FROM prefix WHERE serverid = ?""", context.guild.id)

        self.bot.invalidate_prefixes(context.guild.id)
        return await context.send(
            content=f"Cleared all prefixes for {context.guild.name}. `{DEFAULT_PREFIX}` still works. {self.emoji_table.kuma_tea}",
            delete_after=self.message_timeout,
            ephemeral=True,
        )

    async def autocomplete_trusted(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:  # noqa: ARG002
        """Offers everyone currently trusted, so `trusted remove` never needs an ID typed by hand.

        Reads `bot.owner_ids` rather than the `owners` table: that set is what the checks actually
        consult, and it is the thing being removed from.

        Parameters
        ----------
        interaction: :class:`discord.Interaction`
            The interaction being autocompleted. Unused; the trusted list is global.
        current: :class:`str`
            What has been typed into the parameter so far.

        Returns
        -------
        :class:`list[app_commands.Choice[str]]`
            Up to 25 matching users, each carrying their ID as the value.

        """
        choices: list[app_commands.Choice[str]] = []
        for owner_id in sorted(self.bot.owner_ids):
            # Mine is seeded in `__init__` rather than stored, and `trusted remove` refuses it
            # anyway, so offering it would only be a way to be told no.
            if owner_id == self.bot.owner_user_id:
                continue

            user: Optional[User] = self.bot.get_user(owner_id)
            label: str = f"{user.display_name} ({owner_id})" if user is not None else str(owner_id)
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=str(owner_id)))
        return choices[:25]

    @commands.group(name="trusted", invoke_without_command=True, aliases=["trust"])
    @commands.is_owner()
    @app_commands.default_permissions(administrator=True)
    async def trusted(self, context: Context) -> Message:
        """See everyone trusted with owner only commands."""
        return await self.trusted_list(context)

    @trusted.command(name="list", aliases=["ls"])
    @commands.is_owner()
    async def trusted_list(self, context: Context) -> Message:
        """See everyone trusted with my owner only commands."""
        async with self.bot.pool.acquire() as conn:
            res: list[Row] = await conn.fetchall("""SELECT ownerid FROM owners""")

        # The union of the table and the live set. My own ID is seeded into `owner_ids` at startup
        # and has no row of its own, so the table alone under-reports who can actually run what.
        owner_ids: set[int] = {entry["ownerid"] for entry in res} | set(self.bot.owner_ids)

        lines: list[str] = []
        for owner_id in sorted(owner_ids):
            # A trusted user need not share a guild with where the command runs, so resolve by user,
            # not member.
            user: Optional[User] = self.bot.get_user(owner_id)
            if user is None:
                with contextlib.suppress(discord.HTTPException):
                    user = await self.bot.fetch_user(owner_id)

            name: str = user.display_name if user is not None else "*(unknown user)*"
            marker: str = " · that's you" if owner_id == self.bot.owner_user_id else ""
            lines.append(f"- {name} `{owner_id}`{marker}")

        return await context.send(
            content=f"### Trusted with my owner only commands {self.emoji_table.kuma_peak}\n" + "\n".join(lines),
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @trusted.command(name="add")
    @commands.is_owner()
    @app_commands.describe(member="The Discord user to trust with my owner only commands.")
    async def trusted_add(self, context: Context, member: Union[Member, User]) -> Message:
        """Trust someone with my owner only commands."""
        if member.id in self.bot.owner_ids:
            return await context.send(
                content=f"{member.display_name} is already trusted. {self.emoji_table.kuma_hmm}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        async with self.bot.pool.acquire() as conn:
            # `ownerid` is UNIQUE, so a second add for the same person is a no-op rather than a
            # duplicate row or a raised constraint.
            await conn.execute("""INSERT INTO owners(ownerid) VALUES(?) ON CONFLICT(ownerid) DO NOTHING""", member.id)

        self.bot.owner_ids.add(member.id)
        LOGGER.info(
            "<%s.%s> | Trusted a user. | User: %s | User ID: %s | By: %s",
            __class__.__name__,
            "trusted_add",
            member.name,
            member.id,
            context.author.name,
        )
        return await context.send(
            content=f"Added {member.display_name} to the trusted list. {self.emoji_table.kuma_star_eye}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @trusted.command(name="remove", aliases=["rm", "delete"])
    @commands.is_owner()
    @app_commands.describe(member="The trusted user to remove; pick one from the list.")
    @app_commands.autocomplete(member=autocomplete_trusted)
    async def trusted_remove(self, context: Context, member: str) -> Message:
        """Take away someone's access to my owner only commands."""
        cleaned: str = member.strip().removeprefix("<@").removeprefix("!").removesuffix(">")
        if not cleaned.isdigit():
            return await context.send(
                content=f"I need a user ID or a mention there. {self.emoji_table.kuma_hmm}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        owner_id: int = int(cleaned)
        if owner_id == self.bot.owner_user_id:
            return await context.send(
                content=f"That one's me, and I'm not going anywhere. {self.emoji_table.kuma_pout}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        if owner_id not in self.bot.owner_ids:
            return await context.send(
                content=f"`{owner_id}` isn't on the trusted list. {self.emoji_table.kuma_shrug}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        try:
            async with self.bot.pool.acquire() as conn:
                cur: Cursor = await conn.execute("""DELETE FROM owners WHERE ownerid = ?""", owner_id)
                removed: int = cur.get_cursor().rowcount
        except sqlite3.DatabaseError:
            LOGGER.exception(
                "<%s.%s> | We encountered an error deleting from the owners table. | User ID: %s",
                __class__.__name__,
                "trusted_remove",
                owner_id,
            )
            return await context.send(
                content=f"We encountered an error updating the database. {self.emoji_table.kuma_crying}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        # `discard`, not `remove`: the set and the table are allowed to disagree - a seeded owner ID
        # has no row - so `remove` could `KeyError` on an id that was never in the set.
        self.bot.owner_ids.discard(owner_id)
        LOGGER.info(
            "<%s.%s> | Removed a trusted user. | User ID: %s | Rows: %s | By: %s",
            __class__.__name__,
            "trusted_remove",
            owner_id,
            removed,
            context.author.name,
        )
        return await context.send(
            content=f"Removed `{owner_id}` from the trusted list. {self.emoji_table.kuma_chuckle}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @commands.command(
        name="clear",
        help="Removes the bot's messages, or everyone's. Reply to a message to clear everything after it.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @commands.guild_only()
    @app_commands.describe(all_messages=f"Remove everyone's messages, not just {BOT_NAME}'s. Defaults to False.")
    async def clear(
        self,
        context: GuildContext,
        all_messages: bool = False,
        amount: int = 15,
    ) -> Message:
        """Delete messages in this channel; the bot's own by default, everyone's with `all_messages`."""
        messages: list[discord.Message] = []
        anchor: Union[discord.abc.Snowflake, None] = None

        # A forward carries a reference too, and points at a message in someone else's channel; only
        # a genuine reply is an instruction about *this* one.
        reference: Union[discord.MessageReference, None] = context.message.reference
        if (
            reference is not None
            and reference.type is discord.MessageReferenceType.default
            and reference.message_id is not None
            and reference.channel_id == context.channel.id
        ):
            # `purge(after=...)` is exclusive, which is what we want: the replied-to message is the
            # marker for how far back to go, not part of what goes. Same rule as the message ID path
            # below, so both ways of saying "clear back to here" leave the same thing standing.
            anchor = discord.Object(id=reference.message_id)

        # A Discord Message ID passed in as our amount.
        elif len(str(amount)) > 12:
            try:
                til_message: discord.Message = await context.channel.fetch_message(amount)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                # Returned, not just reported: falling through would leave `amount` as the purge
                # limit, and a message ID is a nineteen digit number of messages to delete.
                return await context.reply(
                    content=f"We were unable to find the Discord Message ID provided. {self.emoji_table.kuma_hmm}",
                    delete_after=self.message_timeout,
                )
            anchor = discord.Object(id=til_message.id)

        limit: Union[int, None] = None if anchor is not None else amount

        if all_messages:
            # Clearing everyone's messages requires elevated permissions.
            if context.author.id not in self.bot.owner_ids and not context.channel.permissions_for(context.author).manage_messages:
                return await context.reply(
                    content=f"I don't have the permissions to do that... {self.emoji_table.kuma_pout}",
                    delete_after=self.message_timeout,
                )
            try:
                messages = await context.channel.purge(
                    limit=limit,
                    check=lambda m: m.id != context.message.id,
                    bulk=False,
                    after=anchor,
                )
            except discord.errors.Forbidden:
                return await context.reply(
                    content=f"I don't have the permissions to do that... {self.emoji_table.kuma_pout}",
                    delete_after=self.message_timeout,
                )
        else:
            # Default: only remove bot messages.
            messages = await context.channel.purge(limit=limit, check=self.bot.is_me, bulk=False, after=anchor)

        tmp: str = f" of {self.bot.user.name} "
        return await context.channel.send(
            content=f"I ate **{len(messages)}**{tmp if not all_messages else ' '}{'messages' if len(messages) > 1 else 'message'}. *nom.. nom..* {self.emoji_table.kuma_rawr}",  # noqa: E501
            delete_after=10,
        )

    @commands.group(name="settings", invoke_without_command=True)
    @commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def settings(self, interaction: GuildContext) -> Message:
        """See this server's Moderator settings. These are the server's, not yours."""
        # Build the panel for `interaction.guild`, the guild the command was run in - not a fixed
        # guild, or the title and icon would name the wrong server.
        settings: ModeratorSettings | None = await self.get_mod_settings(guild=interaction.guild)
        if settings is not None:
            return await interaction.send(
                view=ModeratorSettingsPanel(cog=self, guild=interaction.guild, owner=interaction.author, settings=settings),
                delete_after=self.message_timeout,
            )

        return await interaction.send(
            content=f"We encountered an error reading this server's settings. {self.emoji_table.kuma_crying}",
            delete_after=self.message_timeout,
        )

    @settings.command(name="view")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def view_setting(self, interaction: GuildContext) -> Message:
        """See this server's Moderator settings. These are the server's, not yours."""
        return await self.settings(interaction)

    @settings.command(name="set")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(option=Cog.settings_choices(ModeratorSettings, exclude=MOD_SETTINGS_EXCLUDED))
    async def set_setting(self, interaction: GuildContext, option: Choice[str], value: bool) -> Message:
        """Change one of this server's Moderator settings."""
        settings: ModeratorSettings | None = await self.get_mod_settings(guild=interaction.guild)
        # Set the default settings for the new guild.
        if settings is None:
            await self.set_mod_settings(guild=interaction.guild, default=True)

        updated: ModeratorSettings | None = await self.set_mod_settings(guild=interaction.guild, setting=option.value, value=value)
        if updated is not None:
            return await interaction.send(
                view=ModeratorSettingsPanel(cog=self, guild=interaction.guild, owner=interaction.author, settings=updated),
                delete_after=self.message_timeout,
                ephemeral=True,
            )
        return await interaction.send(
            content=f"We encountered an error updating the database. {self.emoji_table.kuma_crying}",
            delete_after=self.message_timeout,
        )

    @settings.command(name="reset")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def reset_settings(self, interaction: GuildContext) -> Message:
        """Put all of this server's Moderator settings back to their defaults."""
        res: ModeratorSettings | None = await self.reset_mod_settings(guild=interaction.guild)
        if res is not None:
            return await interaction.send(
                view=ModeratorSettingsPanel(cog=self, guild=interaction.guild, owner=interaction.author, settings=res),
                delete_after=self.message_timeout,
                ephemeral=True,
            )

        return await interaction.send(
            content=f"Reset this server's settings. {self.emoji_table.kuma_star_eye}",
            delete_after=self.message_timeout,
        )

    @commands.command(name="who_is", help="See information about the Discord ID.")
    @app_commands.default_permissions(moderate_members=True)
    @commands.guild_only()
    async def who_is(self, context: GuildContext, discord_id: int) -> Message:
        res: User | None = self.bot.get_user(discord_id)
        if res is not None:
            embed = discord.Embed(color=res.color, title=res.global_name, description=f"**{res.id}**")
            return await context.send(embed=embed, ephemeral=True, delete_after=self.message_timeout)
        return await context.send(
            content=f"Unable to find Discord ID: `{discord_id}`. {self.emoji_table.kuma_hmm}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103 # docstring
    await bot.add_cog(Moderator(bot=bot))
