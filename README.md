DeniaWife

Personal Discord translation bot powered by DeepL and Turso (LibSQL).

Setup

Install requirements:

pip install discord.py deepl libsql-experimental python-dotenv emoji


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
