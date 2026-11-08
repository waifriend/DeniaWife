DeniaWife

<<<<<<< HEAD
A Discord bot powered by **DeepL** and **Turso** that automatically translates channel messages and lets users translate on demand into their preferred language.
=======
Personal Discord translation bot powered by DeepL and Turso (LibSQL).
>>>>>>> 635b4446453330437c5ad1c0de4169043c5cd15b

Setup

<<<<<<< HEAD
| Feature | How |
| --- | --- |
| **Set your language** | `/setselflanguage` slash command |
| **Check your language** | `/selflanguage` slash command |
| **Auto-translation** | Translates messages sent in configured multi-language channels |
| **Translate via reaction** | Add 🌐 or flag reactions to any message → Bot translates |
| **Translate via popup** | Right-click message → Apps → **Translate** → Private ephemeral message |
=======
Install requirements:
>>>>>>> 635b4446453330437c5ad1c0de4169043c5cd15b

pip install discord.py deepl libsql-experimental python-dotenv emoji

<<<<<<< HEAD
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
=======

Create a .env file:

DISCORD_TOKEN=your_token
DEEPL_AUTH_KEY=your_deepl_key
TURSO_DATABASE_URL=your_turso_url
TURSO_AUTH_TOKEN=your_turso_token


Run the bot:

python bot.py


Sync commands in Discord:
Run !sync as the bot owner in your server.

Features & Usage

Flag Reactions: React with a flag (🇺🇸, 🇯🇵, 🇰🇷, 🇹🇼, etc.) to translate a message.

Global Mode: Channel 🌐 reactions for interactive button translations.

Context Menu: Right-click message → Apps → Translate.

Commands:

/setselflanguage <lang> — Set target language.

/selflanguage — View target language.

/setglobalchannel <channel> <true/false> — Toggle globe mode.

/setchannellanguage <channel> <lang> — Auto-translate channel setting.

/translate <channel> <src> <tgt> — Set specific translation rule.
>>>>>>> 635b4446453330437c5ad1c0de4169043c5cd15b
