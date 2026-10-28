import asyncio
from datetime import datetime, timezone
import os
import re
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import libsql

# ------------------------------------------------------------------------------
# 28. Environment Variable Validation
# ------------------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
AZURE_TRANSLATE_KEY = os.getenv("AZURE_TRANSLATE_KEY")
AZURE_TRANSLATE_REGION = os.getenv("AZURE_TRANSLATE_REGION")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

required_env = {
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "TURSO_DATABASE_URL": TURSO_DATABASE_URL,
    "TURSO_AUTH_TOKEN": TURSO_AUTH_TOKEN,
    "AZURE_TRANSLATE_KEY": AZURE_TRANSLATE_KEY,
    "AZURE_TRANSLATE_REGION": AZURE_TRANSLATE_REGION,
}
missing_env = [k for k, v in required_env.items() if not v]
if missing_env:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(missing_env)}"
    )

AZURE_MONTHLY_LIMIT = 2_000_000

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

# 2. Strict language validation set
VALID_LANGUAGE_CODES = set(LANGUAGES.values())

ROLE_LANGUAGES = {
    "korean": "ko",
    "english": "en",
    "chinese": "zh-Hans",
    "simplified chinese": "zh-Hans",
    "traditional chinese": "zh-Hant",
    "russian": "ru",
}

FLAG_TRANSLATIONS = {
    "🇹🇼": "zh-Hant",
    "🇰🇷": "ko",
    "🇷🇺": "ru",
    "🇺🇸": "en",
}

EMOTE_REGEX = re.compile(r"(<a?:[a-zA-Z0-9_]+:\d+>|:[a-zA-Z0-9_]+:)")
CODE_BLOCK_REGEX = re.compile(r"```[\s\S]*?```|`[^`]+`")
URL_REGEX = re.compile(r"https?://\S+|www\.\S+")
MENTION_REGEX = re.compile(r"<@!?&?\d+>|<#\d+>")

# Cache structure: {channel_id_str: {"defaults": [...], "rules": [...], "is_global": bool}}
CHANNEL_CACHE: dict[str, dict] = {}

# 10. Bounded cache for prompted messages (cleans old entries to avoid unbounded memory growth)
PROMPTED_MESSAGES: dict[int, float] = {}


def clean_prompted_messages_cache(max_age_seconds: int = 3600):
    now = datetime.now(timezone.utc).timestamp()
    expired = [
        msg_id
        for msg_id, ts in PROMPTED_MESSAGES.items()
        if now - ts > max_age_seconds
    ]
    for msg_id in expired:
        PROMPTED_MESSAGES.pop(msg_id, None)


def invalidate_channel_cache(channel_id: int | str):
    CHANNEL_CACHE.pop(str(channel_id), None)


# ------------------------------------------------------------------------------
# 20. Database Helpers (With context cleanup & transactions)
# ------------------------------------------------------------------------------


def get_db_connection():
    return libsql.connect(
        database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN
    )


def init_db():
    conn = get_db_connection()
    try:
        # 17. Safe migration within a transaction
        conn.execute("BEGIN TRANSACTION;")
        res = conn.execute("PRAGMA table_info(channel_languages)").fetchall()
        if res:
            pk_count = sum(1 for col in res if col[5] > 0)
            if pk_count == 1:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS channel_languages_migrated (channel_id TEXT, target_lang TEXT, PRIMARY KEY (channel_id, target_lang));"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO channel_languages_migrated SELECT channel_id, target_lang FROM channel_languages;"
                )
                conn.execute("DROP TABLE channel_languages;")
                conn.execute(
                    "ALTER TABLE channel_languages_migrated RENAME TO channel_languages;"
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Database migration error: {e}")

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_languages (
                channel_id TEXT,
                target_lang TEXT,
                PRIMARY KEY (channel_id, target_lang)
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
        # 18. Add UNIQUE constraint to prevent duplicate channel rules
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_channel_translations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL,
                src_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                UNIQUE(channel_id, src_lang, target_lang)
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS global_chat_channels (
                channel_id TEXT PRIMARY KEY
            )
        """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS character_usage (
                month_year TEXT PRIMARY KEY,
                char_count INTEGER DEFAULT 0
            )
        """
        )
        conn.commit()
    finally:
        conn.close()


def add_channel_language_db(channel_id: int, target_lang: str):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO channel_languages (channel_id, target_lang) VALUES (?, ?)",
            (str(channel_id), target_lang),
        )
        conn.commit()
    finally:
        conn.close()
    invalidate_channel_cache(channel_id)


def remove_channel_language_db(channel_id: int, target_lang: str):
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM channel_languages WHERE channel_id = ? AND target_lang = ?",
            (str(channel_id), target_lang),
        )
        conn.commit()
    finally:
        conn.close()
    invalidate_channel_cache(channel_id)


def get_channel_languages_db(channel_id: int) -> list[str]:
    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT target_lang FROM channel_languages WHERE channel_id = ?",
            (str(channel_id),),
        )
        return [r[0] for r in res.fetchall()]
    finally:
        conn.close()


def set_global_chat_channel_db(channel_id: int, enabled: bool):
    conn = get_db_connection()
    try:
        if enabled:
            conn.execute(
                "INSERT OR IGNORE INTO global_chat_channels (channel_id) VALUES (?)",
                (str(channel_id),),
            )
        else:
            conn.execute(
                "DELETE FROM global_chat_channels WHERE channel_id = ?",
                (str(channel_id),),
            )
        conn.commit()
    finally:
        conn.close()
    invalidate_channel_cache(channel_id)


def is_global_chat_channel_db(channel_id: int) -> bool:
    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT 1 FROM global_chat_channels WHERE channel_id = ?",
            (str(channel_id),),
        )
        return res.fetchone() is not None
    finally:
        conn.close()


def log_character_usage(char_count: int):
    if char_count <= 0:
        return
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO character_usage (month_year, char_count)
            VALUES (?, ?)
            ON CONFLICT(month_year) DO UPDATE SET char_count = char_count + excluded.char_count
        """,
            (current_month, char_count),
        )
        conn.commit()
    finally:
        conn.close()


def get_monthly_character_usage() -> int:
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT char_count FROM character_usage WHERE month_year = ?",
            (current_month,),
        )
        row = res.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def set_user_language_db(user_id: int, target_lang: str):
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO user_languages (user_id, target_lang)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET target_lang=excluded.target_lang
        """,
            (str(user_id), target_lang),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_language_db(user_id: int) -> str | None:
    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT target_lang FROM user_languages WHERE user_id = ?",
            (str(user_id),),
        )
        row = res.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def has_user_seen_help(user_id: int) -> bool:
    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT 1 FROM user_help_seen WHERE user_id = ?", (str(user_id),)
        )
        return res.fetchone() is not None
    finally:
        conn.close()


def mark_user_seen_help(user_id: int):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO user_help_seen (user_id) VALUES (?)",
            (str(user_id),),
        )
        conn.commit()
    finally:
        conn.close()


def add_custom_channel_translation_db(
    channel_id: int, src_lang: str, target_lang: str
):
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO custom_channel_translations (channel_id, src_lang, target_lang)
            VALUES (?, ?, ?)
        """,
            (str(channel_id), src_lang, target_lang),
        )
        conn.commit()
    finally:
        conn.close()
    invalidate_channel_cache(channel_id)


def get_custom_channel_translations_db(
    channel_id: int,
) -> list[tuple[int, str, str]]:
    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT id, src_lang, target_lang FROM custom_channel_translations WHERE channel_id = ? ORDER BY id ASC",
            (str(channel_id),),
        )
        return res.fetchall()
    finally:
        conn.close()


def get_cached_channel_settings(channel_id: int | str) -> dict:
    cid = str(channel_id)
    if cid not in CHANNEL_CACHE:
        CHANNEL_CACHE[cid] = {
            "defaults": get_channel_languages_db(int(cid)),
            "rules": get_custom_channel_translations_db(int(cid)),
            "is_global": is_global_chat_channel_db(int(cid)),
        }
    return CHANNEL_CACHE[cid]


# ------------------------------------------------------------------------------
# Helpers & Formatting
# ------------------------------------------------------------------------------


def strip_untranslatable_content(text: str) -> str:
    text = CODE_BLOCK_REGEX.sub("", text)
    text = URL_REGEX.sub("", text)
    text = MENTION_REGEX.sub("", text)
    text = EMOTE_REGEX.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def is_worth_translating(text: str) -> bool:
    cleaned = strip_untranslatable_content(text)
    if len(cleaned) <= 3:
        return False
    if not re.search(r"[^\W\d_]", cleaned):
        return False
    return True


def get_clean_lang_name(code: str) -> str:
    for name, lang_code in LANGUAGES.items():
        if lang_code.lower() == code.lower():
            return name.split(" (")[0]
    return code.upper()


def is_lang_match(lang1: str, lang2: str) -> bool:
    l1, l2 = lang1.lower(), lang2.lower()
    if l1 == l2:
        return True
    if l1.split("-")[0] == l2.split("-")[0] and (len(l1) == 2 or len(l2) == 2):
        return True
    return False


async def get_effective_user_language_async(
    user: discord.User | discord.Member,
) -> str:
    db_lang = await asyncio.to_thread(get_user_language_db, user.id)
    if db_lang:
        return db_lang

    if isinstance(user, discord.Member):
        for role in user.roles:
            role_name_lower = role.name.strip().lower()
            if role_name_lower in ROLE_LANGUAGES:
                return ROLE_LANGUAGES[role_name_lower]

    return "en"


def generate_progress_bar(used: int, total: int, length: int = 16) -> str:
    pct = min(used / total, 1.0)
    filled = int(length * pct)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {pct * 100:.1f}%"


def protect_emotes_html(text: str) -> str:
    return EMOTE_REGEX.sub(r'<span class="notranslate">\1</span>', text)


def restore_emotes_html(text: str) -> str:
    return (
        text.replace('<span class="notranslate">', "")
        .replace("</span>", "")
        .replace('<span class="notranslate" >', "")
    )


# ------------------------------------------------------------------------------
# 4. Session Validation & 6. Azure Client with Timeouts and Error Handling
# ------------------------------------------------------------------------------


def require_session(bot_instance) -> aiohttp.ClientSession:
    if bot_instance.session is None or bot_instance.session.closed:
        raise RuntimeError("HTTP session is not available")
    return bot_instance.session


async def detect_text_language(
    session: aiohttp.ClientSession, text: str
) -> tuple[str, int]:
    protected_text = protect_emotes_html(text)
    url = "https://api.cognitive.microsofttranslator.com/detect?api-version=3.0"
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_TRANSLATE_KEY.strip(),
        "Ocp-Apim-Subscription-Region": AZURE_TRANSLATE_REGION.strip(),
        "Content-type": "application/json",
    }
    body = [{"text": protected_text}]

    try:
        async with session.post(url, headers=headers, json=body) as resp:
            # 7. Status checks
            if resp.status == 200:
                data = await resp.json()
                detected_info = data[0]
                detected_code = detected_info.get("language", "Unknown")
                score = detected_info.get("score", 1.0)
                return detected_code, int(score * 100)
            elif resp.status == 429:
                print("Azure API error: Rate limit exceeded (429)")
            elif resp.status in {401, 403}:
                print("Azure API error: Auth/region key failure")
            else:
                print(f"Azure API error status {resp.status}: {await resp.text()}")
    except asyncio.TimeoutError:
        print("Azure API request timed out.")
    except Exception as e:
        print(f"Language detection error: {e}")
    return "Unknown", 0


async def translate_text(
    session: aiohttp.ClientSession, text: str, target_langs: list[str]
) -> list[tuple[str, str, str, int]]:
    if not target_langs:
        return []

    # 8. Quota Check Before Making API Calls
    requested_chars = len(text) * len(target_langs)
    current_usage = await asyncio.to_thread(get_monthly_character_usage)
    if current_usage + requested_chars > AZURE_MONTHLY_LIMIT:
        print("Translation skipped: Monthly Azure quota exceeded.")
        return []

    protected_text = protect_emotes_html(text)
    to_params = "&".join([f"to={lang}" for lang in target_langs])
    url = f"https://api.cognitive.microsofttranslator.com/translate?api-version=3.0&{to_params}&textType=html"

    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_TRANSLATE_KEY.strip(),
        "Ocp-Apim-Subscription-Region": AZURE_TRANSLATE_REGION.strip(),
        "Content-type": "application/json",
    }
    body = [{"text": protected_text}]

    try:
        async with session.post(url, headers=headers, json=body) as resp:
            if resp.status == 200:
                data = await resp.json()
                detected_info = data[0].get("detectedLanguage", {})
                detected_code = detected_info.get("language", "Unknown")
                score = detected_info.get("score", 1.0)
                confidence = int(score * 100)

                await asyncio.to_thread(
                    log_character_usage, len(protected_text) * len(target_langs)
                )

                results = []
                for item in data[0].get("translations", []):
                    translated = restore_emotes_html(item.get("text", ""))
                    target_code = item.get("to", "")
                    results.append(
                        (translated, target_code, detected_code, confidence)
                    )
                return results
            elif resp.status == 429:
                print("Azure API error: Rate limit exceeded (429)")
            elif resp.status in {401, 403}:
                print("Azure API error: Authentication failed")
            else:
                print(f"Azure API error ({resp.status}): {await resp.text()}")
    except asyncio.TimeoutError:
        print("Azure API request timed out.")
    except Exception as e:
        print(f"Translation error: {e}")
    return []


# 5. Return None on Translation Failure instead of fake embed
async def translate_single(
    session: aiohttp.ClientSession, text: str, target_lang: str
) -> tuple[str, str, int] | None:
    res = await translate_text(session, text, [target_lang])
    if res:
        translated, _, src_lang, confidence = res[0]
        return translated, src_lang, confidence
    return None


def build_translation_embed(
    author: discord.User | discord.Member,
    translated_text: str,
    src_code: str,
    target_code: str,
    confidence: int,
) -> discord.Embed:
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
        text=f"Original: {src_name} ({src_code.lower()}) • Confidence: {confidence}% • Provider: Azure AI"
    )

    return embed


# ------------------------------------------------------------------------------
# 15. Persistent UI Views across Restarts
# ------------------------------------------------------------------------------


class GlobalTranslateView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Translate / 翻譯",
        style=discord.ButtonStyle.secondary,
        emoji="🌐",
        custom_id="global_translate_button_v1",  # Explicit custom_id for persistence
    )
    async def translate_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not interaction.message or not interaction.message.content:
            await interaction.response.send_message(
                "Unable to retrieve message content.", ephemeral=True
            )
            return

        cleaned_text = strip_untranslatable_content(interaction.message.content)
        if not cleaned_text:
            await interaction.response.send_message(
                "Cannot translate empty text or non-text elements.", ephemeral=True
            )
            return

        user_lang = await asyncio.to_thread(
            get_user_language_db, interaction.user.id
        )

        if not user_lang:
            guide_embed = discord.Embed(
                title="🌐 Set Target Language / 設定語言",
                description="Please choose your target language below to complete translation:",
                color=discord.Color.gold(),
            )
            prompt_view = LanguagePromptView(
                cleaned_text, interaction.message.author
            )
            await interaction.response.send_message(
                embed=guide_embed, view=prompt_view, ephemeral=True
            )
            return

        result = await translate_single(
            require_session(interaction.client), cleaned_text, user_lang
        )

        # 5. Handle failed translations explicitly
        if result is None:
            await interaction.response.send_message(
                "⚠️ Translation failed or limit reached. Please try again later.",
                ephemeral=True,
            )
            return

        translated, src_lang, confidence = result
        embed = build_translation_embed(
            author=interaction.message.author,
            translated_text=translated,
            src_code=src_lang,
            target_code=user_lang,
            confidence=confidence,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


class LanguagePromptSelect(discord.ui.Select):

    def __init__(
        self, original_text: str, author: discord.User | discord.Member
    ):
        options = [
            discord.SelectOption(label=name, value=code)
            for name, code in list(LANGUAGES.items())[:25]
        ]
        super().__init__(
            placeholder="🌐 Choose language / 選擇語言...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.original_text = original_text
        self.author = author

    async def callback(self, interaction: discord.Interaction):
        selected_lang = self.values[0]
        await asyncio.to_thread(
            set_user_language_db, interaction.user.id, selected_lang
        )
        lang_name = get_clean_lang_name(selected_lang)

        result = await translate_single(
            require_session(interaction.client),
            self.original_text,
            selected_lang,
        )

        if result is None:
            await interaction.response.send_message(
                "⚠️ Translation failed. Please try again later.", ephemeral=True
            )
            return

        translated, src_lang, confidence = result
        embed = build_translation_embed(
            author=self.author,
            translated_text=translated,
            src_code=src_lang,
            target_code=selected_lang,
            confidence=confidence,
        )

        await interaction.response.send_message(
            content=f"✅ Preferred language set to **{lang_name}** (`{selected_lang}`):",
            embed=embed,
            ephemeral=True,
        )


class LanguagePromptView(discord.ui.View):

    def __init__(
        self, original_text: str, author: discord.User | discord.Member
    ):
        super().__init__(timeout=180)
        self.add_item(LanguagePromptSelect(original_text, author))


class WelcomeLanguageSelect(discord.ui.Select):

    def __init__(self):
        options = [
            discord.SelectOption(label=name, value=code)
            for name, code in list(LANGUAGES.items())[:25]
        ]
        super().__init__(
            placeholder="🌐 Choose your language...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_lang = self.values[0]
        await asyncio.to_thread(
            set_user_language_db, interaction.user.id, selected_lang
        )
        lang_name = get_clean_lang_name(selected_lang)

        await interaction.response.send_message(
            f"✅ Preferred translation language set to **{lang_name}** (`{selected_lang}`)!",
            ephemeral=True,
        )


class WelcomeLanguageView(discord.ui.View):

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(WelcomeLanguageSelect())


# ------------------------------------------------------------------------------
# Bot Setup & Unified Error Handlers
# ------------------------------------------------------------------------------


class TranslationBot(commands.Bot):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        # 6. Session Timeout Enforcement
        timeout = aiohttp.ClientTimeout(total=20)
        self.session = aiohttp.ClientSession(timeout=timeout)

        # 9. Non-blocking initialization
        await asyncio.to_thread(init_db)

        # 15. Register Persistent Global View
        self.add_view(GlobalTranslateView())

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = TranslationBot(command_prefix="!", intents=intents)


# 1. Global Tree Error Handler
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "⛔ You need the **Manage Channels** permission to execute this command."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "⛔ You do not have permission to execute this command."
    else:
        print(f"App command error: {error}")
        msg = "⚠️ An unexpected error occurred while processing your request."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


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
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


# 14. Guild Check for !sync
@bot.command()
@commands.is_owner()
async def sync(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("This command can only be executed within a server.")
        return
    bot.tree.copy_global_to(guild=ctx.guild)
    synced = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"Synced {len(synced)} command(s) to this server.")


# ------------------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------------------


@bot.tree.command(
    name="setglobalchat",
    description="Toggle global language chat mode for a channel",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def set_global_chat(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
):
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Please specify a valid text channel.", ephemeral=True
        )
        return

    settings = await asyncio.to_thread(
        get_cached_channel_settings, target_channel.id
    )
    new_status = not settings.get("is_global", False)

    await asyncio.to_thread(
        set_global_chat_channel_db, target_channel.id, new_status
    )

    status_str = "enabled" if new_status else "disabled"
    await interaction.response.send_message(
        f"🌐 Global Language Chat mode has been **{status_str}** for {target_channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="setlanguage",
    description="Set your personal target language for translations",
)
@app_commands.autocomplete(language=language_autocomplete)
async def set_user_language(
    interaction: discord.Interaction,
    language: str,
):
    # 2. Strict Input Validation
    if language not in VALID_LANGUAGE_CODES:
        await interaction.response.send_message(
            "Invalid language selection. Please select an option from the list.",
            ephemeral=True,
        )
        return

    await asyncio.to_thread(set_user_language_db, interaction.user.id, language)
    lang_name = get_clean_lang_name(language)
    await interaction.response.send_message(
        f"Your translation language set to **{lang_name}** (`{language}`).",
        ephemeral=True,
    )


@bot.tree.command(
    name="translatechannel",
    description="Set a rule to translate a specific language in a channel",
)
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.autocomplete(
    detectlanguage=language_autocomplete,
    translatedlanguage=language_autocomplete,
)
async def translate_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    detectlanguage: str,
    translatedlanguage: str,
):
    # 2. Strict Input Validation for both parameters
    if (
        detectlanguage not in VALID_LANGUAGE_CODES
        or translatedlanguage not in VALID_LANGUAGE_CODES
    ):
        await interaction.response.send_message(
            "Invalid language selection. Please choose valid options from the list.",
            ephemeral=True,
        )
        return

    await asyncio.to_thread(
        add_custom_channel_translation_db,
        channel.id,
        detectlanguage,
        translatedlanguage,
    )
    src_name = get_clean_lang_name(detectlanguage)
    target_name = get_clean_lang_name(translatedlanguage)
    await interaction.response.send_message(
        f"Added rule for {channel.mention}: **{src_name}** ➔ **{target_name}**.",
        ephemeral=True,
    )


@bot.tree.command(
    name="stats", description="View real-time Azure character usage and quota"
)
async def stats_command(interaction: discord.Interaction):
    used = await asyncio.to_thread(get_monthly_character_usage)
    remaining = max(0, AZURE_MONTHLY_LIMIT - used)
    progress_bar = generate_progress_bar(used, AZURE_MONTHLY_LIMIT)

    embed = discord.Embed(
        title="📊 Azure Translation Usage Tracker", color=discord.Color.purple()
    )
    embed.add_field(
        name="Monthly Character Usage",
        value=f"`{progress_bar}`",
        inline=False,
    )
    embed.add_field(
        name="Used Characters", value=f"**{used:,}**", inline=True
    )
    embed.add_field(
        name="Remaining Capacity", value=f"**{remaining:,}**", inline=True
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.context_menu(name="Translate")
async def translate_context_menu(
    interaction: discord.Interaction, message: discord.Message
):
    cleaned_content = strip_untranslatable_content(message.content)
    if not cleaned_content:
        await interaction.response.send_message(
            "Cannot translate empty text or non-text elements.", ephemeral=True
        )
        return

    target_lang = await get_effective_user_language_async(interaction.user)
    result = await translate_single(
        require_session(bot), cleaned_content, target_lang
    )

    if result is None:
        await interaction.response.send_message(
            "⚠️ Translation failed or quota limit reached.", ephemeral=True
        )
        return

    translated, src_lang, score = result
    embed = build_translation_embed(
        author=message.author,
        translated_text=translated,
        src_code=src_lang,
        target_code=target_lang,
        confidence=score,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------------------------------------------------------------
# Event Handlers
# ------------------------------------------------------------------------------


@bot.event
async def on_message(message: discord.Message):
    # 12/13. Correct bot filter and process command execution
    if message.author.bot or not message.guild:
        return

    if not is_worth_translating(message.content):
        await bot.process_commands(message)
        return

    settings = await asyncio.to_thread(
        get_cached_channel_settings, message.channel.id
    )
    is_global = settings.get("is_global", False)

    if is_global:
        try:
            await message.add_reaction("🌐")
        except Exception:
            pass
    else:
        defaults = settings.get("defaults", [])
        rules = settings.get("rules", [])

        if defaults or rules:
            text_to_translate = strip_untranslatable_content(message.content)
            if text_to_translate:
                src_lang, _ = await detect_text_language(
                    require_session(bot), text_to_translate
                )
                target_langs = set()

                for _, rule_src, rule_target in rules:
                    if is_lang_match(src_lang, rule_src):
                        target_langs.add(rule_target)

                for def_lang in defaults:
                    if not is_lang_match(src_lang, def_lang):
                        target_langs.add(def_lang)

                if target_langs:
                    translations = await translate_text(
                        require_session(bot),
                        text_to_translate,
                        list(target_langs),
                    )
                    for (
                        translated_text,
                        target_code,
                        detected_code,
                        score,
                    ) in translations:
                        embed = build_translation_embed(
                            author=message.author,
                            translated_text=translated_text,
                            src_code=detected_code,
                            target_code=target_code,
                            confidence=score,
                        )
                        await message.reply(embed=embed, mention_author=False)

    await bot.process_commands(message)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # 3. Guard against bot.user being None
    if bot.user and payload.user_id == bot.user.id:
        return

    emoji_str = str(payload.emoji)
    if emoji_str != "🌐" and emoji_str not in FLAG_TRANSLATIONS:
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

        text_to_translate = strip_untranslatable_content(message.content)
        if not text_to_translate:
            return

        # Handle 🌐 Prompt Button
        if emoji_str == "🌐":
            clean_prompted_messages_cache()
            if payload.message_id in PROMPTED_MESSAGES:
                return

            PROMPTED_MESSAGES[payload.message_id] = datetime.now(
                timezone.utc
            ).timestamp()
            view = GlobalTranslateView()
            await message.reply(
                content="🌐 **Translate Here • 點擊翻譯 • 翻訳 • 번역**",
                view=view,
                mention_author=False,
            )

        # Handle Flag Emojis
        else:
            target_lang = FLAG_TRANSLATIONS[emoji_str]
            result = await translate_single(
                require_session(bot), text_to_translate, target_lang
            )
            if result:
                translated, src_lang, confidence = result
                embed = build_translation_embed(
                    author=message.author,
                    translated_text=translated,
                    src_code=src_lang,
                    target_code=target_lang,
                    confidence=confidence,
                )
                await message.reply(embed=embed, mention_author=False)

    except Exception as e:
        print(f"Reaction translation error: {e}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)