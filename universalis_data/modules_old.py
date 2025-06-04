from __future__ import annotations

import asyncio
import csv
import json
import logging
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Union

import discord
from garlandtools import GarlandTools
from thefuzz import fuzz

from ._enums import (
    DataCenterEnum,
    DataCenterToWorlds,
    GarlandToolsAPI_PatchEnum,
    GarlandToolsAPIIconTypeEnum,
    InventoryLocations,
    ItemQualityEnum,
    JobEnum,
    LocalizationEnum,
    WorldEnum,
)
from ._types import (
    GarlandToolsAPI_FishingLocationsTyped,
    GarlandToolsAPI_ItemKeysTyped,
    GarlandToolsAPI_MobTyped,
    GarlandToolsAPI_NPCTyped,
    UniversalisAPI_CurrentKeysTyped,
)

if TYPE_CHECKING:
    import asqlite
    from aiohttp import ClientResponse
    from requests_cache import CachedResponse, OriginalResponse

    from kuma_kuma import Kuma_Kuma

    from ._types import (
        GarlandToolsAPI_FishingLocationsTyped,
        GarlandToolsAPI_ItemAttrTyped,
        GarlandToolsAPI_ItemCraftIngredientsTyped,
        GarlandToolsAPI_ItemCraftTyped,
        GarlandToolsAPI_ItemFishSpotsTyped,
        GarlandToolsAPI_ItemFishTyped,
        GarlandToolsAPI_ItemIngredientsTyped,
        GarlandToolsAPI_ItemKeysTyped,
        GarlandToolsAPI_ItemPartialsTyped,
        GarlandToolsAPI_ItemTradeShopsTyped,
        GarlandToolsAPI_ItemTyped,
        GarlandToolsAPI_MobTyped,
        GarlandToolsAPI_NPCTyped,
        ItemIDFieldsTyped,
        LocationIDsTyped,
        UniversalisAPI_CurrentKeysTyped,
        UniversalisAPI_CurrentTyped,
        UniversalisAPI_HistoryTyped,
    )

ModulesDataTableAlias = Union["FFXIVWorldDCGroup", "FFXIVWorld", "AllagonToolsInventory", "FFXIVItem"]


class AllagonToolsInventory:
    item_name: str
    type: ItemQualityEnum
    total_quantity: int
    source: str
    location: InventoryLocations

    __slots__: tuple = (
        "item_name",
        "location",
        "source",
        "total_quantity",
        "type",
    )

    def __init__(self, data: list[str]) -> None:
        print("RAW DATA", data)
        for raw_data, keys in zip(data, self.__slots__):
            if keys == "name":
                setattr(self, keys, raw_data)
            elif keys == "type":
                if raw_data.lower() == "hq":
                    setattr(self, keys, ItemQualityEnum(1))
                elif raw_data.lower() == "nq":
                    setattr(self, keys, ItemQualityEnum(0))
            elif keys == "location":
                setattr(self, keys, self.convert_location(loc=raw_data))

    def convert_location(self, loc: str) -> InventoryLocations:
        """
        Converts the Inventory Location from Allagon Tools Column into an IntEnum.
        """

        for count, e in enumerate(iterable=InventoryLocations._member_names_, start=1):
            if loc.lower().startswith(e):
                return InventoryLocations(value=count)
        return InventoryLocations(value=0)


class FFXIVWorld:
    """
    Represents a FFXIV World Server.
    -> "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/World.csv"
    """

    world_id: int
    internal_name: str
    display_name: str
    region: int
    user_type: int
    data_center_group: DataCenterEnum  # This also works as the worldDcRegion ID value.
    is_public: bool

    __slots__: tuple = (  # noqa: RUF023
        "world_id",
        "internal_name",
        "display_name",
        "region",
        "user_type",
        "data_center_group",
        "is_public",
    )

    def __init__(self, data: list[str]) -> None:
        print("RAW DATA", data)
        for raw_data, keys in zip(data, self.__slots__):
            if keys == "data_center_group" and len(raw_data) > 0:
                setattr(self, keys, DataCenterEnum(value=raw_data))
            elif keys == "is_public" and len(raw_data) > 0:
                if raw_data.lower() == "false":
                    setattr(self, keys, False)
                elif raw_data.lower() == "true":
                    setattr(self, keys, True)
            else:
                setattr(self, keys, raw_data)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.world_id == other.world_id

    def __lt__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.world_id < other.world_id


class FFXIVWorldDCGroup:
    """
    Represents an FFXIV World Datacenter Group.
    -> "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/WorldDCGroupType.csv"
    """

    group_id: int
    name: str
    region: int

    __slots__: tuple = (
        "group_id",
        "name",
        "region",
    )

    def __init__(self, data: list[str]) -> None:
        print("RAW DATA", data)
        for raw_data, keys in zip(data, self.__slots__):
            setattr(self, keys, raw_data)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.group_id == other.group_id

    def __lt__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.group_id < other.group_id


class FFXIVItem:
    """
    Represents an FFXIV Item with it's ID and Item Name in different languages.
    -> "https://raw.githubusercontent.com/ffxiv-teamcraft/ffxiv-teamcraft/master/libs/data/src/lib/json/items.json"


    - Houses Universalis API Market Information.
    - Houses Garland Tools API Information.

    """

    # These are from the TeamCraft items.json and are our first entry when building our items.
    item_id: str
    en_name: str
    de_name: str
    ja_name: str
    fr_name: str
    match_val: int

    # Cached attributes
    name: str
    description: str
    jobCategories: str
    repair: int
    equip: int
    sockets: int
    glamourerous: int  # possibly use as a bool
    elvl: int
    jobs: int
    id: int
    patch: GarlandToolsAPI_PatchEnum
    patchCategory: int
    price: int
    ilvl: int
    category: int
    dyecount: bool  # Converting the incoming int into a bool.
    tradeable: bool  # Converting the incoming int into a bool.
    sell_price: int
    rarity: int
    stackSize: int
    icon: int

    # These two keys come from the top level of the GarlandToolsAPI_ItemTyped.
    ingredients: list[GarlandToolsAPI_ItemIngredientsTyped]
    partials: list[GarlandToolsAPI_ItemPartialsTyped]

    # Most Items may or may not have these values below.
    nodes: list[int]
    vendors: list[int]
    tradeShops: list[GarlandToolsAPI_ItemTradeShopsTyped]
    ingredients_of: dict[str, int]  # The Crafted Item ID as the KEY and the VALUE is the number of them to make the Crafted Item.
    levels: list[int]
    desyntheFrom: list[int]
    desynthedTo: list[int]
    alla: dict[str, list[str]]
    supply: dict[str, int]  # The Grand Company Supply Mission. Keys: count: int, xp: int, seals: int
    drops: list[int]
    craft: list[GarlandToolsAPI_ItemCraftTyped]
    ventures: list[int]

    # Weapons/Gear Keys
    attr: GarlandToolsAPI_ItemAttrTyped
    att_hq: GarlandToolsAPI_ItemAttrTyped
    attr_max: GarlandToolsAPI_ItemAttrTyped
    downgrades: list[int]  # The items just below this in terms of ilvl/stats
    models: list[str]
    repair_item: int  # The Garland Tools Item ID to repair the Weapon/Gear
    sharedModels: list
    slot: int  # The Item slot on the Equipment panel
    upgrades: list[int]  # The items just above this in terms of ilvl/stats

    # This belows to Fish type items specifically.
    fish: GarlandToolsAPI_ItemFishTyped
    fishingSpots: list[int]  # This probably belongs to FFXIV and lines up with a Zone ID
    ff14anglerId: int  # This is the ID used to find the fish on FF14 Angler website.
    ff14angler_url: str

    # Universalis API Attributes, these will be tied to __market_cached__.
    universalis_current: UniversalisAPI_CurrentTyped
    universalis_history: UniversalisAPI_HistoryTyped

    # Misc
    garland_link: str
    ffxiv_wiki: str
    _garland_api: GarlandAPIWrapper

    __cached__: bool
    __cached_slots__: tuple[str, ...] = (
        "name",
        "description",
        "jobCategories",
        "repair",
        "equip",
        "sockets",
        "glamourerous",
        "elvl",
        "jobs",
        "id",
        "patch",
        "patchCategory",
        "price",
        "ilvl",
        "category",
        "dyecount",
        "tradeable",
        "sell_price",
        "rarity",
        "stackSize",
        "icon",
        "nodes",
        "vendors",
        "tradeShops",
        "ingredients_of",
        "levels",
        "desyntheFrom",
        "desynthedTo",
        "alla",
        "supply",
        "drops",
        "craft",
        "ventures",
        "attr",
        "att_hq",
        "attr_max",
        "downgrades",
        "models",
        "repair_item",
        "sharedModels",
        "slot",
        "upgrades",
        "fish",
        "fishingSpots",
        "ff14anglerId",
        "ff14angler_url",
    )

    __base_slots__: tuple[str, ...] = (
        "item_id",
        "en_name",
        "de_name",
        "ja_name",
        "fr_name",
        "match_val",
        "__cached__",
        "_garland_api",
    )
    __market__: bool
    __market_slots__: tuple[str, ...] = ("universalis_current", "universalis_history")

    def __init__(self, item_id: str, names: dict, garland_api: GarlandAPIWrapper) -> None:
        # print("FFXIV ITEM DATA", type(item_id), type(names), item_id, names)
        # Default __base_slots__ the fields to UNK for any missing entries in the names dict
        # since we are grabbing localization names for items and they may not have an entry.
        # ------------------------------------------------------------
        # We skip item_id as we are using setattr and init parameters.
        # The remaining slots values are related to GarlandTools API or other Resources so don't overwrite or set them.
        self.logger: logging.Logger = logging.getLogger()
        for entry in self.__base_slots__[:-2]:
            setattr(self, entry, "UNK")

        #
        setattr(self, "_garland_api", garland_api)
        setattr(self, "item_id", item_id)
        setattr(self, "__cached__", False)
        setattr(self, "__market__", False)
        setattr(self, "garland_link", f"https://www.garlandtools.org/db/#item/{item_id}")

        if isinstance(names, dict):
            for key, value in names.items():
                setattr(self, f"{key}_name", value)

        self._no_cache: tuple[str, ...] = (
            "Attribute not cached, call <%s.garland_info_get> first. | Attribute: %s",
            __class__.__name__,
            self.en_name,
        )
        self._no_market: tuple[str, ...] = (
            "Attribute not cached, call <%s.set_current_marketboard> first. | Attribute: %s",
            __class__.__name__,
            self.en_name,
        )

    def __len__(self) -> int:
        return len(self.item_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.item_id == other.item_id

    def __getattr__(self, name: str) -> Any:
        # Prevent accessing unset Garland Tools related attributes during runtime.
        if self.__cached__ is False and name in self.__cached_slots__:
            raise AttributeError(self._no_cache)

        # Prevent accessing unset Universalis related attributes during runtime.
        elif self.__market__ is False and name in self.__market_slots__:
            raise AttributeError(self._no_market)

        try:
            return super().__getattribute__(name)
        except AttributeError:
            return None

    def __hash__(self) -> int:
        return hash(self.item_id)

    def __lt__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.item_id < other.item_id

    def __repr__(self) -> str:
        return " | ".join([f"{e.title()}: {getattr(self, e)}" for e in self.__base_slots__]) + "\n"

    def get_garland_info(self) -> FFXIVItem:
        """
        Retrieves the GarlandTools API Item JSON info and updates our FFXIVItem object.
        """
        to_bool = ["tradeable", "glamourerous"]
        data: GarlandToolsAPI_ItemTyped = self._garland_api.item(item_id=int(self.item_id))
        item: GarlandToolsAPI_ItemKeysTyped | None = data.get("item", None)
        if item is None:
            self.logger.warning("Failed to find any information on Item ID: %s | Item Name: %s", self.item_id, self.en_name)
            return self
        setattr(self, "ingredients", data.get("ingredients", []))
        setattr(self, "partials", data.get("partials", []))
        for key, value in item.items():
            # print("GARLAND INFO TYPES", key, type(value), value)
            if key == "dyecount":
                if isinstance(value, int) and value > 0:
                    setattr(self, key, f"Yes, {value} slots.")
                else:
                    setattr(self, key, "No")

            elif key == "description":
                if isinstance(value, str) and len(value) > 0:
                    value = self.sanitize_html(data=value)
                    setattr(self, key, value)
                else:
                    setattr(self, key, value)

            elif key == "patch":
                # print("FOUND PATCH", isinstance(value, int), value)
                if isinstance(value, float):
                    value = int(value)
                setattr(self, key, GarlandToolsAPI_PatchEnum(value=value))

            elif key in to_bool:
                if isinstance(value, int):
                    setattr(self, key, bool(value))
                else:
                    setattr(self, key, value)
            else:
                setattr(self, key, value)

            self.__cached__ = True
            setattr(self, "ffxiv_wiki", f"https://ffxiv.consolegameswiki.com/wiki/{self.name.replace(' ', '_')}")
        return self

    # todo - This may require knowing the item_type prior or setting an attribute of our self to properly resolve the Icon Type.
    def get_icon(self) -> discord.File:
        """
        Returns a :class:`discord.File` object with the filename set to "item-icon.png".
        """
        res: discord.File | BytesIO = self._garland_api.icon(icon_type=GarlandToolsAPIIconTypeEnum.item, icon_id=int(self.icon), to_file=True)
        if isinstance(res, discord.File):
            return res
        return discord.File(fp=res, filename=f"{self.item_id}.png")

    # todo - Add support for languages?
    def get_hyper_links(self) -> str:
        temp: str = f"- *[Garland]({self.garland_link})"
        if self.__cached__:
            temp += f" | [FFXIV Wiki]({self.ffxiv_wiki})*"
            if self.fish is not None:
                temp += f" | [FF14 Angler]({self.ff14angler_url})"
        return temp

    def get_vendor_information(self) -> str | None:
        vendor_url = "https://www.garlandtools.org/db/#npc/"

        if self.__cached__ is False:
            raise AttributeError(self._no_cache)

        if getattr(self, "vendors") is None:
            return None

        temp: list[str] = []
        len_check: int = 0
        for npc in self.vendors:
            data: GarlandToolsAPI_NPCTyped = self._garland_api.npc(npc_id=npc)
            # Some of the cords are strings and not sure why, so we force to float.
            # Unsure if any data could be non numeric; so we try/except to be safe.
            try:
                cords: list = [float(i) for i in data.get("coords", [])]

            except ValueError:
                self.logger.warning(
                    "We encountered an Error converting NPC cords to Floats inside <gen_vendor_information>. | %s", data.get("coords", [])
                )
                cords = data.get("coords", [])

            var: str = f"- **[{data.get('name', 'UNK')}]({vendor_url}/{data.get('id')})** | {cords}"
            len_check += len(var)
            if len_check > 1024:
                break

            temp.append(var)

        return "\n".join(sorted(temp))

    def get_craft_information(self) -> str | None:
        """
        Generates a str from the list of Craft Information regarding the Self(FFXIVItem) from Garland Tools API.

        """
        # JOB NAME | [INGREDIENT NAME (QTY)]
        if self.__cached__ is False:
            raise AttributeError(self._no_cache)

        if getattr(self, "craft") is None:
            return None

        temp: list[str] = []
        len_check: int = 0
        # print("CRAFTS", self.craft)
        for craftor in self.craft:
            ingredients: list[str] = []
            temp_ingredients: list[GarlandToolsAPI_ItemCraftIngredientsTyped] = craftor.get("ingredients", [])
            # print("TEMP INGREDIENTS", temp_ingredients)
            if len(temp_ingredients) == 0:
                continue

            for i in temp_ingredients:
                item: GarlandToolsAPI_ItemTyped = self._garland_api.item(item_id=i.get("id", 0))
                item_key: GarlandToolsAPI_ItemKeysTyped | None = item.get("item", None)
                if item_key is None:
                    continue
                ingredients.append(f"{i.get('amount')}x {item_key.get('name', 'N/A')}")

            t: str = ", ".join(ingredients)
            var: str = f"**{JobEnum(value=craftor.get('job', 0)).name.title()}**:\n `{t}`"
            len_check += len(var)
            if len_check > 1024:
                break
            # print("VAR", var)
            temp.append(var)

        return "\n".join(sorted(temp))

    # TODO -
    def get_drops(self) -> str | None:
        mob_url = "https://www.garlandtools.org/db/#mob/"

        if self.__cached__ is False:
            raise AttributeError(self._no_cache)

        if getattr(self, "drops") is None:
            return None

        temp: list[str] = []
        len_check: int = 0
        for monster in self.drops:
            data: GarlandToolsAPI_MobTyped = self._garland_api.mob(mob_id=monster)

            var: str = f"Lv. {data['lvl']} | [{data['name']}]({mob_url + str(data['id'])}) | ZoneID: {data['zoneid']}"
            len_check += len(var)
            if len_check > 1024:
                break

            temp.append(var)
        return "\n".join(sorted(temp))

    # TODO - Finish Fishing...
    def get_fish_guide(self) -> str | None:
        # Bait, hookset and tug does not change regardless of the spots location.

        if self.__cached__ is False:
            raise AttributeError(self._no_cache)

        if getattr(self, "fish") is None:
            return None
        setattr(self, "ff14angler_url", f"https://en.ff14angler.com/fish/{self.ff14anglerId}")
        return self.fish.get("guide", "N/A")

    # TODO - Finish Fishing...
    def get_fish_catching(self) -> Any:
        # it appears that the bait is tied to the location.
        # We can also have multiple spot's using different baits.
        # Each entry of "baits" starts off at Bait, Mooch, Mooch
        temp: list[str] = []

        spot_url = "https://wwww.garlandtools.org/db/#fishing/"  # ff14anglerId
        spots: list[GarlandToolsAPI_ItemFishSpotsTyped] = self.fish.get("spots", [])

        if len(spots) > 0:
            # This ID value is used to link to FF14Angler.com
            setattr(self, "ff14anglerId", spots[0].get("ff14angerId", 0))

            temp.append(f"(discord_emoji) {spots[0].get('hookset', 'None')}")
            temp.append("-------")
            # todo - Display [Location name](garland url) | [bait name](garland url) Bait | [bait name] Mooch | etc...
            for entry in spots:
                data: GarlandToolsAPI_FishingLocationsTyped | None = self._garland_api.fishing(spot_id=entry.get("spot", 0))
                if data is None:
                    continue
                temp.append(f"*{data.get('n')}")
                spot_bait: list[Any] = [self._garland_api.item(item_id=i) for i in spots[0].get("baits", [])]
                temp.append(f"{spots[0].get('baits', 'None')}")
            # Here we would generate a list of links with the [Fishing Spot Name](GarlandTools.org) | Location
        pass

    def get_partials(self) -> Any:
        pass

    def get_patch_icon(self) -> discord.File:
        """
        Takes the Patch ID from Garland Tools and converts it into a Enum to retrieve the proper Patch Icon for the item.

        """
        return discord.File(fp=FFXIVResource.resource_path.joinpath(f"{self.patch.name}-icon.png"), filename=f"{self.patch.name}.png")

    def match_val_set(self, value: int) -> FFXIVItem:
        setattr(self, "match_val", value)
        return self

    def match_val_get(self) -> int:
        return self.match_val

    # todo - This may break if they use other highlight colors/etc.
    def sanitize_html(self, data: str) -> str:
        """
        Very basic str replacement for key words.

        Parameters
        -----------
        data: :class:`str`
            The string to replace HTML element's from.

        Returns
        --------
        :class:`str`
            The modified string.
        """
        data = data.replace("<br>", "\n")
        data = data.replace('<span class="highlight-green">', "**", 1)
        data = data.replace('<span class="highlight-green">', "\n**")
        data = data.replace("</span>", "**\n")
        return data

    # May get ALL of this information during this function call.
    def set_marketboard_current(self, data: UniversalisAPI_CurrentTyped) -> FFXIVItem:
        """
        Set the FFXIV Items marketboard data from Universalis API.

        Parameters
        -----------
        data: :class:`UniversalisItemTyped`
            The Universalis API JSON response.
        """
        setattr(self, "universalis_current", data)
        self.__market__ = True
        return self

    def set_marketboard_history(self, data: UniversalisAPI_HistoryTyped) -> FFXIVItem:
        setattr(self, "universalis_history", data)
        self.__market__ = True
        return self

    def get_market_listing_by_world(self, world_id: int) -> Any:
        cur_listings: list[UniversalisAPI_CurrentKeysTyped] = self.universalis_current.get("listings", [])
        for entry in cur_listings:
            if entry.get("worldID", "") == world_id:
                pass
        pass

    def sort_market_listings_by_price(self, price_min: int, price_max: int) -> Any:
        pass


class FFXIVItemWatchList(FFXIVItem):
    universalis_id: int
    item_id: str
    price_min: int
    price_max: int
    last_check: datetime

    __slots__: tuple[str, ...] = ("item_id", "last_check", "price_max", "price_min", "universalis_id")

    def __init__(self, data: FFWatchListDBTyped) -> None:
        for key, value in data.items():
            if key == "last_check" and isinstance(value, float):
                setattr(self, key, datetime.fromtimestamp(value))
            else:
                setattr(self, key, value)

    def __hash__(self) -> int:
        return hash(self.item_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.item_id == other.item_id

    def __lt__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.item_id < other.item_id


class FFXIVUser:
    """
    This class represents our FFXIV User Database table located in the `ffxiv.py` file.

    """

    id: int
    discord_id: int
    guild_id: int
    loc: LocalizationEnum
    watch_list: list[FFXIVItemWatchList]
    home_world: WorldEnum

    __slots__: tuple[str, ...] = ("discord_id", "guild_id", "home_world", "id", "loc", "pool", "watch_list")

    # todo - Overwrite setattr or properties when changed to update the database.
    def __init__(self, data: Any, db_pool: asqlite.Pool) -> None:
        # print("RAW DATA", list(data))
        setattr(self, "_raw", data)
        self.pool: asqlite.Pool = db_pool
        for key in data.keys():  # noqa: SIM118
            if key == "loc" and isinstance(data["loc"], str) and len(data["loc"]) > 0:
                setattr(self, key, LocalizationEnum(value=data["loc"]))

            elif key == "home_world" and isinstance(data["home_world"], int):
                setattr(self, key, WorldEnum(value=data["home_world"]))

            else:
                setattr(self, key, data[key])

    async def get_watch_list_items(self) -> list[FFXIVItemWatchList]:
        """
        Get's the FFXIV User watch list from the Database.
        """
        async with self.pool.acquire() as conn:
            res: list[Any] = await conn.fetchall("""SELECT * FROM watchlist WHERE discord_id = ? and guild_id = ? RETURNING *""")  # type: ignore
            for item in res:
                if item not in self.watch_list:
                    self.watch_list.append(FFXIVItemWatchList(data=item))
        return sorted(self.watch_list)

    # todo - Revisit the datastructure, there may be a better alternative.
    def get_datacenter(self) -> DataCenterEnum:
        """
        Uses the `Self.home_world` value to index DataCenterToWorlds to return the proper DataCenter.

        If it fails to find a match, it will default to DataCenterEnum.Crystal.

        Returns
        --------
        :class:`DataCenterEnum`
            The found DataCenter Enum, defaults to DataCenterEnum.Crystal.
        """

        for data_center in DataCenterToWorlds.__data_centers__:
            if not isinstance(data_center, list):
                return DataCenterEnum.Crystal

            data: list[WorldEnum] = getattr(DataCenterToWorlds, data_center)
            for world in data:
                if self.home_world.name != world.name:
                    continue
                else:
                    return DataCenterEnum(value=getattr(DataCenterEnum, world.name))
        return DataCenterEnum.Crystal

    async def update_watch_list_item(self, item: FFXIVItemWatchList) -> FFXIVItemWatchList:
        async with self.pool.acquire() as conn:
            res: Any = await conn.fetchone(  # type: ignore
                """UPDATE watchlist SET price_min = ? AND price_max = ? AND last_check = ? WHERE universalid_id = ? AND item_id = ? RETURNING *""",
                item.price_min,
                item.price_max,
                item.last_check.timestamp(),
                item.universalis_id,
                item.item_id,
            )

        self.watch_list.remove(item)
        updated_item = FFXIVItemWatchList(data=res)
        self.watch_list.append(updated_item)
        return updated_item

    async def check_watch_list_items(self, cog: Any) -> None:
        pass


class FFXIVResource:
    """
    FFXIV Resources such as Icons, Banners, Emojis, Items, Locations and much more for easier lookup.
    - Houses API objects such as GarlandTools, and Universalis.
    - All the Emoji's are stored on Neko Neko Cafe` Discord Guild.
    -> https://github.com/xivapi/ffxiv-datamining

    Attributes
    -----------
    garland_api: GarlandAPIWrapper
        The Garland API wrapper to make Garland API calls with.
    bot: Kuma_Kuma
        Our Discord Bot object.


    aethernet_icon: discord.File
        A discord.File -> `filename="aethernet-icon.png"`
    item_banner: discord.File
        A discord.File -> `filename="ffxiv-banner.png"`
    universalis_icon: discord.File
        A discord.File -> `filename="uni-con.png"`
    garlandtools_icon: discord.File
        A discord.File -> `filename="gt-icon.png"`

    moogleemoji1: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    moogleemoji2: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    aetherneticon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    gil: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    arr_icon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    hw_icon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    sb_icon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    shb_icon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    ew_icon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    dt_icon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    mbicon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    mbhistoryicon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).
    mbwatchlisticon: int
        The key portion of the :emoji:int(key):. Use `to_inline_emoji()` to generate a emoji link(if needed).

    location_info: list[LocationIDsTyped] | None
        A list of all the FFXIV Map Locations by ID.
    location_url: str
        The URL to fetch the FFXIV Map Locations CSV file.

    items: set[FFXIVItem]
        A set of all the FFXIV Items with Name/ID fields.
    item_url: str
        The URL to fetch the FFXIV Item list file.

    worlds: list[FFXIVWorld]
        A list of all the FFXIV Worlds as objects.
    world_url: str
        The URL to fetch the FFXIV World Info CSV file.
    datacenters: list[FFXIVWorldDCGroup]
        A list of all the FFXIV Datacenters as objects.
    datacenter_url: str
        The URL to fetch the FFXIV Datacenter Info CSV file.

    fishing_spots: list[GarlandToolsAPI_FishingLocationsTyped] | None
        A list of all the known Fishing Locations tied to an ID for easier lookup.

    """

    resource_path: ClassVar[Path] = Path(__file__).parent.joinpath("resources")
    aethernet_icon: ClassVar[discord].File = discord.File(fp=resource_path.joinpath("aethernet-icon.png"), filename="aethernet-icon.png")
    item_banner: ClassVar[discord].File = discord.File(fp=resource_path.joinpath("ffxiv-trail-banner.png"), filename="ffxiv-banner.png")
    universalis_icon: ClassVar[discord].File = discord.File(fp=resource_path.joinpath("universalis-icon.png"), filename="uni-icon.png")
    garlandtools_icon: ClassVar[discord].File = discord.File(fp=resource_path.joinpath("garlandtools-icon.png"), filename="gt-icon.png")

    @classmethod
    def get_banner(cls) -> discord.File:
        return discord.File(fp=cls.resource_path.joinpath("ffxiv-trail-banner.png"), filename="ffxiv-banner.png")

    @classmethod
    def get_universalis_icon(cls) -> discord.File:
        return discord.File(fp=cls.resource_path.joinpath("universalis-icon.png"), filename="uni-icon.png")

    @classmethod
    def get_garlandtools_icon(cls) -> discord.File:
        return discord.File(fp=cls.resource_path.joinpath("garlandtools-icon.png"), filename="gt-icon.png")

    @classmethod
    def get_aethernet_icon(cls) -> discord.File:
        return discord.File(fp=cls.resource_path.joinpath("aethernet-icon.png"), filename="aethernet-icon.png")

    # Misc Emojis
    moogleemoji1: ClassVar[int] = 1360791416007295097
    moogleemoji2: ClassVar[int] = 1360791377679745107
    # moogle3: int = 23
    # moogle4: int = 24
    aetherneticon: ClassVar[int] = 1360791343189983325
    gil: ClassVar[int] = 1359719462550507680

    # Expansion Icons/Emojis
    # These will be related to GarlandToolsAPI_PatchEnum
    arr_icon: ClassVar[int] = 1
    hw_icon: ClassVar[int] = 2
    sb_icon: ClassVar[int] = 3
    shb_icon: ClassVar[int] = 4
    ew_icon: ClassVar[int] = 5
    dt_icon: ClassVar[int] = 6

    # Marketboard/Etc Icons/Emojis
    mbicon: ClassVar[int] = 1360791262910873840
    mbhistoryicon: ClassVar[int] = 1360791284695961731
    mbwatchlisticon: ClassVar[int] = 1360791304841334815

    # Job Icons/Emojis
    # npc_arm_icon: int = 30
    # npc_bs_icon: int = 31
    # npc_carp_icon: int = 32
    # npc_gsm_icon: int = 33
    # npc_ltw_icon: int = 34
    # npc_wvr_icon: int = 35
    # npc_alc_icon: int = 36
    # npc_cul_icon: int = 37
    # npc_min_icon: int = 38
    # npc_bot_icon: int = 39
    # npc_fsh_icon: int = 40

    # Lookup tables/resources
    location_info: list[LocationIDsTyped] | None
    locaton_url: str

    # Other prebuilt Classes/Attributes (if needed?)
    # The list comes from FFXIV TeamCraft github
    items: set[FFXIVItem]
    item_url: str

    # Not currently using these ones
    worlds: list[FFXIVWorld]
    world_url: str
    datacenters: list[FFXIVWorldDCGroup]
    datacenter_url: str

    # This comes from the Garland Tools API
    fishing_spots: list[GarlandToolsAPI_FishingLocationsTyped] | None

    def __init__(self, bot: Kuma_Kuma, garland_api: GarlandAPIWrapper) -> None:
        self.bot: Kuma_Kuma = bot
        self.logger: logging.Logger = logging.getLogger()
        self.logger.name = f"{__class__.__name__}"
        self.garland_api: GarlandAPIWrapper = garland_api

        # XIV Datamining Github URLS
        self.xiv_data_item_url = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/Item.csv"
        self.xiv_data_recipe_level_url = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/RecipeLevelTable.csv"
        self.xiv_data_gathering_url = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/GatheringItem.csv"
        self.xiv_data_recipe_url = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/Recipe.csv"

        # Useful Teamcraft URLs for information.
        self.item_url = "https://raw.githubusercontent.com/ffxiv-teamcraft/ffxiv-teamcraft/master/libs/data/src/lib/json/items.json"
        self.location_url = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/PlaceName.csv"
        self.world_url = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/World.csv"
        self.datacenter_url = "https://raw.githubusercontent.com/xivapi/ffxiv-datamining/refs/heads/master/csv/WorldDCGroupType.csv"

    # todo - Set this up to use a local file first along with downloading it, otherwise use the web url to get a new one.
    async def build_item_list(self) -> None:
        """
        Retrieves the known Item list from the FFXIV TeamCraft Github team.\n
        -> "https://raw.githubusercontent.com/ffxiv-teamcraft/ffxiv-teamcraft/master/libs/data/src/lib/json/items.json"
        """
        self.logger.info("Getting our item.json file from %s", self.item_url)

        data: ClientResponse = await self.bot.session.get(url=self.item_url)
        if data.status != 200:
            self.logger.error("We encountered an error retrieving our items.json data. | URL: %s | Status: %s", self.item_url, data.status)
            raise ConnectionError("We encountered an error retrieving our items.json data.| URL: %s | Status: %s", self.item_url, data.status)

        res: ItemIDFieldsTyped = await data.json(content_type="text/plain")
        temp: set[FFXIVItem] = set()

        for key, value in res.items():
            if isinstance(value, dict):
                temp.add(FFXIVItem(item_id=key, names=value, garland_api=self.garland_api))
                continue
            else:
                self.logger.info("Failed to Parse %s | %s into an FFXIVItem", key, value)

        self.items = temp
        self.logger.info("Universalis: Created %s FFXIV Items", len(self.items))

    @staticmethod
    def to_inline_emoji(emoji: Union[str, int]) -> str | None:
        """
        Converts the emoji provided into a Discord in line str type emoji for usage.

        Parameters
        -----------
        emoji: Union[:class:`str`, :class:`int`]
            Either the emoji name or the ID to lookup..

        Returns
        --------
        :class:`str`
            Discord in line Emoji string.

        Raises
        -------
        :class:`LookupError`
            If the emoji provided does not exist.
        """
        if isinstance(emoji, str):
            emoji = emoji.lower()

        for key, value in FFXIVResource.__dict__.items():
            if (isinstance(emoji, str) and emoji == key and isinstance(value, int)) or (
                isinstance(emoji, int) and emoji == value and isinstance(value, int)
            ):
                return f"<:{key}:{value}>"

        raise LookupError("The Emoji provided does not exist. | %s", emoji)

    def location_id_to_name(self, location_id: int) -> LocationIDsTyped | None:
        """
        Convert a `locaion_id` to a Name, typically used with Fishing and or Gatherable items.
        """
        self.logger.info("")
        if self.location_info is not None:
            for location in self.location_info:
                if location.get("id", 0) == location_id:
                    return location
            return None

    def item_name_to_ids(self, item_name: str, language: str = "en", match: int = 80, limit_results: int = -1) -> list[FFXIVItem]:
        """
        Will attempt to look up the Item Name provided using Fuzzy Logic and will return a single best match.

        Parameters
        -----------
        item_name: :class:`str`
            The FFXIV Item name to search for.
        language: :class:`str`, optional
            The localization to search for the item name by, by default "en".
        match: :class:`int`, optional
            The match score threshold to keep results, by default 80.
        limit_results: :class:`int`, optional
            The number of results to return, by default -1.

        Returns
        --------
        :class:`list[FFXIVItem]`
            A list of FFXIVItems that matched the item_name parameter.

        Raises
        -------
        :exc:`KeyError`
            If unable to find the Item Name provided..
        """
        matches: list[FFXIVItem] = []
        for item in self.items:
            # Will exit early on a direct string match (eg. "honey" as it has ~25+ matches as it's apart of many other item names.)
            if item_name.lower() == getattr(item, f"{language}_name", "UNK").lower():
                self.logger.info("Universalis: Converting Item Name: %s to ID: %s", item_name, item)
                return [item]
            else:
                ratio: int = fuzz.partial_ratio(s1=getattr(item, f"{language}_name", "UNK").lower(), s2=item_name.lower())

            if ratio > match:
                matches.append(item.match_val_set(value=ratio))
        if len(matches) == 0:
            raise KeyError("Unable to find the item name provided. | Item Name: %s", item_name)
        self.logger.info("Universalis: Converting Item Name: %s to IDs: %s", item_name, matches[:limit_results])
        return matches[:limit_results]

    # todo - Finish the logic for this.
    def fishing_spot_id_to_location_name(self, spot_id: int) -> GarlandToolsAPI_FishingLocationsTyped | None:
        # If we already have our Fishing Spots set, let's query.
        if self.fishing_spots is not None:
            for spot in self.fishing_spots:
                if spot_id == spot.get("i"):
                    return spot

            return None

        path: Path = Path(__file__).parent.joinpath("data/garland_fishing.json")
        print(path.as_posix())

        if path.exists() and path.is_file():
            temp: dict[str, GarlandToolsAPI_FishingLocationsTyped] = json.load(path.open())
            data: GarlandToolsAPI_FishingLocationsTyped | None = temp.get("browse")

            # If for some reason the data is unreadable or the key isn't there.. etc etc.
            if data is None:
                self.logger.error("Failed to load our local fishing locations from path: %s", path.as_posix())
            else:
                setattr(self, "fishing_spots", list(data.get("browse", [])))
                return self.fishing(spot_id=spot_id)

        # Clearly our local file is not working, so let's try the GarlandTools API.
        self.logger.warning(
            "Failed to use/find our local garland_fishing.json file, using the Garland Tools API instead.| Path: %s | Exists: %s | Is File: %s",
            path.as_posix(),
            path.exists(),
            path.is_file(),
        )

    # todo - Set this up to use a local file first along with downloading it, otherwise use the web url to get a new one.
    # See get_places_names for CSV parsing
    async def get_fishing_locations(self) -> None:
        # Check local file for `garland_fishing.json`
        # Any failure; use Garland Tools API.
        pass

    # todo - Set this up to use a local file first along with downloading it, otherwise use the web url to get a new one.
    async def get_place_names(self) -> None:
        # Check local file for `PlaceName.csv`
        # Any failure; use aiohttp and get the csv file.

        # Clearly we don't have the location info set.
        data: list[LocationIDsTyped] = []
        path: Path = Path(__file__).parent.joinpath("extensions/universalis_data/data/PlaceName.csv")
        if path.exists() and path.is_file():
            with path.open() as f:
                temp = csv.reader(f)
                for row in temp:
                    if len(row[1]) == 0:
                        continue
                    if len(row[3]) == 0:
                        row[3] = row[1]
                    data.append({"id": int(row[0]), "name": row[1], "alt_name": row[3]})
                setattr(self, "location_info", data)
        pass

    async def parse_csv(self, csv_file: Path) -> Any:
        pass

    async def get_file(self, url: str) -> None:
        res: ClientResponse = await self.bot.session.get(url=url)
        pass


class GarlandAPIWrapper(GarlandTools):
    """
    My own wrapper for the GarlandTools API.
    - Handling the status code checks.
    - Typed Data returns and conversions for some functions.
    """

    def __init__(self, cache_location: Path, cache_expire_after: int = 86400) -> None:
        self.logger: logging.Logger = logging.getLogger()
        if cache_location.exists() and cache_location.is_file():
            raise FileExistsError("You specified a Path to a File, it must be a directory.")
        super().__init__(cache_location=cache_location.as_posix(), cache_expire_after=cache_expire_after)

    # todo - Learn about overloads to predefine type returns
    def icon(self, icon_type: GarlandToolsAPIIconTypeEnum, icon_id: int, to_file: bool = True) -> discord.File | BytesIO:
        res: OriginalResponse | CachedResponse = super().icon(icon_type=icon_type.value, icon_id=icon_id)
        if res.status_code == 200:
            if to_file:
                return discord.File(fp=BytesIO(initial_bytes=res.content), filename="item-icon.png")
            return BytesIO(initial_bytes=res.content)
        self.logger.error(
            "We encountered an error looking up this Icon ID: %s Type: %s for GarlandTools. | Status Code: %s", icon_id, icon_type, res.status_code
        )
        raise ConnectionError(
            "We encountered an error looking up this Icon ID: %s Type: %s for GarlandTools. | Status Code: %s", icon_id, icon_type, res.status_code
        )

    def item(self, item_id: int) -> GarlandToolsAPI_ItemTyped:
        res: OriginalResponse | CachedResponse = super().item(item_id=item_id)
        if res.status_code == 200:
            data: GarlandToolsAPI_ItemTyped = res.json()
            return data
        self.logger.error("We encountered an error looking up this Item ID: %s for GarlandTools. | Status Code: %s", item_id, res.status_code)
        raise ConnectionError("We encountered an error looking up this Item ID: %s for GarlandTools. | Status Code: %s", item_id, res.status_code)

    def npc(self, npc_id: int) -> GarlandToolsAPI_NPCTyped:
        res: OriginalResponse | CachedResponse = super().npc(npc_id=npc_id)
        if res.status_code == 200:
            data: dict[str, GarlandToolsAPI_NPCTyped] = res.json()
            return data["npc"]
        self.logger.error("We encountered an error looking up this NPC ID: %s for GarlandTools. | Status Code: %s", npc_id, res.status_code)
        raise ConnectionError("We encountered an error looking up this NPC ID: %s for GarlandTools. | Status Code: %s", npc_id, res.status_code)

    def mob(self, mob_id: int) -> GarlandToolsAPI_MobTyped:
        res: OriginalResponse | CachedResponse = super().mob(mob_id=mob_id)
        if res.status_code == 200:
            data: dict[str, GarlandToolsAPI_MobTyped] = res.json()
            return data["mob"]
        self.logger.error("We encountered an error looking up this Mob ID: %s for GarlandTools. | Status Code: %s", mob_id, res.status_code)
        raise ConnectionError("We encountered an error looking up this Mob ID: %s for GarlandTools. | Status Code: %s", mob_id, res.status_code)

    def fishing(self) -> GarlandToolsAPI_FishingLocationsTyped:
        res: OriginalResponse | CachedResponse = super().fishing()
        if res.status_code == 200:
            data: dict[str, GarlandToolsAPI_FishingLocationsTyped] = res.json()
            return data["browse"]
        self.logger.error("We encountered an error looking up Fishing Locations for GarlandTools. | Status Code: %s", res.status_code)
        raise ConnectionError("We encountered an error looking up Fishing Locations for GarlandTools. | Status Code: %s", res.status_code)


class UniversalisAPIWrapper:
    """
    My built in class to handle Universalis API queries.
    """

    # Last time an API call was made.
    api_call_time: datetime

    # Current limit is 20 API calls per second.
    max_api_calls: int

    # Universalis API stuff
    base_api_url: str
    api_trim_item_fields: str

    def __init__(self, bot: Kuma_Kuma) -> None:
        self.bot: Kuma_Kuma = bot
        self.logger: logging.Logger = logging.getLogger()
        self.logger.name = __class__.__name__

        # Universalis API
        self.base_api_url = "https://universalis.app/api/v2"
        self.api_call_time = datetime.now()
        self.max_api_calls = 20

        # These are the "Trimmed" API fields for Universalis Market Results.
        self.api_trim_item_fields = "&fields=itemID%2Clistings.quantity%2Clistings.worldName%2Clistings.pricePerUnit%2Clistings.hq%2Clistings.total%2Clistings.tax%2Clistings.retainerName%2Clistings.creatorName%2Clistings.lastReviewTime%2ClastUploadTime"

    async def universalis_call_api(self, url: str) -> UniversalisAPI_CurrentTyped | UniversalisAPI_HistoryTyped:
        cur_time: datetime = datetime.now()
        max_diff = timedelta(milliseconds=1000 / self.max_api_calls)
        if (cur_time - self.api_call_time) < max_diff:
            sleep_time: float = (max_diff - (cur_time - self.api_call_time)).total_seconds() + 0.1
            await asyncio.sleep(delay=sleep_time)

        data: ClientResponse = await self.bot.session.get(url=url)
        if data.status != 200:
            self.logger.error("We encountered an error in Universalis call_api. Status: %s | API: %s", data.status, url)
            raise ConnectionError("We encountered an error in Universalis call_api. Status: %s | API: %s", data.status, url)
        elif data.status == 400:
            self.logger.error(
                "We encountered an error in Universalis call_api due to invalid Parameters. Status: %s | API: %s",
                data.status,
                url,
            )
            raise ConnectionError(
                "We encountered an error in Universalis call_api due to invalid Parameters. Status: %s | API: %s",
                data.status,
                url,
            )
        # 404 - The world/DC or item requested is invalid. When requesting multiple items at once, an invalid item ID will not trigger this.
        # Instead, the returned list of unresolved item IDs will contain the invalid item ID or IDs.
        elif data.status == 404:
            self.logger.error(
                "We encountered an error in Universalis call_api due to invalid World/DC or Item ID. Status: %s | API: %s",
                data.status,
                url,
            )
            raise ConnectionError(
                "We encountered an error in Universalis call_api due to invalid World/DC or Item ID. Status: %s | API: %s",
                data.status,
                url,
            )

        self.api_call_time = datetime.now()
        res: UniversalisAPI_CurrentTyped = await data.json()
        return res

    async def get_universalis_current_mb_data(
        self,
        items: Union[FFXIVItem, list[FFXIVItem], list[str], str],
        world_or_dc: DataCenterEnum | WorldEnum = DataCenterEnum.Crystal,
        num_listings: int = 10,
        num_history_entries: int = 10,
        item_quality: ItemQualityEnum = ItemQualityEnum.NQ,
        trim_item_fields: bool = True,
    ) -> UniversalisAPI_CurrentTyped:
        print("MARKETBOARD DATA", "DATACENTER", world_or_dc.name, "LEN", len(items), type(items))

        if isinstance(items, list):
            for e in items:
                items = ",".join([e.item_id if isinstance(e, FFXIVItem) else e])
        api_url: str = f"{self.base_api_url}/{world_or_dc.name}/{items}?listings={num_listings}&entries={num_history_entries}&hq={item_quality.value}"
        if trim_item_fields:
            api_url += self.api_trim_item_fields

        res: UniversalisAPI_CurrentTyped = await self.universalis_call_api(url=api_url)  # type: ignore - I know the response type because of the URL
        return res

    # todo - need to finish this command, understand overloads to define return types better
    async def marketboard_history_data(
        self,
        items: Union[list[str], str],
        data_center: DataCenterEnum = DataCenterEnum(value=8),
        num_listings: int = 10,
        min_price: int = 0,
        max_price: Union[int, None] = None,
        history: int = 604800000,
    ) -> UniversalisAPI_CurrentTyped | UniversalisAPI_HistoryTyped:
        """

        Universalis Marketboard History Data

        API: https://docs.universalis.app/#market-board-sale-history

        Example URL:
         `https://universalis.app/api/v2/history/Crystal/4698?entriesToReturn=10&statsWithin=604800000&minSalePrice=0&maxSalePrice=9999999999999999`

        Parameters
        -----------
        items: :class:`Union[list[str], str]`
            The Item IDs to look up, limit of 99 entries.
        data_center: :class:`DataCenterEnum`, optional
            _description_, by default DataCenterEnum(value=8).
        num_listings: :class:`int`, optional
            _description_, by default 10.
        min_price: :class:`int`, optional
            _description_, by default 0.
        max_price: :class:`Union[int, None]`, optional
            _description_, by default None.
        history: :class:`int`, optional
            _description_, by default 604800000.

        Raises
        -------
        ValueError:
            If the length of `items` exceeds 99.

        Returns
        --------
        :class:`Any`
            _description_.
        """
        if len(items) > 100:
            raise ValueError(
                "We encountered an error in Universalis.history_marketboard_data(), the array length of items was too long, must be under 100. | %s",
                len(items),
            )

        if isinstance(items, list):
            items = ",".join(items)

        api_url: str = f"{self.base_api_url}/history/{data_center}/{items}?entriesToReturn={num_listings}&statsWithin={history}&minSalePrice={min_price}&maxSalePrice={max_price}"
        res: UniversalisAPI_CurrentTyped | UniversalisAPI_HistoryTyped = await self.universalis_call_api(url=api_url)
        return res
