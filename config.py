import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self) -> None:
        self.token = os.getenv("DISCORD_TOKEN", "").strip()
        self.database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///sentra.db").strip()
        self.sync_commands = os.getenv("SYNC_COMMANDS", "true").lower() in {"true", "1", "yes"}
        raw_premium = os.getenv("PREMIUM_GUILD_IDS", "").replace(" ", "")
        self.premium_guild_ids = {int(x) for x in raw_premium.split(",") if x.isdigit()}
        self.developer = os.getenv("ABOUT_DEVELOPER", "Sentra Team")
        self.support_url = os.getenv("ABOUT_SUPPORT_URL", "")
        self.repo_url = os.getenv("ABOUT_REPO_URL", "")


    def validate(self) -> None:
        if not self.token:
            raise ValueError("DISCORD_TOKEN is required. Set it in the environment or .env file.")
        if not self.database_url.startswith(("sqlite+aiosqlite://", "postgresql+asyncpg://")):
            raise ValueError("DATABASE_URL must use sqlite+aiosqlite:// or postgresql+asyncpg://")


settings = Settings()
