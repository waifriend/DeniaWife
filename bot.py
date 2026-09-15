import asyncio
import os
import aiohttp
import discord
import libsql
from discord import app_commands
from discord.ext import commands

# ── Supported Languages Mapping ─────────────────────────────────────
SUPPORTED_LANGS = {
    "en": "English",
    "ko": "Korean",
    "ru": "Russian",
    "no": "Norwegian",
    "zh-CN": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)",
    "zh-Hans": "Chinese (Simplified)",
    "zh-Hant": "Chinese (Traditional)",
    "ja": "Japanese",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "it": "Italian",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
    "nl": "Dutch",
    "pl": "Polish",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "cs": "Czech",
    "el": "Greek",
    "he": "Hebrew",
    "ro": "Romanian",
    "uk": "Ukrainian",
}


def get_lang_name(code: str) -> str:
    """Helper to cleanly display language names from ISO codes."""
    if not code or code == "Unknown":
        return "Unknown"
    return SUPPORTED_LANGS.get(code, SUPPORTED_LANGS.get(code.lower(), code))


# ── Database Helpers (Turso / libSQL) ────────────────────────────────
def get_turso_conn():
    """Establishes a connection to the remote Turso database."""
    url = os.getenv("TURSO_DATABASE_URL")
    token = os.getenv("TURSO_AUTH_TOKEN")
    if not url or not token:
        return None
    return libsql.connect(database=url, auth_token=token)


def _init_db_sync():
    """Creates tables in Turso if they do not exist."""
    conn = get_turso_conn()
    if not conn:
        print("Warning: Missing TURSO_DATABASE_URL or TURSO_AUTH_TOKEN in environment.")
        return

    with conn:
        # Table for individual user preferences
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_languages (
                user_id    INTEGER PRIMARY KEY,
                lang_code  TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Table for auto-translate channel bindings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_languages (
                channel_id INTEGER PRIMARY KEY,
                lang_code  TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


async def get_user_language(user_id: int) -> str | None:
    def _query():
        conn = get_turso_conn()
        if not conn:
            return None
        with conn:
            cursor = conn.execute(
                "SELECT lang_code FROM user_languages WHERE user_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    return await asyncio.to_thread(_query)


async def set_user_language(user_id: int, lang_code: str):
    def _update():
        conn = get_turso_conn()
        if not conn:
            return
        with conn:
            conn.execute(
                """
                INSERT INTO user_languages (user_id, lang_code)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    lang_code = excluded.lang_code,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, lang_code),
            )
            conn.commit()

    await asyncio.to_thread(_update)


async def get_channel_language(channel_id: int) -> str | None:
    def _query():
        conn = get_turso_conn()
        if not conn:
            return None
        with conn:
            cursor = conn.execute(
                "SELECT lang_code FROM channel_languages WHERE channel_id = ?",
                (channel_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    return await asyncio.to_thread(_query)


async def set_channel_language(channel_id: int, lang_code: str):
    def _update():
        conn = get_turso_conn()
        if not conn:
            return
        with conn:
            conn.execute(
                """
                INSERT INTO channel_languages (channel_id, lang_code)
                VALUES (?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    lang_code = excluded.lang_code,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (channel_id, lang_code),
            )
            conn.commit()

    await asyncio.to_thread(_update)


# ── Azure Translation API Helper ────────────────────────────────────
async def translate_text(text: str, target_lang: str) -> tuple[str, str, int]:
    """
    Translates text using Azure AI Translator.
    Returns: (translated_text, detected_language_code, confidence_percentage)
    """
    key = os.getenv("AZURE_TRANSLATE_KEY")
    region = os.getenv("AZURE_TRANSLATE_REGION")

    if not key or not region:
        return (
            "Translation error: Missing AZURE_TRANSLATE_KEY or AZURE_TRANSLATE_REGION in .env",
            "Unknown",
            0,
        )

    url = f"https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&to={target_lang}"
    headers = {
        "Ocp-Apim-Subscription-Key": key.strip(),
        "Ocp-Apim-Subscription-Region": region.strip(),
        "Content-type": "application/json",
    }
    body = [{"text": text}]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    translated = data[0]["translations"][0]["text"]

                    detected_info = data[0].get("detectedLanguage", {})
                    detected_code = detected_info.get("language", "Unknown")
                    score = detected_info.get("score", 1.0)
                    confidence = int(score * 100)

                    return translated, detected_code, confidence
                else:
                    err_msg = await resp.text()
                    return (
                        f"Azure API Error ({resp.status}): {err_msg}",
                        "Unknown",
                        0,
                    )
    except Exception as e:
        return f"Translation error: {e}", "Unknown", 0


# ── Bot Setup ───────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
TRANSLATE_EMOJI = "🌐"


async def setup_hook():
    await asyncio.to_thread(_init_db_sync)
    print("Database connection initialized successfully.")


bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


# ── Auto-Translation Event ──────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.content.strip():
        await bot.process_commands(message)
        return

    # Look up dynamic target language bound to this channel
    target_lang = await get_channel_language(message.channel.id)

    if target_lang:
        translated, detected_code, confidence = await translate_text(
            message.content, target_lang
        )

        if (
            detected_code != "Unknown"
            and detected_code.lower() != target_lang.lower()
        ):
            target_name = get_lang_name(target_lang)
            detected_name = get_lang_name(detected_code)

            embed = discord.Embed(
                description=f"**{target_name}:**\n{translated}",
                color=0x8B5CF6,
            )

            bot_avatar = (
                bot.user.display_avatar.url if bot.user else None
            )
            embed.set_author(name=bot.user.name if bot.user else "Translator", icon_url=bot_avatar)
            if bot_avatar:
                embed.set_thumbnail(url=bot_avatar)

            embed.set_footer(
                text=f"Original language: {detected_name} ({detected_code}) • Confidence: {confidence}% • Provider: Azure AI"
            )

            await message.reply(embed=embed, mention_author=False)

    await bot.process_commands(message)


# ── Autocomplete Callback ───────────────────────────────────────────
async def language_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    matches = []
    current_lower = current.lower()

    for code, name in SUPPORTED_LANGS.items():
        if current_lower in name.lower() or current_lower in code.lower():
            matches.append(app_commands.Choice(name=name, value=code))
        if len(matches) == 25:
            break

    return matches


# ── Slash Commands ──────────────────────────────────────────────────
@bot.tree.command(
    name="setchannellanguage",
    description="Set auto-translation target language for a specific channel.",
)
@app_commands.describe(
    channel="Select the channel to bind",
    language="Type to search the target language",
)
@app_commands.autocomplete(language=language_autocomplete)
@app_commands.default_permissions(manage_channels=True)
async def setchannellanguage(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    language: str,
):
    lang_code = language.strip()

    if lang_code not in SUPPORTED_LANGS:
        matched_code = next(
            (
                code
                for code, name in SUPPORTED_LANGS.items()
                if name.lower() == lang_code.lower()
            ),
            None,
        )
        if matched_code:
            lang_code = matched_code
        else:
            await interaction.response.send_message(
                f"Invalid language selection: `{language}`. Please select a valid language from the list.",
                ephemeral=True,
            )
            return

    lang_name = SUPPORTED_LANGS[lang_code]
    await set_channel_language(channel.id, lang_code)

    await interaction.response.send_message(
        f"Channel {channel.mention} will now auto-translate non-`{lang_name}` messages into **{lang_name}** (`{lang_code}`).",
        ephemeral=True,
    )


@bot.tree.command(
    name="setlanguage",
    description="Set the language you want messages translated into.",
)
@app_commands.describe(language="Type to search your preferred language")
@app_commands.autocomplete(language=language_autocomplete)
async def setlanguage(interaction: discord.Interaction, language: str):
    lang_code = language.strip()

    if lang_code not in SUPPORTED_LANGS:
        matched_code = next(
            (
                code
                for code, name in SUPPORTED_LANGS.items()
                if name.lower() == lang_code.lower()
            ),
            None,
        )
        if matched_code:
            lang_code = matched_code
        else:
            await interaction.response.send_message(
                f"Invalid language selection: `{language}`. Please select a valid language from the list.",
                ephemeral=True,
            )
            return

    lang_name = SUPPORTED_LANGS[lang_code]
    await set_user_language(interaction.user.id, lang_code)

    await interaction.response.send_message(
        f"Your translation language is now set to **{lang_name}** (`{lang_code}`).\n"
        f"Click the 🌐 reaction on any message, or right-click → Apps → **Translate** to get a translation.",
        ephemeral=True,
    )


@bot.tree.command(
    name="mylanguage",
    description="Check your currently selected translation language.",
)
async def mylanguage(interaction: discord.Interaction):
    lang = await get_user_language(interaction.user.id)
    if lang:
        name = get_lang_name(lang)
        await interaction.response.send_message(
            f"Your language is **{name}** (`{lang}`).", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "You haven't set a language yet. Use `/setlanguage` to pick one.",
            ephemeral=True,
        )


@bot.tree.context_menu(name="Translate")
async def translate_context_menu(
    interaction: discord.Interaction, message: discord.Message
):
    user_lang = await get_user_language(interaction.user.id)
    if not user_lang:
        await interaction.response.send_message(
            "Please use `/setlanguage` first to choose your language.",
            ephemeral=True,
        )
        return

    text = message.content.strip()
    if not text:
        await interaction.response.send_message(
            "This message has no text to translate.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    translated, detected_code, confidence = await translate_text(
        text, user_lang
    )
    target_name = get_lang_name(user_lang)
    detected_name = get_lang_name(detected_code)

    embed = discord.Embed(
        title="🌐 Translation",
        color=0x5865F2,
    )
    embed.add_field(name="Original", value=text[:1024], inline=False)
    embed.add_field(
        name=f"Translated ({target_name})",
        value=translated[:1024],
        inline=False,
    )
    embed.set_footer(
        text=f"Detected: {detected_name} ({detected_code}) • Confidence: {confidence}%"
    )

    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Reaction Event ──────────────────────────────────────────────────
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if str(payload.emoji) != TRANSLATE_EMOJI:
        return
    if payload.member and payload.member.bot:
        return

    user_lang = await get_user_language(payload.user_id)
    if not user_lang:
        try:
            user = bot.get_user(payload.user_id) or await bot.fetch_user(
                payload.user_id
            )
            await user.send(
                "You haven't set a translation language yet.\n"
                "Use `/setlanguage` in the server to pick one."
            )
        except discord.Forbidden:
            pass
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except (discord.NotFound, discord.Forbidden):
            return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.Forbidden):
        return

    text = message.content.strip()
    if not text:
        return

    translated, detected_code, confidence = await translate_text(
        text, user_lang
    )
    target_name = get_lang_name(user_lang)
    detected_name = get_lang_name(detected_code)

    embed = discord.Embed(
        title="🌐 Translation",
        color=0x57F287,
        description=f"**Translated to {target_name}:**\n\n{translated[:3900]}",
    )
    embed.add_field(name="Original", value=text[:1024], inline=False)
    embed.set_footer(
        text=f"Detected: {detected_name} ({detected_code}) • Confidence: {confidence}%"
    )

    try:
        user = bot.get_user(payload.user_id) or await bot.fetch_user(
            payload.user_id
        )
        await user.send(embed=embed)
    except discord.Forbidden:
        print(f"Cannot DM user {payload.user_id} — DMs disabled.")


# ── Guild Command Syncing ──────────────────────────────────────────
@bot.command()
@commands.is_owner()
async def sync(ctx: commands.Context):
    """Sync commands instantly to the current server (Owner only)."""
    msg = await ctx.send("Syncing commands to this server...")
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await msg.edit(
            content=f"Done! Synced {len(synced)} command(s) instantly to this server. Press `Ctrl + R` to refresh."
        )
    except Exception as e:
        await msg.edit(content=f"Sync failed: {e}")


# ── Entry Point & .env Loader ───────────────────────────────────────
if __name__ == "__main__":
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")

    token = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
    if not token:
        print(
            "ERROR: No Discord bot token found. Ensure DISCORD_TOKEN is set in .env"
        )
        raise SystemExit(1)

    bot.run(token)