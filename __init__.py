import pathlib
from pkgutil import ModuleInfo, iter_modules

assert __package__

_ext: list[ModuleInfo] = [
    module for module in iter_modules(path=__path__, prefix=__name__ + ".") if not module.name.startswith(__name__ + "._")
]

# handles loading of private repo cogs.
private_path = pathlib.Path(__package__ + "/private_cogs")
if private_path.exists():
    _ext.extend([
        module
        for module in iter_modules(path=private_path.as_posix(), prefix=__package__ + ".private_cogs.")
        if not module.name.startswith(__name__ + "._")
    ])

EXTENSIONS: list[ModuleInfo] = _ext
