from __future__ import annotations

from enum import Enum, IntEnum
from typing import ClassVar, NamedTuple


class DataCenterEnum(IntEnum):
    Unknown = 0
    Elemental = 1
    Gaia = 2
    Mana = 3
    Aether = 4
    Primal = 5
    Chaos = 6
    Light = 7
    Crystal = 8
    Materia = 9
    Meteor = 10
    Dynamis = 11
    Shadow = 12
    NA_Cloud_DC = 13
    Beta = 99
    Eorzea = 201
    Chocobo = 101
    Moogle = 102
    Fatcat = 103
    Shiba = 104
    UNK = 151


class InventoryLocations(IntEnum):
    """
    Enum for specifying Item Location in relation to the in game world.

    Parameters
    -----------
        PLAYER = 0 |
        BAG = 1 |
        MARKET = 2 |
        PREMIUM_SADDLEBAG_LEFT = 3 |
        PREMIUM_SADDLEBAG_RIGHT = 4 |
        SADDLEBAG_LEFT = 5 |
        SADDLEBAG_RIGHT = 6 |
        FREE_COMPANY = 7 |
        GLAMOUR_CHEST = 8 |
        ARMORY = 9 |
    """

    null = 0
    bag = 1
    market = 2
    premium_saddlebag_left = 3
    premium_saddlebag_right = 4
    saddlebag_left = 5
    saddlebag_right = 6
    free_company = 7
    glamour_chest = 8
    armory = 9
    equipped_gear = 10
    crystals = 11
    currency = 12


class ItemQualityEnum(IntEnum):
    """
    Enum for specifying Item Quality.

    Parameters
    -----------
    NQ = 0 |
    HQ = 1
    """

    NQ = 0
    HQ = 1


class LocalizationEnum(Enum):
    en = "en"
    de = "de"
    ja = "ja"
    fr = "fr"


class SaleTypeEnum(Enum):
    aggregated = 0
    history = 1


class WorldEnum(IntEnum):
    Ravana = 21
    Bismarck = 22
    Asura = 23
    Belias = 24
    Chaos = 25
    Hecatoncheir = 26
    Moomba = 27
    Pandaemonium = 28
    Shinryu = 29
    Unicorn = 30
    Yojimbo = 31
    Zeromus = 32
    Twintania = 33
    Brynhildr = 34
    Famfrit = 35
    Lich = 36
    Mateus = 37
    Shemhazai = 38
    Omega = 39
    Jenova = 40
    Zalera = 41
    Zodiark = 42
    Alexander = 43
    Anima = 44
    Carbuncle = 45
    Fenrir = 46
    Hades = 47
    Ixion = 48
    Kujata = 49
    Typhon = 50
    Ultima = 51
    Valefor = 52
    Exodus = 53
    Faerie = 54
    Lamia = 55
    Phoenix = 56
    Siren = 57
    Garuda = 58
    Ifrit = 59
    Ramuh = 60
    Titan = 61
    Diabolos = 62
    Gilgamesh = 63
    Leviathan = 64
    Midgardsormr = 65
    Odin = 66
    Shiva = 67
    Atomos = 68
    Bahamut = 69
    Chocobo = 70
    Moogle = 71
    Tonberry = 72
    Adamantoise = 73
    Coeurl = 74
    Malboro = 75
    Tiamat = 76
    Ultros = 77
    Behemoth = 78
    Cactuar = 79
    Cerberus = 80
    Goblin = 81
    Mandragora = 82
    Louisoix = 83
    UNK = 84
    Spriggan = 85
    Sephirot = 86
    Sophia = 87
    Zurvan = 88
    Aegis = 90
    Balmung = 91
    Durandal = 92
    Excalibur = 93
    Gungnir = 94
    Hyperion = 95
    Masamune = 96
    Ragnarok = 97
    Ridill = 98
    Sargatanas = 99
    Sagittarius = 400
    Phantom = 401
    Alpha = 402
    Raiden = 403
    Marilith = 404
    Seraph = 405
    Halicarnassus = 406
    Maduin = 407
    Cuchulainn = 408
    Kraken = 409
    Rafflesia = 410
    Golem = 411
    Titania = 412
    Innocence = 413
    Pixie = 414
    Tycoon = 415
    Wyvern = 416
    Lakshmi = 417
    Eden = 418
    Syldra = 419


class DataCenterToWorlds:
    Crystal: list[WorldEnum] = [  # noqa: RUF012
        WorldEnum.Balmung,
        WorldEnum.Brynhildr,
        WorldEnum.Coeurl,
        WorldEnum.Diabolos,
        WorldEnum.Goblin,
        WorldEnum.Malboro,
        WorldEnum.Mateus,
        WorldEnum.Zalera,
    ]
    Aether: list[WorldEnum] = [  # noqa: RUF012
        WorldEnum.Adamantoise,
        WorldEnum.Cactuar,
        WorldEnum.Faerie,
        WorldEnum.Gilgamesh,
        WorldEnum.Jenova,
        WorldEnum.Midgardsormr,
        WorldEnum.Sargatanas,
        WorldEnum.Siren,
    ]

    Dynamis: list[WorldEnum] = [  # noqa: RUF012
        WorldEnum.Cuchulainn,
        WorldEnum.Golem,
        WorldEnum.Halicarnassus,
        WorldEnum.Kraken,
        WorldEnum.Maduin,
        WorldEnum.Marilith,
        WorldEnum.Rafflesia,
        WorldEnum.Seraph,
    ]

    Primal: list[WorldEnum] = [  # noqa: RUF012
        WorldEnum.Behemoth,
        WorldEnum.Excalibur,
        WorldEnum.Exodus,
        WorldEnum.Famfrit,
        WorldEnum.Hyperion,
        WorldEnum.Lamia,
        WorldEnum.Leviathan,
        WorldEnum.Ultros,
    ]
    __data_centers__: ClassVar[list[str]] = ["Crystal", "Aether", "Dynamis", "Primal"]


class JobEnum(Enum):
    gladiator = 1
    pugilist = 2
    marauder = 3
    lancer = 4
    archer = 5
    conjurer = 6
    thaumaturge = 7
    carpenter = 8
    blacksmith = 9
    armorer = 10
    goldsmith = 11
    leatherworker = 12
    weaver = 13
    alchemist = 14
    culinarian = 15
    miner = 16
    botanist = 17
    fisher = 18
    paladin = 19
    monk = 20
    warrior = 21
    dragoon = 22
    bard = 23
    white_mage = 24
    black_mage = 25
    arcanist = 26
    summoner = 27
    scholar = 28
    rogue = 29
    ninja = 30
    machinist = 31
    dark_knight = 32
    astrologian = 33
    samurai = 34
    red_mage = 35
    blue_mage = 36
    gunbreaker = 37
    dancer = 38
    reaper = 39
    sage = 40
    viper = 41
    pictomancer = 42


# todo - find out more of these
class GarlandToolsAPIIconTypeEnum(Enum):
    item = "item"
    achievement = "achievement"


class GarlandToolsAPI_PatchEnum(Enum):
    arr = 1
    hw = 2
    sb = 3
    shb = 4
    ew = 5
    dt = 6
