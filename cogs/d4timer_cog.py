from ast import alias
from discord.ext import commands, tasks
import logging
import os
from datetime import datetime

import cogs._d4_world_boss as _d4_world_boss


class Diablo4(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self._bot: commands.Bot = bot
        self._name: str = os.path.basename(__file__).title()
        self._logger = logging.getLogger()
        self._logger.info(f'**SUCCESS** Initializing {self._name} ')
        self._message_timeout: int = 120

        self._world_boss = _d4_world_boss.WorldBoss()

    def cog_load(self):
        # _boss_time: datetime = self._world_boss.json_load()
        _boss_time = self._world_boss._last_known_spawn
        self._last_boss_spawn = self._world_boss.spawn_counter(last_time=_boss_time)
        if self.next_boss.is_running() == False:
            self._logger.info("Starting Next Boss loop...")
            self.next_boss.start()

    def cog_unload(self):
        if self.next_boss.is_running():
            self._logger.info("Stopping Next Boss loop...")
            self.next_boss.cancel()
        # self._world_boss.json_save()

    @tasks.loop(minutes=1)
    async def next_boss(self):
        self._logger.info("Updating for Next Boss")
        # _boss = self._world_boss.copy_boss
        _boss = self._last_boss_spawn[1]
        _time = self._last_boss_spawn[0]
        # TODO - Finish loop to keep last known spawn current.
        if _time < datetime.now(tz=self._world_boss._tz_info):
            self._last_boss_spawn = self._world_boss.spawn_counter(last_time=_time)
        # _boss_time = self._world_boss._last_known_spawn
        # _world_boss = _d4_world_boss.WorldBoss(last_spawn=)

    @commands.hybrid_group(name="d4")
    async def diablo_4(self, context: commands.Context):
        print()

    @diablo_4.command(name="role", help="The role to ping when a boss is about to spawn.")
    async def diablo4_boss_role(self, context: commands.Context, role):
        # TODO - Possible write this out to a DB? Or simply use the existing .json file?
        print()

    @diablo_4.command(name="next_boss", aliases=["nb", "next"], help="The next World Boss to Spawn")
    async def diablo4_next_boss(self, context: commands.Context):
        _boss = self._world_boss.next_boss
        if isinstance(_boss, str):
            await context.send(content=_boss, delete_after=self._message_timeout)
        else:
            await context.send(content=f"I am unable to find the next World Boss spawn...", delete_after=self._message_timeout)

    @diablo_4.command(name="last_boss", aliases=["lb", "last"], help="The most recent World Boss Spawn")
    async def diablo4_last_boss(self, context: commands.Context):
        _boss = self._world_boss.last_boss
        if isinstance(_boss, str):
            await context.send(content=_boss, delete_after=self._message_timeout)
        else:
            await context.send(content=f"I am unable to find the last World Boss spawn...", delete_after=self._message_timeout)

    @diablo_4.command(name="future_bosses", aliases=["fb", "future"], help="Shows the next `X` number of spawns")
    async def diablo4_future_bosses(self, context: commands.Context, num: int):
        _boss = self._world_boss.copy_boss
        _bosses = _boss.sequence_bosses(num=num)
        self._logger.info(_bosses)
        res = "\n".join([f"- {entry['name']} @ {entry['time']}" for entry in _bosses])
        await context.send(content=f"**Upcoming World Spawns**\n{res}")

    # TODO - Need a channel command
    # TODO - Need a way to get Next spawn and X number of subsequent spawns if wanted.
    # TODO - Need to store last_known time to query off of.
    # TODO/Feature - Possibly use pillow to draw over images times.


async def setup(bot: commands.Bot):
    await bot.add_cog(Diablo4(bot))
