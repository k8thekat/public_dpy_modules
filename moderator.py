import re
import sqlite3
from datetime import datetime, timezone
from sqlite3 import Row
from typing import TYPE_CHECKING, TypedDict, Union

import discord
from asqlite import Cursor
from discord import Member, Message, User, app_commands
from discord.app_commands import Choice
from discord.ext import commands

from extensions import EXTENSIONS
from kuma_kuma import Kuma_Kuma, _get_prefix, _get_trusted
from utils.cog import KumaCog as Cog  # need to replace with your own Cog class
from utils.context import KumaContext as Context

if TYPE_CHECKING:
    from sqlite3 import Row

    import mystbin
    from asqlite import Cursor


BOT_NAME = "Kuma Kuma"

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


from discord import Colour


class ModeratorSettingsEmbed(discord.Embed):
    def __init__(
        self,
        content: ModeratorSettings,
        context: Context,
        footer_icon_url: str = "",
    ) -> None:
        self.context: Context = context
        assert self.context.guild
        super().__init__(
            colour=Colour.blurple(),
            title=f"{BOT_NAME} Moderator settings",
            description=f"These settings are specific to {self.context.guild.name}. ",
        )

        self.add_field(name="", value="-------------------------------")
        self.add_field(inline=False, name="Use Mystbin", value="- " + str(bool(content["use_mystbin"])))
        self.set_footer(text="Kuma Kuma - Moderator Settings Embed", icon_url=footer_icon_url)


class Moderator(Cog):
    """
    Moderator type commands and functionality for Discord.
    """

    repo_url: str = "https://github.com/k8thekat/public_dpy_modules"
    guild_settings = list[dict[int, ModeratorSettings]]  # key will be the guild ID -> guild settings
    CODEBLOCK_PATTERN: re.Pattern[str] = re.compile(
        pattern=r"`{3}(?P<LANG>\w+)?\n?(?P<CODE>(?:(?!`{3}).)+)\n?`{3}", flags=re.DOTALL | re.MULTILINE
    )

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    async def cog_load(self) -> None:
        async with self.bot.pool.acquire() as conn:
            await conn.execute(MODERATOR_SETUP_SQL)

    async def get_mod_settings(self, guild: discord.Guild) -> ModeratorSettings | None:
        """
        Retrieves the Moderator Settings for the provided Discord guild.

        Parameters
        -----------
        guild: :class:`discord.Guild`
            The Discord guild to get Moderator settings for.

        Returns
        --------
        :class:`ModeratorSettings | None`
            The settings related to the Discord guild.

        Raises
        -------
        :exc:`ConnectionError`
            Raises a connection error if unable to connect to the Database for any reason.
        """
        try:
            async with self.bot.pool.acquire() as conn:
                res: ModeratorSettings | None = await conn.fetchone(
                    """SELECT * FROM moderator WHERE serverid = ?""", guild.id
                )  # type: ignore - I know the dataset because of above.
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
            self.logger.error("We encountered an error executing %s", __name__ + "get_mod_settings", exc_info=e)
            raise ConnectionError("Unable to connect to the database.")

    async def set_mod_settings(self, guild: discord.Guild, use_mystbin: bool = False) -> ModeratorSettings | None:
        """
        Set Moderator specific settings for the provided Discord guild.

        Parameters
        -----------
        guild: :class:`discord.Guild`
            The Discord guild object.
        use_mystbin: :class:`bool`, optional
            If the Discord guild should use mystbin conversion for longer messages, by default False.

        Returns
        --------
        :class:`ModeratorSettings | None`
            The Discord guild specific Moderator settings.

        Raises
        -------
        :exc:`sqlite3.DatabaseError`
            If we are unable to INSERT the row into the Database table.
        :exc:`ConnectionError`
            If we are unable to connect to the Database.
        """
        try:
            async with self.bot.pool.acquire() as conn:
                data: ModeratorSettings | None = await conn.fetchone(
                    """INSERT INTO moderator(serverid, use_mystbin) VALUES(?, ?) RETURNING *""", guild.id, use_mystbin
                )  # type: ignore
                if data is None:
                    self.logger.error("We encountered an error inserting a row into the database. | GuildID: %s", guild.id)

                    raise sqlite3.DatabaseError("Unable to insert a row in to the database.")
                else:
                    return data
        except Exception as e:
            self.logger.error("We encountered an error executing %s", __name__ + "set_mod_settings", exc_info=e)
            raise ConnectionError("Unable to connect to the database.")

    @commands.Cog.listener(name="on_message")
    async def on_message_listener(self, message: discord.Message) -> None:
        # ignore ourselves and any other bot accounts.
        if message.author == self.bot.user or message.author.bot == True:
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

    async def _auto_on_mystbin(self, message: Message) -> None:
        """
        Converts a `discord.Message` into a Mystbin URL

        Parameters
        -----------
        message: :class:`Message`
            The Discord Message to be converted.
        """
        content: str = message.content
        assert message.guild  # we are force checking this function earlier to make sure we are in a guild only.
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

    @commands.command(name="reload", help="Reload all extensions.")
    @commands.is_owner()
    async def reload(self, context: Context) -> None:
        """Reloads all extensions inside the extensions folder."""
        await context.typing(ephemeral=True)

        try:
            for extension in EXTENSIONS:
                await self.bot.reload_extension(name=extension.name)
                self.logger.info("Loaded %sextension: %s", "module " if extension.ispkg else "", extension.name)
        except Exception as e:
            self.logger.error("We encountered an error executing %s", context.command, exc_info=e)
            await context.send(
                content=f"__We encountered an Error__ - \n{e}", ephemeral=True, delete_after=self.message_timeout
            )

        await context.send(
            content="**SUCCESS** Reloading All extensions ", ephemeral=True, delete_after=self.message_timeout
        )

    @commands.command(name="sync", help=f"Sync the {BOT_NAME} commands to the guild.")
    @commands.is_owner()
    @commands.guild_only()
    async def sync(self, context: Context, local: bool = True, reset: bool = False) -> Message | None:
        """Syncs Kuma Commands to the current guild this command was used in."""
        await context.typing(ephemeral=True)
        assert context.guild is not None  # Since we limit the command to guild_only()

        if reset == True:
            if local == True:
                self.bot.tree.clear_commands(guild=context.guild)
            else:
                self.bot.tree.clear_commands(guild=None)
            # Local command tree reset
            self.bot.logger.info(
                "%s Commands Reset and Sync'd -- Make sure to clear your Client Cache. | %s by %s",
                self.bot.user.name,
                await self.bot.tree.sync(guild=(context.guild if local is True else None)),
                self.bot.user.name,
            )
            return await context.send(
                content=f"**WARNING** Resetting `{self.bot.user.name}s` Commands... {self.emoji_table.to_inline_emoji(emoji='kuma_bleh')}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        if local == True:
            # Local command tree sync
            self.bot.tree.copy_global_to(guild=context.guild)
            self.bot.logger.info(
                "%s Commands Sync'd Locally: %s", self.bot.user.name, await self.bot.tree.sync(guild=context.guild)
            )
            return await context.send(
                content=f"Successfully Sync'd `{self.bot.user.name}s` Commands to {context.guild}...",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

    @commands.group(name="prefix", invoke_without_command=True)
    @commands.guild_only()
    async def prefix(self, context: Context) -> Message:
        """Returns a list of the current prefixes for the current guild"""
        assert context.guild is not None
        async with self.bot.pool.acquire() as conn:
            res: list[Row] = await conn.fetchall("""SELECT prefix FROM prefix WHERE serverid = ?""", context.guild.id)
            if len(res) > 0:
                prefixes = "\n".join([entry["prefix"] for entry in res])
                return await context.send(
                    content=f"**Current Prefixes:** \n{prefixes}", delete_after=self.message_timeout, ephemeral=True
                )
            else:
                return await context.send(
                    content="It appears you do not have any prefix's set", delete_after=self.message_timeout, ephemeral=True
                )

    @prefix.command(name="add", help=f"Add a prefix to {BOT_NAME}", aliases=["prea", "pa"])
    @commands.is_owner()
    @commands.guild_only()
    async def add_prefix(self, context: Context, prefix: str) -> Message:
        assert context.guild is not None  # We know because guild_only() decorator.
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
    async def delete_prefix(self, context: Context, prefix: str) -> Message:
        assert context.guild is not None
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""DELETE FROM prefix WHERE serverid = ? AND prefix = ?""", context.guild.id, prefix.lstrip())
            return await context.send(content=f"Removed the prefix - `{prefix}`", delete_after=self.message_timeout)

    @prefix.command(name="clear", help=f"Clear all prefixes for {BOT_NAME}in a guild.", aliases=["prec", "pc"])
    @commands.is_owner()
    async def clear_prefix(self, context: Context) -> Message:
        assert context.guild is not None
        async with self.bot.pool.acquire() as conn:
            await conn.execute("""DELETE FROM prefix WHERE serverid = ?""", context.guild.id)
            return await context.send(
                content=f"Removed all prefix's for {context.guild.name}", delete_after=self.message_timeout, ephemeral=True
            )

    @commands.command(name="trusted", help=f"Add/Remove and list {BOT_NAME} Owner IDs.")
    @commands.is_owner()
    @commands.guild_only()
    @app_commands.choices(
        option=[Choice(name="add", value="add"), Choice(name="remove", value="remove"), Choice(name="list", value="list")]
    )
    async def trust(self, context: Context, option: Choice, member: Union[Member, User, int, None]) -> Message | None:
        assert context.guild
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
                return await context.send(
                    content="You are already an owner", ephemeral=True, delete_after=self.message_timeout
                )

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
                return await context.send(
                    content=f"**Current Owners:** \n{f_owners}", ephemeral=True, delete_after=self.message_timeout
                )

    @commands.command(name="clear", help="Removes Member and Bot messages from a channel.")
    @app_commands.default_permissions(manage_messages=True)
    @commands.guild_only()
    @app_commands.describe(
        bot_only=f"Default's to True, removes ALL messages from selected Channel that were sent by {BOT_NAME}."
    )
    async def clear(
        self,
        context: Context,
        amount: int = 15,
        bot_only: bool = False,
    ) -> Message:
        # because guild_only()
        assert isinstance(context.channel, (discord.VoiceChannel, discord.TextChannel, discord.Thread))

        messages: list[discord.Message] = []
        til_message: Union[discord.Message, None] = None
        # Let's see if a Discord Message ID was passed in as our amount.
        if len(str(amount)) > 12:
            try:
                til_message = await context.channel.fetch_message(amount)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await context.reply(
                    content="We were unable to find the Discord Message ID provided.", delete_after=self.message_timeout
                )
                pass

        if bot_only:
            messages = await context.channel.purge(
                limit=(None if til_message is not None else amount),
                check=self.bot.is_me,
                bulk=False,
                after=(til_message.created_at if til_message is not None else None),
            )
        elif (
            context.author.id in self.bot.owner_ids or context.channel.permissions_for(context.author).manage_messages  # type: ignore
        ):
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
            content=f"I ate **{len(messages)}**{tmp if bot_only else ' '}{'messages' if len(messages) > 1 else 'message'}. *nom.. nom..* {self.emoji_table.to_inline_emoji(emoji='kuma_rawr')}",
            delete_after=10,
        )

    @commands.group(name="settings", invoke_without_command=True)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def settings(self, context: Context) -> Message:
        assert context.guild
        settings: ModeratorSettings | None = await self.get_mod_settings(guild=context.guild)
        if settings is not None:
            res: discord.Guild | None = await self.get_guild()
            if res is not None:
                emoji: discord.Emoji | None = res.get_emoji(self.emoji_table.kuma_wow)
                if emoji is not None:
                    return await context.send(
                        embed=ModeratorSettingsEmbed(footer_icon_url=emoji.url, content=settings, context=context)
                    )
                else:
                    return await context.send(
                        embed=ModeratorSettingsEmbed(content=settings, context=context), delete_after=self.message_timeout
                    )

        return await context.send(
            content=self.emoji_table.to_inline_emoji(emoji=self.emoji_table.kuma_crying),
            delete_after=self.message_timeout,
        )


async def setup(bot: Kuma_Kuma) -> None:
    await bot.add_cog(Moderator(bot=bot))
