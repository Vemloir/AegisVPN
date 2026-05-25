"""Test configuration: point the app at a throwaway SQLite file and provide
the minimal settings the app requires at import time."""

import os
import pathlib
import tempfile

os.environ.setdefault("BOT_TOKEN", "123:TEST")
os.environ.setdefault("ADMIN_IDS", "[1]")

_DB = pathlib.Path(tempfile.gettempdir()) / "aegis_pytest.db"
_DB.unlink(missing_ok=True)
# DATABASE_URL takes precedence over any .env DB settings (see Settings.db_url).
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
