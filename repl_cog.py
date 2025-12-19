"""Copyright (C) 2021-2022 Katelynn Cadwallader.

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
import logging
import traceback
from contextlib import redirect_stdout
from typing import Any, Optional, reveal_type

import discord
from discord.ext import commands

from kuma_kuma import Kuma_Kuma
from utils import (
    KumaCog as Cog,  # need to replace with your own Cog class
    KumaContext as Context,
)

LOGGER = logging.getLogger()


class Repl(Cog):
    repo_url: str = "https://github.com/k8thekat/public_dpy_modules"

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    async def cog_load(self) -> None:
        # TODO - Add support for multiple users in a single channel.
        # self._sessions: set[int] = set()
        self._sessions: dict[int, int] = {}

    async def on_message(self, message: discord.Message) -> None:
        if message.channel.id == self._sessions.get(message.author.id):
            return

    async def cog_unload(self) -> None:
        self._sessions = {}

    @commands.command(hidden=True)
    @commands.is_owner()
    async def repl(self, ctx: Context) -> None:
        """Launches an interactive REPL session."""
        variables: dict[str, Any] = {
            "ctx": ctx,
            "bot": self.bot,
            "message": ctx.message,
            "guild": ctx.guild,
            "channel": ctx.channel,
            "author": ctx.author,
            "pool": self.bot.pool,
            "`_": None,
        }

        if ctx.channel.id in self._sessions:
            await ctx.send(content=f"Already running a `REPL` session in this channel for {ctx.author}. Exit it with `quit`.")
            return

        self._sessions[ctx.author.id] = ctx.channel.id
        c_vars = "\n- ".join(variables)
        await ctx.send(
            content=f"""Enter code to execute or evaluate wrapped in '`'. Use `exit()` or `quit` to exit. {self.emoji_table.to_inline_emoji(emoji="kuma_wow")}\nCurrent Set Variables__\n- {c_vars}""",  # noqa: E501
        )

        # def check(message: discord.Message) -> bool:
        #     return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id and message.content.startswith("`")
        def on_msg_check(message: discord.Message) -> bool:
            # print("on_message", message)
            return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id and message.content.startswith("`")

        def on_msg_edit_check(before: discord.Message, after: discord.Message) -> bool:  # noqa: ARG001 # Unsused arg supression.
            # print("on_message_edit", after)
            return after.author.id == ctx.author.id and after.channel.id == ctx.channel.id and after.content.startswith("`")

        while True:
            tasks = [
                asyncio.create_task(self.bot.wait_for("message", check=on_msg_check), name="onmsg"),
                asyncio.create_task(self.bot.wait_for("message_edit", check=on_msg_edit_check), name="editmsg"),
            ]
            try:
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=10 * 60,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # response = await self.bot.wait_for("message", check=self.check, timeout=10.0 * 60.0)
                try:
                    finished: asyncio.Task = next(iter(done))
                except StopIteration:
                    LOGGER.warning(
                        "<%s.%s> | Failed to find a finished Task, <StopIteration> | Tasks: %s",
                        __class__.__name__,
                        "repl",
                        tasks,
                    )
                    msg = f"Exiting `REPL` session due to **StopIteration Error**.{self.emoji_table.to_inline_emoji(emoji='kuma_shock')}"
                    try:
                        await ctx.send(
                            content=msg,reference=ctx.message)
                    except discord.HTTPException:
                        await ctx.send(content=msg)

                    self._sessions.pop(ctx.author.id)
                    return

                for task in pending:
                    try:
                        task.cancel()
                    except asyncio.CancelledError:
                        LOGGER.warning("<%s.%s> | Failed to cancel Task. | Task: %s", __class__.__name__, "repl", task)
                        continue

                response = None
                action = finished.get_name()
                try:
                    result = finished.result()
                except TimeoutError:
                    LOGGER.warning(
                        "<%s.%s> | Failed to get Task results due to <TimeoutError> | Task: %s",
                        __class__.__name__,
                        "repl",
                        finished,
                    )
                    await ctx.send(
                        content=f"Exiting `REPL` session due to TimeoutError.{self.emoji_table.to_inline_emoji(emoji='kuma_shock')}",
                        reference=ctx.message,
                    )
                    self._sessions.pop(ctx.author.id)
                    return

                response: Optional[discord.Message] = result[1] if action == "editmsg" else result
                if response is None:
                    await ctx.send(
                        content=f"Exiting `REPL` session due to failed result parsing..{self.emoji_table.to_inline_emoji(emoji='kuma_shock')}",
                        reference=ctx.message,
                    )
                    self._sessions.pop(ctx.author.id)
                    return

            except TimeoutError:
                await ctx.send(
                    content=f"Exiting `REPL` session.{self.emoji_table.to_inline_emoji(emoji='kuma_shock')}",
                    reference=ctx.message,
                )
                self._sessions.pop(ctx.author.id)
                return

            cleaned = self.cleanup_code(response.content)

            if cleaned in ("quit", "exit", "exit()", "q"):
                await ctx.send(content=f"Exiting. {self.emoji_table.to_inline_emoji('kuma_shrug')}", reference=ctx.message)
                self._sessions.pop(ctx.author.id)
                return

            if cleaned in ("?"):
                await ctx.send(f"{variables.keys()}")

            executor = exec
            code = ""
            use_async_wrapper = False  # NEW: Track if we wrapped code in async function
            if cleaned.count("\n") == 0:
                # single statement, potentially 'eval'
                try:
                    code = compile(cleaned, "<repl session>", "eval")
                except SyntaxError:
                    pass
                else:
                    executor = eval

            # if executor is exec:
            #     try:
            #         code = compile(cleaned, "<repl session>", "exec")
            #     except SyntaxError as e:
            #         await ctx.send(content=self.get_syntax_error(e))
            #         continue
            if executor is exec:
                try:
                    # Wrap code in an async function to support await
                    wrapped = "async def __ex():\n"
                    for line in cleaned.split("\n"):
                        wrapped += f"\t{line}\n"
                    code = compile(wrapped, "<repl session>", "exec")
                    use_async_wrapper = True
                except SyntaxError as e:
                    await ctx.send(content=self.get_syntax_error(e), reference=ctx.message)
                    continue

            variables["message"] = response

            fmt = None
            stdout = io.StringIO()

            # try:
            #     with redirect_stdout(stdout):
            #         result = executor(code, variables)
            #         if inspect.isawaitable(result):
            #             result = await result
            try:
                with redirect_stdout(stdout):
                    if use_async_wrapper:
                        exec(code, variables)  # noqa: S102
                        result = await variables["__ex"]()
                    else:
                        result = executor(code, variables)
                        if inspect.isawaitable(result):
                            result = await result
            except Exception:  # noqa: BLE001 # We can't know the possible exceptions as we are using `exec`.
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
                    fmt = fmt[5:-3]  # remove code block for length check
                    await ctx.send(content="### Results:", reference=ctx.message)
                    if len(fmt) > 2000:
                        res = self.cleanup_output(fmt)
                        content= ""
                        for indx in range(len(res)):

                            if len(content + res[indx]) > 1950:
                                await ctx.send(content=content, reference=ctx.message)
                                content = res[indx] + "\n"
                            else:
                                content += res[indx] + "\n"

                        if len(content) > 0:
                            await ctx.send(content=content, reference=ctx.message)
                    else:
                        await ctx.send(content=fmt, reference=ctx.message)
            except discord.Forbidden:
                pass
            except discord.HTTPException as e:
                await ctx.send(content=f"Unexpected error: `{e}`", reference=ctx.message)

    def cleanup_code(self, content: str) -> str:
        """Automatically removes code blocks from the code."""
        # remove ```py\n```
        if content.startswith("```") and content.endswith("```"):
            temp: str = "\n".join(content.split(sep="\n")[1:-1])
            # temp = f"async def _repl():\n{temp}\n"
            # print(temp)
            return temp

        # remove `foo`
        return content.strip("` \n")

    def get_syntax_error(self, e: SyntaxError) -> str:
        if e.text is None:
            return f"```py\n{e.__class__.__name__}: {e}\n```"
        return f"```py\n{e.text}{'^':>{e.offset}}\n{e.__class__.__name__}: {e}```"



    def cleanup_output(self, content: str, *, split: str = ",") -> list[str]:
        return content.split(sep=split)

async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103
    await bot.add_cog(Repl(bot=bot))
