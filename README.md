# Discord Translation Bot

A Discord bot that auto-detects the language of every message and lets
users translate messages into their own preferred language.

## Features

| Feature | How |
|---|---|
| Set your language | `/setlanguage` slash command |
| Auto language detection | Every message gets a 🌐 reaction |
| Translate via reaction | Click 🌐 → bot DMs you the translation |
| Translate via popup | Right-click message → Apps → **Translate** → ephemeral popup only you see |
| Check your language | `/mylanguage` slash command |

## Important: Discord API Limitation

Discord does **not** allow ephemeral (popup, only-you-see-it) messages
triggered by emoji reactions. That mechanism simply doesn't exist in the
API — ephemeral messages can only come from interactions (slash commands,
context menus, buttons).

This bot provides **two** translation paths so you get the best of both:

1. **Reaction (🌐) → DM** — clicking the reaction sends you a Direct
   Message with the translation. Works on every message automatically.
2. **Context menu → ephemeral popup** — right-click any message,
   select **Apps → Translate**, and you get a true ephemeral popup that
   only you can see. This is the closest to "popup" behavior Discord
   supports.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create your Discord bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, give it a name
3. Go to the **Bot** tab → **Reset Token** → copy the token
4. Under **Privileged Gateway Intents**, enable:
   - **MESSAGE CONTENT INTENT**
   - **SERVER MEMBERS INTENT**

### 3. Set your token

Create a `.env` file in the same folder as `bot.py`:

```
DISCORD_TOKEN=your_bot_token_here
```

Or set an environment variable:

```bash
export DISCORD_TOKEN=your_bot_token_here
```

### 4. Invite the bot to your server

Go to **OAuth2 → URL Generator** in the Developer Portal.
Select these scopes and permissions:

**Scopes:** `bot`, `applications.commands`

**Bot Permissions:**
- View Channels
- Read Message History
- Send Messages
- Add Reactions
- Use Application Commands

Copy the generated URL and open it to invite the bot.

### 5. Run the bot

```bash
python bot.py
```

You should see:
```
Synced 3 application commands.
Logged in as YourBot#1234 (ID: 123456789012345678)
------
```

## Usage

### Set your language
```
/setlanguage language: Japanese
```

### Check your language
```
/mylanguage
```

### Translate a message
- **Click the 🌐 reaction** on any message → you'll get a DM with the translation
- **Right-click** any message → **Apps** → **Translate** → ephemeral popup

## Supported Languages

English, Chinese (Simplified/Traditional), Japanese, Korean, French,
German, Spanish, Portuguese, Russian, Italian, Thai, Vietnamese,
Indonesian, Malay, Turkish, Arabic, Hindi, Dutch, Polish, Swedish,
Danish, Finnish, Czech, Greek, Hebrew, Romanian, Ukrainian

## Storage

User language preferences are stored in a local SQLite database
(`translations.db`). No external database needed.

## Customization

- **Change the emoji**: edit `TRANSLATE_EMOJI` at the top of `bot.py`
- **Add languages**: add entries to the `SUPPORTED_LANGS` dict
- **Swap translation engine**: replace the `translate_text()` function
  with a DeepL or Google Cloud Translation API call — the rest of the
  bot doesn't need changes

## Notes

- Translation uses Google Translate (free, unofficial via `deep-translator`).
  For production use, consider switching to the official
  [DeepL API](https://www.deepl.com/pro-api) or
  [Google Cloud Translation](https://cloud.google.com/translate).
- Language detection (`langdetect`) is unreliable on very short messages
  (under ~3 characters), so the bot skips those.
- The bot only processes messages in servers (guilds), not DMs.
