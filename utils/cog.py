from discord.ext import commands
import os
import logging


class KumaCog(commands.Cog):
    """Generic `commands.Cog` class inheritance to set some self attributes."""

    def __init__(self, bot: commands.Bot) -> None:
        super().__init__()
        self._bot: commands.Bot = bot
        self._logger = logging.getLogger()
        self.repo_url = "https://github.com/k8thekat/dpy_cogs"
