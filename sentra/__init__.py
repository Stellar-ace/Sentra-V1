"""
Sentra bot package wrapper.
Extends __path__ with the project root directory so modules and packages
(cogs, db, music, bot, config, cache, ui) resolve under 'sentra.*'.
"""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if _ROOT not in __path__:
    __path__.append(_ROOT)

try:
    import aiohttp.connector
    import aiohttp.resolver

    aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
except ImportError:
    pass

__version__ = "1.1.0"
