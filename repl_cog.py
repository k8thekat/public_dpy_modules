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
import contextlib
import inspect
import io
import logging
import traceback
from contextlib import redirect_stdout
from typing import Any, Optional, TypedDict

import discord
from discord.ext import commands

from kuma_kuma import Kuma_Kuma
from utils import (
    KumaCog as Cog,  # need to replace with your own Cog class
    KumaContext as Context,
)

LOGGER = logging.getLogger()


class Session(TypedDict):
    user: discord.User | discord.Member
    message: discord.Message
    channel: int


# How long a session waits for its next line before closing itself.
SESSION_TIMEOUT: float = 10 * 60


class Repl(Cog):
    repo_url: str = "https://github.com/k8thekat/public_dpy_modules"

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    async def cog_load(self) -> None:
        # Keyed by user ID: a session belongs to a person, and one channel can hold several.
        self._sessions: dict[int, Session] = {}

    async def cog_unload(self) -> None:
        self._sessions = {}

    async def end_session(self, context: Context, *, reason: str) -> None:
        """Closes the caller's session and says why.

        Every way out of the loop goes through here, so the session is dropped exactly once and the
        goodbye is written exactly once. The old code repeated the pop and the send at each exit,
        and one of them sent the same message twice.

        Parameters
        ----------
        context: :class:`Context`
            The context the session was started from.
        reason: :class:`str`
            Why the session is ending, as a fragment: "timed out", "you asked".

        """
        session: Optional[Session] = self._sessions.pop(context.author.id, None)
        reference: Optional[discord.Message] = session["message"] if session is not None else None
        with contextlib.suppress(discord.HTTPException):
            await context.send(
                content=f"Exiting the `REPL` session — {reason}. {self.emoji_table.kuma_shrug}",
                reference=reference,
            )

    # TODO: Replace ctx.message to reference the most recent edited or reply message
    # Check `pop` references were I remove an existing session. Validate reference messages and content in array.

    # TODO: Fix Results reply formatting.
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
            # "`_`": None, Unsure what this variable was being used for.
        }

        # Keyed by author, so this has to *check* by author too — it looked the channel ID up in a
        # dict of user IDs, never matched, and a second `repl` quietly replaced the first session's
        # bookkeeping while the first loop carried on running against it.
        existing: Optional[Session] = self._sessions.get(ctx.author.id)
        if existing is not None:
            await ctx.send(
                content=f"You already have a `REPL` session open in <#{existing['channel']}>. Exit it with `quit`. "
                f"{self.emoji_table.kuma_hmm}",
            )
            return

        self._sessions[ctx.author.id] = {"user": ctx.author, "channel": ctx.channel.id, "message": ctx.message}

        c_vars = "\n- ".join(variables)
        await ctx.send(
            content=(
                f"Enter code to execute or evaluate wrapped in backticks. {self.emoji_table.kuma_wow}\n"
                f"Use `exit()` or `quit` to exit.\n"
                f"__Session Variables__\n- {c_vars}"
            ),
            reference=self._sessions[ctx.author.id]["message"],
        )

        # def check(message: discord.Message) -> bool:
        #     return message.author.id == ctx.author.id and message.channel.id == ctx.channel.id and message.content.startswith("`")
        def on_msg_check(message: discord.Message) -> bool:
            # print("on_message", message)
            if message.author.id == ctx.author.id and message.channel.id == ctx.channel.id and message.content.startswith("`"):
                self._sessions[ctx.author.id]["message"] = message
                return True
            return False

        def on_msg_edit_check(before: discord.Message, after: discord.Message) -> bool:  # noqa: ARG001 # Unsused arg supression.
            # print("on_message_edit", after)
            if after.author.id == ctx.author.id and after.channel.id == ctx.channel.id and after.content.startswith("`"):
                self._sessions[ctx.author.id]["message"] = after
                return True
            return False

        while True:
            waiters: list[asyncio.Task[Any]] = [
                asyncio.create_task(self.bot.wait_for("message", check=on_msg_check), name="onmsg"),
                asyncio.create_task(self.bot.wait_for("message_edit", check=on_msg_edit_check), name="editmsg"),
            ]
            done, pending = await asyncio.wait(waiters, timeout=SESSION_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)

            # Whatever did not win is cancelled on every path out of here. `wait_for` registers a
            # listener on the bot and only drops it when its future resolves, so a task left pending
            # goes on running its check against a session that has already been closed — and the
            # check reaches into `_sessions` for an entry that is no longer there.
            for task in pending:
                task.cancel()

            if not done:
                # `asyncio.wait` *returns* on timeout rather than raising it, so the `except
                # TimeoutError` this used to sit in was unreachable; a quiet session fell into the
                # StopIteration branch instead, which blamed the wrong thing and left both listeners
                # registered on the way out.
                await self.end_session(ctx, reason="it went quiet for 10 minutes")
                return

            finished: asyncio.Task[Any] = next(iter(done))
            try:
                result: Any = finished.result()
            except Exception:
                LOGGER.exception("<%s.%s> | The REPL waiter raised. | Task: %s", __class__.__name__, "repl", finished.get_name())
                await self.end_session(ctx, reason="something went wrong waiting for your next line")
                return

            # `wait_for("message_edit")` resolves to a `(before, after)` pair; `message` resolves to
            # the message itself.
            response: Optional[discord.Message] = result[1] if finished.get_name() == "editmsg" else result
            if response is None:
                await self.end_session(ctx, reason="I couldn't read that result")
                return

            cleaned = self.cleanup_code(response.content)

            if cleaned in ("quit", "exit", "exit()", "q"):
                await self.end_session(ctx, reason="you asked")
                return

            if cleaned == "?":
                await ctx.send(content="__Session Variables__\n- " + "\n- ".join(variables))
                continue

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
                    await ctx.send(content=self.get_syntax_error(e), reference=self._sessions[ctx.author.id]["message"])
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
                    await ctx.send(content="### Results:", reference=self._sessions[ctx.author.id]["message"])
                    if len(fmt) > 2000:
                        res = self.cleanup_output(fmt)
                        content = ""
                        for indx in range(len(res)):
                            if len(content + res[indx]) > 1950:
                                await ctx.send(content=content, reference=self._sessions[ctx.author.id]["message"])
                                content = res[indx] + "\n"
                            else:
                                content += res[indx] + "\n"

                        if len(content) > 0:
                            await ctx.send(content=content, reference=self._sessions[ctx.author.id]["message"])
                    else:
                        await ctx.send(content=fmt, reference=self._sessions[ctx.author.id]["message"])
            except discord.Forbidden:
                pass
            except discord.HTTPException as e:
                await ctx.send(content=f"Unexpected error: `{e}`", reference=self._sessions[ctx.author.id]["message"])

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
