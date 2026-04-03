# Projeqt-Ayla 🎵

Just another Discord bot built with discord.py, MongoDB, and Lavalink for music streaming.

## ✨ Features

- 🎵 **Music System** - High-quality music playback powered by Lavalink
- 🌐 **i18n Support** - Multi language with smart locale priority
- 🗄️ **MongoDB Integration** - Persistent data storage
- 🔧 **Modular Cog System** - Easy to extend and customize
- 🎛️ **Static Music Channel** - Dedicated channel with persistent embed
- 🎚️ **Interactive Controls** - Button-based player controls
- 📻 **Last.fm Scrobbling** - Automatic track scrobbling for linked accounts

---

## 📋 Requirements

- [Python 3.13+](https://www.python.org/)
- [MongoDB database](https://www.mongodb.com/)
- [Lavalink server](https://github.com/lavalink-devs/Lavalink/releases) (for music features)

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/diamondjirapat/Projeqt-Ayla.git
cd Projeqt-Ayla
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```env
PREFIX=!
DISCORD_TOKEN=your_discord_bot_token_here
MONGODB_URI=mongodb+srv://....
OWNER_IDS=your_owner_id_here
LAVALINK_URI=http://localhost:2333
LAVALINK_PASSWORD=youshallnotpass
```

### 3. Run the Bot


### 🏃Option 1: Windows Batch File (Recommended for Windows)

Simply double-click `run.bat` or run:
```cmd
run.bat
```
---

### 🏃Option 2: Linux/macOS Shell Script

```bash
# Make the script executable (first time only)
chmod +x start.sh

# Run the bot
./start.sh
```
---
### 🏃Option 3: Manual Python Execution

**Windows:**
```cmd
# Create virtual environment (optional but recommended)
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the bot
python bot.py
```

**Linux/macOS:**
```bash
# Create virtual environment (optional but recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the bot
python3 bot.py
```

---

### Option 4: Docker 🐳

**Build and run with Docker:**
```bash
# Build the image
docker build -t projeqt-ayla .

# Run the container
docker run -d --name projeqt-ayla --env-file .env projeqt-ayla
```

**View logs:**
```bash
docker logs -f projeqt-ayla
```

**Stop the bot:**
```bash
docker stop projeqt-ayla
```

---



## 🔧 Adding New Cogs

1. Create a new file in the `cogs/` directory
2. Follow the existing cog structure
3. The bot will automatically load it on startup

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

---

## 🌐 Internationalization (i18n)

### Locale Priority
1. **User locale** - Personal preference
2. **Guild locale** - Server-wide setting
3. **Default locale** - English

### Adding Translations

Add your translations to the locale files in `locales/`:
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
Note: don't forget to edit i18n.py and language.py if you add new language.

---

## ⚙️ Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PREFIX` | ✅ | Command prefix (default: `!`) |
| `DISCORD_TOKEN` | ✅ | Your Discord bot token |
| `MONGODB_URI` | ✅ | MongoDB connection string |
| `OWNER_IDS` | ✅ | Bot owner Discord user IDs |
| `LAVALINK_URI` | ✅ | Lavalink server URI |
| `LAVALINK_PASSWORD` | ✅ | Lavalink server password |
| `LASTFM_API_KEY` | ❌ | Last.fm API key for scrobbling |
| `LASTFM_API_SECRET` | ❌ | Last.fm API secret |
| `BAR_URL` | ❌ | Bar image URL |
| `MUSIC_BANNER_URL` | ❌ | Banner image URL |
| `GITHUB_URL` | ❌ | GitHub repository URL |

---

## 🎵 Music System Features

- **Static Music Channel** - Dedicated channel with persistent embed
- **Interactive Controls** - Play/Pause, Skip, Stop, Loop, Shuffle buttons
- **Auto Message Cleanup** - Keeps the music channel clean
- **Last.fm Scrobbling** - Automatic track scrobbling
- **Multi-language Support** - Full English and Thai translations
- **Rich Player Display** - Progress bar, duration, volume, queue info (WIP)
- **Playlist Support** - Create, save, and share playlists (WIP)
- **AutoPlay** - Automatic song recommendations

---

## 🐛 Troubleshooting

### Bot won't start
- Check if `.env` file exists and is configured correctly
- Verify your Discord token is valid
- Ensure MongoDB is running and accessible

### Music not working
- Verify Lavalink server is running
- Check `LAVALINK_URI` and `LAVALINK_PASSWORD` in `.env`
- Ensure `application.yml` is properly configured

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
