import os
import re
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import libsql

# Load environment variables from .env file
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
AZURE_TRANSLATE_KEY = os.getenv("AZURE_TRANSLATE_KEY")
AZURE_TRANSLATE_REGION = os.getenv("AZURE_TRANSLATE_REGION")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

# Supported language choices for autocomplete
LANGUAGES = {
    "English": "en",
    "Korean (한국어)": "ko",
    "Japanese (日本語)": "ja",
    "Spanish (Español)": "es",
    "French (Français)": "fr",
    "German (Deutsch)": "de",
    "Chinese Simplified (简体中文)": "zh-Hans",
    "Chinese Traditional (繁體中文)": "zh-Hant",
    "Italian (Italiano)": "it",
    "Portuguese (Português)": "pt",
    "Russian (Русский)": "ru",
    "Vietnamese (Tiếng Việt)": "vi",
    "Thai (ไทย)": "th",
    "Indonesian (Bahasa Indonesia)": "id",
    "Arabic (العربية)": "ar",
    "Hindi (हिन्दी)": "hi",
}

# Role-based default language mapping (lowercase for case-insensitive matching)
ROLE_LANGUAGES = {
    "korean": "ko",
    "english": "en",
    "chinese": "zh-Hans",
    "russian": "ru",
}

# Regex to detect custom Discord Nitro emotes (<:name:id> or <a:name:id>)
CUSTOM_EMOTE_REGEX = re.compile(r"<a?:[a-zA-Z0-9_]+:\d+>")

# ------------------------------------------------------------------------------
# Database Helpers (Turso / libSQL)
# ------------------------------------------------------------------------------


def get_db_connection():
    return libsql.connect(
        database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN
    )


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_languages (
            channel_id TEXT PRIMARY KEY,
            target_lang TEXT NOT NULL
        )
    """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_languages (
            user_id TEXT PRIMARY KEY,
            target_lang TEXT NOT NULL
        )
    """
    )
    conn.commit()
    conn.close()


def set_channel_language_db(channel_id: int, target_lang: str):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO channel_languages (channel_id, target_lang)
        VALUES (?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET target_lang=excluded.target_lang
    """,
        (str(channel_id), target_lang),
    )
    conn.commit()
    conn.close()


def get_channel_language_db(channel_id: int) -> str | None:
    conn = get_db_connection()
    res = conn.execute(
        "SELECT target_lang FROM channel_languages WHERE channel_id = ?",
        (str(channel_id),),
    )
    row = res.fetchone()
    conn.close()
    return row[0] if row else None


def set_user_language_db(user_id: int, target_lang: str):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO user_languages (user_id, target_lang)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET target_lang=excluded.target_lang
    """,
        (str(user_id), target_lang),
    )
    conn.commit()
    conn.close()


def get_user_language_db(user_id: int) -> str | None:
    conn = get_db_connection()
    res = conn.execute(
        "SELECT target_lang FROM user_languages WHERE user_id = ?",
        (str(user_id),),
    )
    row = res.fetchone()
    conn.close()
    return row[0] if row else None


# ------------------------------------------------------------------------------
# User Language Resolution (DB Preference -> Server Role -> Fallback)
# ------------------------------------------------------------------------------


def get_effective_user_language(
    user: discord.User | discord.Member,
) -> str:
    """Determines target language: /setlanguage > Server Role > English fallback."""
    # 1. Priority 1: Check manual database preference (/setlanguage)
    db_lang = get_user_language_db(user.id)
    if db_lang:
        return db_lang

    # 2. Priority 2: Check server roles if user is a Member in a guild
    if isinstance(user, discord.Member):
        for role in user.roles:
            role_name_lower = role.name.strip().lower()
            if role_name_lower in ROLE_LANGUAGES:
                return ROLE_LANGUAGES[role_name_lower]

    # 3. Priority 3: Fallback default
    return "en"


# ------------------------------------------------------------------------------
# Emote Protection & Translation Logic
# ------------------------------------------------------------------------------


def protect_emotes(text: str) -> tuple[str, dict[str, str]]:
    """Replaces custom Discord emotes with non-translatable placeholders."""
    placeholders = {}

    def replace(match):
        token = f"EMOTETOKEN{len(placeholders)}"
        placeholders[token] = match.group(0)
        return token

    protected_text = CUSTOM_EMOTE_REGEX.sub(replace, text)
    return protected_text, placeholders


def restore_emotes(text: str, placeholders: dict[str, str]) -> str:
    """Restores original custom emotes after translation."""
    for token, original_emote in placeholders.items():
        pattern = re.compile(
            r"\s*".join(re.escape(c) for c in token), re.IGNORECASE
        )
        text = pattern.sub(original_emote, text)
    return text


async def translate_text(text: str, target_lang: str) -> tuple[str, str, int]:
    """Translates text using Azure AI Translator REST API v3.0."""
    if not AZURE_TRANSLATE_KEY or not AZURE_TRANSLATE_REGION:
        return (
            "Translation error: Missing credentials in .env",
            "Unknown",
            0,
        )

    # Protect Nitro emotes before sending payload
    protected_text, placeholders = protect_emotes(text)

    url = f"https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&to={target_lang}"
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_TRANSLATE_KEY.strip(),
        "Ocp-Apim-Subscription-Region": AZURE_TRANSLATE_REGION.strip(),
        "Content-type": "application/json",
    }
    body = [{"text": protected_text}]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw_translated = data[0]["translations"][0]["text"]

                    # Restore emotes in translation response
                    translated = restore_emotes(raw_translated, placeholders)

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


# ------------------------------------------------------------------------------
# Bot Setup
# ------------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True  # Required to resolve member roles cleanly

bot = commands.Bot(command_prefix="!", intents=intents)


async def language_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=name, value=code)
        for name, code in LANGUAGES.items()
        if current.lower() in name.lower() or current.lower() in code.lower()
    ][:25]


@bot.event
async def on_ready():
    init_db()
    print("Database connection initialized successfully.")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.command()
@commands.is_owner()
async def sync(ctx: commands.Context):
    synced = await bot.tree.sync()
    await ctx.send(f"Synced {len(synced)} command(s) globally.")


# ------------------------------------------------------------------------------
# Slash Commands & Context Menu App
# ------------------------------------------------------------------------------


@bot.tree.command(
    name="setchannellanguage",
    description="Set auto-translation language for a channel",
)
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.autocomplete(language=language_autocomplete)
async def set_channel_language(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    language: str,
):
    set_channel_language_db(channel.id, language)
    lang_name = next(
        (k for k, v in LANGUAGES.items() if v == language), language
    )
    await interaction.response.send_message(
        f"Auto-translation for {channel.mention} set to **{lang_name}** (`{language}`).",
        ephemeral=True,
    )


@bot.tree.command(
    name="setlanguage",
    description="Set your personal default target language for translations",
)
@app_commands.autocomplete(language=language_autocomplete)
async def set_user_language(
    interaction: discord.Interaction,
    language: str,
):
    set_user_language_db(interaction.user.id, language)
    lang_name = next(
        (k for k, v in LANGUAGES.items() if v == language), language
    )
    await interaction.response.send_message(
        f"Your personal translation language has been set to **{lang_name}** (`{language}`).",
        ephemeral=True,
    )


@bot.tree.command(
    name="mylanguage",
    description="Check your current personal target language setting",
)
async def my_language(interaction: discord.Interaction):
    db_lang = get_user_language_db(interaction.user.id)
    effective_lang = get_effective_user_language(interaction.user)

    lang_name = next(
        (k for k, v in LANGUAGES.items() if v == effective_lang), effective_lang
    )

    if db_lang:
        msg = f"Your target language is manually set to **{lang_name}** (`{effective_lang}`)."
    elif (
        isinstance(interaction.user, discord.Member)
        and any(
            r.name.strip().lower() in ROLE_LANGUAGES
            for r in interaction.user.roles
        )
    ):
        msg = f"Your target language is set to **{lang_name}** (`{effective_lang}`) based on your server roles."
    else:
        msg = f"Your target language is defaulting to **English** (`en`). Use `/setlanguage` to override it."

    await interaction.response.send_message(msg, ephemeral=True)


# Apps Menu Translate (Right-click message -> Apps -> Translate)
@bot.tree.context_menu(name="Translate")
async def translate_context_menu(
    interaction: discord.Interaction, message: discord.Message
):
    if not message.content:
        await interaction.response.send_message(
            "Cannot translate an empty message or attachment.", ephemeral=True
        )
        return

    # Automatically checks /setlanguage DB preference, then roles, then fallback
    target_lang = get_effective_user_language(interaction.user)
    translated, src_lang, score = await translate_text(
        message.content, target_lang
    )

    embed = discord.Embed(
        title="Translation Result",
        description=translated,
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Source",
        value=f"{src_lang.upper()} ({score}% confidence)",
        inline=True,
    )
    embed.add_field(
        name="Target", value=target_lang.upper(), inline=True
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------------------------------------------------------------
# Events (Auto-Translate & Reaction Listener)
# ------------------------------------------------------------------------------


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # Skip messages containing only custom emotes and no actual text
    clean_text = CUSTOM_EMOTE_REGEX.sub("", message.content).strip()
    if not clean_text:
        await bot.process_commands(message)
        return

    # Channel auto-translation check
    target_lang = get_channel_language_db(message.channel.id)
    if target_lang:
        translated, src_lang, confidence = await translate_text(
            message.content, target_lang
        )

        if src_lang.lower() != target_lang.lower():
            embed = discord.Embed(
                description=translated, color=discord.Color.green()
            )
            embed.set_author(
                name=message.author.display_name,
                icon_url=message.author.display_avatar.url,
            )
            embed.set_footer(
                text=f"Auto-translated ({src_lang.upper()} ➔ {target_lang.upper()})"
            )
            await message.channel.send(embed=embed)

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Respond to globe emoji reactions (🌐)
    if str(payload.emoji) != "🌐":
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
        guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
        user = await bot.fetch_user(payload.user_id)

        if user.bot or not message.content:
            return

        # Get Member object if in a guild to accurately read roles
        member = guild.get_member(user.id) if guild else user
        target_lang = get_effective_user_language(member or user)

        translated, src_lang, confidence = await translate_text(
            message.content, target_lang
        )

        embed = discord.Embed(
            title="Private Translation Request",
            description=translated,
            color=discord.Color.purple(),
        )
        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        embed.add_field(
            name="Original", value=message.content, inline=False
        )
        embed.set_footer(
            text=f"Detected Language: {src_lang.upper()} | Target: {target_lang.upper()}"
        )

        await user.send(embed=embed)
    except Exception as e:
        print(f"Error handling reaction translation: {e}")


# Start Bot
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)