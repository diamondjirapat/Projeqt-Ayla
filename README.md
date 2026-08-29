# Projeqt-Ayla 🎵

Projeqt-Ayla is a powerful, feature-rich Discord bot designed for music lovers. Built with `discord.py`, `MongoDB`, and `Lavalink`, it provides a seamless, high-quality music streaming experience with advanced playback controls and multi-language support.

---

## ✨ Key Features

### 🎵 Music Excellence
- **High-Quality Audio**: Powered by Lavalink for smooth, low-latency streaming.
- **Interactive Controls**: Play, Pause, Skip, Stop, Loop via easy-to-use buttons.
- **Smart Playback**: Automatic song recommendations (AutoPlay) and persistent music channels.
- **Playlist Support**: Create, save, and share your favorite music playlists (Work in Progress).

### 🌐 Global & Social
- **Multi-language Support**: Smooth experience in both **English** and **Thai**.
- **Last.fm Integration**: Automatically scrobble your tracks to your Last.fm account.

### ⚙️ Robust Backend
- **Persistent Storage**: All user preferences and data are safely stored in MongoDB.
- **Modular Architecture**: A highly extensible "Cog" system, making it easy to add new features.

---



## 🚀 Getting Started

### 📋 Prerequisites
Before running the bot, ensure you have the following installed:
- [Python 3.13+](https://www.python.org/)
- [MongoDB](https://www.mongodb.com/) (For data persistence)
- [Lavalink Server](https://github.com/lavalink-devs/Lavalink/releases) (Required for music playback)
- [Bun](https://bun.sh/) (Required for building the web player frontend)

### 🛠️ Installation & Setup

#### 1. Clone the Repository
```bash
git clone https://github.com/diamondjirapat/Projeqt-Ayla.git
cd Projeqt-Ayla
```

#### 2. Configure Environment
Create a `.env` file in the root directory by copying the example:
```bash
cp .env.example .env
```
Open `.env` and fill in your credentials (Discord Token, MongoDB URI, Lavalink details, etc.).

#### 3. Run the Bot
Choose the method that best suits your environment:

**A. Development Mode (Bot + Frontend Vite Dev Server)**
Runs the bot and launches the frontend dev server with hot reload:
- Windows: `run_dev.bat`
- Linux/macOS: `./run_dev.sh`

**B. Production Mode (Builds Frontend + Runs Bot)**
Installs dependencies, builds the production frontend bundle, and runs the bot:
- Windows: `run_prod.bat`
- Linux/macOS: `./run_prod.sh`

**C. Python (Manual Setup)**
For both Windows and Linux/macOS:
```bash
# Create and activate a virtual environment (Recommended)
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the bot
python bot.py
```

**🐳 Docker (Containerized)**
Perfect for hosting on servers:
```bash
# Build the image
docker build -t projeqt-ayla .

# Run the container
docker run -d --name projeqt-ayla --env-file .env projeqt-ayla
```

---

## ⚙️ Configuration Reference

| Variable | Required | Description |
|-----------|:--------:|-------------|
| `PREFIX` | ✅ | Command prefix (default: `!`) |
| `DISCORD_TOKEN` | ✅ | Your Discord Bot Token |
| `MONGODB_URI` | ✅ | Your MongoDB connection string |
| `OWNER_IDS` | ✅ | Discord User IDs of the bot owners |
| `LAVALINK_URI` | ✅ | Lavalink server URI (e.g., `http://localhost:2333`) |
| `LAVALINK_PASSWORD` | ✅ | Lavalink server password |
| `LASTFM_API_KEY` | ❌ | Last.fm API key for scrobbling |
| `LASTFM_API_SECRET` | ❌ | Last.fm API secret |
| `SESSION_SECRET_KEY` | ✅ with OAuth | Random secret of at least 32 characters used to sign web sessions |
| `WEB_ALLOWED_ORIGINS` | ❌ | Comma-separated development frontend origins allowed by CORS |

---

## 🧑‍💻 For Developers

### Quality Checks

Install the development tools and run the backend checks from the repository root:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m ruff check .
python -m compileall -q bot.py config.py cogs database utils tests
```

Build the Vue frontend with:

```bash
bun --cwd frontend build
```

### 🧩 Adding New Features (Cogs)
The bot uses a modular "Cog" system. To add a new feature:
1. Create a new `.py` file in the `cogs/` directory.
2. Follow the `commands.Cog` structure.
3. The bot will automatically load your new cog on startup.

```python
import discord
from discord.ext import commands
from utils.i18n import i18n

class MyNewFeature(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def hello(self, ctx):
        message = await i18n.t(ctx, 'hello_message')
        await ctx.send(message)

async def setup(bot):
    await bot.add_cog(MyNewFeature(bot))
```

### 🌐 Internationalization (i18n)
Translations are managed via JSON files in the `locales/` folder.

**Locale Priority:**
1. **User locale** (Personal preference)
2. **Guild locale** (Server-wide setting)
3. **Default locale** (English)

**Process:** Add your keys to the relevant language JSON file. Remember to update `i18n.py` and `language.py` if you introduce a brand new language.

---

## ❓ Troubleshooting

**Bot fails to start?**
- Check if your `.env` file is correctly configured.
- Ensure your `DISCORD_TOKEN` is valid.
- Verify that your MongoDB instance is reachable.

**Music not playing?**
- Ensure your Lavalink server is running.
- Double-check `LAVALINK_URI` and `LAVALINK_PASSWORD` in your `.env`.
- Check the `application.yml` configuration in your Lavalink setup.

---

## 📄 License
This project is licensed under the [MIT License](LICENSE).
