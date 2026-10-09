import _bootstrap  # noqa: F401
import logging
import pkgutil

import discord
from discord import app_commands
from discord.ext import commands

from sentra.cache import TTLCache
from sentra.config import settings
from sentra.db.engine import Base, engine

log = logging.getLogger("sentra")


class SentraBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=False
            ),
            activity=discord.Activity(type=discord.ActivityType.watching, name="/help"),
        )
        self.cache = TTLCache(ttl=300.0)
        self.start_time = discord.utils.utcnow()
        self.tree.on_error = self._on_tree_error

    async def setup_hook(self) -> None:
        from sentra.db import models
        import sentra.cogs as cogs_pkg

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        for mod in pkgutil.iter_modules(cogs_pkg.__path__):
            if not mod.name.startswith("_"):
                await self.load_extension(f"sentra.cogs.{mod.name}")
                log.info("loaded cog: %s", mod.name)

        if settings.sync_commands:
            try:
                synced = await self.tree.sync()
                log.info("command tree synced (%d commands)", len(synced))
            except Exception as e:
                log.exception("failed to sync command tree: %s", e)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await engine.dispose()

    async def on_ready(self) -> None:
        user = self.user
        log.info("logged in as %s (%s) in %d guild(s)", user, getattr(user, "id", "?"), len(self.guilds))

    @staticmethod
    async def _on_tree_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down — you can use this again in **{error.retry_after:.0f}s**."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You do not have permission to use this command."
        else:
            log.exception("app command error", exc_info=error)
            message = "Something went wrong running that command."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings.validate()
    bot = SentraBot()
    bot.run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()
