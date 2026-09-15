# Discord Translation Bot

A Discord bot powered by **Azure AI Translator** and **Turso** that automatically translates channel messages and lets users translate on demand into their preferred language.

## Features

| Feature | How |
|---|---|
| **Set your language** | `/setlanguage` slash command |
| **Check your language** | `/mylanguage` slash command |
| **Auto-translation** | Translates messages sent in configured multi-language channels |
| **Translate via reaction** | Add 🌐 reaction to any message → Bot DMs you the translation |
| **Translate via popup** | Right-click message → Apps → **Translate** → Private ephemeral message |

## Environment Variables

Ensure the following variables are set in your `.env` file or hosting provider (Koyeb/Render):

```env
DISCORD_TOKEN=your_discord_bot_token
AZURE_TRANSLATE_KEY=your_azure_key
AZURE_TRANSLATE_REGION=your_azure_region
TURSO_DATABASE_URL=libsql://your-db.turso.io
TURSO_AUTH_TOKEN=your_turso_token