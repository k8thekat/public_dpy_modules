from pathlib import Path
from pkgutil import ModuleInfo, iter_modules

# Private Repo Cogs.
__path__.append(Path(__file__).parent.joinpath("private").as_posix())
_ext: list[ModuleInfo] = [module for module in iter_modules(path=__path__, prefix=__name__ + ".") if not module.name.startswith(__name__ + "._")]

EXTENSIONS: list[ModuleInfo] = _ext
