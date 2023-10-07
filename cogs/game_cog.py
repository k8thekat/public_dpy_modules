from asyncio import tasks
from datetime import datetime, timedelta
import json

import discord
from discord.ext import commands, tasks
import aiohttp
from aiohttp import ClientResponse
import os
import logging
from typing_extensions import Any


class Game(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self._bot: commands.Bot = bot
        self._name: str = os.path.basename(__file__).title()
        self._logger = logging.getLogger()
        self._logger.info(f'**SUCCESS** Initializing {self._name} ')
        self._message_timeout: int = 120

        self._sessions = aiohttp.ClientSession()

        self._status_channel: int = 1004518996701356053
        self._server_id: int = 602285328320954378  # Kat's Paradise
        self._message: discord.Message | None = None
        self._guild: discord.Guild | None

    async def cog_load(self) -> None:
        if self.test_loop.is_running() is False:
            self.test_loop.start()
        self._guild = self._bot.get_guild(self._server_id)

    async def cog_unload(self) -> None:
        if self.test_loop.is_running():
            self.test_loop.stop()

    async def _get_data(self) -> tuple[Any, timedelta] | None:
        request: ClientResponse = await self._sessions.get(url="https://data.d4planner.io/worldbosses.json")
        if request.status != 200:
            self._logger.error("Error: Could not retrieve worldboss data")
            return None
        else:
            self._logger.info("Success: Retrieved worldboss data")
            res: bytes = await request.read()
            diablo_info = json.loads(res.decode())
            return await self._format_bossdata(content=diablo_info)

    # async def webhook(self, content: str) -> None:
    #     data = {"content": content, "username": self._user_name}
    #     result: ClientResponse = await self._sessions.post(self._url, json=data)
    #     if 200 <= result.status < 300:
    #         # self._logger.info(f"Webhook sent {result.status}")
    #         return
    #     else:
    #         self._logger.warn(f"Webhook not sent with {result.status}, response:\n{result.json()}")
    #         return

    def time_convert(self, value: timedelta) -> str:
        """ Days, Hours, Minutes and Seconds."""
        day = f"{value.days} Day(s)"
        hours = value.seconds // 3600
        minutes = int(value.seconds / 60)

        hours = f"{hours} Hour(s)"
        minutes = f"{minutes} Minute(s)"
        seconds = f"{int(value.seconds / 60)} Seconds(s)"
        return f"{day}, {hours}, {minutes}, {seconds}"

    async def _format_bossdata(self, content):
        for spawn_location in content:
            if spawn_location == "helltide":
                continue
            # **Helltides** (Length= 1h)
            # We all know Helltides spawn every 2hr, 15minute, but now we also can determine which chests will be open.
            # **Legion Events**
            # Legion event timers follow a pattern that is similar to world bosses.
            # The time pattern is 1-2-1-2-2 repeating. *note, the ms is an estimate since I don't have ms precision, so that still needs to be tweaked to perfection, otherwise there is drift.
            #     30min,13sec,400ms
            #     33min,29sec,500ms
            #     30min,13sec,400ms
            #     33min,29sec,500ms
            #     33min,29sec,500ms
            # Legion events will always spawn from :05-:10, or :35-:40, so the blackout time is when the minute is 0-5, 10-35, 40-59. If a legion event would spawn during a blackout period, you subtract 5 mins to get it back into the window.
            # The location for all these events is still to be determined, hopefully will be finished tomorrow, but https://d4armory.io/events does post the location ahead of time when the notification appears in game. And this data isn't on my site yet, but will be updated with it tomorrow hopefully. I'll also plan on making an excel or something for easier planning ahead.
            # Shoutout to Rabid for telling me to look at the data for the patterns!
            spawn_time = int(str(content.get(spawn_location).get("nextSpawn")))
            boss_name = content.get(spawn_location).get("name")
            if (datetime.fromtimestamp(spawn_time / 1000)) < datetime.now():
                continue

            when_spawn: timedelta = datetime.fromtimestamp(spawn_time / 1000) - datetime.now()
            return boss_name, when_spawn

    async def _send_message(self):
        res = await self._get_data()
        if res is not None:
            boss_name = res[0]
            when_spawn = res[1]
        else:
            return

        if self._guild:
            guild_channel = self._guild.get_channel(self._status_channel)
            if guild_channel and isinstance(guild_channel, discord.TextChannel):
                if self._message != None and guild_channel.permissions_for(self._guild.me).manage_messages:
                    await self._message.edit(content=f"__**{boss_name}**__ \nSpawns in `{self.time_convert(when_spawn)}` Minutes")
                else:
                    self._message = await guild_channel.send(content=f"__**{boss_name}**__ \nSpawns in `{self.time_convert(when_spawn)}`")
            else:
                self._logger.error("Failed to get Channel")
        else:
            self._logger.error(f"Failed to get Guild")

    @tasks.loop(minutes=1)
    async def test_loop(self) -> None:
        # await self._get_data()
        await self._send_message()

    @commands.command(help="Reset loop", aliases=["d4loop_reset"])
    async def boss_loop_reset(self, context: commands.Context):
        if self.test_loop.is_running():
            self.test_loop.cancel()
            self.test_loop.start()
            await context.send(content=f"Resetting the Diablo 4 Boss Timer loop", delete_after=self._message_timeout)
        else:
            await context.send(content=f"Diablo 4 Boss Timer loop is not running.", delete_after=self._message_timeout)

    @commands.command(help="Diablo 4 Timers", aliases=["d4timer", "boss"])
    async def boss_timers(self, context: commands.Context):
        res = await self._get_data()
        if res is not None:
            boss_name = res[0]
            when_spawn = res[1]
            await context.send(content=f"__**{boss_name}**__ \nSpawns in `{self.time_convert(when_spawn)}` Minutes")
        else:
            return await context.send(content=f"None")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Game(bot))
