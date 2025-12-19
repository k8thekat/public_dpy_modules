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
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pkgutil import ModuleInfo
from sqlite3 import Row
from typing import TYPE_CHECKING, Optional, TypedDict, Union

import discord
from asqlite import Cursor
from discord import Colour, Member, Message, User, app_commands
from discord.app_commands import Choice
from discord.ext import commands

from extensions import EXTENSIONS
from kuma_kuma import Kuma_Kuma, _get_prefix, _get_trusted
from utils import (
    KumaCog as Cog,  # need to replace with your own Cog class
    KumaContext as Context,
    KumaGuildContext as GuildContext,
)

if TYPE_CHECKING:
    from sqlite3 import Row

    import mystbin
    from asqlite import Cursor


BOT_NAME = "Kuma Kuma"
LOGGER = logging.getLogger()

MODERATOR_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS moderator (
    id INTEGER PRIMARY KEY NOT NULL,
    serverid INTEGER NOT NULL,
    use_mystbin INT NOT NULL DEFAULT 0)
"""


class ModeratorSettings(TypedDict):
    id: int
    serverid: int
    use_mystbin: bool
    online_player_count: bool


class ModeratorSettingsEmbed(discord.Embed):
    def __init__(
        self,
        content: ModeratorSettings,
        context: GuildContext,
        footer_icon_url: str = "",
    ) -> None:
        self.context: Context = context
        super().__init__(
            colour=Colour.blurple(),
            title=f"{BOT_NAME} Guild Settings",
            description=f"These settings are specific to **{self.context.guild.name}**. ",
        )

        self.add_field(name="", value="-------------------------------")
        self.add_field(inline=False, name="__Use Mystbin__", value="- " + str(bool(content["use_mystbin"])))
        self.set_footer(text="Kuma Kuma - Moderator Settings Embed", icon_url=footer_icon_url)


class Moderator(Cog):
    """Moderator type commands and functionality for Discord."""

    repo_url: str = "https://github.com/k8thekat/public_dpy_modules"
    guild_settings = list[dict[int, ModeratorSettings]]  # key will be the guild ID -> guild settings
    CODEBLOCK_PATTERN: re.Pattern[str] = re.compile(
        pattern=r"`{3}(?P<LANG>\w+)?\n?(?P<CODE>(?:(?!`{3}).)+)\n?`{3}",
        flags=re.DOTALL | re.MULTILINE,
    )

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    async def cog_load(self) -> None:
        async with self.bot.pool.acquire() as conn:
            await conn.execute(MODERATOR_SETUP_SQL)

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
                    await self.set_mod_settings(guild=guild)
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

    async def set_mod_settings(self, guild: discord.Guild, use_mystbin: bool = False) -> ModeratorSettings | None:  # noqa: FBT001, FBT002
        """Set Moderator specific settings for the provided Discord guild.

        Parameters
        ----------
        guild: :class:`discord.Guild`
            The Discord guild object.
        use_mystbin: :class:`bool`, optional
            If the Discord guild should use mystbin conversion for longer messages, by default False.

        Returns
        -------
        :class:`ModeratorSettings | None`
            The Discord guild specific Moderator settings.

        Raises
        ------
        :exc:`sqlite3.DatabaseError`
            If we are unable to INSERT the row into the Database table.
        :exc:`ConnectionError`
            If we are unable to connect to the Database.

        """
        try:
            async with self.bot.pool.acquire() as conn:
                data: ModeratorSettings | None = await conn.fetchone(
                    """INSERT INTO moderator(serverid, use_mystbin) VALUES(?, ?) RETURNING *""",
                    guild.id,
                    use_mystbin,
                )  # pyright: ignore[reportAssignmentType]
                if data is None:
                    LOGGER.error(
                        "<%s.%s> | We encountered an error inserting a row into the database. | GuildID: %s",
                        __class__.__name__,
                        "set_mod_settings",
                        guild.id,
                    )
                    msg = "Unable to insert a row in to the database."
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
    async def sync(self, context: GuildContext, local: bool = True, reset: bool = False) -> Message | None:  # noqa: FBT001, FBT002
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
                content=f"**WARNING** Resetting `{self.bot.user.name}s` commands... {self.emoji_table.to_inline_emoji(emoji='kuma_bleh')}",
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
                content=f"Hey uhh, I failed to find the member provided.{self.emoji_table.to_inline_emoji('kuma_head_clench')}",
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
        bot_only: bool = False,  # noqa: FBT001, FBT002
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
                    content=f"I don't have the permissions to do that... {self.emoji_table.to_inline_emoji(self.emoji_table.kuma_pout)}",
                    delete_after=self.message_timeout,
                )

        tmp: str = f" of {self.bot.user.name} "
        return await context.channel.send(
            content=f"I ate **{len(messages)}**{tmp if bot_only else ' '}{'messages' if len(messages) > 1 else 'message'}. *nom.. nom..* {self.emoji_table.to_inline_emoji(emoji='kuma_rawr')}",  # noqa: E501
            delete_after=10,
        )

    @commands.group(name="settings", invoke_without_command=True)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def settings(self, context: GuildContext) -> Message:
        settings: ModeratorSettings | None = await self.get_mod_settings(guild=context.guild)
        if settings is not None:
            res: discord.Guild | None = await self.get_guild()
            if res is not None:
                emoji: discord.Emoji | None = res.get_emoji(self.emoji_table.kuma_wow)
                if emoji is not None:
                    return await context.send(embed=ModeratorSettingsEmbed(footer_icon_url=emoji.url, content=settings, context=context))
                return await context.send(
                    embed=ModeratorSettingsEmbed(content=settings, context=context),
                    delete_after=self.message_timeout,
                )

        return await context.send(
            content=self.emoji_table.to_inline_emoji(emoji=self.emoji_table.kuma_crying),
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
