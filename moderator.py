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
import datetime
import inspect
import logging
import re
import sqlite3
from hashlib import sha256
from pkgutil import ModuleInfo
from sqlite3 import Row
from typing import TYPE_CHECKING, Literal, Optional, TypedDict, Union, Unpack, reveal_type

import discord
from asqlite import Cursor
from discord import Colour, Member, Message, User, app_commands
from discord.app_commands import Choice
from discord.ext import commands, tasks

from extensions import EXTENSIONS
from kuma_kuma import Kuma_Kuma, _get_prefix, _get_trusted
from utils import (
    KumaCog as Cog,  # need to replace with your own Cog class
    KumaContext as Context,
    KumaGuildContext as GuildContext,
)
from utils._types import EmbedParams
from utils.embeds import KumaEmbed

if TYPE_CHECKING:
    from sqlite3 import Row

    import mystbin
    from asqlite import Cursor


BOT_NAME = "Kuma Kuma Bear"
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

    def __init__(self, count: int, timestamp: Optional[datetime.datetime] = None) -> None:
        # self.messages = messages
        self.count = count
        self._hashes = []
        # This is getting overwritten right after count == 1
        # I didn't want to set it to None; as then that's another logic check.
        if timestamp is None:
            timestamp = datetime.datetime.now(tz=datetime.UTC)
        self.timestamp = timestamp

    def __repr__(self) -> str:
        return f"Count: {self.count} | Hashes Len: {len(self.hashes)} | Timestamp: {self.timestamp}"

    @property
    def hashes(self) -> list[str]:
        return self._hashes

    @hashes.setter
    def hashes(self, value: str) -> None:
        if len(self.hashes) == 2:
            self.hashes.pop(0)
            self.hashes.append(value)
        else:
            self.hashes.append(value)


class ModeratorSettings(TypedDict):
    id: int
    serverid: int
    use_mystbin: bool
    # online_player_count: bool
    spam_filter: bool


def _mod_settings_choices() -> list[app_commands.Choice[str]]:
    choices: list[app_commands.Choice] = []
    # inspect.get_annotations(ModeratorSettings)
    for k in ModeratorSettings.__annotations__:
        if k in ["id", "serverid", "online_player_count"]:
            continue
        choices.append(app_commands.Choice(name=k, value=k))
    return choices


class ModeratorSettingsEmbed(KumaEmbed):
    def __init__(self, cog: Cog, content: ModeratorSettings, guild: discord.Guild, **kwargs: Unpack[EmbedParams]) -> None:

        if kwargs.get("title") is None:
            kwargs["title"] = f"{guild} Settings"

        super().__init__(cog=cog, **kwargs)

        self.add_blank_field()
        for key in content.keys():  # noqa: SIM118 - It thinks it's a dict; when it's a sqlite3.Row Tuple object.
            # Omitted keys from ModeratorSettings
            if key in ["id", "serverid", "online_player_count"]:
                continue
            self.add_field(inline=False, name=f"__{key.title()}__", value=str(bool(content[key])))
        self.set_footer(text="Kuma Kuma Bear - Moderator Settings")
        self.set_thumbnail(url=guild.icon)


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
    spam_messages: dict[int, MessageRecords]

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    async def cog_load(self) -> None:
        async with self.bot.pool.acquire() as conn:
            await conn.execute(MODERATOR_SETUP_SQL)
        self.spam_messages = {}
        # LOGGER.info(_mod_settings_choices())
        # LOGGER.info(ModeratorSettings.__annotations__)

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
                    await self.set_mod_settings(guild=guild, default=True)
                else:
                    # data: ModeratorSettings = {
                    #     "id": res["id"],
                    #     "serverid": res["serverid"],
                    #     "use_mystbin": res["use_mystbin"],
                    # }
                    return res
        except Exception as e:
            LOGGER.exception("<%s.%s> | We encountered an error executing %s", __class__.__name__, "get_mod_settings", exc_info=e)
            msg = "Unable to connect to the database."
            raise ConnectionError(msg) from None

    # Allowed column names for UPDATE — guards against SQL injection via the setting parameter.
    _MOD_SETTING_COLUMNS: frozenset[str] = frozenset({"use_mystbin", "spam_filter"})

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
                    data: ModeratorSettings | None = await conn.fetchone(
                        """INSERT INTO moderator(serverid) VALUES(?) RETURNING *""",
                        guild.id,
                    )  # pyright: ignore[reportAssignmentType]
                else:
                    data = await conn.fetchone(
                        f"""UPDATE moderator SET {setting} = ? WHERE serverid = ? RETURNING *""",  # noqa: S608 - column name validated above
                        value,
                        guild.id,
                    )  # pyright: ignore[reportAssignmentType]

                if data is None:
                    LOGGER.error(
                        "<%s.%s> | We encountered an error %s a row in the database. | GuildID: %s",
                        __class__.__name__,
                        "set_mod_settings",
                        "inserting" if default else "updating",
                        guild.id,
                    )
                    msg = f"Unable to {'insert' if default else 'update'} a row in the database."
                    raise sqlite3.DatabaseError(msg)  # noqa: TRY301
                return data
        except Exception as e:
            LOGGER.exception("<%s.%s> | We encountered an error executing %s", __class__.__name__, "set_mod_settings", exc_info=e)
            msg = "Unable to connect to the database."
            raise ConnectionError(msg) from None

    @commands.Cog.listener(name="on_message")
    async def on_message_listener(self, message: discord.Message) -> None:
        # ignore ourselves and any other bot accounts.
        if message.author == self.bot.user or message.author.bot is True:
            return

        # ignore messages that start with a prefix.
        if message.content.startswith(tuple(await _get_prefix(bot=self.bot, message=message))):
            return
        # ignore messages not in a guild.
        if message.guild is not None:
            res: ModeratorSettings | None = await self.get_mod_settings(guild=message.guild)

            # So if our message is over 1k char length and the guild settings is True.
            if (res is not None and res["use_mystbin"] is True) and (
                message.channel.type is not discord.ChannelType.news and len(message.content) > 1000
            ):
                await self._auto_on_mystbin(message=message)
            if res is not None and bool(res["spam_filter"]) is True:
                await self._duplicate_attachment_check(message)

    @commands.Cog.listener(name="on_thread_update")
    async def mod_on_thread_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        # Updates Title of Threads with the respective prefix to the name depending on what was done to them.
        if (
            isinstance(before, discord.Thread)
            and isinstance(after, discord.Thread)
            and before.permissions_for(before.guild.me).manage_threads is True
        ):
            if before.locked is False and after.locked is True:
                if before.name.lower().startswith("[locked] -") or after.name.lower().startswith("[locked] -"):
                    return
                LOGGER.info(
                    "Updating Locked Thread's name. | Guild ID: %s | Thread Name: %s | Thread ID: %s | Thread Name After: %s",
                    before.guild.id,
                    before.name,
                    before.id,
                    after.name,
                )
                await after.edit(name=f"[LOCKED] - {after.name}")
            elif before.archived is False and after.archived is True:
                if before.name.lower().startswith("[closed] - ") or after.name.lower().startswith("[closed] - "):
                    return
                res: discord.Thread = await after.edit(archived=False)
                await res.edit(name=f"[CLOSED] - {after.name}", archived=True)
                LOGGER.info(
                    "Updating Locked Thread's name. | Guild ID: %s | Thread Name: %s | Thread ID: %s | Thread Name After: %s",
                    before.guild.id,
                    before.name,
                    before.id,
                    res.name,
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
            # LOGGER.info("<%s.%s> | Duplicate Attachment Check Failed", __class__.__name__, "duplicate_attachment_check")
            return

        if message.author.guild_permissions.administrator is True:
            return

        check_attachments: bool = False
        check_content: bool = False
        url: str | None = None

        if len(message.attachments) != 0:
            # LOGGER.info("User sent an Attachment")
            check_attachments = True

        match: re.Match[str] | None = re.search(HTTP_REGEX, message.content)
        if match is not None:
            url = match.group()
            check_content = True
            # LOGGER.info("User sent a Content URL. | URL: %s", url)

        LOGGER.debug(
            "<%s.%s> | Author Type: %s | Author Admin: %s | Message Guild: %s | Msg Attachment Count: %s",
            __class__.__name__,
            "duplicate_attachment_check",
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
        if check_content and url is not None:
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

        # self.spam_messages.update({message.author.id : record})
        # If the users count breaks SPAM LIMIT, we try to ban the user.
        if record.count >= self.SPAM_LIMIT:
            cur_time: datetime.datetime = datetime.datetime.now(tz=datetime.UTC)
            # If they triggered the SPAM LIMIT and it's been over 3 minutes since the first "dupe";
            # ignore the count, reset the counter to 1 (as it's still a "dupe") and adjust the timestamp for a new 3 minute window.
            if cur_time - record.timestamp > datetime.timedelta(minutes=3):
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
            await self.bot.owner.send(embed=embed, silent=True)
        # LOGGER.info("User Record: %s", record)

    async def _hash_parse(self, author: discord.Member, record: MessageRecords, url: str) -> bool:
        data: bytes | None = await self.get_request(url=url)
        if data is None:
            return False
        # Use the bytes from the get request, truncate to 16 chars and compare against existing hashes.
        res = sha256(data).hexdigest()[:16]
        if res not in record.hashes:
            # LOGGER.info("Added a Hash to the User")
            record.hashes = res
            return False
        # LOGGER.info("Found a duplicate Hash for the User")
        record.count += 1
        if record.count == 1:
            record.timestamp = datetime.datetime.now(tz=datetime.UTC)
        self.spam_messages.update({author.id: record})
        return True

    @commands.command(name="reload", help="Reloads all extensions unless specified.")
    @commands.is_owner()
    async def reload(self, context: Context, args: Optional[str] = None) -> None:
        await context.typing(ephemeral=True)
        _flag = False
        try:
            name = "UNK"
            for extension in EXTENSIONS:
                if isinstance(extension, ModuleInfo):
                    name = extension.name.split(".")[1]

                    # If any additional args; attempt to find a match.
                    if args is not None and args.lower() in extension.name.lower():
                        await self.bot.reload_extension(name=extension.name)
                        LOGGER.info("Loaded %sextension: %s", "module " if extension.ispkg else "", extension.name)
                        _flag = True
                        break

                    # else we have no args; reload each module each iteration.
                    if args is None:
                        await self.bot.reload_extension(name=extension.name)
                        LOGGER.info("Loaded %sextension: %s", "module " if extension.ispkg else "", extension.name)

            if _flag:
                await context.send(
                    content=f"**SUCCESS** Reloading the `{name}` extension.",
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )
                return

            await context.send(
                content=f"**SUCCESS** Reloading all {len(EXTENSIONS)} Extensions.",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
        except Exception as e:
            LOGGER.exception("<%s.%s> | We encountered an error executing %s", __class__.__name__, context.command, exc_info=e)
            await context.send(content=f"__We encountered an Error__ - \n{e}", ephemeral=True, delete_after=self.message_timeout)

    @commands.command(name="sync", help=f"Sync the {BOT_NAME} commands to the guild.")
    @commands.is_owner()
    @commands.guild_only()
    async def sync(self, context: GuildContext, local: bool = True, reset: bool = False) -> Message | None:
        await context.typing(ephemeral=True)

        if reset is True:
            if local is True:
                self.bot.tree.clear_commands(guild=context.guild)
            else:
                self.bot.tree.clear_commands(guild=None)
            # Local command tree reset
            LOGGER.info(
                "%s Commands reset and sync'd -- Make sure to clear your client cache(*ctrl+F5*). | %s by %s",
                self.bot.user.name,
                await self.bot.tree.sync(guild=(context.guild if local is True else None)),
                self.bot.user.name,
            )
            return await context.send(
                content=f"**WARNING** Resetting `{self.bot.user.name}s` commands... {self.emoji_table.kuma_bleh}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        if local is True:
            # Local command tree sync
            self.bot.tree.copy_global_to(guild=context.guild)
            LOGGER.info("%s Commands Sync'd Locally: %s", self.bot.user.name, await self.bot.tree.sync(guild=context.guild))
            return await context.send(
                content=f"Successfully sync'd `{self.bot.user.name}s` commands to {context.guild}...",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
        return None

    @commands.group(name="prefix", invoke_without_command=True)
    @commands.guild_only()
    async def prefix(self, context: GuildContext) -> Message:
        """Returns a list of the current prefixes for the current guild."""
        async with self.bot.pool.acquire() as conn:
            res: list[Row] = await conn.fetchall("""SELECT prefix FROM prefix WHERE serverid = ?""", context.guild.id)
            if len(res) > 0:
                prefixes = "\n".join([entry["prefix"] for entry in res])
                return await context.send(content=f"**Current Prefixes:** \n{prefixes}", delete_after=self.message_timeout, ephemeral=True)
            return await context.send(
                content="It appears you do not have any prefix's set",
                delete_after=self.message_timeout,
                ephemeral=True,
            )

    @prefix.command(name="add", help=f"Add a prefix to {BOT_NAME}", aliases=["prea", "pa"])
    @commands.is_owner()
    @commands.guild_only()
    async def add_prefix(self, context: GuildContext, prefix: str) -> Message:
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""INSERT INTO prefix(serverid, prefix) VALUES(?, ?)""", context.guild.id, prefix.lstrip())
            return await context.send(
                content=f"Added the prefix `{prefix}` for {context.guild.name}",
                delete_after=self.message_timeout,
                ephemeral=True,
            )

    @prefix.command(name="delete", help=f"Delete a prefix from {BOT_NAME} for a guild.", aliases=["pred", "pd"])
    @commands.is_owner()
    @commands.guild_only()
    async def delete_prefix(self, context: GuildContext, prefix: str) -> Message:
        # assert context.guild is not None
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""DELETE FROM prefix WHERE serverid = ? AND prefix = ?""", context.guild.id, prefix.lstrip())
            return await context.send(content=f"Removed the prefix - `{prefix}`", delete_after=self.message_timeout)

    @prefix.command(name="clear", help=f"Clear all prefixes for {BOT_NAME}in a guild.", aliases=["prec", "pc"])
    @commands.is_owner()
    async def clear_prefix(self, context: GuildContext) -> Message:
        # assert context.guild is not None
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""DELETE FROM prefix WHERE serverid = ?""", context.guild.id)
            return await context.send(
                content=f"Removed all prefix's for {context.guild.name}",
                delete_after=self.message_timeout,
                ephemeral=True,
            )

    @commands.command(name="trusted", help=f"Add/Remove and list {BOT_NAME} Owner IDs.")
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.choices(
        option=[Choice(name="add", value="add"), Choice(name="remove", value="remove"), Choice(name="list", value="list")],
    )
    async def trust(self, context: GuildContext, option: Choice | str, member: Union[Member, User, int, None]) -> Message | None:
        if isinstance(member, int):
            member = context.guild.get_member(member)
        if member is None:
            return await context.send(
                content=f"Hey uhh, I failed to find the member provided.{self.emoji_table.kuma_head_clench}",
                ephemeral=True,
            )
        if option == "add":
            if member.id not in self.bot.owner_ids:
                async with self.bot.pool.acquire() as conn:
                    await conn.execute("""INSERT INTO owners(ownerid) VALUES(?)""", member.id)
                    self.bot.owner_ids.add(member.id)
                    return await context.send(
                        content=f"Added {member.mention} to the owner list",
                        ephemeral=True,
                        delete_after=self.message_timeout,
                    )
            else:
                return await context.send(content=f"{member} are already an owner", ephemeral=True, delete_after=self.message_timeout)

        elif option == "remove":
            async with self.bot.pool.acquire() as conn:
                cur: Cursor = await conn.execute("""DELETE FROM owners WHERE ownerid = ?""", member.id)
                self.bot.owner_ids.remove(member.id)
                return await context.send(
                    content=f"Removed {cur.get_cursor().rowcount} Users as an owner",
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )

        elif option == "list":
            async with self.bot.pool.acquire() as conn:
                res: list[Row] = await conn.fetchall("""SELECT ownderid FROM owners""")
                owners: list[Member] = [await context.guild.fetch_member(entry["id"]) for entry in res]
                f_owners: str = "\n".join([entry.display_name for entry in owners])
                return await context.send(content=f"**Current Owners:** \n{f_owners}", ephemeral=True, delete_after=self.message_timeout)
        return None

    @commands.command(name="clear", help="Removes all messges and or just bot messages.")
    @app_commands.default_permissions(manage_messages=True)
    @commands.guild_only()
    @app_commands.describe(bot_only=f"Default's to True, removes ALL messages from selected Channel that were sent by {BOT_NAME}.")
    async def clear(
        self,
        context: GuildContext,
        amount: int = 15,
        bot_only: bool = False,
    ) -> Message:
        messages: list[discord.Message] = []
        til_message: Union[discord.Message, None] = None
        # Let's see if a Discord Message ID was passed in as our amount.
        if len(str(amount)) > 12:
            try:
                til_message = await context.channel.fetch_message(amount)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await context.reply(content="We were unable to find the Discord Message ID provided.", delete_after=self.message_timeout)

        if bot_only:
            messages = await context.channel.purge(
                limit=(None if til_message is not None else amount),
                check=self.bot.is_me,
                bulk=False,
                after=(til_message.created_at if til_message is not None else None),
            )
        elif context.author.id in self.bot.owner_ids or context.channel.permissions_for(context.author).manage_messages:
            try:
                messages = await context.channel.purge(
                    limit=(None if til_message is not None else amount),
                    bulk=False,
                    after=(til_message.created_at if til_message is not None else None),
                )
            except discord.errors.Forbidden:
                return await context.reply(
                    content=f"I don't have the permissions to do that... {self.emoji_table.kuma_pout}",
                    delete_after=self.message_timeout,
                )

        tmp: str = f" of {self.bot.user.name} "
        return await context.channel.send(
            content=f"I ate **{len(messages)}**{tmp if bot_only else ' '}{'messages' if len(messages) > 1 else 'message'}. *nom.. nom..* {self.emoji_table.kuma_rawr}",  # noqa: E501
            delete_after=10,
        )

    # @app_commands.guild_only()
    @commands.hybrid_group(name="settings")
    @app_commands.default_permissions(manage_messages=True)
    async def settings(self, interaction: GuildContext) -> Message:
        settings: ModeratorSettings | None = await self.get_mod_settings(guild=interaction.guild)
        if settings is not None:
            res: discord.Guild | None = await self.get_guild()
            if res is not None:
                embed = ModeratorSettingsEmbed(cog=self, content=settings, guild=res)
                return await interaction.send(
                    embed=embed,
                    delete_after=self.message_timeout,
                    files=embed.attachments,
                )

        return await interaction.send(
            content=self.emoji_table.kuma_crying,
            delete_after=self.message_timeout,
        )

    @settings.command(name="set")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.choices(option=_mod_settings_choices())
    async def set_setting(self, context: GuildContext, option: Choice[str], value: bool) -> Message:
        updated: ModeratorSettings | None = await self.set_mod_settings(guild=context.guild, setting=option.value, value=value)

        if updated is not None:
            embed = ModeratorSettingsEmbed(cog=self, content=updated, guild=context.guild)
            return await context.send(
                embed=embed,
                delete_after=self.message_timeout,
                files=embed.attachments,
            )
        return await context.send(
            content=self.emoji_table.kuma_crying,
            delete_after=self.message_timeout,
        )

    @commands.command(name="who_is", help="See information about the Discord ID.")
    @app_commands.default_permissions(moderate_members=True)
    @commands.guild_only()
    async def who_is(self, context: GuildContext, discord_id: int) -> Message:
        res: User | None = self.bot.get_user(discord_id)
        if res is not None:
            embed = discord.Embed(color=res.color, title=res.global_name, description=f"**{res.id}**")
            return await context.send(embed=embed)
        return await context.send(content=f"Unable to find the Discord ID: {discord_id}")


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103 # docstring
    await bot.add_cog(Moderator(bot=bot))
