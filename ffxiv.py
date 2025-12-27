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

import asyncio
import copy
import datetime
import io
import logging
import pathlib
import platform
import sqlite3
import time
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    NotRequired,
    Optional,
    Self,
    TypedDict,
    TypeVar,
    Union,
    Unpack,
    overload,
    reveal_type,
)

import discord
from async_garlandtools import GarlandToolsAsync, Language
from async_garlandtools.modules import Object
from async_universalis import (
    DEFAULT_DATACENTER,
    CurrentData,
    CurrentDataEntries,
    DataCenter,
    DataCenterToWorlds,
    HistoryDataEntries,
    ItemQuality,
    World,
)
from discord import Color, Colour, app_commands
from discord.errors import Forbidden, HTTPException, NotFound
from discord.ext import commands
from moogle_intuition import CraftType, Moogle
from moogle_intuition._types import CurrencySpender
from moogle_intuition.ext.converters import Converter
from moogle_intuition.ff14angler import AnglerBaits, AnglerFish
from moogle_intuition.modules import (
    Currency,
    Expansion,
    FishingSpot,
    GatheringNode,
    Item,
    ItemUICategory,
    MoogleLookupError,
    PlaceName,
    Recipe,
)

from utils import FFXIVResources, KumaCog as Cog, KumaContext as Context, KumaEmbed as Embed
from utils._types import Metrics

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asqlite
    from discord.ui.item import Item as uiItem
    from moogle_intuition._types import CurMarketBoardParams, CurrencySpender, GatheringNodeData, ShoppingCurrency, ShoppingItem, Vendor
    from moogle_intuition.ff14angler import AnglerBaits, AnglerFish

    from kuma_kuma import Kuma_Kuma
    from utils import ButtonParams, EmbedParams, SelectParams

    EmbedTypeAlias = Union["MoogleEmbed", "ItemEmbed", "RecipeEmbed", "UniversalisEmbed", "FishingEmbed", "CurrencyEmbed"]
# V = TypeVar("V", bound="GenericButton", covariant=True)

cookie_dir = pathlib.Path(__file__).parent.joinpath("cookies")
LOGGER = logging.getLogger()
RESOURCES: FFXIVResources = FFXIVResources()


FFXIVUSER_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS ffxivuser (
    id INTEGER PRIMARY KEY NOT NULL,
    discord_id INTEGER NOT NULL,
    guild_id INTEGER DEFAULT 0,
    datacenter INTEGER NOT NULL,
    language TEXT NOT NULL,
    UNIQUE (guild_id, discord_id)
    )"""

INVENTORY_SETUP_SQL = """
"""

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
    datacenter: int
    language: str


class WatchListDB(TypedDict):
    user_id: int
    item_id: int
    price_min: int
    price_max: int
    last_check: int


class ViewParams(TypedDict):
    """:class:`BaseView` base parameters.

    Params
    ------
    cog: :class:`FFXIV`
        The Cog that dispatched the view.
    xivuser: :class:`XIVUser`
        The XIV User object associated to the `owner` of the :class:`View`.
    recent_interaction: :class:`discord.Message`
        The most recent :class:`discord.Interaction` that sent content..
    components: :class:`NotRequired[list[discord.ui.Item]]`
        Any Items to pre-append to the View and display.
    owner: :class:`discord.Member | discord.User`
        The Member or User who dispatched the view/interaction.
    embeds: :class:`NotRequired[Sequence[MoogleEmbed | ItemEmbed] | None]`
        The Embeds associated with the view, if applicable.
    dispatched_by: Optional[BaseView | discord.ui.Button[BaseView]]
        The Object that dispatched the View..
    timeout: :class:`NotRequired[float | None]`
        Default View timeout parameter.
    """

    cog: FFXIV
    "The Cog that dispatched the view."
    xivuser: XIVUser
    "The XIV User object associated to the `owner` of the :class:`View`."
    recent_interaction: NotRequired[Optional[discord.Interaction]]
    "The most recent :class:`discord.Interaction` that sent content.."
    components: NotRequired[list[discord.ui.Item]]
    "Any Items to pre-append to the View and display during `__init__`"
    owner: discord.Member | discord.User
    "The Member or User who dispatched the view/interaction."
    embeds: Sequence[MoogleEmbed | ItemEmbed] | None
    "The Embeds associated with the view, if applicable."
    dispatched_by: Optional[BaseView | discord.ui.Button[BaseView]]
    "Who dispatched the View..."
    timeout: NotRequired[float | None]
    "Default View timeout parameter."


class ViewParamsPartial(TypedDict):
    """Similar to :class:`ViewParams`, but only `cog`, `xivuser` and `owner` are required.

    Params
    ------
    cog: :class:`FFXIV`
        The Cog that dispatched the view.
    xivuser: :class:`XIVUser`
        The XIV User object associated to the `owner` of the :class:`View`.
    owner: :class:`discord.Member | discord.User`
        The Member or User who dispatched the view/interaction.
    recent_interaction: :class:`NotRequired[discord.Message]`
        The most recent :class:`discord.Interaction` that sent content..
    components: :class:`NotRequired[list[discord.ui.Item]]`
        Any Items to pre-append to the View and display.
    embeds: :class:`NotRequired[Sequence[MoogleEmbed | ItemEmbed] | None]`
        The Embeds associated with the view, if applicable.
    dispatched_by: :class:`NotRequired[Optional[BaseView | discord.ui.Button[BaseView]]]`
        The Object that dispatched the View..
    timeout: :class:`NotRequired[float | None]`
        Default View timeout parameter.

    """

    cog: FFXIV
    "The Cog that dispatched the view."
    xivuser: XIVUser
    "The XIV User object associated to the `owner` of the :class:`View`."
    owner: discord.Member | discord.User
    "The Member or User who dispatched the view/interaction."
    recent_interaction: NotRequired[Optional[discord.Interaction]]
    "The most recent :class:`discord.Interaction` that sent content.."
    components: NotRequired[list[discord.ui.Item]]
    "Any Items to pre-append to the View and display during `__init__`"
    embeds: NotRequired[Sequence[MoogleEmbed | ItemEmbed] | None]
    "The Embeds associated with the view, if applicable."
    dispatched_by: NotRequired[Optional[BaseView | discord.ui.Button[BaseView]]]
    "Who dispatched the View..."
    timeout: NotRequired[float | None]
    "Default View timeout parameter."


class State(TypedDict):
    original: discord.ui.View


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


class XIVUser:
    id: int
    discord_id: int
    guild_id: int
    language: Language
    "GarlandTools and Universalis will use this value, using GarlandToolsAsync Language Enum."
    watch_list: list[WatchList]
    datacenter: DataCenter
    "Since neither int values overlap we can determine the type of Enum by it's int value."
    pool: asqlite.Pool

    __slots__: tuple[str, ...] = ("datacenter", "discord_id", "guild_id", "id", "language", "pool", "watch_list")

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

            elif key == "datacenter":
                LOGGER.info("<%s.__init__() | Checking Datacenters.", __class__.__name__)
                try:
                    self.datacenter = DataCenter(value)
                    continue
                except ValueError:
                    LOGGER.warning("<%s.__init__() | Unable to find %s in <DataCenter>", __class__.__name__, value)

                LOGGER.warning("<%s.__init__() | Setting datacenter to default value. | Value: %s", __class__.__name__, DataCenter.Crystal)
                self.datacenter = DataCenter.Crystal

            else:
                setattr(self, key, value)

    def __repr__(self) -> str:
        return (
            f"{__class__.__name__}: {self.id}\n- Discord ID: {self.discord_id} | Guild ID: "
            f"{self.guild_id}\n- Lang: {self.language} | World/DC: {self.datacenter.name}"
            f"\n- Watch List: {len(self.watch_list)}"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def __setattr__(self, name: str, value: Any) -> None:
        LOGGER.debug("<%s.__setattr__() | Name: %s | Value: %s", __class__.__name__, name, value)
        return super().__setattr__(name, value)

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
            )  # pyright: ignore[reportAssignmentType]
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
            res: WatchListDB = await conn.fetchone(  # pyright: ignore[reportAssignmentType]
                """UPDATE watchlist SET price_min = ?
                AND price_max = ? AND last_check = ? WHERE universalid_id = ? AND item_id = ? RETURNING *""",
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
        datacenter: DataCenter = DataCenter.Crystal,
        guild: discord.Guild | None = None,
        language: Language = Language.English,
    ) -> XIVUser:
        """Add or Get an FFXIV User from the database.

        Parameters
        ----------
        pool: :class:`asqlite.Pool`
            _description_.
        user: :class:`discord.User | discord.Member`
            _description_.
        datacenter: :class:`DataCenter`, optional
            _description_, by default DataCenter.Crystal.
        guild: :class:`discord.Guild | None`, optional
            _description_, by default None.
        language: :class:`Language`, optional
            _description_, by default Language.English.

        Returns
        -------
        :class:`XIVUser | None`
            _description_.

        Raises
        ------
        sqlite3.DataError
            _description_.

        """
        LOGGER.debug(
            "<%s.add_or_get_user | Parameters | User: %s | Guild: %s | DataCenter: %s | Language: %s",
            __class__.__name__,
            user,
            guild,
            datacenter,
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
                return XIVUser(**res, db_pool=pool)

            # No user...
            LOGGER.debug(
                "<%s.add_or_get_user()> | Adding Name: %s | ID: %s | Guild ID: %s | DataCenter: %s | Localization: %s to the Database.",
                __class__.__name__,
                user.name,
                user.id,
                (guild.id if guild is not None else guild),
                datacenter.value,
                language,
            )
            res: UserDB | None = await conn.fetchone(
                """INSERT INTO ffxivuser(discord_id, guild_id, datacenter, language) VALUES(?, ?, ?, ?) RETURNING *""",
                user.id,
                (guild.id if guild is not None else 0),
                datacenter.value,
                language.value,
            )  # type: ignore - I know the dataset because of above.

            if res is None:
                LOGGER.error(
                    "<%s.add_or_get_user()> | We encountered an error inserting a Row. | GuildID: %s | UserID: %s | DataCenter: %s",
                    __class__.__name__,
                    (guild.id if guild is not None else 0),
                    user.id,
                    datacenter.value,
                )
                msg = "We encountered an error inserting a Row into the database."
                raise sqlite3.DataError(msg)

            return XIVUser(**res, db_pool=pool)

    async def update(self) -> None:
        """Updates the Database with current object parameters."""
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                """UPDATE ffxivuser SET datacenter = ?, language = ? WHERE discord_id = ? AND guild_id = ?""",
                self.datacenter.value,
                self.language.value,
                self.discord_id,
                self.guild_id,
            )
            LOGGER.debug(
                "<%s.%s> | Updating FFXIV User. | User: %s | Count: %s",
                __class__.__name__,
                "update",
                self,
                res.get_cursor().rowcount,
            )
        return


class MoogleEmbed(Embed):
    """Base Embed class.

    .. note::
    By default will attempt to set icon's to `attachment://moogle-icon.png`.

    """

    resources: FFXIVResources
    cog: FFXIV

    def __init__(self, cog: FFXIV, *, info: Optional[discord.AppInfo] = None, **kwargs: Unpack[EmbedParams]) -> None:
        self.resources = FFXIVResources()
        self.cog = cog  # pyright: ignore[reportIncompatibleVariableOverride] # I need to learn better type overrides for Child inheritance.

        timestamp: datetime.datetime | None = kwargs.get("timestamp")
        if timestamp is None:
            kwargs["timestamp"] = datetime.datetime.now(tz=datetime.UTC)

        super().__init__(cog=cog, **kwargs)
        if info is not None:
            self.set_footer(text=f"Moogles Intuition made by {info.owner.name}")
        self.set_author(name="Moogles Intuition", icon_url="attachment://avatar-icon.png")

        self.set_thumbnail(url="attachment://thumbnail-icon.png")
        self.set_footer()

    @property
    def thumbnail_icon(self) -> None:
        pass

    @property
    def avatar_icon(self) -> None:
        pass

    @property
    def footer_icon(self) -> None:
        pass

    @property
    def field_image(self) -> None:
        pass

    @property
    def attachments(self) -> Sequence[discord.File]:
        icons: list[discord.File | None] = [self.thumbnail_icon, self.avatar_icon, self.footer_icon, self.field_image]
        return [entry for entry in icons if entry is not None]

    def set_footer(
        self,
        *,
        text: Optional[str] = "Moogles Intuition",
        icon_url: Optional[str] = "attachment://footer-icon.png",
        # timestamp: bool = False,
    ) -> Self:
        """Set the footer of the Embed.

        Parameters
        ----------
        text: :class:`Optional[str]`, optional
            The text parameter for `super().set_footer()`, by default "Moogles Intuition".
        icon_url: :class:`_type_`, optional
            The icon url parameter for `super().set_footer()`, by default "attachment://footer-icon.png".
        timestamp: :class:`bool`, optional
            Add a `discord timestamp` of when the embed was sent to the end of the `text` parameter, by default False.

        Returns
        -------
        :class:`Self`
            Returns a :class:`Self` for fluent code typing.

        """
        # if timestamp is True and text is not None:
        #     text += f" | {datetime.datetime.now(tz=datetime.UTC).strftime('%d/%m | %H:%M (%Z)')}"
        return super().set_footer(text=text, icon_url=icon_url)


class UserEmbed(MoogleEmbed):
    """FFXIV User information."""

    def __init__(
        self,
        cog: FFXIV,
        user: discord.User | discord.Member,
        ffxiv_user: XIVUser,
        info: Optional[discord.AppInfo] = None,
        **kwargs: Unpack[EmbedParams],
    ) -> None:
        if kwargs.get("description") is None:
            kwargs["description"] = "Your FFXIV user information and settings..."
        if kwargs.get("title") is None:
            kwargs["title"] = f"**{user.display_name}**"
        if kwargs.get("color") is None:
            kwargs["color"] = user.color if isinstance(user, discord.Member) else discord.Color.blurple()

        super().__init__(cog=cog, info=info, **kwargs)
        self.set_author(name="Moogles Intuition: FFXIV User information", icon_url="attachment://moogle-icon.png")
        self.set_thumbnail(url=user.display_avatar.url)

        # Fields
        self.add_field(name="Language:", value=ffxiv_user.language.name)
        self.add_field(name="World or DataCenter:", value=ffxiv_user.datacenter.name)


class ControlPanelEmbed(MoogleEmbed):
    moogle: Moogle

    def __init__(self, cog: FFXIV, *, moogle: Moogle, **kwargs: Unpack[EmbedParams]) -> None:
        self.moogle = moogle
        if kwargs.get("description") is None:
            kwargs["description"] = "Here you can control static data related to Moogle's Intuition or see metrics."
        if kwargs.get("title") is None:
            kwargs["title"] = "Control Panel"
        super().__init__(cog, **kwargs)
        self.set_author(name="Moogles Intuition", icon_url="attachment://avatar-icon.png")
        self.set_thumbnail(url="attachment://thumbnail-icon.png")

    async def add_metrics(self) -> Self:
        self.add_field(name="Total XIV Users:", value=await self.cog.count_users())
        self.add_blank_field()
        metrics = {
            "Uptime": self.cog.to_discord_timestamp(self.cog.metrics["FFXIV"]["uptime"]["start"]),
            "Item Cache": len(self.moogle._items_cache),  # noqa: SLF001
            "Total # of Items": len(self.moogle._items),  # noqa: SLF001
            "Item Queries": self.cog.metrics["FFXIV"]["item_queries"],
        }
        self.add_field(name="Metrics:", value="\n- ".join([f"{key}: {value}" for key, value in metrics.items()]), inline=False)
        return self

    @property
    def thumbnail_icon(self) -> discord.File:
        return FFXIVResources.get_moogle_icon(filename="thumbnail-icon.png")

    @property
    def avatar_icon(self) -> discord.File:
        return FFXIVResources.get_moogle_icon(filename="avatar-icon.png")


class ItemEmbed(MoogleEmbed):
    """FFXIV Item information."""

    item: Item
    _links_index: int

    def __init__(self, cog: FFXIV, item: Item, *, add_links: bool = True, **kwargs: Unpack[EmbedParams]) -> None:
        """__init__ _summary_.

        Parameters
        ----------
        cog: :class:`Cog`
            _description_.
        item: :class:`Item`
            _description_.
        add_links: :class:`bool`, optional
            Adds pre-filled links to the bottom of the Embed (just above the footer), by default True.
        **kwargs: :class:`Unpack[EmbedParams]`
            Any keyword args for `discord.Embed` class creation.

        """
        self.item = item
        if kwargs.get("description") is None:
            if item.description is not None:
                item.description = item._moogle._builder.sanitize_html(item.description)  # noqa: SLF001
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
        self.set_author(name="Moogles Intuition: FFXIV Item lookup", icon_url="attachment://avatar-icon.png")
        self.set_thumbnail(url="attachment://thumbnail-icon.png")

        content: list[str] = []
        if not self.item.is_untradable:
            content.append("**Tradeable**")
            # self.add_field(name="Tradeable", value="\u200b", inline=False)
        if self.item.recipe is not None:
            content.append("**Craftable**")
            # self.add_field(name="Craftable", value="\u200b", inline=False)
        if self.item.gathering is not None:
            content.append("**Gatherable**")
            # self.add_field(name="Gatherable:", value="\u200b", inline=False)
        if self.item.fishing is not None:
            content.append("**Fishable**")
            # self.add_field(name="Fishable:", value="\u200b", inline=False)

        if len(content) >= 1:
            self.add_field(name=" | ".join(content), value="\u200b", inline=False)
        # else:
        #     self.add_field(name=content, value="\u200b", inline=False)

        if self.item.garlandtools_data is not None:
            vendors: list[Vendor] | None = self.item.get_vendors()
            if vendors is not None:
                self.add_shop_info(shops=vendors, name="Vendors", inline=False)

            tradeshops: list[Vendor] | None = self.item.get_tradeshops()
            if tradeshops is not None:
                self.add_shop_info(shops=tradeshops, name="Tradeshops", inline=False)

        # Useful links
        if add_links is True:
            self.add_links()

    @property
    def thumbnail_icon(self) -> discord.File:
        if self.item._icon_data is not None:  # noqa: SLF001
            return discord.File(io.BytesIO(self.item._icon_data.data), filename="thumbnail-icon.png")  # noqa: SLF001
        return FFXIVResources.get_moogle_icon(filename="thumbnail-icon.png")

    @property
    def avatar_icon(self) -> discord.File:
        if self.item.garlandtools_data is not None:
            return FFXIVResources.get_patch_icon(patch_id=self.item.garlandtools_data.get("item").get("patch"), filename="avatar-icon.png")
        return FFXIVResources.get_patch_icon(patch_id=1, filename="avatar-icon.png")

    @property
    def footer_icon(self) -> discord.File:
        return FFXIVResources.get_moogle_icon(filename="footer-icon.png")

    @property
    def mapped_links(self) -> dict[str, str]:

        _mapped_links: dict[str, str] = {
            "garlandtools": f"[GarlandTools]({self.item.garland_tools_url})",
            "xivwiki": f"[FFXIV Wiki]({self.item.ffxivconsolegames_wiki_url})",
        }

        if self.item.fishing is not None:
            _mapped_links["angler"] = f"[FF14 Angler]({self.item.fishing.angler_url})"

        if self.item.recipe is not None:
            _mapped_links["teamcraft"] = f"[Create Teamcaft]({self.cog.moogle.teamcraft_list([self.item])})"

        if not self.item.is_untradable or self.item.item_ui_category != ItemUICategory.other:
            _mapped_links["universalis"] = f"[Universalis]({self.item.universalis_url})"

        return _mapped_links

    def add_links(self, value: Optional[str] = None, *, index: int = 25, inline: bool = False) -> Self:
        """Adds useful links to the bottom of the Embed.

        .. note::
            When `value` is None, `value` will be replaced with :class:`Self.mapped_links`.

        Parameters
        ----------
        value: :class:`Optional[str]`
            The value parameter for `View.insert_field_at()`, default is None.
        index: :class:`int`, optional
            The index to insert the field at, limit is 25; default is 25.
        inline: :class:`bool`, optional
            If the field should be `inline` or not. default is False.

        """
        self.add_blank_field(index=index - 1, inline=False)
        fields = ""
        if value is None:
            for value in self.mapped_links.values():
                fields += value + " | "
            self.insert_field_at(
                index=index,
                name="Links:",
                # value=(
                #     f"[GarlandTools]({self.item.garland_tools_url}) | "
                #     f"[FFXIV Wiki]({self.item.ffxivconsolegames_wiki_url}) | "
                #     f"[Universalis]({self.item.universalis_url}) | "
                #     f"[Create Teamcaft]({self.cog.moogle.teamcraft_list([self.item])})"
                # ),
                value=fields,
                inline=inline,
            )
        else:
            self.insert_field_at(index=index, name="Links:", value=value, inline=inline)
        return self

    async def add_gathering_info(self) -> Self:
        """Add Gathering information to the :class:`ItemEmbed`.

        Parameters
        ----------
        inline: :class:`bool`, optional
            If the field should be `inline` or not, default is `False`.

        Returns
        -------
        :class:`Self`
            Returns `Self` for fluent coding..

        """
        if self.item.garlandtools_data is not None and self.item.gathering is not None:
            nodes: list[GatheringNode] | None = await self.item.gathering.get_gathering_nodes()
            if nodes is None:
                self.add_field(name="Gathering Node Locations:", value="Error getting node information..", inline=False)
                return self

            temp = ""
            for node in nodes:
                zone_name = node.zone_name.name if isinstance(node.zone_name, PlaceName) else node.zone_name
                temp += f"> **Lv. {node.lvl}** | [{zone_name}, {node.area_name}]({node.garland_tools_url}) | `{node.coords}`\n"
            self.add_field(name="Gathering Node Locations:", value=temp, inline=False)

        return self

    def add_shop_info(self, shops: list[Vendor], limit: int = 4, *, name: Literal["Vendors", "Tradeshops"], inline: bool = False) -> Self:
        """Add :class:`Vendor` information fields to the embed.

        .. note::
            By default will remove any links field from the embed.

        Parameters
        ----------
        shops: :class:`list[Vendor]`
            The list of Vendor information.
        limit: :class:`int`, optional
            Limit of Vendor entries to parse, by default 3.
        name: :class:`Literal["Vendors", "Tradeshops"]`
            The name of the shop.
        inline: :class:`bool`, optional
            If the field should be `inline` or not, default is `False`.

        Returns
        -------
        :class:`Self`
            Returns `Self` for fluent coding..

        """
        data: list[str] = []
        last_currency: Optional[Item] = None
        # limit = 3
        for idx, cur_shop in enumerate(iterable=shops, start=1):
            if idx > limit:
                break

            # This should allow some variance to which TradeShops we get.
            currency: Item | None = cur_shop.get("currency", None)
            if last_currency is None:
                last_currency = currency
            elif isinstance(last_currency, Item) and isinstance(currency, Item) and last_currency.id == currency.id:
                limit += 1
                continue

            value = f"[**{cur_shop.get('name')}** | {cur_shop.get('shop_name')}]({cur_shop.get('url', 'N/A')})"
            if currency is not None:
                emoji = self.cog.resolve_currency(currency.name.lower(), inline=True)
                value += f"\n> **{cur_shop.get('price', 0):,d}** [{emoji}]({currency.garland_tools_url})"
            else:
                value += f"\n> **{cur_shop.get('price', 0):,d}** {self.resources.emojis.gil}"
            data.append(value)

        self.add_field(name=f"{RESOURCES.emojis.vendor_icon} {name}:", value="\n".join(data), inline=inline)

        return self

    def add_currency_info(self, data: CurrencySpender) -> Self:

        self.remove_field(len(self.fields) - 2)
        self.remove_field(len(self.fields) - 1)
        self.remove_field(len(self.fields))

        if self.item.mb_current is None:
            self.add_field(name="Error:", value=f"{RESOURCES.emojis.error_icon} | Failed to fetch Marketboard information...")
            return self

        self.add_field(name=f"{data['currency'].name.replace('_', ' ').title()}", value=f"Cost: **{data['cost']:,d}**", inline=False)

        timestamp: str | int = (
            self.cog.to_discord_timestamp(self.item.mb_current.last_upload_time)
            if isinstance(self.item.mb_current.last_upload_time, datetime.datetime)
            else self.item.mb_current.last_upload_time
        )
        struct = [
            f"- Sale Velocity: **{self.item.mb_current.regular_sale_velocity:,.0f}**",
            f"\t - Avg. Price: **{self.item.mb_current.average_price:,.0f}** {self.resources.emojis.gil} | "
            f"Current Min: **{self.item.mb_current.min_price:,.0f}** {self.resources.emojis.gil}",
            f"- PPu/Curency: **{(self.item.mb_current.min_price / data['cost']):,.0f} {self.resources.emojis.gil}**",
        ]
        world = self.item.mb_current.world_name if self.item.mb_current.world_name is not None else self.item.mb_current.dc_name
        self.add_field(name=f"Marketboard for {world} | {timestamp} ", value="\n".join(struct), inline=False)

        # self.add_blank_field(inline=False)
        self.add_links()
        return self


class FishingEmbed(ItemEmbed):
    def __init__(
        self,
        cog: FFXIV,
        item: Item,
        angler_data: Optional[AnglerFish] = None,
        **kwargs: Unpack[EmbedParams],
    ) -> None:
        self.item = item
        if kwargs.get("description") is None:
            if item.description is not None:
                item.description = item._moogle._builder.sanitize_html(item.description)  # noqa: SLF001
                if len(item.description) > 1020:
                    kwargs["description"] = f"*{item.description[:1020] + ' ...'}*"
                else:
                    kwargs["description"] = item.description
            else:
                kwargs["description"] = "..."

        if kwargs.get("title") is None:
            kwargs["title"] = f"**{item.name}** [{item.id}]"

        super().__init__(item=self.item, add_links=False, cog=cog, **kwargs)
        # Using patch
        self.set_author(name="Moogles Intuition: Fishing Information", icon_url="attachment://avatar-icon.png")
        self.set_thumbnail(url="attachment://thumbnail-icon.png")

        if item.fishing is None or (item.fishing.angler_data is None and angler_data is None):
            self.add_field(name="Error:", value=f"{RESOURCES.emojis.error_icon}  No Fishing data found...", inline=False)
            return
        if angler_data is None and item.fishing.angler_data is not None:
            angler_data = item.fishing.angler_data[0]

        if angler_data is None:
            self.add_field(name="Error:", value=f"{RESOURCES.emojis.error_icon} No Fishing data found...", inline=False)
            return

        general_fields: list[str] = []
        # place_name = "UNK"
        if item.fishing.fishing_spot is not None:
            fishing_spot: FishingSpot = item.fishing.fishing_spot

            # if isinstance(fishing_spot.place_name, PlaceName):
            #     place_name = fishing_spot.place_name.name

            stars = ""
            if item.fishing.ocean_stars > 0:
                stars = f"{item.fishing.ocean_stars} \U00002b50"
            # f"Location: {place_name} | {fishing_spot.x},{fishing_spot.z}",

            general_fields.append(f"- `Lv.{fishing_spot.gathering_level}` **{fishing_spot.fishing_spot_category.name.title()}** {stars}")
            if fishing_spot.rare is True:
                general_fields.append(f"- Rare: {fishing_spot.rare}")
            if item.fishing.is_hidden is True:
                general_fields.append(f"- Hidden: {item.fishing.is_hidden}")
            self.add_field(name="__Info__:", value="\n ".join(general_fields), inline=False)


        # FF14 Angler Data parsing.
        best_bait: AnglerBaits | None = angler_data.best_bait()
        if best_bait is None:
            best_bait = next(iter(angler_data.baits.values()))

        data = []
        if len(angler_data.restrictions) > 1:
            data.append(f"- Restrictions: {','.join(angler_data.restrictions)}")

        data.extend([
            f"{RESOURCES.emojis.hook} **~{angler_data.hook_time}** | {self.hook_converter(angler_data.double_fish)} ",
            f"Best Bait: **{best_bait.bait_name.title()}** [{best_bait.hook_percent * 100}%]",
        ])
        # Attempt to add X,Z cordinates of the Fishing spot.
        if (
            item.fishing.fishing_spot is not None
            and isinstance(item.fishing.fishing_spot.place_name, PlaceName)
            and angler_data.sub_area_name is not None
        ):
            # print(item.fishing.fishing_spot._raw)
            # print(item.fishing.angler_data)
            # TODO(@k8thekat): Finish adding location Parent name zone and links

            if item.fishing.fishing_spot.place_name.name.lower() == angler_data.sub_area_name.lower():
                if angler_data.area_name is not None:
                    location = f"{angler_data.area_name}: {angler_data.sub_area_name} | [{item.fishing.fishing_spot.x}, {item.fishing.fishing_spot.z}]"
                else:
                    location = f"{angler_data.sub_area_name} | [{item.fishing.fishing_spot.x}, {item.fishing.fishing_spot.z}]"
                data.insert(0, f"- **{location}**")

            else:
                data.insert(0, f"- {angler_data.sub_area_name}")

        self.add_field(name="__Locations__:", value="\n".join(data), inline=False)
        self.add_blank_field(inline=False)
        self.add_links()

    def hook_converter(self, value: int) -> str:
        if value == 2:
            return RESOURCES.emojis.double_hook
        if value == 3:
            return RESOURCES.emojis.triple_hook
        return RESOURCES.emojis.hook


class UniversalisEmbed(ItemEmbed):
    """Universalis Embed."""

    def __init__(
        self,
        cog: FFXIV,
        item: Item,
        world_or_dc: World | DataCenter,
        cur_listings: list[CurrentDataEntries],
        hist_listings: list[HistoryDataEntries],
        **kwargs: Unpack[EmbedParams],
    ) -> None:
        """__init__ _summary_.

        Parameters
        ----------
        cog: :class:`Cog`
            FFXIV Cog.
        item: :class:`Item`
            The XIV Item.
        world_or_dc: :class:`World | DataCenter`
            The :class:`XIVUser` or supplied `World | DataCenter`.
        cur_listings: :class:`list[CurrentDataEntries]`
            The array of Current Listing data to format for an Embed field.
        hist_listings: :class:`list[HistoryDataEntries]`
            The array of History Listing data to format for an Embed field.
        **kwargs: :class:`Unpack[EmbedParams]`
            Any addition `discord.Embed` parameters.

        """
        self.item: Item = item
        if kwargs.get("title") is None:
            kwargs["title"] = f"**{item.name}** [{item.id}]"
        if kwargs.get("description") is None:
            kwargs["description"] = f"Universalis results for {world_or_dc.__class__.__name__}: **{world_or_dc.name}**"
        if kwargs.get("color") is None:
            kwargs["color"] = discord.Color.og_blurple()

        super().__init__(item=self.item, cog=cog, add_links=False, **kwargs)

        self.set_author(name="Moogles Intuition: Universalis Marketboard", icon_url="attachment://avatar-icon.png")
        self.set_thumbnail(url="attachment://thumbnail-icon.png")
        if item.mb_current is None:
            self.add_field(name="Error:", value="No Marketboard data found.")
            return

        mb_data: CurrentData = item.mb_current

        # Setting default sorting to price per unit.
        if isinstance(mb_data.last_upload_time, datetime.datetime):
            ftime: str | int = self.cog.to_discord_timestamp(mb_data.last_upload_time, style="R")
        else:
            ftime = mb_data.last_upload_time

        general_fields: list[str] = [
            f"Total # of Units for Sale: **{mb_data.units_for_sale:,d}**",
            f"Num of Current listings fetched **{mb_data.listings_count:,d}** | History *({mb_data.recent_history_count:,d})*",
            f"Updated: **{ftime}**",
            f"Sale Velocity HQ **{int(mb_data.hq_sale_velocity):,d}** | NQ **{int(mb_data.nq_sale_velocity):,d}**",
            f"Num Units Sold: **{mb_data.units_sold:,d}** *(per fetched History data)*",
            "*note* - Total prices include `TAX` | **Total**`(Price Per Unit)`",
        ]

        self.add_field(name="__Listing Stats__:", value="\n- ".join(general_fields), inline=False)

        # Universalis Current Data
        if len(cur_listings) >= 1:
            listing_data = []
            for indx, entry in enumerate(cur_listings, 1):
                if indx == 10:
                    break
                world: str | None = entry.dc_name if entry.world_name is None else entry.world_name
                if isinstance(entry.last_review_time, datetime.datetime):
                    ftime: str | int = self.cog.to_discord_timestamp(entry.last_review_time, style="R")
                else:
                    ftime = entry.last_review_time

                data = (
                    f"> {entry.quantity}x | **{(entry.total + entry.tax):,d}**"
                    f"`({entry.price_per_unit:,d})`{self.resources.emojis.gil} | **{world}** [{ftime}]"
                )

                # Helps control single entry limit for the "embed" field value parameter.
                if len("\n".join(listing_data)) + len(data) > 1015:
                    listing_data.append("...")
                    break
                listing_data.append(data)
            # self.add_field(name=f"__Current Listings__: ({count}/{mb_data.listings_count})", value="\n".join(listing_data), inline=False)
            self.add_field(
                name=f"{RESOURCES.emojis.mbicon} __Current Listings__: ({len(cur_listings)})",
                value="\n".join(listing_data),
                inline=False,
            )

        else:
            self.add_field(name="__Current Listings__:", value=f"> No listings for {world_or_dc.name}")

        # Universalis History Data
        if len(hist_listings) >= 1:
            listing_data = []

            for indx, entry in enumerate(hist_listings, 1):
                if indx == 10:
                    break
                world: str | None = entry.dc_name if entry.world_name is None else entry.world_name
                hq = " **HQ** |" if entry.hq is True else ""
                if isinstance(entry.timestamp, datetime.datetime):
                    ftime: str | int = self.cog.to_discord_timestamp(entry.timestamp, style="R")
                else:
                    ftime = entry.timestamp

                data = (
                    f"> {entry.quantity}x |{hq} **{(entry.quantity * entry.price_per_unit):,d}**"
                    f"`({entry.price_per_unit:,d})`{self.resources.emojis.gil} | **{world}** [{ftime}]"
                )

                # Helps control single entry limit for the "embed" field value parameter.
                if len("\n".join(listing_data)) + len(data) > 1015:
                    listing_data.append("...")
                    break
                listing_data.append(data)
            self.add_field(
                name=f"{RESOURCES.emojis.mbhistoryicon} __History Listings__: ({len(hist_listings)})",
                value="\n".join(listing_data),
                inline=False,
            )

        else:
            self.add_field(name=f"{RESOURCES.emojis.mbhistoryicon} __History Listings__:", value=f"> No listings for {world_or_dc.name}")

        self.add_blank_field(inline=False)
        self.add_links()


class RecipeEmbed(ItemEmbed):
    item: Item

    def __init__(
        self,
        cog: FFXIV,
        item: Item,
        job_recipe: Optional[str] = None,
        **kwargs: Unpack[EmbedParams],
    ) -> None:
        self.item = item
        if kwargs.get("description") is None:
            if item.description is not None:
                item.description = item._moogle._builder.sanitize_html(item.description)  # noqa: SLF001
                if len(item.description) > 1020:
                    kwargs["description"] = f"*{item.description[:1020] + ' ...'}*"
                else:
                    kwargs["description"] = item.description
            else:
                kwargs["description"] = "..."

        if kwargs.get("title") is None:
            kwargs["title"] = f"**{item.name}** [{item.id}]"

        super().__init__(item=self.item, cog=cog, add_links=False, **kwargs)
        # Using patch
        self.set_author(name="Moogles Intuition: Recipe Information", icon_url="attachment://avatar-icon.png")
        self.set_thumbnail(url="attachment://thumbnail-icon.png")

        # If we somehow got to an Item Recipe but the data actually isn't there?!
        if item.recipe is None:
            self.add_field(name="Error:", value="No Recipe data found...")
            return

        data = []
        # This is for when we use the "Change Job" select/button setup.
        if job_recipe is not None:
            recipe: Recipe = getattr(item.recipe, job_recipe)
        else:
            recipe = item.recipe[0]
        if recipe.craft_type is not None:
            result = "" if recipe.amount_result < 1 else f" | *Creates: {recipe.amount_result}*"
            for ingredients in recipe:
                data.append(  # noqa: PERF401
                    f"- **{ingredients[1]}**  [**{ingredients[0].name}** [*{ingredients[0].id}*]]({ingredients[0].garland_tools_url})",
                )
            job_abbr = recipe.craft_type.to_abbr()
            job_emoji = getattr(RESOURCES.emojis, job_abbr.lower() + "_icon")
            self.add_field(name=f"{job_emoji} __{recipe.craft_type.name.title()}__ {result}", value="\n".join(data), inline=False)

        self.add_links()

    async def add_crafting_cost(self, **kwargs: Unpack[CurMarketBoardParams]) -> Self:
        """Handles the information from :class:`Recipe.get_crafting_cost()` and populates the embed.

        .. note::
            Total Vendor Gil may not include total cost to craft the :class:`Recipe.item_result`
            as some items may not be purchased from a Vendor or TradeShops (eg using Currency).

        Returns
        -------
        :class:`Self`
            A breakdown of the cost to craft the entire :class:`Item` combined with the cost of each individual ingredient.

        """
        self.remove_field(len(self.fields) - 1)
        self.remove_field(len(self.fields))

        if self.item.recipe is None:
            self.add_field(
                name="Crafting Cost:",
                value=f"{RESOURCES.emojis.error_icon} Error - This item doesn't have a Recipe..",
                inline=False,
            )
            return self

        res: dict[int, ShoppingItem] | None = await self.item.recipe.get_crafting_cost(**kwargs)
        if res is None:
            self.add_field(
                name="Crafting Cost:",
                value=f"{RESOURCES.emojis.error_icon}  Error - Failed to get crafting cost for Recipe: {self.item.recipe.id}",
                inline=False,
            )
            return self

        currency = {"marketboard_gil": 0, "currencies": {0: 0}}
        vendor_gil: bool = False
        # content: list[str] = []
        general_fields: list[str] = [
            "*note - Totals may or may not include EVERY ingredient as some cannot be purchased or traded...*",
        ]
        await self.item.get_current_marketboard(**kwargs)
        if self.item.mb_current is not None:
            general_fields.insert(
                0,
                f"Current Lowest MB Price `(NQ|HQ)`: **{self.item.mb_current.min_price_nq}** {RESOURCES.emojis.gil} | **{self.item.mb_current.min_price_hq}** {RESOURCES.emojis.gil}",
            )
            world_or_dc = kwargs.get("world_or_dc")
            if world_or_dc is not None:
                general_fields.insert(
                    0,
                    f"{world_or_dc.__class__.__name__}: **{world_or_dc.name}** | # of listings: **{self.item.mb_current.listings_count}**",
                )

        # indx = 1
        # TODO(@k8thekat): Parse ingredients and add up our totals.
        for entry in res:
            # market_cost = 0
            # market_cost_per = 0
            # vendor_cost = 0
            price = 0
            vendor_gil = False
            # inline = True
            # if indx != 1 and indx&1:
            #     inline = False
            value: ShoppingItem | None = res.get(entry)
            if value is None or value["item"].mb_current is None:
                continue
            item: Item = value["item"]
            # Marketboard/Universalis Info.
            if item.mb_current is not None:
                LOGGER.debug("<%s.%s> | Parsing Universalis for Item | Item: %s", __class__.__name__, "_parse_makeplace_item", item)
                marketboard: CurrentData = item.mb_current
                market_listing: CurrentDataEntries = sorted(marketboard.listings, key=lambda x: x.price_per_unit)[0]
                currency["marketboard_gil"] += market_listing.price_per_unit * value["count"]
                # tax_per = market_listing.tax / market_listing.quantity
                # market_cost = int(market_listing.price_per_unit * value["count"] + tax_per)
                # market_cost_per = market_listing.price_per_unit
            else:
                LOGGER.debug(
                    "<%s.%s> | No Universalis Marketboard information. | Item: %s ",
                    __class__.__name__,
                    "parse_crafting_cost",
                    item,
                )

            # Vendors info...
            if item.vendors is not None:
                LOGGER.debug("<%s.%s> | Parsing Vendors for Item | Item: %s", __class__.__name__, "_parse_makeplace_item", item)
                currency_item: Item | None = item.vendors[0].get("currency")
                price = item.vendors[0].get("price", 0)
                if currency_item is None and vendor_gil is False:
                    currency["currencies"][0] += price * value["count"]
                    # vendor_cost = price * value["count"]
                    vendor_gil = True
            else:
                LOGGER.debug("<%s.%s> | No Vendor information. | Item: %s ", __class__.__name__, "parse_crafting_cost", item)

            # Tradeshop Info...
            if item.tradeshops is not None:
                LOGGER.debug("<%s.%s> | Parsing TradeShops for Item | Item: %s", __class__.__name__, "_parse_makeplace_item", item)
                currency_item: Item | None = item.tradeshops[0].get("currency")
                price = item.tradeshops[0].get("price", 0)
                # Essentially we fail to lookup "gil"
                if currency_item is None and vendor_gil is False:
                    currency["currencies"][0] += price * value["count"]
                    # vendor_cost = price * value["count"]
                    vendor_gil = True
            else:
                LOGGER.debug("<%s.%s> | No Tradeshop information. | Item: %s ", __class__.__name__, "parse_crafting_cost", item)
            # market_str = ""
            # vendor_str = ""
            # if market_cost > 0:
            #     market_str += f"- Market: {market_cost:,d}`({market_cost_per})` {RESOURCES.emojis.gil}\n"
            # if vendor_cost > 0:
            #     vendor_str += f"- Vendor: {vendor_cost:,d}`({price})` {RESOURCES.emojis.gil}\n"
            # content.append(f"[{item.name} [*{item.id}*]]({item.garland_tools_url}) x{value['count']}\n{market_str}{vendor_str}")
            # print(indx, inline)
            # self.add_field(
            #     name="\u200b",
            #     value=f"[**{item.name}** [*{item.id}*]]({item.garland_tools_url})\nCount: **{value['count']}**\n{market_str}{vendor_str}",
            #     inline=inline,
            # )
            # indx += 1

        self.add_field(name="**__Crafting Cost Breakdown__**:", value="\n".join(general_fields), inline=False)

        self.add_field(
            name="**Totals**",
            value=f"- Market: **{currency['marketboard_gil']:,d}** {RESOURCES.emojis.gil}\n- Vendor: **{currency['currencies'][0]:,d}** {RESOURCES.emojis.gil}",
            inline=False,
        )
        # temp = ""
        # flag = True
        # for entry in content:
        #     if len(temp + entry) > 1024:
        #         if flag is True:
        #             self.add_field(name="**__Breakdown:__**", value=temp, inline=False)
        #             flag = False
        #             temp = ""
        #         else:
        #             self.add_field(name="**__Breakdown Cont:__**", value=temp)
        #             temp = ""

        #     temp += entry

        # if len(temp) > 1:
        #     if flag is True:
        #         self.add_field(name="**__Breakdown:__**", value=temp)
        #     else:
        #         self.add_field(name="**__Breakdown Cont:__**", value=temp)
        self.add_links()
        return self


class CurrencyEmbed(ItemEmbed):
    item: Item

    def __init__(self, data: CurrencySpender, cog: FFXIV, **kwargs: Unpack[EmbedParams]) -> None:
        self.item: Item = data["item"]
        if kwargs.get("description") is None:
            if self.item.description is not None:
                self.item.description = self.item._moogle._builder.sanitize_html(self.item.description)  # noqa: SLF001
                if len(self.item.description) > 1020:
                    kwargs["description"] = f"*{self.item.description[:1020] + ' ...'}*"
                else:
                    kwargs["description"] = self.item.description
            else:
                kwargs["description"] = "..."

        if kwargs.get("title") is None:
            kwargs["title"] = f"**{self.item.name}** [{self.item.id}]"

        super().__init__(item=self.item, cog=cog, add_links=False, **kwargs)
        self.set_author(name="Moogles Intuition: Currency Item Lookup", icon_url="attachment://avatar-icon.png")
        self.set_thumbnail(url="attachment://thumbnail-icon.png")

        if self.item.mb_current is None:
            self.add_field(name="Error:", value="Failed to fetch Marketboard information...")
            return
        self.add_blank_field(inline=False)
        self.add_field(name=f"{data['currency'].name.replace('_', ' ').title()}", value=f"Cost: **{data['cost']:,d}**", inline=False)

        timestamp: str | int = (
            self.cog.to_discord_timestamp(self.item.mb_current.last_upload_time)
            if isinstance(self.item.mb_current.last_upload_time, datetime.datetime)
            else self.item.mb_current.last_upload_time
        )
        struct = [
            f"- Sale Velocity: **{self.item.mb_current.regular_sale_velocity:,.0f}**",
            f"\t - Avg. Price: **{self.item.mb_current.average_price:,.0f}** {self.resources.emojis.gil} | "
            f"Current Min: **{self.item.mb_current.min_price:,.0f}** {self.resources.emojis.gil}",
            f"- PPu/Curency: **{(self.item.mb_current.min_price / data['cost']):,.0f} {self.resources.emojis.gil}**",
        ]
        world = self.item.mb_current.world_name if self.item.mb_current.world_name is not None else self.item.mb_current.dc_name
        self.add_field(name=f"Marketboard for {world} | {timestamp} ", value="\n".join(struct), inline=False)

        self.add_blank_field(inline=False)


class BaseView(discord.ui.View):
    """Our "Base" :class:`discord.ui.View`.

    Already has a "Reset", "Previous" and "Next" buttons built in.

    .. warning::
        Overwrite `reset_view()` function if you want to implement different functionality;
        otherwise the view will clear all items and re-add any :class:`discord.ui.Item` in the `.components` attribute.

    Attributes
    ----------
    owner: :class:`discord.Member | discord.User`
        The Discord User or Member who started the interaction.
    xivuser: :class:`XIVUser`
        The Database XIV User.
    cog: :class:`FFXIV`
        A pointer for useful functionality if needed.
    recent_interaction: :class:`Optional[discord.Interaction]`
        The most recent :class:`discord.Interaction` that sent content, if applicable, by default `None`.
    components: :class:`list[discord.ui.Item[Any]]`
        A list of Items to be added to the view.
    dispatched_by: :class:`Optional[BaseView | discord.ui.Button[BaseView]]`
        Any embeds attached to the view..
    embeds: :class:`Optional[Sequence[MoogleEmbed | ItemEmbed | UniversalisEmbed | RecipeEmbed | FishingEmbed | CurrencyEmbed]]`
        The Embeds related to the View, if applicable.
    indx: :class:`int`
        Index key for Embeds[], by default is 0.

    """

    owner: discord.Member | discord.User
    "The Discord User."
    xivuser: XIVUser
    "The Database FFXIV User."
    cog: FFXIV
    "The parent Cog."
    recent_interaction: Optional[discord.Interaction]
    "The most recent :class:`discord.Interaction` that sent content.."
    components: list[discord.ui.Item[Any]]
    "A list of Items to be added to the view."
    dispatched_by: Optional[BaseView | discord.ui.Button[BaseView]]
    "Who dispatched the View..."
    embeds: Optional[Sequence[EmbedTypeAlias]]
    "Any embeds attached to the view.."
    indx: int
    "Index key for Embeds[], by default is 0."
    _timeout: Optional[float]

    ts_string: str
    "UTC aware timestamp formatted string"

    def __init__(
        self,
        xivuser: XIVUser,
        owner: discord.Member | discord.User,
        cog: FFXIV,
        *,
        embeds: Optional[Sequence[MoogleEmbed | ItemEmbed]] = None,
        components: Optional[list[discord.ui.Item[Any]]] = None,
        recent_interaction: Optional[discord.Interaction] = None,
        dispatched_by: Optional[BaseView | discord.ui.Button[BaseView]] = None,
        timeout: Optional[float] = 180,
    ) -> None:
        """Create our BaseView Instance object..

        Parameters
        ----------
        owner: :class:`discord.Member | discord.User`
            The Discord User or Member who started the interaction.
        xivuser: :class:`XIVUser`
            The Database XIV User.
        cog: :class:`FFXIV | Cog`
            A pointer for useful functionality if needed.
        recent_interaction: :class:`Optional[discord.Interaction]`
            The most recent :class:`discord.Interaction` that sent content, if applicable, by default `None`.
        components: :class:`list[discord.ui.Item[Any]]`
            A list of Items to be added to the view.
        dispatched_by: :class:`Optional[BaseView | discord.ui.Button[BaseView]]`
            Any embeds attached to the view..
        embeds: :class:`Optional[Sequence[MoogleEmbed | ItemEmbed]]`
            The Embeds related to the View, if applicable.
        timeout: :class:`Optional[float]`
            Timeout in seconds from last interaction with the UI before no longer accepting input.
            If `None` then the timeout is 180 seconds.
            - Default is 180 seconds.

        """
        self.indx = 0
        self.embeds = embeds
        self.owner = owner
        self.xivuser = xivuser
        self.cog = cog
        self.dispatched_by = dispatched_by
        self.recent_interaction = recent_interaction
        self.ts_string = self.cog.to_discord_timestamp(datetime.datetime.now(tz=datetime.UTC))
        self._timeout = timeout

        super().__init__(timeout=timeout)
        self.components = []
        # We add new components only if we are supplied with additional Items.
        if components is not None and len(components) > 0:
            for entry in components:
                self.add_item(item=entry)

        self.components.extend([self.previous_callback, self.next_callback, self.reset_callback])

        if self.embeds is not None and len(self.embeds) <= 1:
            self.remove_item(item=self.previous_callback)
            self.remove_item(item=self.next_callback)

    def get_datacenter_select(self, *, sort: bool = True, use_default: bool = False) -> list[discord.SelectOption]:
        """Create's a :class:`list[discord.SelectOption]`.

        Has the option of making the default option
        match the current value of the :class:`XIVUser.datacenter`.

        .. note::
            The :class:`list` of options will be sorted by default in order by :class:`discord.SelectOption.label`.


        Parameters
        ----------
        use_default: :class:`bool`, optional
            If you want the default :class:`discord.SelectOption` to match the :class:`XIVUser.datacenter`, by default False.
        sort: :class:`bool`, optional
            To sort the listings or not.

        Returns
        -------
        :class:`list[discord.SelectOption]`
            A :class:`DataCenter` values converted into a :class:`list[discord.SelectOption]`.

        """
        options: list[discord.SelectOption] = []
        for entry in DataCenter:
            if use_default is True and entry == self.xivuser.datacenter:
                options.append(discord.SelectOption(label=entry.name, value=str(entry.value), default=True))
                continue
            options.append(discord.SelectOption(label=entry.name, value=str(entry.value)))
        if sort is True:
            return sorted(options, key=lambda x: x.label)
        return options

    def get_world_select(self, datacenter: DataCenter) -> list[discord.SelectOption]:
        """Create's a :class:`list[discord.SelectOption]` related to the :class:`DataCenter` supplied.

        .. note::
            We fetch the :class:`World`s belonging to the supplied :class:`DataCenter`.


        Parameters
        ----------
        datacenter: :class:`DataCenter`
            A :class:`DataCenter` object to get a list of :class:`World` belonging to the :class:`DataCenter` object.
            - See :class:`DataCenterToWorlds.get_worlds()`


        Returns
        -------
        :class:`list[discord.SelectOption]`
            A list of :class:`World` values related to the parameter `datacenter` converted into a :class:`list[discord.SelectOption]`.

        """
        options: list[discord.SelectOption] = []

        worlds: list[World] | None = DataCenterToWorlds.get_worlds(datacenter=datacenter)
        if worlds is None:
            return options

        for entry in worlds:
            options.append(discord.SelectOption(label=entry.name, value=str(entry.value)))
            continue

        return sorted(options, key=lambda x: x.label)

    def get_language_select(self, *, use_default: bool = False) -> list[discord.SelectOption]:
        """Create's a :class:`list[discord.SelectOption]` using :class:`Language` options.

        Parameters
        ----------
        use_default: :class:`bool`, optional
            If you want the default :class:`discord.SelectOption` to match the :class:`XIVUser.language`, by default False.

        Returns
        -------
        :class:`list[discord.SelectOption]`
            A list of :class:`Language` values converted into a :class:`list[discord.SelectOption]`.

        """
        options: list[discord.SelectOption] = []
        for entry in Language:
            if use_default is True and entry == self.xivuser.language:
                options.append(discord.SelectOption(label=entry.name, value=entry.value, default=True))
                continue

            options.append(discord.SelectOption(label=entry.name, value=entry.value))
        return sorted(options, key=lambda x: x.label)

    @discord.ui.button(label="Reset", style=discord.ButtonStyle.danger, emoji=RESOURCES.emojis.error_icon, disabled=True, row=4)
    async def reset_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "reset_callback")

        item.disabled = True
        view = self.reset_view()
        view.recent_interaction = interaction
        if self.dispatched_by is not None and isinstance(self.dispatched_by, ItemView):
            view = ItemView(
                item=self.dispatched_by.item,
                xivuser=self.xivuser,
                owner=self.owner,
                cog=self.cog,
                embeds=self.dispatched_by.embeds,
                dispatched_by=self,
            )

        if view.embeds is not None:
            await interaction.response.edit_message(view=view, embed=view.embeds[0], attachments=view.embeds[0].attachments)
        else:
            await interaction.response.edit_message(view=view)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.primary, emoji=RESOURCES.emojis.left_arrow_icon, disabled=True, row=1)
    async def previous_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "previous_callback")

        if self.embeds is None:
            self.reset_view()
            await interaction.response.edit_message(view=self)
            return

        self.recent_interaction = interaction
        # Sanity check (if somehow our indx get's too small?)
        if self.indx >= 0:
            self.indx -= 1
            if self.indx < len(self.embeds) - 1:
                self.next_callback.disabled = False

            if self.indx == 0:
                item.disabled = True

            embed: EmbedTypeAlias = self.embeds[self.indx].set_footer(
                text=f"{self.indx + 1} out of {len(self.embeds)} | Moogles Intuition",
            )
            if isinstance(embed, ItemEmbed):
                await interaction.response.edit_message(embed=embed, view=self, attachments=embed.attachments)
                return
            await interaction.response.edit_message(embed=embed, view=self, attachments=[])

    @discord.ui.button(label="Next", style=discord.ButtonStyle.green, emoji=RESOURCES.emojis.right_arrow_icon, disabled=False, row=1)
    async def next_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "next_callback")
        if self.embeds is None:
            self.reset_view()
            await interaction.response.edit_message(view=self)
            return

        self.indx += 1
        self.recent_interaction = interaction

        # If our indx is still within range of our embeds, update the footer and resend it.
        if self.indx <= len(self.embeds) - 1:
            self.reset_callback.disabled = False
            self.previous_callback.disabled = False

            # Disable the Button once we hit the end of our "embeds".
            if self.indx == len(self.embeds) - 1:
                item.disabled = True
            embed: EmbedTypeAlias = self.embeds[self.indx].set_footer(
                text=f"{self.indx + 1} out of {len(self.embeds)} | Moogles Intuition",
            )
            await interaction.response.edit_message(embed=embed, view=self, attachments=embed.attachments)
            return
        self.reset_view()
        await interaction.response.edit_message(view=self)

    def reset_view(self) -> Self:
        """Clear's the current View's items, adds the Items in `self.components` array and resets `self.indx` to 0.

        .. note::
            Commonly overwrite this function with the proper components and handling needed per view.

        Returns
        -------
        :class:`Self`
            Returns `Self` for fluent coding..


        """
        LOGGER.warning("<%s.%s> | Resetting View... | Obj: %s", __class__.__name__, "reset_view", self)
        # Possible re-assigning the view and adding the components this way may suffice?
        self.clear_items()
        # Possible print len of components and investigate.
        LOGGER.info("Components Len: %s", len(self.components))

        self.indx = 0
        self.recent_interaction = None
        if self.components is None:
            return self

        if len(self.components) < 25:
            for _, item in enumerate(iterable=self.components, start=0):
                self.add_item(item=item)

        else:
            for _, item in enumerate(iterable=self.components, start=0):
                if len(self.children) < 25:
                    self.add_item(item=item)
                else:
                    LOGGER.warning(
                        "<%s.%s> | View has reached max item limit of 25, cannot add more items. | Obj: %s",
                        __class__.__name__,
                        "reset_view",
                        self,
                    )
                    break

        return self

    def add_item(self, item: discord.ui.Item[Any]) -> Self:
        """Adds the item to our `self.components` and calls `super().add_item(item)`."""
        if item not in self.components:
            self.components.append(item)
        return super().add_item(item=item)

    def remove_item(self, item: discord.ui.Item[Any]) -> Self:
        """Removes the item from our `self.components` and calls `super().remove_item(item)`."""
        if item in self.components:
            self.components.remove(item)
        return super().remove_item(item=item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        LOGGER.debug("<%s.%s>", __class__.__name__, "interaction_check")
        if interaction.user != self.owner:
            await interaction.response.send_message(
                content=f"Yea, you know this doesn't belong to you..{self.cog.emoji_table.to_inline_emoji('kuma_chuckle')}",
                ephemeral=True,
            )
            return False
        self.recent_interaction = interaction
        return await super().interaction_check(interaction)

    async def on_timeout(self) -> None:
        if type(self) is BaseView:
            return
        if self.recent_interaction is not None:
            try:
                await self.recent_interaction.delete_original_response()
                self.recent_interaction = None
                # print("Deleted Response", self)
            except (Forbidden, HTTPException, NotFound) as e:
                LOGGER.debug(
                    "<%s.%s> | Failed to delete View on timeout. | Error: %s | Obj: %s",
                    __class__.__name__,
                    "on_timeout",
                    e,
                    self.recent_interaction,
                )
                return
        # else:
        # LOGGER.warning(
        #     "<%s.%s> | Unable to delete View on timeout, no recent interaction. | Obj: %s",
        #     __class__.__name__,
        #     "on_timeout",
        #     self.recent_interaction,
        # )
        return

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: uiItem[Any]) -> None:
        LOGGER.error("<%s.%s> | Encountered an error in our View. | Error: %s | Item: %s", __class__.__name__, "on_error", error, item)
        if interaction.user != self.owner:
            return await super().on_error(interaction, error, item)
        self.reset_view()
        try:
            await interaction.response.edit_message(view=self)
        except discord.errors.InteractionResponded:
            await interaction.edit_original_response(view=self)
        return None


class ItemView(BaseView):
    """Our custom View for Items.

    Typically this is dispatched first for "most" items in the game unless otherwise needed.
    - Contains 4 buttons for Universalis(Marketboard), Crafting, Gathering, and Fishing(FF14 Angler)

    .. note::
        Supply the `embeds` parameter of this view with your :class:`discord.Embeds`  you plan to display to handle the Pagination, otherwise


    .. note::
        Does not overwrite, `on_timeout()` or `on_error()` functionality,
        this is handled by :class:`BaseView` and should be overwritten to handle any Component states.


    Attributes
    ----------
    item: :class:`Item`
        The Moogle's Intuition Item, if applicable.

    """

    item: Item
    "The Moogle's Intuition Item, if applicable."

    def __init__(self, item: Item, **kwargs: Unpack[ViewParams]) -> None:
        """Build our ItemView.

        Parameters
        ----------
        item: :class:`Item`
            TThe Moogle's Intuition Item.
        **kwargs: :class:`ViewParams`
            Any additional args needed to build the :class:`BaseView` object.

        """
        LOGGER.debug("<%s.%s> | Builder | ID: %s", __class__.__name__, "__init__", id(self))
        self.item = item
        super().__init__(**kwargs)

        self.components.extend([self.universalis_callback, self.recipe_callback, self.gathering_callback, self.fishing_callback])

        # Universalis related Buttons.
        # ItemUICategory(63) is a possible filter to catch "Currency" type items that can be traded to a vendor but not the Marketboard.
        if self.item.is_untradable or self.item.item_ui_category == ItemUICategory.other:
            self.remove_item(self.universalis_callback)

        if self.item.recipe is None:
            self.remove_item(self.recipe_callback)

        if self.item.gathering is None:
            self.remove_item(self.gathering_callback)

        if self.item.fishing is None:
            self.remove_item(self.fishing_callback)

    @discord.ui.button(
        label="Gathering",
        style=discord.ButtonStyle.primary,
        emoji=RESOURCES.emojis.gathering_log_icon,
        disabled=False,
        row=0,
    )
    async def gathering_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "gathering_callback")

        if self.embeds is None:
            self.reset_view()
            await interaction.response.edit_message(view=self)
            return

        # Disable the Gathering button.
        item.disabled = True
        self.recent_interaction = interaction
        embed = self.embeds[self.indx]
        if isinstance(embed, ItemEmbed):
            embed = await embed.add_gathering_info()
            await interaction.response.edit_message(view=self, embed=embed)
            return
        self.reset_view()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Marketboard", style=discord.ButtonStyle.primary, emoji=RESOURCES.emojis.mb_2_icon, disabled=False, row=0)
    async def universalis_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        LOGGER.debug("<%s.%s>", __class__.__name__, "universalis_callback")

        await interaction.response.defer()

        self.recent_interaction = None
        get_entries = 50
        step = 10

        await self.item.get_current_marketboard(
            world_or_dc=self.xivuser.datacenter,
            num_listings=get_entries,
            num_history_entries=get_entries,
            item_quality="NQ",
        )
        await self.item.get_icon()
        # Sort the Data how we need.
        cur_listings = []
        hist_listings = []
        if self.item.mb_current is not None:
            cur_listings = sorted(self.item.mb_current.listings, key=lambda x: x.price_per_unit)
            if self.item.mb_current.recent_history is not None:
                hist_listings = sorted(self.item.mb_current.recent_history, key=lambda x: x.timestamp, reverse=True)

        embeds = []
        for indx in range(0, get_entries, step):
            try:
                cur_entry = cur_listings[indx : indx + step]
            except IndexError:
                if indx > len(cur_listings):  # noqa: SIM108
                    cur_entry = cur_listings[len(cur_listings) - 1 :]
                else:
                    cur_entry = cur_listings[indx : len(cur_listings) - 1]

            try:
                hist_entry = hist_listings[indx : indx + step]
            except IndexError:
                if indx > len(hist_listings):
                    hist_entry = hist_listings[len(hist_listings) - 1 :]
                else:
                    hist_entry = hist_listings[indx : len(hist_listings) - 1]

            embed = UniversalisEmbed(
                cog=self.cog,
                item=self.item,
                world_or_dc=self.xivuser.datacenter,
                cur_listings=cur_entry,
                hist_listings=hist_entry,
            )
            embeds.append(embed)

        view = UniversalisView(
            item=self.item,
            cog=self.cog,
            xivuser=self.xivuser,
            recent_interaction=interaction,
            owner=interaction.user,
            dispatched_by=self,
            embeds=embeds,
            timeout=self._timeout,
        )

        embed: UniversalisEmbed = embeds[self.indx]
        embed.set_footer(text=f"{self.indx + 1} out of {len(embeds)} | Moogles Intuition")
        await interaction.edit_original_response(embed=embed, view=view, attachments=embed.attachments)

    @discord.ui.button(label="Recipe", style=discord.ButtonStyle.primary, emoji=RESOURCES.emojis.crafting_log_icon, disabled=False, row=0)
    async def recipe_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        LOGGER.debug("<%s.%s>", __class__.__name__, "recipe_callback")
        await self.item.get_icon()

        embed = RecipeEmbed(cog=self.cog, item=self.item)
        view = RecipeView(
            item=self.item,
            cog=self.cog,
            xivuser=self.xivuser,
            recent_interaction=interaction,
            owner=interaction.user,
            dispatched_by=self,
            embeds=[embed],
            timeout=self._timeout,
        )
        self.recent_interaction = interaction
        await interaction.response.edit_message(embed=embed, view=view, attachments=embed.attachments)

    @discord.ui.button(label="Fishing", style=discord.ButtonStyle.primary, emoji=RESOURCES.emojis.fishing_log_icon, disabled=False, row=0)
    async def fishing_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        LOGGER.info("<%s.%s>", __class__.__name__, "fishing_callback")

        await interaction.response.defer()

        if self.item.fishing is not None:
            await self.item.fishing.get_angler_data()

        await self.item.get_icon()
        embed = FishingEmbed(cog=self.cog, item=self.item)
        view = FishingView(
            item=self.item,
            dispatched_by=self,
            cog=self.cog,
            xivuser=self.xivuser,
            recent_interaction=interaction,
            owner=interaction.user,
            embeds=[embed],
            timeout=self._timeout,
        )
        self.recent_interaction = interaction
        await interaction.edit_original_response(view=view, embed=embed, attachments=embed.attachments)
        return


class UserView(BaseView):
    def __init__(
        self,
        **kwargs: Unpack[ViewParamsPartial],
    ) -> None:
        super().__init__(**kwargs)

        self.components.extend([self.set_dc_callback, self.set_lang_callback, self.cancel_callback])

    @discord.ui.button(label="Set DataCenter", style=discord.ButtonStyle.primary, disabled=False)
    async def set_dc_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "set_dc_callback")
        item.disabled = True

        self.add_item(DataCenterSelect(view=self, options=self.get_datacenter_select()))
        self.cancel_callback.disabled = False

        await interaction.response.edit_message(view=self)
        # await interaction.response.defer()

    @discord.ui.button(label="Set Language", style=discord.ButtonStyle.primary, disabled=False)
    async def set_lang_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "set_lang_callback")
        item.disabled = True

        self.add_item(LanguageSelect(view=self, options=self.get_language_select()))
        self.cancel_callback.disabled = False

        await interaction.response.edit_message(view=self)
        # await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, disabled=True)
    async def cancel_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "cancel_callback")
        item.disabled = True
        self.reset_view()
        await interaction.response.edit_message(view=self)

    def reset_view(self) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "reset_view")
        self.set_dc_callback.disabled = False
        self.set_lang_callback.disabled = False
        self.cancel_callback.disabled = True

    async def update_view(self, interaction: discord.Interaction) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "update_view")
        # await self.recent_interaction.edit(view=self, embed=UserEmbed(self.cog, user=interaction.user, ffxiv_user=self.xivuser))
        await interaction.response.edit_message(view=self, embed=UserEmbed(self.cog, user=interaction.user, ffxiv_user=self.xivuser))


class UniversalisView(ItemView):
    world: Optional[World]
    quality: Literal["HQ", "NQ"]

    def __init__(
        self,
        item: Item,
        *,
        quality: Literal["HQ", "NQ"] = "NQ",
        **kwargs: Unpack[ViewParams],
    ) -> None:
        self.item = item
        self.quality = quality

        super().__init__(item, **kwargs)
        self.components.extend([self.world_callback, self.undo_callback, self.universalis_callback])
        self.reset_callback.disabled = False

        # Temporarily removing the ItemView buttons until I decide how I want them to interact with the Embeds.
        self.remove_item(self.recipe_callback)
        self.remove_item(self.gathering_callback)
        self.remove_item(self.fishing_callback)

        self.world = None
        self.world_select = WorldSelect(view=self, options=self.get_world_select(datacenter=self.xivuser.datacenter), row=3)

        if self.item.can_be_hq is False:
            self.remove_item(self.universalis_callback)

    @discord.ui.button(label="HQ", style=discord.ButtonStyle.secondary, disabled=False, row=0)
    async def universalis_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        LOGGER.debug("<%s.%s>", __class__.__name__, "universalis_callback")
        await interaction.response.defer()
        self.quality = "HQ"
        get_entries = 50
        step = 10

        await self.item.get_current_marketboard(
            world_or_dc=self.xivuser.datacenter,
            num_listings=get_entries,
            num_history_entries=get_entries,
            item_quality="HQ",
        )

        await self.item.get_icon()
        # Sort the Data how we need.
        cur_listings = []
        hist_listings = []
        if self.item.mb_current is not None:
            cur_listings = sorted(self.item.mb_current.listings, key=lambda x: x.price_per_unit)
            if self.item.mb_current.recent_history is not None:
                hist_listings = sorted(self.item.mb_current.recent_history, key=lambda x: x.timestamp, reverse=True)

        embeds = []
        for indx in range(0, get_entries, step):
            try:
                cur_entry = cur_listings[indx : indx + step]
            except IndexError:
                if indx > len(cur_listings):  # noqa: SIM108
                    cur_entry = cur_listings[len(cur_listings) - 1 :]
                else:
                    cur_entry = cur_listings[indx : len(cur_listings) - 1]

            try:
                hist_entry = hist_listings[indx : indx + step]
            except IndexError:
                if indx > len(hist_listings):
                    hist_entry = hist_listings[len(hist_listings) - 1 :]
                else:
                    hist_entry = hist_listings[indx : len(hist_listings) - 1]

            embed = UniversalisEmbed(
                cog=self.cog,
                item=self.item,
                world_or_dc=self.xivuser.datacenter,
                cur_listings=cur_entry,
                hist_listings=hist_entry,
            )
            embeds.append(embed)

        self.embeds = embeds
        self.recent_interaction = interaction
        embed: UniversalisEmbed = embeds[0]
        embed.set_footer(text=f"{self.indx + 1} out of {len(embeds)} | Moogles Intuition")
        # await self.view.message.edit(embed=embed, view=view, attachments=[embed.universalis_icon, embed.item_icon])
        await interaction.edit_original_response(embed=embed, view=self, attachments=embed.attachments)

    @discord.ui.button(label="By World", style=discord.ButtonStyle.secondary, emoji=RESOURCES.emojis.world_visit, disabled=False, row=0)
    async def world_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "world_callback")

        self.add_item(self.world_select)
        self.undo_callback.disabled = False
        item.disabled = True
        self.recent_interaction = interaction
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.danger, emoji=RESOURCES.emojis.loop_icon, disabled=True, row=4)
    async def undo_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        LOGGER.debug("<%s.%s>", __class__.__name__, "undo_callback")

        self.reset_view()
        await interaction.response.defer()
        get_entries = 50
        step = 10

        await self.item.get_current_marketboard(
            world_or_dc=self.xivuser.datacenter,
            num_listings=get_entries,
            num_history_entries=get_entries,
            item_quality="NQ",
        )

        await self.item.get_icon()
        # Sort the Data how we need.
        cur_listings = []
        hist_listings = []
        if self.item.mb_current is not None:
            cur_listings = sorted(self.item.mb_current.listings, key=lambda x: x.price_per_unit)
            if self.item.mb_current.recent_history is not None:
                hist_listings = sorted(self.item.mb_current.recent_history, key=lambda x: x.timestamp, reverse=True)

        embeds = []
        for indx in range(0, get_entries, step):
            try:
                cur_entry = cur_listings[indx : indx + step]
            except IndexError:
                if indx > len(cur_listings):  # noqa: SIM108
                    cur_entry = cur_listings[len(cur_listings) - 1 :]
                else:
                    cur_entry = cur_listings[indx : len(cur_listings) - 1]

            try:
                hist_entry = hist_listings[indx : indx + step]
            except IndexError:
                if indx > len(hist_listings):
                    hist_entry = hist_listings[len(hist_listings) - 1 :]
                else:
                    hist_entry = hist_listings[indx : len(hist_listings) - 1]

            embed = UniversalisEmbed(
                cog=self.cog,
                item=self.item,
                world_or_dc=self.xivuser.datacenter,
                cur_listings=cur_entry,
                hist_listings=hist_entry,
            )
            embeds.append(embed)

        self.embeds = embeds
        self.recent_interaction = interaction
        embed: UniversalisEmbed = embeds[self.indx]
        embed.set_footer(text=f"{self.indx + 1} out of {len(embeds)} | Moogles Intuition")
        await interaction.edit_original_response(embed=embed, view=self, attachments=embed.attachments)

    def reset_view(self) -> Self:
        LOGGER.debug("<%s.%s>", __class__.__name__, "reset_view")
        super().reset_view()

        self.world_callback.disabled = False
        self.remove_item(self.world_select)
        self.undo_callback.disabled = True

        if self.dispatched_by is None:
            self.undo_callback.disabled = True

        if self.item.can_be_hq is False:
            self.remove_item(self.universalis_callback)

        return self


class CurrencyView(BaseView):
    indx: int

    def __init__(
        self,
        **kwargs: Unpack[ViewParams],
    ) -> None:
        super().__init__(**kwargs)

        if self.embeds is not None and len(self.embeds) > 1:
            self.next_callback.disabled = False
        # if self.embeds[self.indx].item.is_untradable is False:
        #     self.mb_button = GenericButton(style=discord.ButtonStyle.green, label="Marketboard")
        #     self.add_item(self.mb_button)
        # self.components.extend([self.previous_callback, self.next_callback])

    # @discord.ui.button(label="Previous", style=discord.ButtonStyle.blurple, disabled=True)
    # async def previous_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
    #     if self.embeds is None:
    #         await self.reset_view()
    #         return

    #     LOGGER.debug("<%s.%s> | Args: %s", __class__.__name__, "previous_callback", (self.indx, len(self.embeds), len(self.embeds) - 1))
    #     if self.indx != 0:
    #         self.indx -= 1

    #         embed: ItemEmbed | MoogleEmbed = self.embeds[self.indx].set_footer(
    #             text=f"{self.indx + 1} out of {len(self.embeds)} | Moogles Intuition",
    #         )
    #         if isinstance(embed, ItemEmbed):
    #             await interaction.response.edit_message(embed=embed, view=self, attachments=embed.attachments)
    #             return
    #         await interaction.response.edit_message(embed=embed, view=self, attachments=embed.attachments)

    # @discord.ui.button(label="Next", style=discord.ButtonStyle.blurple, disabled=True)
    # async def next_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
    #     await interaction.response.defer()
    #     if self.embeds is None:
    #         await self.reset_view()
    #         return

    #     LOGGER.debug("<%s.%s> | Args: %s", __class__.__name__, "next_callback", (self.indx, len(self.embeds), len(self.embeds) - 1))
    #     self.previous_callback.disabled = False
    #     self.indx += 1
    #     if self.indx == len(self.embeds) - 1:
    #         item.disabled = True

    #     elif self.indx < len(self.embeds) - 1:
    #         embed: ItemEmbed | MoogleEmbed = self.embeds[self.indx].set_footer(
    #             text=f"{self.indx + 1} out of {len(self.embeds)} | Moogles Intuition",
    #         )
    #         if isinstance(embed, ItemEmbed):
    #             await interaction.response.edit_message(embed=embed, view=self, attachments=embed.attachments)
    #             return
    #         await interaction.response.edit_message(embed=embed, view=self, attachments=embed.attachments)

    def reset_view(self) -> Self:
        if self.embeds is not None and len(self.embeds) > 1:
            self.next_callback.disabled = False
        return self
        # self.previous_callback.disabled = True
        # if self.embeds is None and self.recent_interaction is not None:
        #     super().reset_view()
        #     await self.recent_interaction.response.edit_message(view=self)
        #     return

        # if self.embeds is None or self.recent_interaction is None:
        #     return

        # embed: Optional[ItemEmbed | MoogleEmbed] = None
        # if len(self.embeds) > 1:
        #     self.next_callback.disabled = False
        #     self.indx = 0
        #     embed = self.embeds[self.indx].set_footer(text=f"{self.indx + 1} out of {len(self.embeds)} | Moogles Intuition")

        # if isinstance(embed, ItemEmbed):
        #     await self.recent_interaction.edit_original_response(embed=embed, view=self, attachments=embed.attachments)
        #     return

        # await self.recent_interaction.edit_original_response(embed=embed, view=self, attachments=[])


class RecipeView(ItemView):
    def __init__(
        self,
        item: Item,
        **kwargs: Unpack[ViewParams],
    ) -> None:
        super().__init__(item, **kwargs)

        # Removes our `ItemView` components we added so we display only what we need.
        for entry in self.components:
            self.remove_item(entry)

        # This is to satisfy the linter; we are checking this attribute prior to creating this view.
        if item.recipe is None:
            return

        # If we only have ONE job recipe; no need to offer changing Jobs.
        if len(item.recipe) <= 1:
            self.change_job_callback.disabled = True

        else:
            options = [
                discord.SelectOption(label=entry.craft_type.name, value=entry.craft_type.to_abbr())
                for entry in item.recipe
                if entry.craft_type is not None
            ]
            self.job_select = JobSelect(view=self, options=options, row=4)

        self.components.extend([self.change_job_callback, self.crafting_cost_callback])

    @discord.ui.button(
        label="Crafting Cost",
        style=discord.ButtonStyle.secondary,
        emoji=RESOURCES.emojis.glittery_gil_satchel,
        disabled=False,
        row=1,
    )
    async def crafting_cost_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.info("<%s.%s>", __class__.__name__, "crafting_cost_callback")
        await interaction.response.defer(ephemeral=True)
        item.disabled = True
        self.undo_callback.disabled = False

        if self.item is None or self.embeds is None:
            self.reset_view()
            return

        try:
            embed = self.embeds[self.indx]
        except IndexError:
            # I don't care about the exception as it's a built in. I triggered an IndexError here prior and this is purely for catching it.
            # (assuming it happens again)
            LOGGER.error(  # noqa: TRY400
                "<%s.%s> | Index error accessing View Embeds. | Indx: %s | Embeds: %s/%s",
                __class__.__name__,
                "crafting_cost_callback",
                self.indx,
                len(self.embeds),
                self.embeds,
            )
            self.reset_view()
            await interaction.edit_original_response(view=self)
            return

        if isinstance(embed, RecipeEmbed):
            embed = await embed.add_crafting_cost(world_or_dc=self.xivuser.datacenter)
            await interaction.edit_original_response(view=self, embed=embed, attachments=embed.attachments)
        else:
            await interaction.edit_original_response(view=self, embed=embed, attachments=embed.attachments)
        return

    @discord.ui.button(label="Change Job", style=discord.ButtonStyle.secondary, emoji=RESOURCES.emojis.doh_icon, disabled=False, row=1)
    async def change_job_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        LOGGER.debug("<%s.%s>", __class__.__name__, "change_job_callback")
        # item.disabled = True
        self.undo_callback.disabled = False

        self.add_item(self.job_select)
        # await interaction.edit_original_response(view=self)
        await interaction.response.edit_message(view=self)
        # await interaction.response.defer()

    @discord.ui.button(
        label="Ingredients",
        style=discord.ButtonStyle.secondary,
        emoji=RESOURCES.emojis.inventory_icon,
        disabled=False,
        row=1,
    )
    async def ingredients_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "ingredients_callback")
        await interaction.response.defer()

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.danger, emoji=RESOURCES.emojis.loop_icon, disabled=True, row=2)
    async def undo_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "reset_callback")

        self.reset_view()
        await interaction.response.defer()

        item.disabled = True
        embed = RecipeEmbed(cog=self.cog, item=self.item)
        view = RecipeView(
            item=self.item,
            cog=self.cog,
            xivuser=self.xivuser,
            recent_interaction=interaction,
            owner=interaction.user,
            dispatched_by=self,
            embeds=[embed],
            timeout=self._timeout,
        )
        self.recent_interaction = interaction
        await interaction.response.edit_message(embed=embed, view=view, attachments=embed.attachments)

    def reset_view(self) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "reset_view")
        self.crafting_cost_callback.disabled = False
        if self.item is None:
            for entry in self.components:
                if isinstance(entry, discord.ui.Button):
                    entry.disabled = True
            return

        if self.item.recipe is not None and len(self.item.recipe) < 2:
            self.change_job_callback.disabled = True
        else:
            self.change_job_callback.disabled = False


class FishingView(ItemView):
    def __init__(
        self,
        item: Item,
        **kwargs: Unpack[ViewParams],
    ) -> None:
        self.item = item

        super().__init__(item, **kwargs)

        # Removes our `ItemView` components we added so we display only what we need.
        for entry in self.components:
            self.remove_item(entry)

        if item.fishing is not None and item.fishing.angler_data is not None:
            locations: list[discord.SelectOption] = [
                discord.SelectOption(label=str(entry.sub_area_name), value=str(entry.sub_area_name))
                for entry in item.fishing.angler_data
                if entry.sub_area_name is not None
            ]
            self.loc_select = FishingSpotSelect(view=self, options=locations, row=4)

            if len(item.fishing.angler_data) > 1:
                # self.remove_item(self.spots_callback)
                self.spots_callback.disabled = False

        self.components.extend([self.spots_callback])

    @discord.ui.button(label="Fishing Spots", style=discord.ButtonStyle.green, disabled=True)
    async def spots_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:  # noqa: ARG002
        await interaction.response.defer()
        if self.item.fishing is not None and self.item.fishing.angler_data is not None:
            self.add_item(self.loc_select)
            await interaction.response.edit_message(view=self)
        else:
            self.reset_view()
            await interaction.response.edit_message(view=self)

    def reset_view(self) -> None:
        if self.item.fishing is not None and self.item.fishing.angler_data is not None and len(self.item.fishing.angler_data) > 1:
            self.spots_callback.disabled = False


class ControlPanelView(BaseView):
    def __init__(self, **kwargs: Unpack[ViewParams]) -> None:
        super().__init__(**kwargs)
        self.remove_item(item=self.previous_callback)
        self.remove_item(item=self.next_callback)
        self.remove_item(item=self.reset_callback)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.owner:
            await interaction.response.send_message(
                content="The Garlean Empire knows all, and knows this Interaction isn't yours!",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Rebuild Moogle Data", style=discord.ButtonStyle.red, emoji=RESOURCES.emojis.loop_icon, disabled=False)
    async def rebuild_callback(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        item.disabled = True
        await interaction.response.defer(thinking=True, ephemeral=True)

        stime = time.time()
        await interaction.followup.send(content="Rebuilding Moogle's Intuition data, this may take a while...")

        old_item_count = len(self.cog.moogle._items)  # noqa: SLF001
        self.cog.moogle = await Moogle().build(relocate_data=True)
        self.cog.item_choices = self.cog.build_item_choices()
        msg = (
            f"{interaction.user.mention}\nWe finished rebuilding XIV data in **{int(time.time() - stime)}** seconds. \n > "
            f"- We have **{len(self.cog.moogle._items) - old_item_count}** new Items!."  # noqa: SLF001
        )
        await interaction.edit_original_response(
            content=msg,
        )

    @discord.ui.button(label="Rebuild Item Choice", style=discord.ButtonStyle.red, emoji=RESOURCES.emojis.chest2, disabled=False)
    async def rebuild_item_choices(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        item.disabled = True

        await interaction.response.defer(thinking=True, ephemeral=True)
        self.cog.item_choices = self.cog.build_item_choices()
        await interaction.followup.send(content="Reset Moogle's Intuition Item Choices for application commands.", ephemeral=True)

    @discord.ui.button(
        label="Clear Item Cache",
        style=discord.ButtonStyle.blurple,
        emoji=RESOURCES.emojis.bag_with_exclamation,
        disabled=False,
    )
    async def clear_cache(self, interaction: discord.Interaction, item: discord.ui.Button[Self]) -> None:
        item.disabled = True
        self.cog.moogle._items_cache = {}  # noqa: SLF001
        await interaction.response.send_message(content=f"Reset the **Moogle** {RESOURCES.emojis.inv_icon} Items cache.", ephemeral=True)


class DataCenterSelect(discord.ui.Select):
    _view: UserView

    def __init__(self, view: UserView, **kwargs: Unpack[SelectParams]) -> None:
        super().__init__(**kwargs)
        self._view = view
        # self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "callback")
        if len(self.values) > 0:
            self._view.xivuser.datacenter = DataCenter(int(self.values[0]))
            await self._view.xivuser.update()
            self._view.remove_item(self)
            self._view.reset_view()
            await interaction.response.edit_message(view=self._view)
            # await interaction.response.defer()


class WorldSelect(discord.ui.Select):
    _view: UniversalisView

    def __init__(self, view: UniversalisView, **kwargs: Unpack[SelectParams]) -> None:
        super().__init__(**kwargs)
        self._view = view
        # self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "callback")
        if len(self.values) > 0:
            # This could fail with an improper choice? Maybe?
            world = World(value=int(self.values[0]))
            listing_count = 60
            step = 10

            self._view.world = world
            # We get specific Universalis information for the World, need to decide HQ or LQ
            await self._view.item.get_current_marketboard(world_or_dc=world, num_listings=listing_count, item_quality=self._view.quality)
            # Remove the item from the view.
            self._view.remove_item(self)

            cur_listings = []
            hist_listings = []
            if self._view.item.mb_current is not None:
                cur_listings = sorted(self._view.item.mb_current.listings, key=lambda x: x.price_per_unit)
                if self._view.item.mb_current.recent_history is not None:
                    hist_listings = sorted(self._view.item.mb_current.recent_history, key=lambda x: x.timestamp, reverse=True)

            embeds: list[UniversalisEmbed] = []
            for indx in range(0, listing_count, step):
                try:
                    cur_entry = cur_listings[indx : indx + step]
                except IndexError:
                    if indx > len(cur_listings):
                        cur_entry = cur_listings[len(cur_listings) - 1 :]
                    else:
                        cur_entry = cur_listings[indx : len(cur_listings) - 1]

                try:
                    hist_entry = hist_listings[indx : indx + step]
                except IndexError:
                    if indx > len(hist_listings):
                        hist_entry = hist_listings[len(hist_listings) - 1 :]
                    else:
                        hist_entry = hist_listings[indx : len(hist_listings) - 1]

                embed = UniversalisEmbed(
                    cog=self._view.cog,
                    item=self._view.item,
                    world_or_dc=world,
                    cur_listings=cur_entry,
                    hist_listings=hist_entry,
                )
                embeds.append(embed)

            embeds[0].set_footer(text=f"{self._view.indx + 1} out of {len(embeds)} | Moogles Intuition")
            # Generate our new embed with the new listings populated to the object.
            self._view.embeds = embeds
            await interaction.response.edit_message(view=self._view, embed=embeds[0])


class LanguageSelect(discord.ui.Select):
    _view: UserView

    def __init__(self, view: UserView, **kwargs: Unpack[SelectParams]) -> None:
        super().__init__(**kwargs)
        self._view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "callback")
        if len(self.values) > 0:
            self._view.xivuser.language = Language(self.values[0])
            await self._view.xivuser.update()
            self._view.remove_item(self)
            self._view.reset_view()
            await interaction.response.edit_message(view=self._view)


class JobSelect(discord.ui.Select):
    _view: RecipeView

    def __init__(self, view: RecipeView, **kwargs: Unpack[SelectParams]) -> None:
        super().__init__(**kwargs)
        self._view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "callback")
        if len(self.values) > 0:
            embed = RecipeEmbed(cog=self._view.cog, item=self._view.item, job_recipe=self.values[0])
            self._view.remove_item(self)
            await interaction.response.edit_message(view=self._view, embed=embed)


class FishingSpotSelect(discord.ui.Select):
    _view: FishingView

    def __init__(self, view: FishingView, **kwargs: Unpack[SelectParams]) -> None:
        super().__init__(**kwargs)
        self._view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        LOGGER.debug("<%s.%s>", __class__.__name__, "callback")
        if len(self.values) > 0:
            await interaction.response.defer()
            # TODO: (Compare selection to Location Names; send data to Embed)
            embed = FishingEmbed(cog=self._view.cog, item=self._view.item)
            self._view.remove_item(self)
            await interaction.response.edit_message(view=self._view, embed=embed)


class FFXIVContext(Context):
    ffxiv_user: XIVUser


class XIVMetrics(Metrics):
    item_queries: int


class FFXIV(Cog):
    """Final Fantasy 14 Cog."""

    moogle: Moogle
    item_choices: Optional[list[app_commands.Choice]] = None
    metrics: dict[str, XIVMetrics]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        self.metrics = {__class__.__name__: {"item_queries": 0, "uptime": {"start": datetime.datetime.now(datetime.UTC)}}}

    async def cog_load(self) -> None:
        async with self.bot.pool.acquire() as conn:
            await conn.execute(FFXIVUSER_SETUP_SQL)
            await conn.execute(WATCH_LIST_SETUP_SQL)

        # self.moogle = await Moogle(garlandtools=GarlandToolsAsync()).build()
        self.moogle = await Moogle(session=self.bot.session).build()

        if self.item_choices is None:
            self.item_choices = self.build_item_choices()

    # async def cog_unload(self) -> None:
    #     await self.moogle.clean_up()

    # async def cog_before_invoke(self, ctx):
    #     return await super().cog_before_invoke(ctx)

    async def _disambiguate_items(
        self,
        interaction: discord.Interaction | Context,
        query: str,
        *,
        ephemeral: bool = True,
        limit_results: int = 25,
    ) -> None | Item:
        LOGGER.debug("%s.%s | Disambiguate Items | interaction: %s", __class__.__name__, "_disambiguate_items", interaction)
        try:
            results = self.moogle.get_item(query, limit_results=limit_results)
            if len(results) == 1:
                return results[0]

            multi_view = discord.ui.View()
            select = discord.ui.Select(
                options=[discord.SelectOption(label=entry.name, value=str(entry.id)) for entry in results[:25]],
                custom_id="xiv_item",
            )
            multi_view.add_item(select)

            def get_results(var: discord.Interaction) -> bool:
                if isinstance(interaction, discord.Interaction):
                    return interaction.user == var.user and var.data is not None and var.data.get("custom_id") == "xiv_item"
                return interaction.author == var.user and var.data is not None and var.data.get("custom_id") == "xiv_item"

            if isinstance(interaction, discord.Interaction):
                await interaction.edit_original_response(content=f"Multiple results for {query}:", view=multi_view)
            else:
                await interaction.send(content=f"Multiple results for {query}:", view=multi_view)
            res = await self.bot.wait_for("interaction", timeout=30, check=get_results)
            await res.response.defer()

            return self.moogle.get_item(select.values[0], limit_results=1)

        except MoogleLookupError:
            if isinstance(interaction, discord.Interaction):
                await interaction.followup.send(content=f"Failed to lookup Item: {query}", ephemeral=ephemeral)
            else:
                await interaction.send(content=f"Failed to lookup Item: {query}", ephemeral=ephemeral)
            try:
                if isinstance(interaction, Context):
                    return None
                await interaction.delete_original_response()
            except:  # noqa: E722 # Don't care as I just want to remove the original message, if it fails idgaf.
                return None
            return None
        except TimeoutError:
            if isinstance(interaction, Context):
                return None
            await interaction.followup.send(content="Failed to make a selection in time...", ephemeral=ephemeral)
            return None

    @overload
    def resolve_currency(self, emoji: str | int, *, inline: Literal[True]) -> str: ...

    @overload
    def resolve_currency(self, emoji: str | int, *, inline: Literal[False]) -> discord.Emoji: ...

    def resolve_currency(self, emoji: str | int, *, inline: bool = True) -> discord.Emoji | str:
        """Will attempt to resolve the currency name or ID to an Emoji object.

        This will support partial comparisons also. So "blue_crafters'_scrip_token" == "scrip_token".

        .. note::
            See :class:`GarlandtoolsAsync.Vendor.currency` key for more information.

        Parameters
        ----------
        emoji: :class:`str | int`
            The Emoji name or ID.
        inline: :class:`bool`, optional
            If we want an "inline" emoji str to use or not, by default True.

        Returns
        -------
        :class:`discord.Emoji | str`
            Etheir an :class:`discord.Emoji` object or a Discord inline emoji string when inline is

        """
        # 'glamour_prism_(woodworking)
        if isinstance(emoji, str):
            emoji = emoji.replace(" ", "_")
            # this should handle some other oddities.
            # "steel_amalj'ok"
            emoji = emoji.replace("'", "")
        LOGGER.debug("<%s.%s> | Attempting to resolve Currency. | Args %s", __class__.__name__, "resolve_currency", (emoji, inline))
        for entry in self.bot.app_emojis:
            if (entry.name.lower() == str(emoji).lower() or str(emoji).lower() in entry.name.lower()) or entry.id == emoji:
                LOGGER.debug("Results: %s", entry)
                return entry if inline is False else f"<:{entry.name}:{entry.id}>"
        LOGGER.warning("<%s.%s> | Failed to resolve Currency. | Args %s", __class__.__name__, "resolve_currency", (emoji, inline))
        return RESOURCES.emojis.error_icon if inline is False else f"{RESOURCES.emojis.error_icon}"

    async def count_users(self) -> int:
        async with self.bot.pool.acquire() as conn:
            res = await conn.fetchall("""SELECT COUNT(*) as count FROM ffxivuser""")
            return res[0]["count"]

    async def get_ffxiv_user(self, ctx: FFXIVContext | discord.Interaction) -> XIVUser:
        LOGGER.info("<%s.%s> | Context Type: %s", __class__.__name__, "get_ffxiv_user", type(ctx))
        pool: asqlite.Pool = self.bot.pool
        if isinstance(ctx, discord.Interaction):
            user: discord.User | discord.Member = ctx.user
        else:
            user = ctx.author

        try:
            res: XIVUser = await XIVUser.add_or_get_user(pool=pool, user=user, guild=ctx.guild)
        except sqlite3.DataError:
            LOGGER.exception(
                "<%s.%s> | SQLite DataError. | Context Type: %s | Context Data: %s",
                __class__.__name__,
                "get_ffxiv_user",
                type(ctx),
                (pool, "==?", self.bot.pool, ctx.guild, user),
            )
            res = XIVUser(
                db_pool=pool,
                id=0,
                discord_id=0,
                guild_id=0,
                datacenter=DataCenter.Crystal,
                language=Language.English.value,
            )
        if isinstance(ctx, discord.Interaction):
            ctx.extras["ffxiv_user"] = res
            return res
        ctx.ffxiv_user = res
        return res

    def build_item_choices(self) -> list[app_commands.Choice[str]]:
        temp = []
        for name, value in self.moogle._items_ref.items():  # noqa: SLF001
            if isinstance(name, str):
                # May want to implement localization for other languages.
                # temp.append(app_commands.Choice(name=name.title(), value=value))
                temp.append(app_commands.Choice(name=name, value=value))
        return temp

    async def autocomp_item_list(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:  # noqa: ARG002 # Not using interaction parameter.
        if self.item_choices is None:
            return [entry for entry in self.build_item_choices() if current.lower() in (entry.name.lower() or str(entry.value))][:25]
        return [entry for entry in self.item_choices if current.lower() in (entry.name.lower() or str(entry.value))][:25]

    async def autocomp_worlds(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        xiv_user = await self.get_ffxiv_user(interaction)
        worlds = DataCenterToWorlds.get_worlds(datacenter=xiv_user.datacenter)
        if worlds is not None:
            return [
                app_commands.Choice(name=entry.name, value=str(entry.value))
                for entry in worlds
                if current.lower() in (entry.name.lower() or str(entry.value))
            ][:25]
        return [
            app_commands.Choice(name=entry.name, value=str(entry.value))
            for entry in World
            if current.lower() in (entry.name.lower() or str(entry.value))
        ][:25]

    @commands.is_owner()
    @commands.command(name="xivcp", aliases=["xivcontrol_panel", "xivstats"])
    async def control_panel(self, context: FFXIVContext) -> None:
        await context.typing(ephemeral=True)
        # await interaction.response.defer(thinking=True, ephemeral=True)

        embed = ControlPanelEmbed(cog=self, moogle=self.moogle, colour=context.author.color)
        await embed.add_metrics()
        user: XIVUser = await self.get_ffxiv_user(ctx=context)
        view = ControlPanelView(cog=self, xivuser=user, owner=context.author, embeds=[embed], recent_interaction=None, dispatched_by=None)
        await context.send(embed=embed, files=embed.attachments, view=view, delete_after=self.message_timeout, ephemeral=True)

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
        await context.send(embed=UserEmbed(cog=self, ffxiv_user=context.ffxiv_user, user=context.author))

    @commands.before_invoke(get_ffxiv_user)
    @commands.is_owner()
    @commands.guild_only()
    @commands.command(description="Item search development command")
    @app_commands.autocomplete(query=autocomp_item_list)
    async def xivdev(self, interaction: FFXIVContext, query: str, *, timeout: Optional[float] = 180.0, ephemeral: bool = True) -> None:  # noqa: ASYNC109
        await interaction.typing()

        assert interaction.guild  # noqa: S101 # We are using the `commands.guild_only()` decorator so we know the guild exists.
        item: None | Item = await self._disambiguate_items(interaction, query, ephemeral=ephemeral)
        if item is None:
            return
        # Get the garland tools data and building the Icon.
        await item.get_garlandtools_data()
        # Handles getting the item Icon URL from garland tools.
        await item.get_icon()
        await item.get_current_marketboard()
        user: XIVUser = await self.get_ffxiv_user(ctx=interaction)
        embed = ItemEmbed(cog=self, item=item, color=interaction.author.color)
        # await interaction.followup.send(embed=item_embed, ephemeral=True, files=[patch_icon, item_icon])
        # await interaction.edit_original_response(content="Results:", embed=item_embed )
        # embed.add_currency_info(
        #     data={
        #         "item": item,
        #         "cost": 3,
        #         "currency": Currency(28),
        #     },
        # )

        if timeout == 0:
            timeout = None

        view = ItemView(item=item, cog=self, owner=interaction.author, xivuser=user, embeds=[embed], timeout=timeout, dispatched_by=None)
        embed.set_footer(text=f"{view.indx + 1} out of 1 | Moogles Intuition")
        await interaction.send(content="Results:", embed=embed, view=view, files=embed.attachments, delete_after=self.message_timeout)
        return

    @app_commands.command(name="xivitem", description="Get an FFXIV Item.")
    @app_commands.autocomplete(query=autocomp_item_list)
    async def items(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        timeout: Optional[float] = 180.0,  # noqa: ASYNC109
        ephemeral: bool = True,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)

        item: None | Item = await self._disambiguate_items(interaction, query, ephemeral=ephemeral)
        if item is None:
            return

        # our generic incremet for query tracking metrics.
        self.metrics[__class__.__name__]["item_queries"] = self.metrics[__class__.__name__]["item_queries"] + 1

        # Get the garland tools data and building the Icon.
        await item.get_garlandtools_data()
        # Handles getting the item Icon URL from garland tools.
        await item.get_icon()

        embed = ItemEmbed(cog=self, item=item, color=interaction.user.color)
        # await interaction.followup.send(embed=item_embed, ephemeral=True, files=[patch_icon, item_icon])
        user: XIVUser = await self.get_ffxiv_user(ctx=interaction)
        # await interaction.edit_original_response(content="Results:", embed=item_embed )

        if timeout == 0:
            timeout = None

        view = ItemView(
            item=item,
            cog=self,
            owner=interaction.user,
            xivuser=user,
            embeds=[embed],
            timeout=timeout,
            recent_interaction=interaction,
            dispatched_by=None,
        )
        embed.set_footer(text=f"{view.indx + 1} out of 1 | Moogles Intuition")
        await interaction.edit_original_response(
            content="Results:",
            embed=embed,
            view=view,
            attachments=embed.attachments,
        )
        return

    @app_commands.command(name="xivmb", description="Get Universalis information for Item.")
    @app_commands.describe(world="If a World is supplied, it will overwrite the DataCenter parameter.")
    @app_commands.describe(listing_count="The number of Universalis listings to fetch.")
    @app_commands.autocomplete(query=autocomp_item_list)
    @app_commands.autocomplete(world=autocomp_worlds)
    async def mb_item(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        quality: Literal["HQ", "NQ"] = "NQ",
        datacenter: Optional[DataCenter],
        world: Optional[str],
        listing_count: int = 60,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            results = self.moogle.get_item(query, limit_results=25)
            if len(results) == 1:
                item: Item = results[0]
            else:
                multi_view = discord.ui.View()
                select = discord.ui.Select(
                    options=[discord.SelectOption(label=entry.name, value=str(entry.id)) for entry in results[:25]],
                    custom_id="xiv_item",
                )
                multi_view.add_item(select)

                def get_results(var: discord.Interaction) -> bool:
                    return interaction.user == var.user and var.data is not None and var.data.get("custom_id") == "xiv_item"

                await interaction.edit_original_response(content=f"Multiple results for {query}:", view=multi_view)
                res = await self.bot.wait_for("interaction", timeout=30, check=get_results)
                await res.response.defer()

                item = self.moogle.get_item(select.values[0], limit_results=1)

        except MoogleLookupError:
            await interaction.followup.send(content=f"Failed to lookup Item: {query}", ephemeral=True)
            try:
                await interaction.delete_original_response()
            except:  # noqa: E722 # Don't care as I just want to remove the original message, if it fails idgaf.
                return
            return
        except TimeoutError:
            await interaction.followup.send(content="Failed to make a selection in time...", ephemeral=True)
            return

        # our generic incremet for query tracking metrics.
        self.metrics[__class__.__name__]["item_queries"] = self.metrics[__class__.__name__]["item_queries"] + 1

        await item.get_icon()

        user: XIVUser = await self.get_ffxiv_user(ctx=interaction)
        # We will attempt to use the xiv Users datacenter, otherwise we will use the commands parameters.
        world_or_dc = user.datacenter
        if datacenter is not None:
            world_or_dc = datacenter
        elif world is not None:
            try:
                world_or_dc = World(int(world))
            except ValueError:
                world_or_dc = user.datacenter

        await item.get_current_marketboard(
            num_listings=listing_count,
            num_history_entries=listing_count,
            item_quality=quality,
            world_or_dc=world_or_dc,
        )

        cur_listings = []
        hist_listings = []
        if item.mb_current is not None:
            cur_listings = sorted(item.mb_current.listings, key=lambda x: x.price_per_unit)
            if item.mb_current.recent_history is not None:
                hist_listings = sorted(item.mb_current.recent_history, key=lambda x: x.timestamp, reverse=True)

        embeds: list[UniversalisEmbed] = []
        for indx in range(0, listing_count, 10):
            try:
                cur_entry = cur_listings[indx : indx + 10]
            except IndexError:
                if indx > len(cur_listings):  # noqa: SIM108
                    cur_entry = cur_listings[len(cur_listings) - 1 :]
                else:
                    cur_entry = cur_listings[indx : len(cur_listings) - 1]

            try:
                hist_entry = hist_listings[indx : indx + 10]
            except IndexError:
                if indx > len(hist_listings):
                    hist_entry = hist_listings[len(hist_listings) - 1 :]
                else:
                    hist_entry = hist_listings[indx : len(hist_listings) - 1]

            embed = UniversalisEmbed(
                cog=self,
                item=item,
                world_or_dc=user.datacenter,
                cur_listings=cur_entry,
                hist_listings=hist_entry,
            )
            embeds.append(embed)

        view = UniversalisView(item=item, cog=self, xivuser=user, owner=interaction.user, embeds=embeds, dispatched_by=None)

        await interaction.edit_original_response(
            content="Results:",
            embed=embeds[0],
            view=view,
            attachments=embeds[0].attachments,
        )

    @app_commands.command(name="xivcurrency", description="Get items available to purchase per Currency.")
    async def currency(
        self,
        interaction: discord.Interaction,
        query: Currency,
        *,
        patch: Expansion = Expansion.dawntrail,
        datacenter: DataCenter = DataCenter.Crystal,
        sale_threshold: int = 1,
    ) -> None:
        if query.value == 0:
            await interaction.response.send_message(
                content=f"Unable to lookup {query.name}",
                ephemeral=True,
                delete_after=self.message_timeout,
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send(content="This may take a while to process....", ephemeral=True)

        res: dict[int, CurrencySpender] | None = await self.moogle.currency_spender(
            currency=query,
            num_listings=60,
            patch=patch,
            world_or_dc=datacenter,
        )
        if res is None:
            await interaction.followup.send(content=f"Failed to find results for {query.name}", ephemeral=True)
            return

        embeds: list[ItemEmbed] = []
        for entry in res:
            value: CurrencySpender | None = res.get(entry)
            if value is None:
                continue
            if value["item"].mb_current is not None and value["item"].mb_current.regular_sale_velocity < sale_threshold:
                continue
            await value["item"].get_icon()
            await value["item"].get_garlandtools_data()

            embed = ItemEmbed(item=value["item"], cog=self).add_currency_info(data=value)
            embeds.append(embed)

        try:
            embeds = sorted(embeds, key=lambda x: x.item.mb_current.regular_sale_velocity, reverse=True)  # pyright: ignore[reportOptionalMemberAccess] # I know the data exists because of `currency_spender` getting marketboard data.
        except AttributeError:
            pass

        user: XIVUser = await self.get_ffxiv_user(ctx=interaction)
        embed: ItemEmbed = embeds[0]
        embed.set_footer(text=f"1 out of {len(embeds)} | Moogles Intuition")
        view = CurrencyView(embeds=embeds, cog=self, xivuser=user, owner=interaction.user, timeout=0, dispatched_by=None)
        await interaction.edit_original_response(
            content="Results: ",
            embed=embed,
            view=view,
            attachments=embed.attachments,
        )

    @app_commands.command(name="xiv_user", description="Update your FFXIV user profile.")
    async def user(self, interaction: discord.Interaction) -> None:
        """Get your FFXIV user information."""
        # Temp user handling.
        user: XIVUser = await self.get_ffxiv_user(ctx=interaction)

        moogle_icon = FFXIVResources().get_moogle_icon()
        embed = UserEmbed(cog=self, ffxiv_user=user, user=interaction.user)
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send(embed=embed, ephemeral=True, files=[moogle_icon])
        view = UserView(
            cog=self,
            xivuser=user,
            owner=interaction.user,
        )
        await interaction.edit_original_response(embed=embed, view=view)


async def setup(bot: Kuma_Kuma) -> None:  # noqa: D103
    await bot.add_cog(FFXIV(bot=bot))
