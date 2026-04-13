"""Shared games cog -- dice, cards, coins, eightball, toss."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from dice_roller.DiceThrower import DiceThrower
from card_picker.Deck import Deck
from card_picker.Card import StandardCard, ShadowCard, TarotCard, UnoCard
from flipper.Tosser import Tosser
from flipper.Casts import Coin, EightBall

from .embeds import make_embed

logger = logging.getLogger('games')


class GamesCog(commands.Cog):
    """Game tools! Custom RNG tools for whatever."""

    def __init__(self, bot):
        self.bot = bot

    async def on_load(self):
        logger.info('Games cog loaded')

    async def on_unload(self):
        logger.info('Games cog unloaded')

    @app_commands.command(name="dice", description="Roll dice using standard notation (e.g., 2d6+3)")
    async def slash_dice(self, interaction: discord.Interaction, roll: str = "1d20"):
        """Roll dice with standard notation."""
        try:
            msg = DiceThrower().throw(roll)
        except Exception as e:
            logger.error("Dice roll error: %s", e)
            await interaction.response.send_message("Error rolling dice. Check your syntax.", ephemeral=True)
            return

        if isinstance(msg, dict):
            msg['roller'] = interaction.user
            if msg['natural'] == msg['modified']:
                msg.pop('modified', None)
            embed = make_embed('Dice Roll', msg)
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Error parsing dice.", ephemeral=True)

    @app_commands.command(name="card", description="Draw cards from a deck")
    @app_commands.choices(deck=[
        app_commands.Choice(name="Standard (52 cards)", value="standard"),
        app_commands.Choice(name="Tarot (78 cards)", value="tarot"),
        app_commands.Choice(name="Shadow (Wraith)", value="shadow"),
        app_commands.Choice(name="Uno", value="uno"),
    ])
    async def slash_card(self, interaction: discord.Interaction, deck: str = "standard", count: int = 1):
        """Draw cards from a deck."""
        card_conv = {
            'standard': StandardCard,
            'shadow': ShadowCard,
            'tarot': TarotCard,
            'uno': UnoCard
        }

        cards = card_conv.get(deck, StandardCard)
        try:
            deck_obj = Deck(cards)
            deck_obj.create()
            deck_obj.shuffle()
            hand = deck_obj.deal(count)
        except Exception as e:
            logger.error("Card deal error: %s", e)
            await interaction.response.send_message("Error dealing cards.", ephemeral=True)
            return

        if isinstance(hand, list):
            title = f'Card Hand ({deck.title()})'
            embed = make_embed(title, hand)
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Error parsing cards.", ephemeral=True)

    @app_commands.command(name="coin", description="Flip a coin")
    async def slash_coin(self, interaction: discord.Interaction, count: int = 1):
        """Flip coins."""
        try:
            tosser = Tosser(Coin)
            result = tosser.toss(count)
        except Exception as e:
            logger.error("Coin flip error: %s", e)
            await interaction.response.send_message("Error flipping coin.", ephemeral=True)
            return

        if isinstance(result, list):
            embed = make_embed('Coin Flip', result)
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Error parsing coin.", ephemeral=True)

    @app_commands.command(name="eightball", description="Ask the magic 8-ball")
    async def slash_eightball(self, interaction: discord.Interaction):
        """Ask the magic 8-ball."""
        try:
            tosser = Tosser(EightBall)
            result = tosser.toss(1)
        except Exception as e:
            logger.error("Eightball error: %s", e)
            await interaction.response.send_message("Error with eightball.", ephemeral=True)
            return

        if isinstance(result, list):
            embed = make_embed('Eightball', result)
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Error parsing eightball.", ephemeral=True)

    @app_commands.command(name="toss", description="Pick randomly from a comma-separated list")
    async def slash_toss(self, interaction: discord.Interaction, items: str, count: int = 1, unique: bool = True):
        """Pick from a list."""
        MAX_ITEMS = 100
        MAX_ITEM_LENGTH = 200
        MAX_COUNT = 50
        MAX_INPUT_LENGTH = 10000

        if len(items) > MAX_INPUT_LENGTH:
            await interaction.response.send_message("Input too long.", ephemeral=True)
            return

        count = max(1, min(count, MAX_COUNT))
        words = [w.strip()[:MAX_ITEM_LENGTH] for w in items.split(',') if w.strip()]
        if not words:
            await interaction.response.send_message("Please provide at least one non-empty item.", ephemeral=True)
            return
        if len(words) > MAX_ITEMS:
            await interaction.response.send_message(f"Too many items. Maximum is {MAX_ITEMS}.", ephemeral=True)
            return

        user_list = lambda: None
        setattr(user_list, 'SIDES', words)

        try:
            tosser = Tosser(user_list)
            result = tosser.toss(count, unique)
        except Exception as e:
            logger.error("Toss error: %s", e)
            await interaction.response.send_message("Error picking from list.", ephemeral=True)
            return

        if isinstance(result, list):
            embed = make_embed('Random Pick', result)
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message("Error parsing list.", ephemeral=True)
