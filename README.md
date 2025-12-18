# Projeqt-Ayla

A modular Discord bot built with discord.py 2.6.4 and MongoDB.

## Features

- Modular cog system for easy extension
- MongoDB integration for data persistence
- **i18n support (English & Thai)**
- **Smart locale priority system**
- General commands (ping, info, profile)
- Moderation commands (kick, ban, purge)
- Language management commands
- Comprehensive error handling and logging

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Setup environment variables:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and add your Discord bot token and MongoDB URI.

3. **Run the bot:**
   ```bash
   python bot.py
   ```

## Project Structure

```
├── bot.py              # Main bot file
├── config.py           # Configuration management
├── requirements.txt    # Python dependencies
├── .env.example       # Environment variables template
├── cogs/              # Bot command modules
│   ├── general.py     # General commands
│   ├── moderation.py  # Moderation commands
│   └── language.py    # Language management
├── database/          # Database related files
│   ├── connection.py  # MongoDB connection manager
│   └── models.py      # Database models
├── utils/             # Utility functions
│   ├── helpers.py     # Helper functions
│   └── i18n.py        # Internationalization system
└── locales/           # Translation files
    ├── en.json        # English translations
    └── th.json        # Thai translations
```

## Adding New Cogs

1. Create a new file in the `cogs/` directory
2. Follow the existing cog structure
3. The bot will automatically load it on startup

Example cog structure:
```python
import discord
from discord.ext import commands
from utils.i18n import i18n

class ExampleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command()
    async def example(self, ctx):
        message = await i18n.t(ctx, 'example.message')
        await ctx.send(message)

async def setup(bot):
    await bot.add_cog(ExampleCog(bot))
```

## Internationalization (i18n)

The bot supports multiple languages with a smart locale priority system:

### Locale Priority
1. **User locale** (personal preference)
2. **Guild locale** (server-wide setting)
3. **Default locale** (English)

### Adding Translations

1. Add your translations to the appropriate locale files in `locales/`
2. Use dot notation for nested keys: `"commands.ping.response_title"`
3. Support for string formatting: `"Hello {user}!"`

Example translation structure:
```json
{
  "commands": {
    "ping": {
      "response_title": "🏓 Pong!",
      "response_description": "Latency: {latency}ms"
    }
  }
}
```

## Environment Variables

Required:
- `DISCORD_TOKEN` - Your Discord bot token
- `MONGODB_URI` - MongoDB connection string

Music System:
- `LAVALINK_URI` - Lavalink server host+port
- `LAVALINK_PASSWORD` - Lavalink server password

Optional:
- `PREFIX` - Default command prefix (default: `!`)
- `LASTFM_API_KEY` - Last.fm API key for scrobbling
- `LASTFM_API_SECRET` - Last.fm API secret for scrobbling

## Music System Features (TBD)

- **Static Music Channel**: Dedicated channel with persistent embed that updates in real-time
- **Interactive Controls**: Button-based player controls (play/pause, skip, stop, loop, shuffle)
- **Auto Message Cleanup**: User messages are automatically deleted in static channel to keep it clean
- **Last.fm Scrobbling**: Automatic track scrobbling for linked accounts
- **Multi-language Support**: Full English and Thai translations
- **Rich Player Display**: Progress bar, duration, volume, queue information

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
