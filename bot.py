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
    "simplified chinese": "zh-Hans",
    "traditional chinese": "zh-Hant",
    "russian": "ru",
}

# Flag reactions for in-chat public translation
FLAG_TRANSLATIONS = {
    "🇹🇼": "zh-Hant",
    "🇰🇷": "ko",
    "🇷🇺": "ru",
    "🇺🇸": "en",
}

# Regex matching custom Nitro emotes (<:name:id>) and standard shortcodes (:name:)
EMOTE_REGEX = re.compile(r"(<a?:[a-zA-Z0-9_]+:\d+>|:[a-zA-Z0-9_]+:)")

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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_help_seen (
            user_id TEXT PRIMARY KEY
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


def has_user_seen_help(user_id: int) -> bool:
    """Checks if the user has used /help before."""
    conn = get_db_connection()
    res = conn.execute(
        "SELECT 1 FROM user_help_seen WHERE user_id = ?", (str(user_id),)
    )
    row = res.fetchone()
    conn.close()
    return row is not None


def mark_user_seen_help(user_id: int):
    """Marks that a user has seen the /help command prompt."""
    conn = get_db_connection()
    conn.execute(
        "INSERT OR IGNORE INTO user_help_seen (user_id) VALUES (?)",
        (str(user_id),),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------------------
# Language Helpers & Resolution Logic
# ------------------------------------------------------------------------------


def get_clean_lang_name(code: str) -> str:
    """Returns the main display name for a language code (e.g., 'Korean')."""
    for name, lang_code in LANGUAGES.items():
        if lang_code.lower() == code.lower():
            return name.split(" (")[0]
    return code.upper()


def get_effective_user_language(user: discord.User | discord.Member) -> str:
    """Determines target language: /setlanguage > Server Role > English fallback."""
    db_lang = get_user_language_db(user.id)
    if db_lang:
        return db_lang

    if isinstance(user, discord.Member):
        for role in user.roles:
            role_name_lower = role.name.strip().lower()
            if role_name_lower in ROLE_LANGUAGES:
                return ROLE_LANGUAGES[role_name_lower]

    return "en"


# ------------------------------------------------------------------------------
# HTML Emote Protection & Azure API Integration
# ------------------------------------------------------------------------------


def protect_emotes_html(text: str) -> str:
    """Wraps all emote types in HTML tags so Azure ignores them."""
    return EMOTE_REGEX.sub(r'<span class="notranslate">\1</span>', text)


def restore_emotes_html(text: str) -> str:
    """Strips the protection HTML tags after translation."""
    return (
        text.replace('<span class="notranslate">', "")
        .replace("</span>", "")
        .replace('<span class="notranslate" >', "")
    )


async def translate_text(text: str, target_lang: str) -> tuple[str, str, int]:
    """Translates text using Azure AI Translator REST API v3.0 with HTML protection."""
    if not AZURE_TRANSLATE_KEY or not AZURE_TRANSLATE_REGION:
        return (
            "Translation error: Missing credentials in .env",
            "Unknown",
            0,
        )

    protected_text = protect_emotes_html(text)

    url = f"https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&to={target_lang}&textType=html"
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
                    translated = restore_emotes_html(raw_translated)

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


def build_translation_embed(
    author: discord.User | discord.Member,
    translated_text: str,
    src_code: str,
    target_code: str,
    confidence: int,
) -> discord.Embed:
    """Builds the custom purple embed card for translated messages."""
    target_name = get_clean_lang_name(target_code)
    src_name = get_clean_lang_name(src_code)

    embed = discord.Embed(
        description=f"**{target_name}:**\n{translated_text}",
        color=discord.Color.purple(),
    )
    embed.set_author(
        name=author.display_name, icon_url=author.display_avatar.url
    )
    embed.set_thumbnail(url=author.display_avatar.url)
    embed.set_footer(
        text=f"Original language: {src_name} ({src_code.lower()}) • Confidence: {confidence}% • Provider: Azure AI"
    )

    return embed


# ------------------------------------------------------------------------------
# Bot Setup
# ------------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

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
    name="help",
    description="View instructions and guide for using the Translator bot",
)
async def help_command(interaction: discord.Interaction):
    is_first_time = not has_user_seen_help(interaction.user.id)

    embed = discord.Embed(
        title="🌐 Translator Bot Guide",
        description="Here is how to use translation features across the server!",
        color=discord.Color.purple(),
    )

    # First-Time Multilingual Prompt Banner
    if is_first_time:
        first_time_msg = (
            "🌍 **Select your preferred language / 請選擇您的首選語言 / 선호하는 언어를 선택하세요 / Выберите ваш язык:**\n\n"
            "👉 Please run **`/setlanguage`** to set your default language!\n"
            "• **EN:** Use `/setlanguage` to pick your preferred translation target.\n"
            "• **ZH-TW:** 請輸入 `/setlanguage` 來選擇您的預設翻譯語言。\n"
            "• **KO:** `/setlanguage` 명령어로 기본 번역 언어를 설정하세요.\n"
            "• **RU:** Используйте `/setlanguage` для выбора основного языка."
        )
        embed.add_field(
            name="✨ First-Time Setup / 語言設定 guidance",
            value=first_time_msg,
            inline=False,
        )
        mark_user_seen_help(interaction.user.id)

    embed.add_field(
        name="👤 Personal Target Language Settings",
        value=(
            "• **`/setlanguage`**: Override your preferred translation language.\n"
            "• **`/mylanguage`**: Check your current active target language.\n"
            "• **Role Auto-Detection**: If you haven't set a language manually, the bot automatically checks your server roles (e.g., *Korean*, *Traditional Chinese*, *Russian*, *English*) to pick your default!"
        ),
        inline=False,
    )

    embed.add_field(
        name="🚩 Quick Flag Reactions (Public)",
        value=(
            "React to any message with a flag to translate it directly in chat for everyone:\n"
            "• 🇹🇼 ➔ **Traditional Chinese** (`zh-Hant`)\n"
            "• 🇰🇷 ➔ **Korean** (`ko`)\n"
            "• 🇷🇺 ➔ **Russian** (`ru`)\n"
            "• 🇺🇸 ➔ **English** (`en`)\n"
            "• 🌐 ➔ Sends a **private DM** translation using your personal target language."
        ),
        inline=False,
    )

    embed.add_field(
        name="🖱️ Context Menu Translation",
        value="Right-click any message ➔ **Apps** ➔ **Translate** to receive a private, ephemeral translation card.",
        inline=False,
    )

    embed.add_field(
        name="⚙️ Admin Commands",
        value="• **`/setchannellanguage`**: Set automatic channel-wide translation for all non-bot messages.",
        inline=False,
    )

    embed.set_footer(
        text="Powered by Azure AI • Emote & Short Message Filter Active"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    lang_name = get_clean_lang_name(language)
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
    lang_name = get_clean_lang_name(language)
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
    lang_name = get_clean_lang_name(effective_lang)

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

    target_lang = get_effective_user_language(interaction.user)
    translated, src_lang, score = await translate_text(
        message.content, target_lang
    )

    embed = build_translation_embed(
        author=message.author,
        translated_text=translated,
        src_code=src_lang,
        target_code=target_lang,
        confidence=score,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------------------------------------------------------------
# Events (Auto-Translate & Reaction Listeners)
# ------------------------------------------------------------------------------


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # Skip translation if message is a bot command (e.g. !sync)
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    # Strip emotes to check remaining text length
    clean_text = EMOTE_REGEX.sub("", message.content).strip()
    if len(clean_text) <= 3:
        await bot.process_commands(message)
        return

    target_lang = get_channel_language_db(message.channel.id)
    if target_lang:
        translated, src_lang, confidence = await translate_text(
            message.content, target_lang
        )

        if src_lang.lower() != target_lang.lower():
            embed = build_translation_embed(
                author=message.author,
                translated_text=translated,
                src_code=src_lang,
                target_code=target_lang,
                confidence=confidence,
            )
            await message.channel.send(embed=embed)

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    emoji_str = str(payload.emoji)

    if emoji_str not in FLAG_TRANSLATIONS and emoji_str != "🌐":
        return

    channel = bot.get_channel(payload.channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            return

    try:
        user = await bot.fetch_user(payload.user_id)
        if user.bot:
            return

        message = await channel.fetch_message(payload.message_id)
        if not message or not message.content:
            return

        # Handle flag reactions (Public in-chat translation for everyone)
        if emoji_str in FLAG_TRANSLATIONS:
            target_lang = FLAG_TRANSLATIONS[emoji_str]
            translated, src_lang, confidence = await translate_text(
                message.content, target_lang
            )

            embed = build_translation_embed(
                author=message.author,
                translated_text=translated,
                src_code=src_lang,
                target_code=target_lang,
                confidence=confidence,
            )
            await channel.send(embed=embed)

        # Handle globe reaction (Private DM translation for reactor)
        elif emoji_str == "🌐":
            guild = (
                bot.get_guild(payload.guild_id) if payload.guild_id else None
            )
            member = guild.get_member(user.id) if guild else user
            target_lang = get_effective_user_language(member or user)

            translated, src_lang, confidence = await translate_text(
                message.content, target_lang
            )

            embed = build_translation_embed(
                author=message.author,
                translated_text=translated,
                src_code=src_lang,
                target_code=target_lang,
                confidence=confidence,
            )
            await user.send(embed=embed)

    except Exception as e:
        print(f"Error handling reaction translation: {e}")


# Start Bot
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)