# Discord Translation Bot

A Discord bot powered by **DeepL** and **Turso** that automatically translates channel messages and lets users translate on demand into their preferred language.

## Features

| Feature | How |
| --- | --- |
| **Set your language** | `/setselflanguage` slash command |
| **Check your language** | `/selflanguage` slash command |
| **Auto-translation** | Translates messages sent in configured multi-language channels |
| **Translate via reaction** | Add 🌐 or flag reactions to any message → Bot translates |
| **Translate via popup** | Right-click message → Apps → **Translate** → Private ephemeral message |

## Environment Variables

Ensure the following variables are set in your `.env` file or hosting provider:

```env
DISCORD_TOKEN=your_discord_bot_token
DEEPL_AUTH_KEY=your_deepl_auth_key
TURSO_DATABASE_URL=your_turso_database_url
TURSO_AUTH_TOKEN=your_turso_auth_token
```

## Setup

1. Install requirements:
   ```bash
   pip install discord.py deepl libsql-experimental python-dotenv emoji
   ```

2. Run the bot:
   ```bash
   python bot.py
   ```

3. Sync slash commands:
   Run `!sync` in your Discord server as the bot application owner.
