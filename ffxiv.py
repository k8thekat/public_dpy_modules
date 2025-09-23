"""Copyright (C) 2021-2025 Katelynn Cadwallader.

This file is part of Kuma Kuma.

Kuma Kuma is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3, or (at your option)
any later version.

Kuma Kuma is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
License for more details.

You should have received a copy of the GNU General Public License
along with Kuma Kuma; see the file COPYING.  If not, write to the Free
Software Foundation, 51 Franklin Street - Fifth Floor, Boston, MA
02110-1301, USA.
"""

from __future__ import annotations

import datetime
import io
import logging
import pathlib
import platform
import sqlite3
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Optional, Self, TypedDict, Unpack, reveal_type

import discord
from async_garlandtools import GarlandToolsAsync, Language
from async_garlandtools.modules import Object
from async_universalis import CurrentData, DataCenter, World
from discord import Color, Colour, app_commands
from discord.ext import commands
from moogle_intuition import Moogle
from moogle_intuition.modules import Item, MoogleLookupError

from utils import FFXIVResources as Resources, KumaCog as Cog, KumaContext as Context, KumaEmbed as Embed

if TYPE_CHECKING:
    import asqlite
    from discord.ui.item import Item as uiItem
    from moogle_intuition._types import Vendor

    from kuma_kuma import Kuma_Kuma
    from utils import ButtonParams, EmbedParams

cookie_dir = pathlib.Path(__file__).parent.joinpath("cookies")
LOGGER = logging.getLogger()

FFXIVUSER_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS ffxivuser (
    id INTEGER PRIMARY KEY NOT NULL,
    discord_id INTEGER NOT NULL,
    guild_id INTEGER DEFAULT 0,
    world_or_dc INTEGER NOT NULL,
    language TEXT NOT NULL,
    UNIQUE (guild_id, discord_id)
    )"""

WATCH_LIST_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    price_min INT DEFAULT 0,
    price_max INTEGER DEFAULT 999999999,
    last_check INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES ffxivuser(id)
    UNIQUE (user_id, item_id)
    )"""


class UserDB(TypedDict):
    id: int
    discord_id: int
    guild_id: int
    world_or_dc: int
    language: str


class WatchListDB(TypedDict):
    user_id: int
    item_id: int
    price_min: int
    price_max: int
    last_check: int


class WatchList:
    user_id: int
    item_id: int
    price_min: int
    price_max: int
    last_check: datetime.datetime

    __slots__: tuple[str, ...] = ("item_id", "last_check", "price_max", "price_min", "universalis_id")

    def __init__(self, data: WatchListDB) -> None:
        LOGGER.debug("<%s.__init__() | Raw Data | Data: %s", __class__.__name__, data)
        for key, value in data.items():
            if key == "last_check" and isinstance(value, float):
                try:
                    self.last_check = datetime.datetime.fromtimestamp(timestamp=value, tz=datetime.UTC)
                except ValueError:
                    LOGGER.warning("<%s.__init__() | Failed to set last_check. | Value: %s", __class__.__name__, value)
                    self.last_check = datetime.datetime.now(datetime.UTC)
            else:
                setattr(self, key, value)

    def __hash__(self) -> int:
        return hash(self.item_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.item_id == other.item_id

    def __lt__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.item_id < other.item_id


class FFXIVUser:
    id: int
    discord_id: int
    guild_id: int
    language: Language
    "GarlandTools and Universalis will use this value, using GarlandToolsAsync Language Enum."
    watch_list: list[WatchList]
    world_or_dc: World | DataCenter
    "Since neither int values overlap we can determine the type of Enum by it's int value."
    pool: asqlite.Pool

    __slots__: tuple[str, ...] = ("discord_id", "guild_id", "id", "language", "pool", "watch_list", "world_or_dc")

    def __init__(self, db_pool: asqlite.Pool, **data: Unpack[UserDB]) -> None:
        LOGGER.debug("RAW DATA: %s", data)
        self.pool: asqlite.Pool = db_pool

        self.watch_list = []
        for key, value in data.items():
            if key == "language":
                try:
                    self.language = Language(value)
                except ValueError:
                    LOGGER.warning("<%s.__init__() | Failed to find %s in <Language>.", __class__.__name__, value)
                    self.language = Language.English

            elif key == "world_or_dc":
                try:
                    self.world_or_dc = World(value)
                    continue
                except ValueError:
                    LOGGER.warning("<%s.__init__() | Unable to find %s in <World>.", __class__.__name__, value)
                LOGGER.info("<%s.__init__() | Checking Datacenters.", __class__.__name__)
                try:
                    self.world_or_dc = DataCenter(value)
                    continue
                except ValueError:
                    LOGGER.warning("<%s.__init__() | Unable to find %s in <DataCenter>", __class__.__name__, value)

                LOGGER.warning("<%s.__init__() | Setting world_or_dc to default value. | Value: %s", __class__.__name__, DataCenter.Crystal)
                self.world_or_dc = DataCenter.Crystal

            else:
                setattr(self, key, value)

    def __repr__(self) -> str:
        return (
            f"{__class__.__name__}: {self.id}\n- Discord ID: {self.discord_id} | Guild ID: "
            f"{self.guild_id}\n- Lang: {self.language} | World/DC: {self.world_or_dc.name}"
            f"\n- Watch List: {len(self.watch_list)}"
        )

    def __str__(self) -> str:
        return self.__repr__()

    async def get_watch_list(self) -> list[WatchList]:
        """Get a Discord Users FFXIV watched items.

        Returns
        -------
        :class:`list[WatchList]`
            A list of the users watched items.

        """
        async with self.pool.acquire() as conn:
            res: list[WatchListDB] = await conn.fetchall(
                """SELECT * FROM watchlist WHERE discord_id = ? and guild_id = ? RETURNING *""",
            )  # type: ignore
            for item in res:
                if item not in self.watch_list:
                    self.watch_list.append(WatchList(data=item))
        return sorted(self.watch_list)

    # TODO(@k8thekat): Fully implement.
    async def update_watch_list_item(self, item: WatchList) -> WatchList:
        """update_watch_list_item _summary_.

        Parameters
        ----------
        item: :class:`WatchList`
            _description_.

        Returns
        -------
        :class:`WatchList`
            _description_.

        """
        async with self.pool.acquire() as conn:
            res: WatchListDB = await conn.fetchone(  # type: ignore
                """UPDATE watchlist SET price_min = ? AND price_max = ? AND last_check = ? WHERE universalid_id = ? AND item_id = ? RETURNING *""",
                item.price_min,
                item.price_max,
                item.last_check.timestamp(),
                item.universalis_id,
                item.item_id,
            )

        self.watch_list.remove(item)
        updated_item = WatchList(data=res)
        self.watch_list.append(updated_item)
        return updated_item

    # TODO(@k8thekat): Fully implement.
    async def check_watch_list_items(self, cog: FFXIV) -> None:
        pass

    @classmethod
    async def add_or_get_user(
        cls,
        pool: asqlite.Pool,
        user: discord.User | discord.Member,
        world_or_dc: DataCenter | World = DataCenter.Crystal,
        guild: discord.Guild | None = None,
        language: Language = Language.English,
    ) -> FFXIVUser:
        """Add or Get an FFXIV User from the database.

        Parameters
        ----------
        pool: :class:`asqlite.Pool`
            _description_.
        user: :class:`discord.User | discord.Member`
            _description_.
        world_or_dc: :class:`DataCenter | World`, optional
            _description_, by default DataCenter.Crystal.
        guild: :class:`discord.Guild | None`, optional
            _description_, by default None.
        language: :class:`Language`, optional
            _description_, by default Language.English.

        Returns
        -------
        :class:`FFXIVUser | None`
            _description_.

        Raises
        ------
        sqlite3.DataError
            _description_.

        """
        LOGGER.debug(
            "<%s.add_or_get_user()| User: %s | Guild: %s | World or DC: %s | Language: %s",
            __class__.__name__,
            user,
            guild,
            world_or_dc,
            language,
        )
        async with pool.acquire() as conn:
            # Try to find our user first..
            res: UserDB | None = await conn.fetchone(
                """SELECT * FROM ffxivuser WHERE discord_id = ? and guild_id = ?""",
                user.id,
                (guild.id if guild is not None else 0),
            )  # type: ignore - I know the dataset because of above.
            if res is not None:
                return FFXIVUser(**res, db_pool=pool)

            # No user...
            LOGGER.debug(
                "<%s.add_or_get_user()> | Adding Name: %s | ID: %s | Guild ID: %s | World/DC: %s | Localization: %s to the Database.",
                __class__.__name__,
                user.name,
                user.id,
                (guild.id if guild is not None else guild),
                world_or_dc.value,
                language,
            )
            res: UserDB | None = await conn.fetchone(
                """INSERT INTO ffxivuser(discord_id, guild_id, world_or_dc, language) VALUES(?, ?, ?, ?) RETURNING *""",
                user.id,
                (guild.id if guild is not None else 0),
                world_or_dc.value,
                language.value,
            )  # type: ignore - I know the dataset because of above.

            if res is None:
                LOGGER.error(
                    "<%s.add_or_get_user()> | We encountered an error inserting a Row. | GuildID: %s | UserID: %s | World/DC: %s",
                    __class__.__name__,
                    (guild.id if guild is not None else 0),
                    user.id,
                    world_or_dc.value,
                )
                msg = "We encountered an error inserting a Row into the database."
                raise sqlite3.DataError(msg)

            return FFXIVUser(**res, db_pool=pool)

    async def update(self) -> None:
        """Updates the Database with current object parameters."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE ffxivuser SET world_or_dc = ? AND language = ? WHERE discord_id = ? AND guild_id = ? RETURNING *""",
                self.world_or_dc,
                self.language.value,
                self.id,
                self.guild_id,
            )


class MoogleEmbed(Embed):
    resources: Resources
    cog: Cog

    def __init__(self, cog: Cog, info: Optional[discord.AppInfo] = None, **kwargs: Unpack[EmbedParams]) -> None:
        self.resources = Resources()
        self.cog = cog

        timestamp: datetime.datetime | None = kwargs.get("timestamp")
        if timestamp is None:
            kwargs["timestamp"] = datetime.datetime.now(tz=datetime.UTC)

        super().__init__(**kwargs)
        if info is not None:
            self.set_footer(text=f"Moogles Intuition made by {info.owner.name}", icon_url="attachment://moogle-icon.png")


class UserEmbed(MoogleEmbed):
    """FFXIV User information."""

    def __init__(self, cog: Cog, info: discord.AppInfo, user: FFXIVUser, **kwargs: Unpack[EmbedParams]) -> None:
        if kwargs.get("description") is None:
            kwargs["description"] = "Information about the user..."
        if kwargs.get("title") is None:
            kwargs["title"] = "FFXIV User"

        super().__init__(cog=cog, info=info, **kwargs)
        self.set_author(name="Moogles Intuition: FFXIV User information")

        # Fields
        self.add_field(name="Language:", value=user.language.name)
        self.add_field(name="World or DataCenter:", value=user.world_or_dc.name)


class ItemEmbed(MoogleEmbed):
    """FFXIV Item information.

    .. note::
        - Requires a "item-icon.png` that is the icon for the item parameter.
        - Requires a "patch-icon.png" attachment for the embed.
    """

    item: Item

    def __init__(self, cog: Cog, item: Item, **kwargs: Unpack[EmbedParams]) -> None:  # noqa: C901
        self.item = item
        if kwargs.get("description") is None:
            if item.description is not None:
                if len(item.description) > 1020:
                    kwargs["description"] = f"*{item.description[:1020] + ' ...'}*"
                else:
                    kwargs["description"] = item.description
            else:
                kwargs["description"] = "..."

        if kwargs.get("title") is None:
            kwargs["title"] = f"**{item.name}** [{item.id}]"

        super().__init__(cog=cog, **kwargs)
        # Using patch
        self.set_author(name="Moogles Intuition: FFXIV Item lookup", icon_url="attachment://patch-icon.png")
        self.set_thumbnail(url="attachment://item-icon.png")

        self.add_field(name="Tradeable", value=not self.item.is_untradable, inline=False)
        self.add_field(name="Craftable", value="True" if self.item.recipe is not None else "False")
        self.add_field(name="Gatherable:", value="True" if self.item.gathering is not None else "False")
        self.add_field(name="Fishable:", value="True" if self.item.fishing is not None else "False")

        if self.item.garlandtools_data is not None:
            vendors: list[Vendor] | None = self.item.get_vendors()
            if vendors is not None:
                data: list[str] = []
                last_currency: Optional[Item] = None
                limit = 3
                for idx, cur_vendor in enumerate(iterable=vendors, start=1):
                    if idx > limit:
                        break

                    # Getting the Currency object and populating Shop information.
                    currency: Item | None = cur_vendor.get("currency", None)
                    if last_currency is None:
                        last_currency = currency
                    elif isinstance(last_currency, Item) and isinstance(currency, Item) and last_currency.id == currency.id:
                        limit += 1
                        continue

                    value: str = f"[{cur_vendor.get('name')} | {cur_vendor.get('shop_name')}]({cur_vendor.get('url', 'N/A')})"
                    if currency is not None:
                        value += f"\n> **Currency**: {currency.name} | Cost: {cur_vendor.get('price', 0):,d}"
                    else:
                        value += f"\n> **Currency**: {self.resources.gil_emoji} | Cost: {cur_vendor.get('price', 0):,d}"

                    data.append(value)
                self.add_field(name="Vendors:", value="\n".join(data))

            tradeshops: list[Vendor] | None = self.item.get_tradeshops()
            if tradeshops is not None:
                data: list[str] = []
                limit = 3
                last_currency: Optional[Item] = None
                for idx, cur_shop in enumerate(iterable=tradeshops, start=1):
                    if idx > limit:
                        break

                    # This should allow some variance to which TradeShops we get.
                    currency: Item | None = cur_shop.get("currency", None)
                    if last_currency is None:
                        last_currency = currency
                    elif isinstance(last_currency, Item) and isinstance(currency, Item) and last_currency.id == currency.id:
                        limit += 1
                        continue

                    value = f"[{cur_shop.get('name')} | {cur_shop.get('shop_name')}]({cur_shop.get('url', 'N/A')})"
                    if currency is not None:
                        value += f"\n> **Currency**: {currency.name} | Cost: {cur_shop.get('price', 0):,d}"
                    else:
                        value += f"\n> **Currency**: {self.resources.gil_emoji} | Cost: {cur_shop.get('price', 0):,d}"
                    data.append(value)
                self.add_field(name="Tradeshop:", value="\n".join(data))

        # Useful links
        self.add_field(
            name="Links:",
            value=(
                f"[GarlandTools]({item.garland_tools_url}) | "
                f"[FFXIV Wiki]({item.ffxivconsolegames_wiki_url}) | "
                f"[Universalis]({item.universalis_url})"
            ),
            inline=False,
        )


class UniversalisEmbed(MoogleEmbed):
    """Universalis Embed.

    .. note::
        - Requires a "universalis-icon.png` that is the icon for the footer url.
        - Requires a "patch-icon.png" attachment for the embed.
    """

    item: Item

    # TODO(@k8thekat): Possibly implement a `limit` parameter to control the number of listings shown?
    def __init__(
        self,
        cog: Cog,
        item: Item,
        world_or_dc: World | DataCenter,
        marketboard_type: str = "Current",
        **kwargs: Unpack[EmbedParams],
    ) -> None:
        self.item: Item = item
        if kwargs.get("title") is None:
            kwargs["title"] = f"**{item.name}** [{item.id}]"
        if kwargs.get("description") is None:
            kwargs["description"] = f"{marketboard_type} Universalis results on **{world_or_dc.name}**"
        if kwargs.get("color") is None:
            kwargs["color"] = discord.Color.og_blurple()

        super().__init__(cog=cog, **kwargs)

        self.set_author(name="Moogles Intuition: Universalis Marketboard", icon_url="attachment://universalis-icon.png")
        self.set_thumbnail(url="attachment://item-icon.png")
        if item.mb_current is None:
            self.add_field(name="Error:", value="No Marketboard data found.")
            return

        mb_data: CurrentData = item.mb_current
        if isinstance(mb_data.last_upload_time, datetime.datetime):
            ftime: str | int = self.cog.to_discord_timestamp(mb_data.last_upload_time)
        else:
            ftime = mb_data.last_upload_time

        general_fields: list[str] = [
            f"# of listings: {mb_data.listings_count}",
            f"Last Updated: **{ftime}**",
            f"Sale Velocity(HQ/NQ): {mb_data.hq_sale_velocity}/{mb_data.nq_sale_velocity}",
        ]

        self.add_field(name="Listing Stats:", value="\n- ".join(general_fields), inline=False)
        self.add_blank_field(inline=False)

        limit = min(6, len(mb_data.listings))
        for ind, entry in enumerate(mb_data.listings, start=1):
            if ind > limit:
                break
            world: str | None = entry.dc_name if entry.world_name is None else entry.world_name
            listing_data: list[str] = [
                f"World/DC: {world}",
                f"Quantity: {entry.quantity}",
                f"PPu: {entry.price_per_unit:,d}",
                f"Total(+Tax): {(entry.total + entry.tax):,d}",
            ]
            self.add_field(name="Listing:", value="\n> ".join(listing_data))


class DataCenterSelectModal(discord.ui.Modal):
    """Discord Component Modal with a Select prompt for Universalis FFXIV DataCenters."""

    item: Item
    cog: Cog

    def __init__(self, cog: Cog, item: Item, title: str, timeout: float | None = None, **kwargs: Any) -> None:
        self.item = item
        self.cog = cog
        super().__init__(title=title, timeout=timeout, **kwargs)
        choices: list[discord.SelectOption] = [discord.SelectOption(label=entry.name, value=str(entry.value)) for entry in DataCenter]
        self.add_item(
            discord.ui.Label(
                text="Select a Datacenter..",
                component=discord.ui.Select(options=choices, placeholder="Select a DataCenter...", id=999),
                description="Will be used to fetch Universalis results.",
            ),
        )

    async def on_submit(self, interaction: discord.Interaction) -> discord.InteractionCallbackResponse:
        LOGGER.debug(__class__.__name__, "on_submit")
        # Since our Select is wrapped in a discord.ui.Label we have to use `find_item`
        # and then check the `.values` attribute to find our results.
        res: uiItem | None = self.find_item(999)
        if res is not None and isinstance(res, discord.ui.Select):
            # TODO(@k8thekat): See about getting the old attachment and moving it to this embed to prevent another web request?
            # Icon handling...
            universalis_icon: discord.File = Resources().get_universalis_icon()
            data: Object | None = await self.item.get_icon()
            item_icon: discord.File = (
                discord.File(io.BytesIO(data.data), filename="item-icon.png")
                if isinstance(data, Object)
                else Resources().get_moogle_icon(filename="item-icon.png")
            )

            # Build our DataCenter object; and then fetch marketboard Data using it.
            # This allows us to only fetch Universalis data once someone clicks the button.
            world_or_dc = DataCenter(value=int(res.values[0]))
            await self.item.get_current_marketboard(world_or_dc=world_or_dc)

            return await interaction.response.send_message(
                embed=UniversalisEmbed(cog=self.cog, item=self.item, world_or_dc=world_or_dc),
                files=[universalis_icon, item_icon],
                ephemeral=True,
            )
        return await interaction.response.send_message("Failed Modal...", ephemeral=True)


class MarketboardButton(discord.ui.Button):
    view: ItemView
    result: DataCenterSelectModal
    item: Item

    def __init__(self, item: Item, **kwargs: Unpack[ButtonParams]) -> None:
        kwargs["style"] = discord.ButtonStyle.green
        kwargs["label"] = "Marketboard"
        self.item = item
        super().__init__(**kwargs)

    async def callback(self, interaction: discord.Interaction) -> Any:
        LOGGER.debug(__class__.__name__, "callback")
        if interaction.user == self.view.user:
            self.disabled = True
            self.results = DataCenterSelectModal(cog=self.view.cog, item=self.item, title="Universalis Marketboard")

            await interaction.response.send_modal(self.results)
        return None


class ItemView(discord.ui.View):
    item: Item
    user: discord.Member | discord.User
    cog: Cog

    def __init__(self, cog: Cog, item: Item, user: discord.Member | discord.User, timeout: float | None = 180) -> None:
        self.item = item
        self.user = user
        self.cog = cog
        super().__init__(timeout=timeout)
        self.add_item(MarketboardButton(item=item))

    async def interaction_check(self, interaction: discord.Interaction) -> Any:
        return await super().interaction_check(interaction)


class FFXIVContext(Context):
    ffxiv_user: FFXIVUser


class FFXIV(Cog):
    """Final Fantasy 14 Cog."""

    moogle: Moogle
    item_choices: Optional[list[app_commands.Choice]] = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    async def cog_load(self) -> None:
        async with self.bot.pool.acquire() as conn:
            await conn.execute(FFXIVUSER_SETUP_SQL)
            await conn.execute(WATCH_LIST_SETUP_SQL)

        self.moogle = await Moogle(garlandtools=GarlandToolsAsync()).build()

        if self.item_choices is None:
            self.item_choices = self.build_item_choices()

    async def cog_unload(self) -> None:
        await self.moogle.clean_up()

    async def get_ffxiv_user(self, ctx: FFXIVContext) -> None:
        try:
            res: FFXIVUser = await FFXIVUser.add_or_get_user(pool=ctx.bot.pool, user=ctx.author)
        except sqlite3.DataError:
            self.logger.exception(
                "<%s.%s> | SQLite DataError. | Context Type: %s | Context Data: %s",
                __class__.__name__,
                "get_ffxiv_user",
                type(ctx),
                (ctx.bot.pool, "==?", self.bot.pool, ctx.guild, ctx.author),
            )
            res = FFXIVUser(
                db_pool=ctx.bot.pool,
                id=0,
                discord_id=0,
                guild_id=0,
                world_or_dc=DataCenter.Crystal,
                language=Language.English.value,
            )
        ctx.ffxiv_user = res

    def build_item_choices(self) -> list[app_commands.Choice[str]]:
        temp = []
        for name, value in self.moogle._items_ref.items():  # noqa: SLF001
            if isinstance(name, str):
                # May want to implement localization for other languages.

                temp.append(app_commands.Choice(name=name.title(), value=value))
        return temp

    async def autocomp_item_list(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:  # noqa: ARG002 # Not using interaction parameter.
        if self.item_choices is None:
            return [entry for entry in self.build_item_choices() if current.lower() in (entry.name.lower() or str(entry.value))][:25]
        return [entry for entry in self.item_choices if current.lower() in (entry.name.lower() or str(entry.value))][:25]

    @commands.before_invoke(get_ffxiv_user)
    @commands.command(help="", aliases=["xivtest"])
    async def test_func(self, context: FFXIVContext, item_id: str = "") -> Any:
        await context.typing()
        await context.send(f"__Test Func__: \n- {context.ffxiv_user}\n- ItemID: {item_id}")

    # TODO: - Verify DB data handling, User creation and moogle session handling.
    @commands.before_invoke(get_ffxiv_user)
    @commands.is_owner()
    @commands.guild_only()
    @commands.command()
    async def ffxiv_user_dev(self, context: FFXIVContext) -> None:
        await context.typing()

        assert context.guild  # noqa: S101 # We are using the `commands.guild_only()` decorator so we know the guild exists.
        context.ffxiv_user.guild_id = context.guild.id
        context.ffxiv_user.language = Language.English
        await context.ffxiv_user.update()
        information: discord.AppInfo = await self.bot.application_info()
        await context.send(embed=UserEmbed(cog=self, info=information, user=context.ffxiv_user))

    @app_commands.command(name="xiv_item", description="Get an FFXIV Item.")
    @app_commands.autocomplete(query=autocomp_item_list)
    async def items(self, interaction: discord.Interaction, query: str) -> Any:
        try:
            item: Item = self.moogle.get_item(item=query, limit_results=1)
        except MoogleLookupError:
            return await interaction.response.send_message(content=f"Failed to lookup Item: {query}")

        # Get the garland tools data and building the Icon.
        await item.get_garlandtools_data()
        data: Object | None = await item.get_icon()
        item_icon: discord.File = (
            discord.File(io.BytesIO(data.data), filename="item-icon.png")
            if isinstance(data, Object)
            else Resources().get_moogle_icon(filename="item-icon.png")
        )

        if isinstance(item.garlandtools_data, dict):
            patch_icon: discord.File = Resources().get_patch_icon(patch_id=item.garlandtools_data.get("item").get("patch"))
        else:
            patch_icon = Resources().get_patch_icon(patch_id=1)
        item_embed = ItemEmbed(cog=self, item=item)

        return await interaction.response.send_message(
            embed=item_embed,
            view=ItemView(cog=self, item=item, user=interaction.user),
            files=[item_icon, patch_icon],
            delete_after=self.message_timeout,
        )


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103
    await bot.add_cog(FFXIV(bot=bot))
