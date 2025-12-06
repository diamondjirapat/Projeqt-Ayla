import discord
from typing import Optional, Union
import logging

logger = logging.getLogger(__name__)

def setup_logging():
    """Setup logging configuration"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('bot.log', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )

# def create_embed(title: str, description: str = None, color: discord.Color = discord.Color.blue()) -> discord.Embed:
#     """Create a standard embed"""
#     embed = discord.Embed(title=title, description=description, color=color)
#     return embed
#
# def format_user_mention(user: Union[discord.User, discord.Member]) -> str:
#     """Format user mention with fallback"""
#     return f"{user.mention} ({user.display_name})"
#
# async def safe_send(channel, content=None, **kwargs):
#     """Safely send a message with error handling"""
#     try:
#         return await channel.send(content, **kwargs)
#     except discord.Forbidden:
#         logger.warning(f"No permission to send message in {channel}")
#     except discord.HTTPException as e:
#         logger.error(f"HTTP error sending message: {e}")
#     except Exception as e:
#         logger.error(f"Unexpected error sending message: {e}")
#     return None
