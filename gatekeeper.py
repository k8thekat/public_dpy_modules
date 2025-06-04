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

from __future__ import annotations

import asyncio
from configparser import ConfigParser
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import discord
from ampapi import (
    ActionResult,
    ActionResultError,
    AMPADSInstance,
    AMPControllerInstance,
    AMPInstance,
    AMPMinecraftInstance,
    APIParams,
    Bridge,
    Instance,
    InstanceTypeAliases,
    Role,
    Status,
)
from discord import app_commands, embeds
from discord.ext import commands, tasks
from lemminflect import getInflection, getLemma

from kuma_kuma import Kuma_Kuma
from utils.cog import KumaCog as Cog  # need to replace with your own Cog class

if TYPE_CHECKING:
    from pathlib import Path

    from discord.guild import GuildChannel

    from kuma_kuma import Kuma_Kuma
    from utils.context import KumaContext as Context

BOT_NAME = "Kuma Kuma"

# Discord USER ID = [INSTANCE IDs]
SERVER_OWNERS: dict[str, list[str]] = {
    "447422100798570496": ["3534c4c1-e212-458f-a013-31aa86935c49"],  # Vaskels Server
    "172808207536881664": ["61d811aa-b897-44ba-bb75-b488278a13c4"],  # Fret's server
    "651454696208465941": ["99a98c16-9474-4888-9eb7-45abce5d1bdd"],  # Ducki's Server (Bo_bi)
    "606648465065246750": ["04a36223-f761-4e3f-9f50-9ce2d831ea57"],  # Flying's Server
    "409050137780944911": ["6f567df4-d1b4-4758-bfe6-529fb3bc2ff9"],  # Public ATM10 // "FullSpring"
}


# class MetricsEmbed(discord.Embed):
#     def __init__(self, title: str, description: str, data: Any) -> None:
#         color: discord.Color = discord.Color.blurple()

#         super().__init__(color=color, title=title, description="", timestamp=discord.utils.utcnow())


class Gatekeeper(Cog):
    ADS: AMPControllerInstance
    neko_neko = 602285328320954378

    NO_CONNECT = discord.Permissions(
        connect=False,
        view_channel=True,
        move_members=False,
        send_voice_messages=False,
        send_tts_messages=False,
        send_messages=False,
        read_messages=False,
    )

    servers_dict: dict[str, InstanceTypeAliases]

    def __init__(self, bot: Kuma_Kuma) -> None:
        super().__init__(bot=bot)

    async def cog_load(self) -> None:
        self.ini_load()
        self.ADS = AMPControllerInstance()
        self.servers: set[AMPInstance | AMPMinecraftInstance | AMPADSInstance] = await self.ADS.get_instances()
        # Easier lookup to get AMP Instance objects.
        self.servers_dict = {}
        for server in self.servers:
            self.servers_dict[server.friendly_name] = server

        self.server_choices: list[app_commands.Choice[str]] = [app_commands.Choice(name=e.friendly_name, value=e.instance_id) for e in self.servers]
        if self.update_server_list.is_running() == False:
            self.logger.info(
                "Starting Gatekeeper.update_server_list task, running every %s minutes. | Reconnect: %s",
                self.update_server_list.minutes,
                self.update_server_list.reconnect,
            )
            self.update_server_list.start()
        if self.server_status_via_channels.is_running() == False:
            self.logger.info(
                "Starting Gatekeeper.server_status_via_channels task, running every %s minutes. | Reconnect: %s",
                self.server_status_via_channels.minutes,
                self.server_status_via_channels.reconnect,
            )
            self.server_status_via_channels.start()

    def ini_load(self) -> None:
        """
        Parse my local ini file to load AMP login information.
        """
        file: Path = self.bot.local_ini
        if file.is_file():
            settings = ConfigParser(converters={"list": lambda setting: [value.strip() for value in setting.split(",")]})
            settings.read(filenames=file)
            # login creds
            url = settings.get(section="AMP", option="url", fallback=None)
            user = settings.get(section="AMP", option="user", fallback=None)
            password = settings.get(section="AMP", option="password", fallback=None)
            token = settings.get(section="AMP", option="token", fallback=None)
            self.logger.debug(("Gatekeeper Creds. | Url: %s | User: %s | Token: %s | Path: %s"), url, user, token, file.as_posix())
            if url is None or user is None or password is None or token is None:
                raise ValueError(
                    "Gatekeeper failed to load credentials. | Url: %s | User: %s | Token: %s | Password: %s",
                    url,
                    user,
                    token,
                    password if password is None else "",
                )
            Bridge(api_params=APIParams(url=url, user=user, password=password, token=token, use_2fa=True))
        else:
            raise ValueError("Gatekeeper failed to load credentials from path: %s", file)
        return

    async def autocomp_server_list(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        global SERVER_OWNERS

        if await self.bot.is_owner(interaction.user):
            return [entry for entry in self.server_choices if current.lower() in (entry.name.lower() or entry.value.lower())]
        else:
            temp: list[str] | None = SERVER_OWNERS.get(str(object=interaction.user.id))
            if temp is None:
                return [app_commands.Choice(name="No Access - GTFO", value="NONE")]
            # This should limit the server options to those that the user should only have access to via SERVER_OWNERS dict.
            priv_servers: list[app_commands.Choice[str]] = [entry for entry in self.server_choices if entry.value in temp]
            return [entry for entry in priv_servers if current.lower() in (entry.name.lower() or entry.value.lower())]

    async def autocomp_role_list(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str | int]]:
        roles: list[Role] | ActionResultError = await self.ADS.get_role_data()
        if isinstance(roles, ActionResultError):
            return [app_commands.Choice(name="Failed Lookup", value="NONE")]
        return [
            app_commands.Choice(name=role.name, value=role.id) for role in roles if current.lower() in role.name.lower() or current.lower() in role.id
        ]

    @tasks.loop(minutes=10, reconnect=True)
    async def update_server_list(self) -> None:
        res: set[AMPInstance | AMPMinecraftInstance | AMPADSInstance] = await self.ADS.get_instances()
        if isinstance(res, ActionResultError):
            return self.logger.error("Failed to retrieve AMP Instances via `get_instances()`. | Error: %s", ActionResultError)
        if res != self.servers:
            self.servers = res
            for server in self.servers:
                self.servers_dict[server.friendly_name] = server
            self.logger.info("Updated our list of available AMP Instances. | Num of Instances: %s", len(self.servers))

    @tasks.loop(minutes=3, reconnect=True)
    async def server_status_via_channels(self) -> None:
        neko_guild: discord.Guild | None = await self.get_guild()
        if neko_guild is None:
            self.logger.error("Failed to retrieve the Discord Guild")
            return

        mc_role: discord.Role | None = neko_guild.get_role(1364268722360942743)
        if mc_role is None:
            self.logger.error("Failed to retrieve the Discord Role: %s from Discord Guild: %s", 1364268722360942743, neko_guild)
            return
        # This is the Hosted Servers category ID.``
        server_category: GuildChannel | None = neko_guild.get_channel(1364274969336610867)
        if isinstance(server_category, discord.CategoryChannel):
            found = False
            for channel in server_category.channels:
                if channel.name.startswith("ATM10:"):
                    found = True
                    # Attempt to get our AMP Instance object from the dictionary.
                    instance: InstanceTypeAliases | None = self.servers_dict.get("Public ATM10", None)
                    if instance is None:
                        self.logger.error("Failed to get Instance by Friendly Name: %s", "Public ATM10")
                        return

                    # Now lets get updated Instance information so we can update the channel better.
                    status: Status | ActionResultError = await instance.get_application_status()
                    if isinstance(status, ActionResultError):
                        self.logger.error("Failed to get_application_status on Instance: %s", instance.friendly_name)
                        return

                    if instance.metrics is not None and instance.metrics.active_users is not None:
                        await channel.edit(
                            name=f"ATM10: {'Online' if instance.running else 'Offline'} | Users: {instance.metrics.active_users.raw_value}"
                        )
                    else:
                        await channel.edit(name=f"ATM10: {'Online' if instance.running else 'Offline'} | Users: 0")

            # If we cannot find our Status Voice Channel, create it and set the proper permissions. (Hopefully)
            if found is False:
                status_chan: discord.VoiceChannel = await server_category.create_voice_channel(name="ATM10:")
                try:
                    await status_chan.set_permissions(
                        mc_role,
                        overwrite=discord.PermissionOverwrite().update(**self.NO_CONNECT.__dict__),
                        reason="AMP Server status channel.",
                    )
                except Exception as e:
                    self.logger.error("Unable to set_permissions for Channel: %s | Error: %s", status_chan, e)

        else:
            self.logger.error("Failed to retrieve the Guild Channel: %s", 1364274969336610867)

    @commands.hybrid_command(name="whitelist", help="Whitelist a user to the server.", aliases=["wl"])
    @app_commands.autocomplete(server=autocomp_server_list)
    async def whitelist_server(
        self,
        context: Context,
        server: str,
        name: str,
        action: str = "add",
    ) -> discord.Message | None:
        if server == "NONE":
            return await context.send(content=f"{self.emoji_table.to_inline_emoji('kuma_uwu')}", ephemeral=True, delete_after=self.message_timeout)

        try:
            instance: Instance | ActionResultError = await self.ADS.get_instance(instance_id=server)
            if isinstance(instance, ActionResultError):
                self.logger.error(
                    "Failed Instance lookup in Whitelist Server. | Instance ID: %s | IGN: %s | Action: %s | Discord User: %s",
                    server,
                    name,
                    action,
                    context.author,
                )
                raise
        except Exception as e:
            return await context.send(content=f"Failed to get instance by ID: {server} || {e}")

        if isinstance(instance, AMPMinecraftInstance) and instance.running is True:
            await instance.send_console_message(msg=f"/whitelist {action} {name}")
            # We delay one second in an attempt to gaurentee the console update we get is the whitelist message.
            # We may get the wrong message though.
            await asyncio.sleep(delay=1)
            await instance.get_updates()
            return await context.send(content=instance.console_entries[-1].contents, ephemeral=True, delete_after=self.message_timeout)
        else:
            return await context.send(
                content=f"It appears the Instance is having trouble...{self.emoji_table.to_inline_emoji('kuma_bleh')}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

    @commands.hybrid_command(name="console_message", help="Send a message to the console.", aliases=["console", "cmsg"])
    @app_commands.autocomplete(server=autocomp_server_list)
    async def console_message(self, context: Context, server: str, message: str) -> discord.Message | None:
        if server == "NONE":
            return await context.send(content=f"{self.emoji_table.to_inline_emoji('kuma_uwu')}", ephemeral=True, delete_after=self.message_timeout)
        try:
            instance: InstanceTypeAliases | ActionResultError = await self.ADS.get_instance(instance_id=server)
            if isinstance(instance, ActionResultError):
                raise
        except Exception as e:
            return await context.send(content=f"Failed to get instance by ID: {server} || {e}", ephemeral=True, delete_after=self.message_timeout)

        if isinstance(instance, InstanceTypeAliases) and instance.running is True:
            # By calling get_updates() early with how the API handles console entries gaurentee's the next entry in the console will most likely be our console message and the response to it.
            await instance.get_updates()
            await instance.send_console_message(msg=message)
            await asyncio.sleep(delay=1)
            await instance.get_updates()
            console_content = "\n".join([entry.contents for entry in instance.console_entries])

            # Basic truncating of the content as 2k is the char limit.
            if len(console_content) > 2000:
                console_content = console_content[:1980] + "\n...."

            return await context.send(
                content=f"Issues Command: {message}\n" + console_content,
                ephemeral=True,
                delete_after=self.message_timeout,
            )
            # return await context.send(content=instance.console_entries[-1].contents, ephemeral=True, delete_after=self.message_timeout)
        else:
            await context.send(
                content=f"It appears the Instance is having trouble...{self.emoji_table.to_inline_emoji('kuma_bleh')}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )

    @commands.hybrid_command(name="duplicate_role", help="Duplicate an AMP User Role", aliases=["role_dupe", "amprd", "amp_rd"])
    @commands.is_owner()
    @app_commands.autocomplete(source=autocomp_role_list)
    async def duplicate_amp_role(self, context: Context, source: str, new_role_name: str) -> discord.Message | None:
        roles: list[Role] | ActionResultError = await self.ADS.get_role_data()
        res: ActionResult | ActionResultError = await self.ADS.create_role(role_name=new_role_name, as_common_role=False)
        source_role: Role | ActionResultError = await self.ADS.get_role(role_id=source)

        new_role: Role | ActionResultError | None = None
        if res.result is not None and isinstance(res, ActionResult):
            new_role = await self.ADS.get_role(role_id=res.result)

        if source_role is None or new_role is None or isinstance(source_role, ActionResultError) or isinstance(new_role, ActionResultError):
            self.logger.error("Failed to locate Role ID: %s . | Roles: %s", source, roles)
            return await context.send(content=f"Failed to locate the Role ID: {source}", ephemeral=True, delete_after=self.message_timeout)

        temp: list[str] = []
        for perm in source_role.permissions:
            if perm.startswith("-"):
                await self.ADS.set_amp_role_permission(role_id=new_role.id, permission_node=perm.replace("-", ""), enabled=False)
                temp.append(f"Set {perm.replace('-', '')}: **False**")
            else:
                print(await self.ADS.set_amp_role_permission(role_id=new_role.id, permission_node=perm, enabled=True))
                temp.append(f"Set {perm}: **True**")

        return await context.send(
            content=f"Duplicated {source_role.name} to {new_role.name}, with permissions:" + "\n".join(temp),
            ephemeral=True,
            delete_after=self.message_timeout,
        )

    @commands.hybrid_command(
        name="instance_app_control", help="Perform certain actions on the Server.", aliases=["app_control", "apc", "iac", "iapp_control"]
    )
    @app_commands.autocomplete(server=autocomp_server_list)
    @app_commands.describe(action="The Instance application action to perform.")
    async def instance_app_control(
        self, context: Context, server: str, action: Literal["start", "stop", "restart", "update"]
    ) -> discord.Message | None:
        await context.defer()
        if server == "NONE":
            return await context.send(content=f"{self.emoji_table.to_inline_emoji('kuma_uwu')}", ephemeral=True, delete_after=self.message_timeout)
        try:
            instance: InstanceTypeAliases | ActionResultError = await self.ADS.get_instance(instance_id=server)
            # We raise just to trigger the Exception clause and spit out a reply.
            if isinstance(instance, ActionResultError):
                raise
        except Exception as e:
            return await context.send(content=f"Failed to get instance by ID: {server} || {e}", ephemeral=True, delete_after=self.message_timeout)
        failed = False
        res = None
        if isinstance(instance, InstanceTypeAliases) and instance.running is True:
            # ? Suggestions
            # Need to improve logic on checking app_state names to prevent errors.
            # We can still get ActionResultErrors for other reasons outside of the app_state.
            if action == "start":
                if instance.app_state.name in ("starting", "installing", "failed", "stopping", "preparing_for_sleep"):
                    failed = True
                else:
                    res: ActionResult | ActionResultError | None = await instance.start_application()

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
                    content=f"The application on **{instance.friendly_name}** Instance was already `{instance.app_state.name}`.",
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )
            # WE should only see this on errors such as failed connections or the Application is already running/etc.
            if isinstance(res, ActionResultError):
                await context.send(
                    content=f"It appears the application on **{instance.friendly_name}** Instance ran into an error.\n**Status**: {instance.app_state}\n**Error**:\t{res}",
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )

            elif isinstance(res, ActionResult) or res is None:
                await context.send(
                    content=f"The {instance.module_display_name} applcation on **{instance.friendly_name}** Instance was {self.string_inflection(action)}...",
                    ephemeral=True,
                    delete_after=self.message_timeout,
                )


async def setup(bot: Kuma_Kuma) -> None:
    await bot.add_cog(Gatekeeper(bot=bot))
