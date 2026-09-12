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

from __future__ import annotations

import asyncio
import logging
from configparser import ConfigParser
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Optional

import discord
from ampapi import (
    ActionResult,
    ActionResultError,
    AMPADSInstance,
    AMPControllerInstance,
    AMPInstance,
    AMPInstanceState,
    AMPMinecraftInstance,
    APIParams,
    Bridge,
    Instance,
    InstanceTypeAliases,
    Role,
    Status,
    Updates,
)
from discord import app_commands
from discord.ext import commands, tasks

from kuma_kuma import Kuma_Kuma
from utils import KumaCog as Cog, KumaEmbed  # need to replace with your own Cog class

if TYPE_CHECKING:
    from pathlib import Path

    from ampapi.modules import AnalyticsSummary
    from discord.ext.tasks import Loop
    from discord.guild import GuildChannel

    from kuma_kuma import Kuma_Kuma
    from utils import KumaContext as Context

BOT_NAME = "Kuma Kuma"


class WhitelistActions(Enum):
    add = "add"
    remove = "remove"
    on = "on"
    off = "off"
    list = "list"


# Discord USER ID = [INSTANCE IDs]
SERVER_OWNERS: dict[str, list[str]] = {
    "447422100798570496": ["3534c4c1-e212-458f-a013-31aa86935c49"],  # Vaskels Server
    "172808207536881664": ["61d811aa-b897-44ba-bb75-b488278a13c4"],  # Fret's server
    "651454696208465941": ["99a98c16-9474-4888-9eb7-45abce5d1bdd"],  # Ducki's Server (Bo_bi)
    "606648465065246750": ["04a36223-f761-4e3f-9f50-9ce2d831ea57"],  # Flying's Server
    "409050137780944911": ["6f567df4-d1b4-4758-bfe6-529fb3bc2ff9"],  # Public ATM10 // "FullSpring"
}
# Could possibly store the Guild ID in the future for public usage.
# Instance.friendly_name = Channel ID
SERVER_CHANNELS: dict[str, int] = {
    "6f567df4-d1b4-4758-bfe6-529fb3bc2ff9": 1364276004285317120,
}
# class MetricsEmbed(discord.Embed):
#     def __init__(self, title: str, description: str, data: Any) -> None:
#         color: discord.Color = discord.Color.blurple()

#         super().__init__(color=color, title=title, description="", timestamp=discord.utils.utcnow())
LOGGER = logging.getLogger()


class Gatekeeper(Cog):
    """Drives CubeCoders AMP game servers from Discord, via the `ampapi` library.

    The successor to GatekeeperV2, folded in as a cog rather than run as its own bot. AMP's
    Application Deployment Server (the `ADS` controller instance) owns a fleet of game server
    instances - Minecraft, Source, Valheim and the rest - and `ampapi` wraps its HTTP API in typed
    Python. This exposes that fleet to a guild without handing anyone the AMP panel itself.

    What it does today:

    - **Live status.** Channel names track each instance's state on a loop, so a glance at the
      sidebar answers "is the server up" without asking.
    - **Chat bridge.** Server console chatter is relayed into a mapped Discord channel and back.
    - **Instance control.** Start, stop, restart and update an instance from Discord.
    - **Whitelisting.** Add a player to a server without touching its config.
    - **Console access.** Send a command to an instance's console.
    - **AMP roles.** Duplicate an existing AMP user role as the basis for a new one.

    Where it is going: GatekeeperV2 covered rather more - per-instance permissions tied to Discord
    roles, display banners, donator perks and server metadata - and those are the shape of what this
    cog is expected to grow into. The command surface here is the subset that has been ported.

    .. note::
        Every command needs `ampapi` to have authenticated against the ADS first; see
        :attr:`Gatekeeper.ADS` and the :meth:`update_server_list` loop that keeps the fleet fresh.

    """

    ADS: AMPControllerInstance
    servers_dict: dict[str, InstanceTypeAliases]
    servers_chat_dict: dict[str, GuildChannel]
    _servers: set[InstanceTypeAliases]

    # Server Status Emojis
    stopped_emoji = "\U0001f6d1"  # Octagonal Sign
    running_emoji: str = "\U00002705"  # WHITE HEAVY CHECK MARK

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    @property
    def servers(self) -> set[InstanceTypeAliases]:
        return self._servers

    async def cog_load(self) -> None:
        self.ini_load()
        self.ADS = AMPControllerInstance(session=self.bot.session)

        # Easier lookup to get AMP Instance objects.
        result: set[InstanceTypeAliases] | ActionResultError = await self.ADS.get_instances()
        self.servers_dict = {}

        self.servers_chat_dict = {}

        if isinstance(result, ActionResultError):
            LOGGER.error(
                "Failed to retrieve AMP Instances via `AMPControllerInstance.get_instances()`. | Error: %s",
                result,
            )
            return

        self._servers = result
        for server in self.servers:
            self.servers_dict[server.friendly_name] = server

        self.server_choices: list[app_commands.Choice[str]] = [
            app_commands.Choice(name=e.friendly_name, value=e.instance_id) for e in self.servers
        ]
        if self.update_server_list.is_running() is False:
            LOGGER.info(
                "Starting Gatekeeper.update_server_list task, running every %s minutes. | Reconnect: %s",
                self.update_server_list.minutes,
                self.update_server_list.reconnect,
            )
            self.update_server_list.start()
            self.bot.task_loops.append(self.update_server_list)

        # if self.server_status_via_channels.is_running() == False:
        #     LOGGER.info(
        #         "Starting Gatekeeper.server_status_via_channels task, running every %s minutes. | Reconnect: %s",
        #         self.server_status_via_channels.minutes,
        #         self.server_status_via_channels.reconnect,
        #     )
        #     self.server_status_via_channels.start()
        #     self.bot.task_loops.append(self.server_status_via_channels)

        # if self.server_chat_via_channels.is_running() == False:
        #     LOGGER.info(
        #         "Starting Gatekeeper.server_chat_via_channels task, running every %s minutes. | Reconnect: %s",
        #         self.server_chat_via_channels.minutes,
        #         self.server_chat_via_channels.reconnect,
        #     )
        #     self.server_chat_via_channels.start()
        #     self.bot.task_loops.append(self.server_chat_via_channels)

    async def cog_unload(self) -> None:
        """Stops our task loops and un-registers them, so a reload does not stack duplicates in `bot.task_loops`.

        A reloaded cog is a new object, so `is_running()` is False on its fresh `Loop` and `cog_load` starts and
        appends again - the previous entry stays in the list forever unless it is removed here.

        The two channel loops are named even though `cog_load` currently leaves them commented out; both guards
        are False for a loop that never started, so un-commenting them up there needs no change down here.
        """
        for loop in (self.update_server_list, self.server_status_via_channels, self.server_chat_via_channels):
            if loop.is_running():
                loop.cancel()
            if loop in self.bot.task_loops:
                self.bot.task_loops.remove(loop)

    def ini_load(self) -> None:
        """Parse the local ini file to load AMP login information."""
        file: Path = self.bot.local_ini
        if file.is_file():
            settings = ConfigParser(converters={"list": lambda setting: [value.strip() for value in setting.split(",")]})
            settings.read(filenames=file)
            # login creds
            url: Optional[str] = settings.get(section="AMP", option="url", fallback=None)
            user: Optional[str] = settings.get(section="AMP", option="user", fallback=None)
            password: Optional[str] = settings.get(section="AMP", option="password", fallback=None)
            token: Optional[str] = settings.get(section="AMP", option="token", fallback=None)
            LOGGER.debug(("Gatekeeper Creds. | Url: %s | User: %s | Token: %s | Path: %s"), url, user, token, file.as_posix())
            if url is None or user is None or password is None or token is None:
                # Formatted here rather than handed to the exception as logging style `%s` arguments,
                # which never interpolate - the message reached the console with literal `%s` in it.
                # Says which options are missing rather than echoing any of their values back.
                missing: str = ", ".join(
                    name for name, value in (("url", url), ("user", user), ("password", password), ("token", token)) if value is None
                )
                msg = f"Gatekeeper failed to load credentials. | Missing: {missing} | Path: {file.as_posix()}"
                raise ValueError(msg)
            Bridge(api_params=APIParams(url=url, user=user, password=password, token=token, use_2fa=True))
        else:
            msg = f"Gatekeeper failed to load credentials from path: {file.as_posix()}"
            raise ValueError(msg)
        return

    @staticmethod
    def _matches(query: str, *fields: str) -> bool:
        """Whether the query appears in any of the fields.

        `query in (a or b)` was the old spelling, and `or` hands back the first *truthy* operand --
        so it only ever searched `a`, and searching an Instance by its ID silently did nothing.

        Parameters
        ----------
        query : :class:`str`
            What has been typed so far.
        *fields : :class:`str`
            The fields to search, e.g. a friendly name and an Instance ID.

        Returns
        -------
        :class:`bool`
            Whether any field contains the query.

        """
        needle: str = query.lower()
        return any(needle in field.lower() for field in fields if field)

    async def autocomp_server_list(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if await self.bot.is_owner(interaction.user):
            return [entry for entry in self.server_choices if self._matches(current, entry.name, entry.value)][:25]

        temp: Optional[list[str]] = SERVER_OWNERS.get(str(object=interaction.user.id))
        if temp is None:
            return [app_commands.Choice(name="No Access - GTFO", value="NONE")]
        # This should limit the server options to those that the user should only have access to via SERVER_OWNERS dict.
        priv_servers: list[app_commands.Choice[str]] = [entry for entry in self.server_choices if entry.value in temp]
        return [entry for entry in priv_servers if self._matches(current, entry.name, entry.value)][:25]

    async def autocomp_role_list(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:  # noqa: ARG002
        roles: list[Role] | ActionResultError = await self.ADS.get_role_data()
        if isinstance(roles, ActionResultError):
            return [app_commands.Choice(name="Failed Lookup", value="NONE")]
        # Discord caps an autocomplete response at 25; AMP hands back every role it has.
        return [app_commands.Choice(name=role.name, value=role.id) for role in roles if self._matches(current, role.name, role.id)][:25]

    @tasks.loop(minutes=1, reconnect=True)
    async def update_server_list(self) -> None:
        res: set[InstanceTypeAliases] | ActionResultError = await self.ADS.get_instances()
        if isinstance(res, ActionResultError):
            LOGGER.error("Failed to retrieve AMP Instances via `get_instances()`. | Error: %s", res)
            return
        if res != self.servers:
            # Update the attribute with the new Instance set.
            self._servers = res
            # Update the dictionary we use to store the Instance objects.
            for server in self.servers:
                self.servers_dict[server.friendly_name] = server
            LOGGER.info("Updated our list of available AMP Instances. | Num of Instances: %s", len(self.servers))
        return None

    @tasks.loop(minutes=3, reconnect=True)
    async def server_status_via_channels(self) -> Any:
        if self.bot.is_ready() is False:
            LOGGER.debug("Kuma Kuma bear is not ready, restarting the Gatekeeper.server_status_via_channels task in %s seconds.", 5)
            await asyncio.sleep(5)
            self.server_status_via_channels.restart()
            return

        neko_guild: Optional[discord.Guild] = await self.get_guild()
        if neko_guild is None:
            LOGGER.error("Failed to retrieve the Discord Guild inside <Gatekeeper.server_status_via_channels>.")
            return

        # We go through all the AMP Instances and see if we have any channels set; then update them.
        for instance in self.servers:
            chan_id: Optional[int] = SERVER_CHANNELS.get(instance.instance_id)
            if chan_id is None:
                # This isn't really an error or warning worthy as we have hard coded the dict.
                LOGGER.debug("Failed to find a Channel ID for Instance: %s", instance.friendly_name)
                continue

            guild_channel: Optional[GuildChannel] = neko_guild.get_channel(chan_id)
            if guild_channel is None:
                LOGGER.warning("Failed to retrieve Guild Channel ID: %s for Instance: %s", chan_id, instance.friendly_name)
                continue

            if guild_channel.permissions_for(neko_guild.me).manage_channels is False:
                LOGGER.warning(
                    "Unable to manage the Guild Channel: %s, we do not have the `manage_channels` permission.",
                    guild_channel,
                )
                continue

            # Now lets get updated Instance information so we can update the channel better.
            status: Status | ActionResultError = await instance.get_application_status()
            if isinstance(status, ActionResultError):
                LOGGER.error(
                    "<Instance.get_application_stats()> returned an Error.| Instance: %s | Error: %s",
                    instance.friendly_name,
                    status,
                )
                return

            state_emoji: str = self.running_emoji if instance.running else self.stopped_emoji
            players: int = (
                instance.metrics.active_users.raw_value if instance.metrics is not None and instance.metrics.active_users is not None else 0
            )
            res: GuildChannel = await guild_channel.edit(name=f"{state_emoji}-atm10︱users-{players}")

            # Add the Guild Channel to our Dict for easier usage on updating Discord Chat.
            self.servers_chat_dict[instance.friendly_name] = res

    @tasks.loop(seconds=30, reconnect=True)
    async def server_chat_via_channels(self) -> Any:
        if self.bot.is_ready() is False:
            LOGGER.debug("Kuma Kuma bear is not ready, restarting the Gatekeeper.server_status_via_channels task in %s seconds.", 5)
            await asyncio.sleep(5)
            self.server_chat_via_channels.restart()
            return

        neko_guild: discord.Guild | None = await self.get_guild()
        if neko_guild is None:
            LOGGER.error("Failed to retrieve the Discord Guild inside <Gatekeeper.server_chat_via_channels()>.")
            return

        for instance in self.servers:
            # Let's first attempt to get updated information about the AMPInstance.
            amp_instance: InstanceTypeAliases | ActionResultError = await instance.get_instance_status()
            # Any AMP API issues fall into this if check.
            if isinstance(amp_instance, ActionResultError):
                LOGGER.error(
                    "Failed to retrieve <Instance.get_instance_status()> for Instance %s. | Error: %s",
                    instance.friendly_name,
                    amp_instance,
                )
                continue

            # Let's also update your dict with the most recent AMPInstance object.
            self.servers_dict[instance.friendly_name] = amp_instance
            if amp_instance.running is False:
                LOGGER.warning("The Instance: %s is not Running, unable to get console updates.", amp_instance.friendly_name)
                continue
            res: Updates | ActionResultError = await amp_instance.get_updates()
            if isinstance(res, ActionResultError):
                LOGGER.error(
                    "Failed to retrieve <Instance.get_updates()> for Instance %s. | Error: %s",
                    amp_instance.friendly_name,
                    res,
                )
                continue

            chan_id: Optional[int] = SERVER_CHANNELS.get(instance.instance_id)
            if chan_id is None:
                # This isn't really an error or warning worthy as we have hard coded the dict.
                LOGGER.debug("Failed to find a Channel ID for Instance: %s", instance.friendly_name)
                continue

            guild_channel: Optional[GuildChannel] = neko_guild.get_channel(chan_id)
            if guild_channel is None:
                LOGGER.warning("Failed to retrieve Guild Channel ID: %s for Instance: %s", chan_id, instance.friendly_name)
                continue

            if guild_channel.permissions_for(neko_guild.me).send_messages is False:
                LOGGER.warning("Unable to manage the Guild Channel: %s, we do not have the `send_messages` permission.", guild_channel)
                continue

            # Now let's send our Console messages to the channel.
            if isinstance(guild_channel, discord.TextChannel):
                # ? Suggestion
                # Need to create a function to take the entries and join on Newline/etc and
                # check line length does not exceed 2k per channel send.
                for message in res.console_entries:
                    if message.type.lower() == "chat":
                        # This is to prevent Rate limiting in a hacky way atm.
                        await asyncio.sleep(0.15)
                        await guild_channel.send(content=message.contents)

    @commands.hybrid_command(name="whitelist", help="Whitelist a user to the server.", aliases=["wl"])
    @app_commands.autocomplete(server=autocomp_server_list)
    async def whitelist_server(
        self,
        context: Context,
        server: str,
        name: str,
        action: WhitelistActions = WhitelistActions.add,
    ) -> Optional[discord.Message]:
        await context.typing(ephemeral=True)

        if server == "NONE":
            return await context.send(
                content=f"{self.emoji_table.kuma_uwu}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        try:
            instance: Instance | ActionResultError = await self.ADS.get_instance(instance_id=server)
            if isinstance(instance, ActionResultError):
                LOGGER.error(
                    "Failed Instance lookup in Whitelist Server. | Instance ID: %s | IGN: %s | Action: %s | Discord User: %s",
                    server,
                    name,
                    action,
                    context.author,
                )
                return await context.send(content=f"Failed to get instance by ID: `{server}` {self.emoji_table.kuma_sad}\n> {instance}")
        except Exception as e:  # noqa: BLE001
            return await context.send(content=f"Failed to get instance by ID: `{server}` {self.emoji_table.kuma_sad}\n> {e}")

        # TODO(@k8theat): - The app_state check may cause problems in the future. Need to test.
        # if isinstance(instance, AMPMinecraftInstance) and instance.running is True and instance.app_state == AMPInstanceState.ready:
        if isinstance(instance, AMPMinecraftInstance) and instance.running is True:
            # `action.value`, not `action`. Interpolating the Enum itself sent the console the literal
            # `/whitelist WhitelistActions.add <name>`, which Minecraft rejected every single time.
            await instance.send_console_message(msg=f"/whitelist {action.value} {name}")
            # We delay one second in an attempt to gaurentee the console update we get is the whitelist message.
            # We may get the wrong message though.
            await asyncio.sleep(delay=1)
            await instance.get_updates()

            if len(instance.console_entries) > 0:
                try:
                    # Managed to get an Index error on the console_entries.
                    return await context.send(
                        content=instance.console_entries[-1].contents,
                        ephemeral=True,
                        delete_after=self.message_timeout,
                    )
                except IndexError:
                    pass
            return await context.send(
                content=f"Failed to get console reply. {self.emoji_table.kuma_hmm}", ephemeral=True, delete_after=self.message_timeout
            )

        return await context.send(
            content=f"It appears the Instance is having trouble...{self.emoji_table.kuma_bleh}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(name="console_message", help="Send a message to the console.", aliases=["console", "cmsg"])
    @app_commands.autocomplete(server=autocomp_server_list)
    async def console_message(self, context: Context, server: str, message: str) -> discord.Message | None:
        if server == "NONE":
            return await context.send(
                content=f"{self.emoji_table.kuma_uwu}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
        try:
            instance: InstanceTypeAliases | ActionResultError = await self.ADS.get_instance(instance_id=server)
            if isinstance(instance, ActionResultError):
                return await context.send(
                    content=f"Failed to get instance by ID: `{server}` {self.emoji_table.kuma_sad}",
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )
        except Exception as e:  # noqa: BLE001
            return await context.send(
                content=f"Failed to get instance by ID: `{server}` {self.emoji_table.kuma_sad}\n> {e}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        if isinstance(instance, InstanceTypeAliases) and instance.running is True:
            # By calling get_updates() early with how the API handles console entries gaurentee's the next entry in the console will most
            # likely be our console message and the response to it.
            await instance.get_updates()
            await instance.send_console_message(msg=message)
            await asyncio.sleep(delay=1)
            await instance.get_updates()
            console_content: str = "\n".join([entry.contents for entry in instance.console_entries])

            # Basic truncating of the content as 2k is the char limit.
            if len(console_content) > 2000:
                console_content = console_content[:1980] + "\n...."

            return await context.send(
                content=f"**Sent Message**: {message}\n**Response**:" + console_content,
                ephemeral=True,
                delete_after=self.message_timeout,
            )
            # return await context.send(content=instance.console_entries[-1].contents, ephemeral=True, delete_after=self.message_timeout)
        await context.send(
            content=f"It appears the Instance is having trouble...{self.emoji_table.kuma_bleh}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )
        return None

    @commands.hybrid_command(name="duplicate_role", help="Duplicate an AMP User Role", aliases=["role_dupe", "amprd", "amp_rd"])
    @commands.is_owner()
    @app_commands.autocomplete(source=autocomp_role_list)
    async def duplicate_amp_role(self, context: Context, source: str, new_role_name: str) -> discord.Message | None:
        roles: list[Role] | ActionResultError = await self.ADS.get_role_data()
        res: ActionResult | ActionResultError = await self.ADS.create_role(role_name=new_role_name, as_common_role=False)
        source_role: Role | ActionResultError = await self.ADS.get_role(role_id=source)

        new_role: Optional[Role | ActionResultError] = None
        # The `isinstance` has to come first: an `ActionResultError` has no `result`, so reading it
        # up front raised `AttributeError` on exactly the failure this check exists to handle.
        if isinstance(res, ActionResult) and res.result is not None:
            new_role = await self.ADS.get_role(role_id=res.result)

        if source_role is None or new_role is None or isinstance(source_role, ActionResultError) or isinstance(new_role, ActionResultError):
            LOGGER.error("Failed to locate Role ID: %s . | Roles: %s", source, roles)
            return await context.send(
                content=f"Failed to locate Role ID: `{source}` {self.emoji_table.kuma_hmm}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        temp: list[str] = []
        for perm in source_role.permissions:
            if perm.startswith("-"):
                await self.ADS.set_amp_role_permission(role_id=new_role.id, permission_node=perm.replace("-", ""), enabled=False)
                temp.append(f"Set {perm.replace('-', '')}: **False**")
            else:
                LOGGER.info(await self.ADS.set_amp_role_permission(role_id=new_role.id, permission_node=perm, enabled=True))
                temp.append(f"Set {perm}: **True**")

        return await context.send(
            content=f"Duplicated **{source_role.name}** to **{new_role.name}** with permissions: {self.emoji_table.kuma_star_eye}\n"
            + "\n".join(temp),
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    # TODO(@k8thekat): Convert to view with proper components?
    @commands.hybrid_command(
        name="instance_app_control",
        help="Perform certain actions on the Server.",
        aliases=["app_control", "apc", "iac", "iapp_control"],
    )
    @app_commands.autocomplete(server=autocomp_server_list)
    @app_commands.describe(action="The Instance application action to perform.")
    async def instance_app_control(
        self,
        context: Context,
        server: str,
        action: Literal["start", "stop", "restart", "update"],
    ) -> Optional[discord.Message]:
        await context.defer()
        if server == "NONE":
            return await context.send(
                content=f"{self.emoji_table.kuma_uwu}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
        try:
            instance: InstanceTypeAliases | ActionResultError = await self.ADS.get_instance(instance_id=server)
        except Exception as e:  # noqa: BLE001
            return await context.send(
                content=f"Failed to get instance by ID: `{server}` {self.emoji_table.kuma_sad}\n> {e}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        if isinstance(instance, ActionResultError):
            return await context.send(
                content=f"Failed to get instance by ID: `{server}` {self.emoji_table.kuma_sad}\n> {instance}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        failed = False
        res = None
        if isinstance(instance, InstanceTypeAliases) and instance.running is True:
            if action == "start":
                if instance.app_state.name in ("starting", "installing", "failed", "stopping", "preparing_for_sleep"):
                    failed = True
                else:
                    res: Optional[ActionResult | ActionResultError] = await instance.start_application()

            elif action == "stop":
                if instance.app_state.name in ("stopped", "stopping"):
                    failed = True
                else:
                    res = await instance.stop_application()

            elif action == "restart":
                if instance.app_state.name in ("updating", "restarting", "installing"):
                    failed = True
                else:
                    res = await instance.restart_application()

            elif action == "update":
                if instance.app_state.name in ("updating", "restarting", "preparing_for_sleep", "awaiting_user_input"):
                    failed = True
                else:
                    res = await instance.update_application()

            if failed is True:
                return await context.send(
                    content=f"**{instance.friendly_name}** is already `{instance.app_state.name}`. {self.emoji_table.kuma_hmm}",
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )
            if isinstance(res, ActionResultError):
                await context.send(
                    content=(
                        f"**{instance.friendly_name}** ran into an error. {self.emoji_table.kuma_crying}\n"
                        f"**Status**: {instance.app_state}\n**Error**:\t{res}"
                    ),
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )

            elif isinstance(res, ActionResult) or res is None:
                await context.send(
                    content=(
                        f"The {instance.module_display_name} application on **{instance.friendly_name}** "
                        f"was {self.string_inflection(action)}. {self.emoji_table.kuma_wow}"
                    ),
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )
            return None

        # The Instance itself is not running, so there is no application to act on. This used to fall
        # off the end and answer nothing at all, which reads as the command being broken.
        return await context.send(
            content=f"**{instance.friendly_name}** isn't running, so there's no application to {action}. {self.emoji_table.kuma_shrug}",
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(name="instance_info", help="Retrieve relevant information about an AMP Instance", aliases=["aii", "ampii"])
    @app_commands.autocomplete(server=autocomp_server_list)
    async def instance_information(self, context: Context, server: str) -> Any:
        await context.defer()
        if server == "NONE":
            return await context.send(
                content=f"{self.emoji_table.kuma_uwu}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
        try:
            instance: InstanceTypeAliases | ActionResultError = await self.ADS.get_instance(instance_id=server)
        except Exception as e:  # noqa: BLE001
            return await context.send(
                content=f"Failed to get instance by ID: {server} || {e}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        if isinstance(instance, ActionResultError):
            return await context.send(
                content=f"Failed to get instance by ID: {server} || {instance}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        if not (isinstance(instance, InstanceTypeAliases) and instance.running is True):
            return await context.send(
                content=f"**{instance.friendly_name}** isn't running, so there's nothing to report. {self.emoji_table.kuma_shrug}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        # Application info - state, player count, uptime.
        status: Status | ActionResultError = await instance.get_application_status()
        # Instance info - ID, friendly name, description, disk usage.
        info: InstanceTypeAliases | ActionResultError = await instance.get_instance_status()
        # Metrics - memory, CPU, and the analytics summary.
        analytics: AnalyticsSummary | ActionResultError = await instance.get_analytics_summary()

        if isinstance(status, ActionResultError):
            return await context.send(
                content=f"Failed to retrieve the application status for **{instance.friendly_name}**. {self.emoji_table.kuma_sad}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
        if isinstance(info, ActionResultError):
            return await context.send(
                content=f"Failed to retrieve the instance status for **{instance.friendly_name}**. {self.emoji_table.kuma_sad}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

        # Everything above this line was already being fetched; none of it was ever sent anywhere,
        # which is the whole of "appears to be broken and won't handle a message". Analytics is the
        # one part allowed to be missing - not every module reports it - so it is a soft failure.
        embed = KumaEmbed(
            cog=self,
            title=f"__{instance.friendly_name}__",
            description=info.description or "*No description set.*",
            color=discord.Color.green() if instance.running else discord.Color.red(),
        )
        embed.add_field(name="State:", value=f"`{status.state.name}`", inline=True)
        embed.add_field(name="Uptime:", value=f"`{status.uptime}`", inline=True)
        embed.add_field(name="Module:", value=f"`{instance.module_display_name}`", inline=True)

        if status.metrics is not None:
            for label, metric in (
                ("Players", status.metrics.active_users),
                ("CPU", status.metrics.cpu_usage),
                ("Memory", status.metrics.memory_usage),
            ):
                if metric is None:
                    continue
                embed.add_field(name=f"{label}:", value=f"`{metric.raw_value}/{metric.max_value}{metric.units}`", inline=True)

        embed.add_field(name="Disk Usage:", value=f"`{info.disk_usage_mb}MB`", inline=True)
        embed.add_field(name="Instance ID:", value=f"`{instance.instance_id}`", inline=False)

        if isinstance(analytics, ActionResultError):
            LOGGER.warning(
                "<%s.%s> | Failed to retrieve the analytics summary. | Instance: %s | Error: %s",
                __class__.__name__,
                "instance_information",
                instance.friendly_name,
                analytics,
            )
        elif analytics.top_players:
            top: str = "\n".join(f"- **{player.username}** - {player.display_session_time}" for player in analytics.top_players[:5])
            embed.add_field(name="Top Players:", value=top, inline=False)

        return await context.send(embed=embed, files=embed.attachments, ephemeral=True, delete_after=self.message_timeout)


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103 # docstring
    await bot.add_cog(Gatekeeper(bot=bot))
