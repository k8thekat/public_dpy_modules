"""
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

"""

import asyncio
import inspect
import io
import traceback
from contextlib import redirect_stdout
from typing import Any

import discord
from discord.ext import commands

from kuma_kuma import Kuma_Kuma
from utils.cog import KumaCog as Cog  # need to replace with your own Cog class
from utils.context import KumaContext as Context


class Repl(Cog):
    repo_url: str = "https://github.com/k8thekat/public_dpy_modules"

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    async def cog_load(self) -> None:
        self._sessions: set[int] = set()

    @commands.Cog.listener(name="on_message")
    async def on_message_listener(self, message: discord.Message) -> None:
        # This is for our `REPL` sessions.
        if message.channel.id in self._sessions:
            return

    @commands.command(hidden=True)
    @commands.is_owner()
    # todo - figure out why the repl session is failing to handle `await` type code.
    async def repl(self, ctx: Context) -> None:
        """Launches an interactive REPL session."""
        variables: dict[str, Any] = {
            "ctx": ctx,
            "bot": self.bot,
            "message": ctx.message,
            "guild": ctx.guild,
            "channel": ctx.channel,
            "author": ctx.author,
            "_": None,
        }

        if ctx.channel.id in self._sessions:
            await ctx.send(content="Already running a `REPL` session in this channel. Exit it with `quit`.")
            return

        self._sessions.add(ctx.channel.id)
        c_vars = "\n- ".join(variables)
        await ctx.send(
            content=f"""Enter code to execute or evaluate. `exit()` or `quit` to exit. {self.emoji_table.to_inline_emoji(emoji="kuma_wow")}\n__Current Set Variables__\n- {c_vars}"""
        )

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == ctx.author.id
                and message.channel.id == ctx.channel.id
                and message.content.startswith("`")
            )

        while True:
            try:
                response = await self.bot.wait_for("message", check=check, timeout=10.0 * 60.0)
            except asyncio.TimeoutError:
                await ctx.send(content=f"Exiting `REPL` session.{self.emoji_table.to_inline_emoji(emoji='kuma_shock')}")
                self._sessions.remove(ctx.channel.id)
                break

            cleaned = self.cleanup_code(response.content)

            if cleaned in ("quit", "exit", "exit()"):
                await ctx.send(content=f"Exiting. {self.emoji_table.to_inline_emoji('kuma_shrug')}")
                self._sessions.remove(ctx.channel.id)
                return

            if cleaned in ("?"):
                await ctx.send(f"{variables.keys()}")

            executor = exec
            code = ""
            if cleaned.count("\n") == 0:
                # single statement, potentially 'eval'
                try:
                    code = compile(cleaned, "<repl session>", "eval")
                except SyntaxError:
                    pass
                else:
                    executor = eval

            if executor is exec:
                try:
                    code = compile(cleaned, "<repl session>", "exec")
                except SyntaxError as e:
                    await ctx.send(content=self.get_syntax_error(e))
                    continue

            variables["message"] = response

            fmt = None
            stdout = io.StringIO()

            try:
                with redirect_stdout(stdout):
                    result = executor(code, variables)
                    if inspect.isawaitable(result):
                        result = await result
            except Exception:
                value = stdout.getvalue()
                fmt = f"```py\n{value}{traceback.format_exc()}\n```"
            else:
                value = stdout.getvalue()
                if result is not None:
                    fmt = f"```py\n{value}{result}\n```"
                    variables["_"] = result
                elif value:
                    fmt = f"```py\n{value}\n```"

            try:
                if fmt is not None:
                    if len(fmt) > 2000:
                        await ctx.send(
                            content="Content is over 2,000 lines to be printed in it's entirety, sending the last 2,000 lines."
                        )
                        await ctx.send(content=fmt[-2000:])
                    else:
                        await ctx.send(content=fmt)
            except discord.Forbidden:
                pass
            except discord.HTTPException as e:
                await ctx.send(content=f"Unexpected error: `{e}`")

    def cleanup_code(self, content: str) -> str:
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith("```") and content.endswith("```"):
            temp: str = "\n".join(content.split(sep="\n")[1:-1])
            # temp = f"async def _repl():\n{temp}\n"
            print(temp)
            return temp

        # remove `foo`
        return content.strip("` \n")

    def get_syntax_error(self, e: SyntaxError) -> str:
        if e.text is None:
            return f"```py\n{e.__class__.__name__}: {e}\n```"
        return f"```py\n{e.text}{'^':>{e.offset}}\n{e.__class__.__name__}: {e}```"


async def setup(bot: Kuma_Kuma) -> None:
    await bot.add_cog(Repl(bot=bot))
