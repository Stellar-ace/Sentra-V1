"""
Bootstraps the project environment:
1. Makes 'sentra' importable as a top-level package pointing to the project root.
2. Fixes Windows / environment aiodns DNS lookup failures ("Could not contact DNS servers")
   by defaulting aiohttp to use Python's built-in ThreadedResolver (OS getaddrinfo).
"""
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
_root_str = str(PROJECT_ROOT)

if _root_str not in sys.path:
    sys.path.insert(0, _root_str)

if "sentra" not in sys.modules:
    sentra_pkg = types.ModuleType("sentra")
    sentra_pkg.__path__ = [_root_str]
    init_file = PROJECT_ROOT / "__init__.py"
    if init_file.exists():
        sentra_pkg.__file__ = str(init_file)
    sentra_pkg.__package__ = "sentra"
    sentra_pkg.__version__ = "1.1.0"
    sys.modules["sentra"] = sentra_pkg
else:
    sentra_pkg = sys.modules["sentra"]
    if not hasattr(sentra_pkg, "__version__"):
        sentra_pkg.__version__ = "1.1.0"
    if hasattr(sentra_pkg, "__path__") and _root_str not in sentra_pkg.__path__:
        sentra_pkg.__path__.append(_root_str)

# Ensure aiohttp uses ThreadedResolver instead of aiodns/pycares which breaks on Windows DNS
try:
    import aiohttp.connector
    import aiohttp.resolver

    aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
except ImportError:
    pass
