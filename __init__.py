"""Copyright (C) 2021-2025 Katelynn Cadwallader.

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
from pathlib import Path
from pkgutil import ModuleInfo, iter_modules

# Private Repo Cogs.
__path__.append(Path(__file__).parent.joinpath("private").as_posix())
_ext: list[ModuleInfo] = [
    module for module in iter_modules(path=__path__, prefix=__name__ + ".") if not module.name.startswith(__name__ + "._")
]

EXTENSIONS: list[ModuleInfo] = _ext
