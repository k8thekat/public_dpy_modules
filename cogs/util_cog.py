'''
   Copyright (C) 2021-2022 Katelynn Cadwallader.

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

'''
from __future__ import annotations
# Discord Libs
from discord import app_commands
from discord.ext import commands
import discord

# Python Libs
import os
import aiofiles

import psutil
import re
import time
from datetime import timedelta
import aiohttp
import json
import mystbin
import aiohttp
import os
import json
import time
import mystbin
import unicodedata
import inspect
from typing import Union

# Local libs
from utils import cog
# TODO - Write get log function.
# Possibly pull the entire file, parts of the file (0-50) and or key words/errors from `logger.warn` or `logger.error`


class Util(cog.KumaCog):
    PATTERN: re.Pattern[str] = re.compile(
        r'`{3}(?P<LANG>\w+)?\n?(?P<CODE>(?:(?!`{3}).)+)\n?`{3}', flags=re.DOTALL | re.MULTILINE)
    _default_repo = "https://github.com/k8thekat/dpy_cogs"
    _default_branch = "main"

    def __init__(self, bot: commands.Bot):
        super().__init__(bot=bot)
        self._name: str = os.path.basename(__file__).title()
        self._logger.info(f'**SUCCESS** Initializing {self._name}')

    async def cog_load(self) -> None:
        self._prefix: str = self._bot.command_prefix  # type:ignore
        self._message_timeout: int = 120
        self._start_time = time.time()
        self._mb_client = mystbin.Client()

    async def cog_unload(self) -> None:
        await self._mb_client.close()

    @property
    def _uptime(self) -> timedelta:
        return timedelta(seconds=(round(time.time() - self._start_time)))

    @commands.Cog.listener('on_message')
    async def on_message_listener(self, message: discord.Message) -> None:
        if self._self_check:
            return
        # If we are not in my own personal guild.
        if message.guild != None and message.guild.id != 602285328320954378:
            return

        if (isinstance(message.channel, discord.abc.GuildChannel) and
            message.channel.type is not discord.ChannelType.news and
                str(message.channel.category).lower() not in ['staff', 'dev channels', 'gaming', 'info']):
            # So if our message is over 1k char length and doesn't use our prefix; Lets push it to a mystbin URL.
            if len(message.content) > 1000 and not message.content.startswith(self._prefix):
                await self._auto_on_mystbin(message)

    def _self_check(self, message: discord.Message) -> bool:
        return message.author == self._bot.user

    async def _auto_on_mystbin(self, message: discord.Message) -> None:
        """Converts a `discord.Message` into a Mystbin URL"""
        content = message.content

        files: list[mystbin.File] = []

        should_upload_to_bin: bool = False

        for idx, match in enumerate(self.PATTERN.finditer(content), start=1):
            language: str = match.group('LANG') or 'python'
            filename: str = f'File-{idx}.{language}'
            file_content: str = match.group('CODE')

            files.append(mystbin.File(filename=filename, content=file_content))
            content = content.replace(match.group(), f'`[{filename}]`')

            should_upload_to_bin = should_upload_to_bin or len(
                file_content) > 1100
        if should_upload_to_bin:
            paste = await self._mb_client.create_multifile_paste(files=files)

            author = discord.utils.escape_markdown(str(message.author))
            await message.channel.send(f"Hey {message.author.mention}, *Kuma Kuma Bear* moved your codeblock(s) to `Mystbin`\n\n{content}\n\n{paste.url}")
            if message.channel.permissions_for(message.guild.me).manage_messages:  # type:ignore
                await message.delete()

    async def _auto_on_hastebin(self, message: discord.Message) -> None:
        """Converts a `discord.Message` into a Hastebin URL"""
        url = "https://hastebin.com/documents "
        if message.content.startswith(self._prefix):
            message.content = message.content[8:]
        async with aiohttp.ClientSession() as session:
            session_post = await session.post(url=url, data=message.content)
            response = json.loads(await session_post.text())
        await message.channel.send(content=f"Here is {message.author.mention} Hastebin `url` \n> {url[:-10]}raw/{response['key']}")

    async def count_lines(self, path: str, filetype: str = ".py", skip_venv: bool = True):
        lines = 0
        for i in os.scandir(path):
            if i.is_file():
                if i.path.endswith(filetype):
                    if skip_venv and re.search(r"(\\|/)?venv(\\|/)", i.path):
                        continue
                    lines += len((await (await aiofiles.open(i.path, "r")).read()).split("\n"))
            elif i.is_dir():
                lines += await self.count_lines(i.path, filetype)
        return lines

    async def count_others(self, path: str, filetype: str = ".py", file_contains: str = "def", skip_venv: bool = True):
        """Counts the files in directory or functions."""
        line_count = 0
        for i in os.scandir(path):
            if i.is_file():
                if i.path.endswith(filetype):
                    if skip_venv and re.search(r"(\\|/)?venv(\\|/)", i.path):
                        continue
                    line_count += len(
                        [line for line in (await (await aiofiles.open(i.path, "r")).read()).split("\n") if file_contains in line]
                    )
            elif i.is_dir():
                line_count += await self.count_others(i.path, filetype, file_contains)
        return line_count

    @commands.command(help="Shows info about the bot", aliases=["botinfo", "info", "bi"])
    async def about(self, ctx: commands.Context):
        """Tells you information about the bot itself."""
        await ctx.defer()
        assert self._bot.user
        information = await self._bot.application_info()
        embed = discord.Embed()
        # embed.add_field(name="Latest updates:", value=get_latest_commits(limit=5), inline=False)

        embed.set_author(
            name=f"Made by {information.owner.name}", icon_url=information.owner.display_avatar.url,)
        memory_usage = psutil.Process().memory_full_info().uss / 1024**2
        cpu_usage = psutil.cpu_percent()

        embed.add_field(
            name="Process", value=f"{memory_usage:.2f} MiB\n{cpu_usage:.2f}% CPU")
        embed.add_field(
            name=f"{self._bot.user.name} info:",
            value=f"**Uptime:**\n{self._uptime}")
        try:
            embed.add_field(
                name="Lines",
                value=f"Lines: {await self.count_lines('./', '.py'):,}"
                f"\nFunctions: {await self.count_others('./', '.py', 'def '):,}"
                f"\nClasses: {await self.count_others('./', '.py', 'class '):,}",
            )
        except (FileNotFoundError, UnicodeDecodeError):
            pass

        embed.set_footer(
            text=f"Made with discord.py v{discord.__version__}",
            icon_url="https://i.imgur.com/5BFecvA.png",
        )
        embed.timestamp = discord.utils.utcnow()
        await ctx.send(embed=embed)

    @commands.hybrid_command(name='clear')
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(all='Default\'s to False, removes ALL messages from selected Channel regardless of who sent them when True.')
    async def clear(self, interaction: discord.Interaction | commands.Context, channel: Union[discord.VoiceChannel, discord.TextChannel, discord.Thread, None], amount: app_commands.Range[int, 0, 100] = 15, all: bool = False):
        """Cleans up Messages sent by anyone. Limit 100"""
        if isinstance(interaction, discord.Interaction):
            await interaction.response.defer()

        assert isinstance(interaction.channel, (discord.VoiceChannel, discord.TextChannel, discord.Thread))
        channel = channel or interaction.channel

        if all:
            messages = await channel.purge(limit=amount, bulk=False)
        else:
            messages = await channel.purge(limit=amount, check=self._self_check, bulk=False)

        return await channel.send(f'Cleaned up **{len(messages)} {"messages" if len(messages) > 1 else "message"}**. Wow, look at all this space!', delete_after=self._message_timeout)

    @commands.command(name='charinfo')
    async def charinfo(self, context: commands.Context, *, characters: str):
        """Shows you information about a number of characters.
        Only up to 25 characters at a time.
        """

        def to_string(c):
            digit = f'{ord(c):x}'
            name = unicodedata.name(c, 'Name not found.')
            return f'`\\U{digit:>08}`: {name} - `{c}` \N{EM DASH} {c} \N{EM DASH} <http://www.fileformat.info/info/unicode/char/{digit}>'

        msg = '\n'.join(map(to_string, characters))
        if len(msg) > 2000:
            return await context.send('Output too long to display.')
        await context.send(msg)

    @commands.command(name='mimic')
    @commands.is_owner()
    async def mimic(self, context: commands.Context):
        """Invokes the previously run `command` with parameters."""
        await context.send(f'*Kuma Kuma Kuma* `{self._context.command}`')
        await self._context.reinvoke(restart=True)

    @commands.command(name='ping')
    async def ping(self, context: commands.Context):
        """Pong..."""
        self._context = context
        await context.send(f'Pong `{round(self._bot.latency * 1000)}ms`', ephemeral=True, delete_after=self._message_timeout)

    @commands.command(name='webhooks')
    async def webhooks(self, context: commands.Context, channel: Union[discord.VoiceChannel, discord.TextChannel, discord.StageChannel, discord.ForumChannel, None]):
        """Displays a channels webhooks by `Name` and `ID`"""

        assert isinstance(context.channel, (discord.VoiceChannel, discord.TextChannel, discord.StageChannel, discord.ForumChannel))

        channel = channel or context.channel
        channel_webhooks = "\n".join([f"**{webhook.name}** | ID: `{webhook.id}`" for webhook in await channel.webhooks()])
        await context.send(f'> {channel.mention} Webhooks \n{channel_webhooks}')

    @commands.command(name='hb')
    async def hastebin_me(self, context: commands.Context):
        """Converts a `str` to a Haste bin url"""
        await context.defer()
        await self._auto_on_hastebin(context.message)
        await context.message.delete()

    @commands.command(name='mb')
    async def mystbin_me(self, context: commands.Context):
        """Converts a `str` to a Mystbin url"""
        await context.defer()
        await self._auto_on_mystbin(context.message)
        await context.message.delete()

    @commands.command(name='link')
    async def url_linking(self, context: commands.Context, var: str):
        """Provides a Useful URL based upon the var parameter"""
        listing: dict[str, str] = {
            # Gatekeeper Github Links
            "gatekeeper": "https://github.com/k8thekat/GatekeeperV2",
            "gk": "https://github.com/k8thekat/GatekeeperV2",

            # Cube Coders Links
            "amp": "https://discord.gg/cubecoders",
            "cubecoders": "https://cubecoders.com/",
            "cc": "https://cubecoders.com/",

            # Discord.py Server Links
            "dpy": "https://discord.gg/dpy",
            "d.py": "https://discord.gg/dpy",
            "discord.py": "https://discord.gg/dpy",
            "dpy_docs": "https://discordpy.readthedocs.io/en/stable/",

            # Gatekeeper Wiki Links
            "gkwiki": "https://github.com/k8thekat/GatekeeperV2/wiki",
            "gkcommands": "https://github.com/k8thekat/GatekeeperV2/wiki/Commands",
            "gkperms": "https://github.com/k8thekat/GatekeeperV2/wiki/Permissions",
            "gkbanners": "https://github.com/k8thekat/GatekeeperV2/wiki/Server-Banners",
            "gkwl": "https://github.com/k8thekat/GatekeeperV2/wiki/Auto-Whitelisting",

            # Patreon/Donation Links
            "patreon": "https://www.patreon.com/Gatekeeperv2"}

        var = var.lower()
        if var in listing:
            await context.send(f'{listing[var]}')
        elif var == "?":
            await context.send(f"Possible Entries:\n> {(', ').join([key.title() for key in listing.keys()])}")

    @commands.command(name='source')
    async def source(self, context: commands.Context, *, command: Union[str, None]):
        """Displays my full source code or for a specific command.
        To display the source code of a subcommand you can separate it by
        periods, e.g. tag.create for the create subcommand of the tag command
        or by spaces.
        """
        source_url = 'https://github.com/k8thekat/Kuma_Kuma'
        branch = 'main'
        if command is None:
            return await context.send(source_url)

        if command == 'help':
            src = type(self._bot.help_command)
            module = src.__module__
            filename = inspect.getsourcefile(src)

        else:
            obj = self._bot.get_command(command.replace('.', ' '))
            if obj is None:
                return await context.send('Could not find command.')

            # since we found the command we're looking for, presumably anyway, let's
            # try to access the code itself
            src = obj.callback.__code__
            module = obj.callback.__module__
            filename = src.co_filename
            code_class = obj._cog

            # Handles my seperate repo URLs. (Could store this as part of the cog class?)
            # This requires you do define `repo_url` per script for files in a different parent directory than your bot.py
            if code_class != None and hasattr(code_class, "repo_url"):
                source_url = getattr(obj._cog, "repo_url")

        lines, firstlineno = inspect.getsourcelines(src)
        if not module.startswith('discord'):
            # not a built-in command
            if filename is None:
                return await context.send('Could not find source for command.')

            location = os.path.relpath(filename).replace('\\', '/')

        else:
            location = module.replace('.', '/') + '.py'
            branch = 'main'

        final_url = f'<{source_url}/blob/{branch}/{location}#L{firstlineno}-L{firstlineno + len(lines) - 1}>'
        await context.send(final_url)

    @commands.command(name="gpull", aliases=["gp"])
    async def git_pull(self, context: commands.Context, repo: str = _default_repo, branch: str = _default_branch):
        # TODO - Implement a git pull method to be used for cog edits
        import git
        repo = git.Repo().init()  # type: ignore
        res = git.remote.Remote(repo=repo, name="origin")  # type: ignore
        res.pull(branch)
        # import git
        # g = git.Git('git-repo')
        # g.pull('origin','branch-name')


async def setup(bot: commands.Bot):
    await bot.add_cog(Util(bot))
