import random
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from sentra.db.engine import session_factory
from sentra.db.models import Guild, Member
from sentra.ui import EMOJI, FUN, style, TICK

TRIVIA_BANK = [
    ("Which planet has the most moons?", ["Saturn", "Jupiter", "Neptune", "Mars"], 0),
    ("What year did the first iPhone launch?", ["2005", "2007", "2009", "2010"], 1),
    ("What is the capital of Australia?", ["Sydney", "Melbourne", "Canberra", "Perth"], 2),
    ("Which language has the most native speakers?", ["English", "Hindi", "Spanish", "Mandarin Chinese"], 3),
    ("What does 'HTTP' stand for?", ["HyperText Transfer Protocol", "High Transfer Text Protocol", "HyperTransfer Text Path", "Hosted Text Transfer Protocol"], 0),
    ("Which element has symbol 'Fe'?", ["Fluorine", "Iron", "Francium", "Fermium"], 1),
    ("Largest ocean on Earth?", ["Atlantic", "Indian", "Arctic", "Pacific"], 3),
    ("Who painted 'The Starry Night'?", ["Monet", "Van Gogh", "Dalí", "Picasso"], 1),
    ("How many bits are in a byte?", ["4", "8", "16", "32"], 1),
    ("Which country invented tea?", ["India", "China", "Japan", "England"], 1),
]

WYR_BANK = [
    ("Have unlimited free pizza for life", "Have unlimited free tacos for life"),
    ("Be able to fly", "Be able to teleport"),
    ("Always be 10 minutes late", "Always be 20 minutes early"),
    ("Live without music", "Live without movies"),
    ("Have a pause button for life", "Have a rewind button for life"),
    ("Know the history of every object you touch", "Be able to talk to animals"),
    ("Explore deep space", "Explore the deep ocean"),
    ("Read one book a week forever", "Never read a book again but get perfect recall"),
]

EIGHTBALL = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "Signs point to yes.",
    "Reply hazy — try again.", "Ask again later.", "Better not tell you now.",
    "Don't count on it.", "My reply is no.", "Very doubtful.", "Absolutely not.",
]

CHOICE_LETTERS = ["🇦", "🇧", "🇨", "🇩"]


async def adjust_balance(guild_id: int, user_id: int, delta: int) -> int:
    from sqlalchemy import update as sa_update

    async with session_factory() as session:
        result = await session.execute(
            sa_update(Member)
            .where(Member.guild_id == guild_id, Member.user_id == user_id)
            .values(balance=Member.balance + delta)
        )
        if result.rowcount:  # type: ignore[attr-defined]
            row = await session.execute(
                select(Member.balance).where(Member.guild_id == guild_id, Member.user_id == user_id)
            )
            bal = int(row.scalar_one())
            if bal < 0:
                await session.execute(
                    sa_update(Member)
                    .where(Member.guild_id == guild_id, Member.user_id == user_id)
                    .values(balance=0)
                )
                bal = 0
            await session.commit()
            return bal
        # No row exists — create one
        new_bal = max(delta, 0)
        session.add(Member(guild_id=guild_id, user_id=user_id, balance=new_bal))
        await session.commit()
        return new_bal


async def _get_balance(guild_id: int, user_id: int) -> int:
    async with session_factory() as session:
        member = (
            await session.execute(
                select(Member).where(Member.guild_id == guild_id, Member.user_id == user_id)
            )
        ).scalar_one_or_none()
        return member.balance if member else 0


class TriviaView(discord.ui.View):
    def __init__(self, cog: "FunCog", question_index: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.question_index = question_index

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def a(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_trivia(interaction, self.question_index, 0)

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
    async def b(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_trivia(interaction, self.question_index, 1)

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
    async def c(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_trivia(interaction, self.question_index, 2)

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
    async def d(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_trivia(interaction, self.question_index, 3)


class ConfessionModal(discord.ui.Modal, title="Anonymous confession"):
    body = discord.ui.TextInput(
        label="Your confession",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="Staff will review it before it's posted publicly.",
    )

    def __init__(self, cog: "FunCog") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        config = await self.cog.get_config(interaction.guild_id)
        review_channel = interaction.guild.get_channel(config.get("confession_review_channel") or 0)
        if not isinstance(review_channel, discord.TextChannel):
            await interaction.response.send_message("Confessions aren't set up yet — ask staff to run `/fun confess-channel`.", ephemeral=True)
            return
        token = uuid.uuid4().hex[:8]
        self.cog.pending_confessions[token] = (interaction.guild_id, str(self.body))
        embed = discord.Embed(description=str(self.body), color=discord.Color.dark_grey())
        embed.set_footer(text=f"Pending review · token {token}")
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"fun:conf:approve:{token}"))
        view.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"fun:conf:deny:{token}"))
        try:
            await review_channel.send(embed=embed, view=view)
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't reach the review channel.", ephemeral=True)
            return
        await interaction.response.send_message("<:sentra_check:1546486790779572385> Submitted anonymously for review.", ephemeral=True)


class FunCog(commands.Cog):
    fun = app_commands.Group(name="fun", description="Games and good times", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.trivia_winners: dict[tuple[int, int], int] = {}
        self.pending_confessions: dict[str, tuple[int, str]] = {}
        self.duels: dict[str, dict] = {}

    async def get_config(self, guild_id: int) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            stored = dict((guild.config or {}).get("fun", {})) if guild else {}
        return stored

    async def update_config(self, guild_id: int, mutate) -> None:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
            full = dict(guild.config or {})
            cfg = dict(full.get("fun", {}))
            mutate(cfg)
            full["fun"] = cfg
            guild.config = full
            await session.commit()

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component or interaction.guild is None:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id.startswith("fun:t:"):
            _, _, q_idx, letter = custom_id.split(":")
            await self.handle_trivia(interaction, int(q_idx), CHOICE_LETTERS.index(letter))
        elif custom_id.startswith("fun:conf:"):
            await self.handle_confession_review(interaction, custom_id.rsplit(":", 2)[-2], custom_id.rsplit(":", 1)[-1])
        elif custom_id.startswith("fun:duel:"):
            parts = custom_id.split(":")
            await self.handle_duel_button(interaction, parts[2], parts[3] if len(parts) > 4 else None)

    async def handle_trivia(self, interaction: discord.Interaction, q_index: int, choice: int) -> None:
        today = time.gmtime().tm_yday
        key = (interaction.guild_id, today)
        question, options, correct = TRIVIA_BANK[q_index % len(TRIVIA_BANK)]
        if key in self.trivia_winners:
            won_by = self.trivia_winners[key]
            correct_text = f"The answer was **{options[correct]}**. <@{won_by}> already claimed today's prize!"
            color = discord.Color.dark_grey()
            if choice == correct:
                await interaction.response.send_message(f"You're right — but {correct_text}", ephemeral=False)
                return
            await interaction.response.send_message(f"Nope! {correct_text}", ephemeral=False)
            return
        if choice != correct:
            await interaction.response.defer()
            return
        self.trivia_winners[key] = interaction.user.id
        await adjust_balance(interaction.guild_id, interaction.user.id, 75)
        embed = discord.Embed(
            description=f"🎉 <@{interaction.user.id}> answered **{question}**\nCorrect answer: **{options[correct]}** · **+75** coins!",
            color=discord.Color.gold(),
        )
        style(embed, interaction, emoji=EMOJI["fun"])
        await interaction.response.send_message(embed=embed)

    @fun.command(name="daily", description="Today's community mini-game — first right answer wins")
    @app_commands.checks.cooldown(1, 5.0)
    async def daily(self, interaction: discord.Interaction) -> None:
        day_index = time.gmtime().tm_yday
        q_index = day_index % len(TRIVIA_BANK)
        question, options, _ = TRIVIA_BANK[q_index]
        embed = discord.Embed(
            title=f"🗓️ Daily Docket — Day {day_index}",
            description=f"**{question}**\n\n" + "\n".join(f"{l} {o}" for l, o in zip(CHOICE_LETTERS, options)),
            color=FUN,
        )
        embed.set_footer(text="First correct answer wins 75 coins!")
        style(embed, interaction, emoji=EMOJI["fun"])
        await interaction.response.send_message(embed=embed, view=TriviaView(self, q_index))

    @fun.command(name="duel", description="Challenge someone to rock-paper-scissors for coins")
    @app_commands.checks.cooldown(1, 15.0)
    @app_commands.describe(opponent="Who dares face you", wager="Coins at stake (both pay in)")
    async def duel(self, interaction: discord.Interaction, opponent: discord.Member, wager: app_commands.Range[int, 5, 500] = 25) -> None:
        if opponent.bot or opponent.id == interaction.user.id:
            await interaction.response.send_message("Pick a worthy human opponent.", ephemeral=True)
            return
        # Balance check — both players must be able to cover the wager
        challenger_bal = await _get_balance(interaction.guild_id, interaction.user.id)
        opponent_bal = await _get_balance(interaction.guild_id, opponent.id)
        if challenger_bal < wager:
            await interaction.response.send_message(f"You only have **{challenger_bal}** coins — you need at least **{wager}** to duel.", ephemeral=True)
            return
        if opponent_bal < wager:
            await interaction.response.send_message(f"{opponent.mention} only has **{opponent_bal}** coins — they can't cover that wager.", ephemeral=True)
            return
        token = uuid.uuid4().hex[:6]
        self.duels[token] = {
            "guild": interaction.guild_id,
            "players": [interaction.user.id, opponent.id],
            "picks": {},
            "wager": wager,
        }
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Accept ⚔️", style=discord.ButtonStyle.success, custom_id=f"fun:duel:accept:{token}"))
        embed = discord.Embed(
            title="⚔️ Duel challenge",
            description=f"<@{interaction.user.id}> challenges <@{opponent.id}> to RPS!\n**Wager:** {wager} coins each.",
            color=discord.Color.orange(),
        )
        style(embed, interaction, emoji=EMOJI["fun"])
        await interaction.response.send_message(content=f"<@{opponent.id}>", embed=embed, view=view)

    async def handle_duel_button(self, interaction: discord.Interaction, token: str, pick: str | None) -> None:
        duel = self.duels.get(token)
        if not duel:
            await interaction.response.send_message("This duel expired.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid not in duel["players"]:
            await interaction.response.send_message("Not your duel!", ephemeral=True)
            return
        if pick is None:
            view = discord.ui.View(timeout=None)
            for letter, emoji in (("rock", "🪨"), ("paper", "📄"), ("scissors", "✂️")):
                view.add_item(discord.ui.Button(label=letter.title(), emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"fun:duel:pick:{token}:{letter}"))
            await interaction.response.send_message("Choose your weapon:", view=view, ephemeral=True)
            return
        if uid in duel["picks"]:
            await interaction.response.send_message("You already picked!", ephemeral=True)
            return
        duel["picks"][uid] = pick
        await interaction.response.send_message(f"Weapon locked: **{pick}**", ephemeral=True)
        if len(duel["picks"]) < 2:
            return
        del self.duels[token]
        a, b = duel["players"]
        # Re-check balances at resolution time to prevent exploit
        wager = duel["wager"]
        guild_id = duel["guild"]
        bal_a = await _get_balance(guild_id, a)
        bal_b = await _get_balance(guild_id, b)
        if bal_a < wager or bal_b < wager:
            short = a if bal_a < wager else b
            embed = discord.Embed(description=f"⚔️ Duel cancelled — <@{short}> no longer has enough coins.", color=discord.Color.dark_grey())
            style(embed)
            try:
                await interaction.channel.send(embed=embed)
            except discord.HTTPException:
                pass
            return
        pa, pb = duel["picks"][a], duel["picks"][b]
        beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
        if pa == pb:
            text = f"🤝 Tie! Both played **{pa}** — wagers refunded."
            color = discord.Color.dark_grey()
        elif beats[pa] == pb:
            await adjust_balance(guild_id, b, -wager)
            new_balance = await adjust_balance(guild_id, a, wager)
            text = f"🏆 <@{a}>'s **{pa}** beats **{pb}**! Wins **{wager * 2}** coins (now {new_balance})."
            color = discord.Color.green()
        else:
            await adjust_balance(guild_id, a, -wager)
            new_balance = await adjust_balance(guild_id, b, wager)
            text = f"🏆 <@{b}>'s **{pb}** beats **{pa}**! Wins **{wager * 2}** coins (now {new_balance})."
            color = discord.Color.green()
        embed = discord.Embed(description=text, color=color)
        style(embed)
        try:
            await interaction.channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @fun.command(name="wyr", description="Would You Rather?")
    @app_commands.checks.cooldown(2, 10.0)
    async def wyr(self, interaction: discord.Interaction) -> None:
        a, b = random.choice(WYR_BANK)
        embed = discord.Embed(
            title="🤔 Would You Rather",
            description=f"**🅰️ {a}**\n\n**🅱️ {b}**",
            color=FUN,
        )
        style(embed, interaction, emoji=EMOJI["fun"])
        await interaction.response.send_message(embed=embed)

    @fun.command(name="8ball", description="Consult the magic 8-ball")
    @app_commands.checks.cooldown(3, 10.0)
    @app_commands.describe(question="What do you want to know?")
    async def eightball(self, interaction: discord.Interaction, question: str) -> None:
        await interaction.response.send_message(
            f"🎱 **{question[:180]}**\n>>> {random.choice(EIGHTBALL)}"
        )

    @fun.command(name="coinflip", description="Flip a coin")
    @app_commands.checks.cooldown(3, 10.0)
    async def coinflip(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(f"🪙 It's **{random.choice(['Heads', 'Tails'])}**!")

    @fun.command(name="dice", description="Roll dice")
    @app_commands.checks.cooldown(3, 10.0)
    @app_commands.describe(sides="Sides per die", count="Number of dice")
    async def dice(self, interaction: discord.Interaction, sides: app_commands.Range[int, 2, 120] = 6, count: app_commands.Range[int, 1, 10] = 1) -> None:
        rolls = [random.randint(1, sides) for _ in range(count)]
        await interaction.response.send_message(f"🎲 `{', '.join(map(str, rolls))}` → total **{sum(rolls)}**")

    @fun.command(name="confess", description="Submit an anonymous confession")
    async def confess(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ConfessionModal(self))

    async def handle_confession_review(self, interaction: discord.Interaction, action: str, token: str) -> None:
        pending = self.pending_confessions.pop(token, None)
        if not pending:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_messages:
            await interaction.response.send_message("Staff only.", ephemeral=True)
            self.pending_confessions[token] = pending
            return
        guild_id, body = pending
        if action == "deny":
            await interaction.response.edit_message(embed=discord.Embed(description="~~denied~~", color=discord.Color.dark_grey()), view=None)
            return
        config = await self.get_config(guild_id)
        publish_channel = interaction.guild.get_channel(config.get("confession_publish_channel") or config.get("confession_review_channel") or 0)
        if isinstance(publish_channel, discord.TextChannel):
            embed = discord.Embed(description=body, color=discord.Color.dark_purple())
            embed.set_footer(text="Anonymous confession")
            embed.timestamp = discord.utils.utcnow()
            try:
                await publish_channel.send(embed=embed)
            except discord.HTTPException:
                pass
        await interaction.response.edit_message(embed=discord.Embed(description="<:sentra_check:1546486790779572385> Published", color=discord.Color.green()), view=None)

    @fun.command(name="confess-channel", description="Set confession channels")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(review="Where submissions land for approval", publish="Where approved confessions post (default: same as review)")
    async def confess_channel(
        self,
        interaction: discord.Interaction,
        review: discord.TextChannel,
        publish: discord.TextChannel | None = None,
    ) -> None:
        await self.update_config(
            interaction.guild_id,
            lambda cfg: cfg.update(
                confession_review_channel=review.id,
                confession_publish_channel=(publish or review).id,
            ),
        )
        await interaction.response.send_message(f"<:sentra_check:1546486790779572385> Review → {review.mention}, Publish → {(publish or review).mention}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FunCog(bot))
