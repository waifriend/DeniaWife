import asyncio
import html
import logging
from collections import OrderedDict
from datetime import datetime, timezone
import os
import re
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import libsql

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("translation-bot")

# ------------------------------------------------------------------------------
# Environment Variable Validation
# ------------------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEEPL_AUTH_KEY = os.getenv("DEEPL_AUTH_KEY")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

required_env = {
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "TURSO_DATABASE_URL": TURSO_DATABASE_URL,
    "TURSO_AUTH_TOKEN": TURSO_AUTH_TOKEN,
    "DEEPL_AUTH_KEY": DEEPL_AUTH_KEY,
}
missing_env = [k for k, v in required_env.items() if not v]
if missing_env:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(missing_env)}"
    )

# DeepL Free plan = 500_000 characters / month.
# Pro is billed (not hard-capped). Soft local guard is configurable.
_default_limit = "500000" if (DEEPL_AUTH_KEY or "").endswith(":fx") else "10000000"
DEEPL_MONTHLY_LIMIT = int(os.getenv("DEEPL_MONTHLY_LIMIT", _default_limit))

# Free keys end with ":fx" → use the free endpoint
DEEPL_BASE_URL = (
    "https://api-free.deepl.com"
    if (DEEPL_AUTH_KEY or "").endswith(":fx")
    else "https://api.deepl.com"
)

# DeepL language codes (case-insensitive on the API side; we normalise to upper).
# Ordered by approximate usage so the first 25 fit in a Discord Select menu.
LANGUAGES = {
    "English (American)": "EN-US",
    "English (British)": "EN-GB",
    "Korean (한국어)": "KO",
    "Japanese (日本語)": "JA",
    "Chinese Simplified (简体中文)": "ZH",
    "Chinese Traditional (繁體中文)": "ZH-HANT",
    "Spanish (Español)": "ES",
    "French (Français)": "FR",
    "German (Deutsch)": "DE",
    "Russian (Русский)": "RU",
    "Vietnamese (Tiếng Việt)": "VI",
    "Thai (ไทย)": "TH",
    "Indonesian (Bahasa Indonesia)": "ID",
    "Portuguese (Brazilian)": "PT-BR",
    "Portuguese (European)": "PT-PT",
    "Italian (Italiano)": "IT",
    "Arabic (العربية)": "AR",
    "Hindi (हिन्दी)": "HI",
    "Dutch (Nederlands)": "NL",
    "Polish (Polski)": "PL",
    "Turkish (Türkçe)": "TR",
    "Ukrainian (Українська)": "UK",
    "Swedish (Svenska)": "SV",
    "Czech (Čeština)": "CS",
    "Danish (Dansk)": "DA",
    # Beyond Discord Select's 25-option limit — still available via /setlanguage
    "Finnish (Suomi)": "FI",
    "Greek (Ελληνικά)": "EL",
    "Hungarian (Magyar)": "HU",
    "Romanian (Română)": "RO",
    "Bulgarian (Български)": "BG",
}

VALID_LANGUAGE_CODES = set(LANGUAGES.values())

ROLE_LANGUAGES = {
    "korean": "KO",
    "english": "EN-US",
    "chinese": "ZH",
    "simplified chinese": "ZH",
    "traditional chinese": "ZH-HANT",
    "russian": "RU",
}

FLAG_TRANSLATIONS = {
    "🇹🇼": "ZH-HANT",
    "🇰🇷": "KO",
    "🇷🇺": "RU",
    "🇺🇸": "EN-US",
    "🇬🇧": "EN-GB",
    "🇯🇵": "JA",
    "🇨🇳": "ZH",
    "🇪🇸": "ES",
    "🇫🇷": "FR",
    "🇩🇪": "DE",
    "🇮🇹": "IT",
    "🇧🇷": "PT-BR",
    "🇵🇹": "PT-PT",
    "🇻🇳": "VI",
    "🇹🇭": "TH",
    "🇮🇩": "ID",
    "🇸🇦": "AR",
    "🇮🇳": "HI",
    "🇳🇱": "NL",
    "🇵🇱": "PL",
    "🇹🇷": "TR",
    "🇺🇦": "UK",
}

EMOTE_REGEX = re.compile(r"(<a?:[a-zA-Z0-9_]+:\d+>|:[a-zA-Z0-9_]+:)")
CODE_BLOCK_REGEX = re.compile(r"```[\s\S]*?```|`[^`]+`")
URL_REGEX = re.compile(r"https?://\S+|www\.\S+")
MENTION_REGEX = re.compile(r"<@!?&?\d+>|<#\d+>")

# Bounded LRU-style cache for channel settings
CHANNEL_CACHE_MAX = 512
CHANNEL_CACHE: OrderedDict[str, dict] = OrderedDict()

# Bounded cache for prompted messages (prevents duplicate 🌐 prompts)
PROMPTED_MESSAGES: dict[int, float] = {}

# Simple per-user / per-channel rate limiting (in-memory)
# key → last timestamp
USER_COOLDOWN: dict[int, float] = {}
CHANNEL_COOLDOWN: dict[int, float] = {}
USER_COOLDOWN_SECONDS = 3.0
CHANNEL_COOLDOWN_SECONDS = 1.5
COOLDOWN_PRUNE_AGE = 300.0  # drop entries older than 5 minutes

# Created in setup_hook so it binds to the running event loop
DEEPL_SEMAPHORE: asyncio.Semaphore | None = None


def clean_prompted_messages_cache(max_age_seconds: int = 3600):
    now = datetime.now(timezone.utc).timestamp()
    expired = [
        msg_id
        for msg_id, ts in PROMPTED_MESSAGES.items()
        if now - ts > max_age_seconds
    ]
    for msg_id in expired:
        PROMPTED_MESSAGES.pop(msg_id, None)


def clean_cooldown_caches():
    """Prune stale cooldown entries to avoid unbounded growth."""
    now = time.monotonic()
    for store in (USER_COOLDOWN, CHANNEL_COOLDOWN):
        expired = [k for k, ts in store.items() if now - ts > COOLDOWN_PRUNE_AGE]
        for k in expired:
            store.pop(k, None)


def invalidate_channel_cache(channel_id: int | str):
    CHANNEL_CACHE.pop(str(channel_id), None)


def _check_cooldown(user_id: int, channel_id: int) -> bool:
    """Return True if the action is allowed (not on cooldown)."""
    now = time.monotonic()
    last_user = USER_COOLDOWN.get(user_id, 0.0)
    last_chan = CHANNEL_COOLDOWN.get(channel_id, 0.0)
    if now - last_user < USER_COOLDOWN_SECONDS:
        return False
    if now - last_chan < CHANNEL_COOLDOWN_SECONDS:
        return False
    USER_COOLDOWN[user_id] = now
    CHANNEL_COOLDOWN[channel_id] = now
    return True


# ------------------------------------------------------------------------------
# Database Helpers (With context cleanup & transactions)
# ------------------------------------------------------------------------------


def get_db_connection():
    return libsql.connect(
        database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN
    )


def init_db():
    conn = get_db_connection()
    try:
        # Safe migration within a transaction
        try:
            conn.execute("BEGIN TRANSACTION;")
            res = conn.execute("PRAGMA table_info(channel_languages)").fetchall()
            if res:
                pk_count = sum(1 for col in res if col[5] > 0)
                if pk_count == 1:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS channel_languages_migrated "
                        "(channel_id TEXT, target_lang TEXT, "
                        "PRIMARY KEY (channel_id, target_lang));"
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO channel_languages_migrated "
                        "SELECT channel_id, target_lang FROM channel_languages;"
                    )
                    conn.execute("DROP TABLE channel_languages;")
                    conn.execute(
                        "ALTER TABLE channel_languages_migrated "
                        "RENAME TO channel_languages;"
                    )
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.error("Database migration error: %s", e)

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


def clear_user_language_db(user_id: int):
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM user_languages WHERE user_id = ?",
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


def remove_custom_channel_translation_db(
    channel_id: int, src_lang: str, target_lang: str
):
    conn = get_db_connection()
    try:
        conn.execute(
            """
            DELETE FROM custom_channel_translations
            WHERE channel_id = ? AND src_lang = ? AND target_lang = ?
        """,
            (str(channel_id), src_lang, target_lang),
        )
        conn.commit()
    finally:
        conn.close()
    invalidate_channel_cache(channel_id)


def clear_channel_rules_db(channel_id: int):
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM custom_channel_translations WHERE channel_id = ?",
            (str(channel_id),),
        )
        conn.execute(
            "DELETE FROM channel_languages WHERE channel_id = ?",
            (str(channel_id),),
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
            "SELECT id, src_lang, target_lang FROM custom_channel_translations "
            "WHERE channel_id = ? ORDER BY id ASC",
            (str(channel_id),),
        )
        return res.fetchall()
    finally:
        conn.close()


def get_cached_channel_settings(channel_id: int | str) -> dict:
    cid = str(channel_id)
    if cid in CHANNEL_CACHE:
        CHANNEL_CACHE.move_to_end(cid)
        return CHANNEL_CACHE[cid]

    settings = {
        "defaults": get_channel_languages_db(int(cid)),
        "rules": get_custom_channel_translations_db(int(cid)),
        "is_global": is_global_chat_channel_db(int(cid)),
    }
    CHANNEL_CACHE[cid] = settings
    if len(CHANNEL_CACHE) > CHANNEL_CACHE_MAX:
        CHANNEL_CACHE.popitem(last=False)
    return settings


def resolve_channel_id(channel: discord.abc.Messageable) -> int:
    """Return parent channel ID for threads, otherwise the channel's own ID."""
    if isinstance(channel, discord.Thread) and channel.parent_id:
        return channel.parent_id
    return channel.id  # type: ignore[attr-defined]


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
    code_upper = code.upper()
    for name, lang_code in LANGUAGES.items():
        if lang_code.upper() == code_upper:
            return name.split(" (")[0]
    fallbacks = {
        "EN": "English",
        "ZH": "Chinese",
        "PT": "Portuguese",
    }
    return fallbacks.get(code_upper, code_upper)


def is_lang_match(lang1: str, lang2: str) -> bool:
    """
    Loose match that treats EN / EN-US / EN-GB as the same family.
    Explicitly does NOT treat ZH and ZH-HANT as the same so that
    Simplified ↔ Traditional translation still occurs.
    """
    l1, l2 = lang1.upper(), lang2.upper()
    if l1 == l2:
        return True

    # Special case: keep ZH and ZH-HANT distinct
    zh_family = {"ZH", "ZH-HANS", "ZH-HANT"}
    if l1 in zh_family and l2 in zh_family:
        return False

    base1 = l1.split("-")[0]
    base2 = l2.split("-")[0]
    if base1 == base2:
        return True
    return False


def normalize_deepl_code(code: str) -> str:
    """Normalise stored / user-facing codes to the form DeepL expects."""
    return code.upper().replace("_", "-")


async def get_effective_user_language_async(
    user: discord.User | discord.Member,
) -> str:
    db_lang = await asyncio.to_thread(get_user_language_db, user.id)
    if db_lang:
        return normalize_deepl_code(db_lang)

    if isinstance(user, discord.Member):
        for role in user.roles:
            role_name_lower = role.name.strip().lower()
            if role_name_lower in ROLE_LANGUAGES:
                return ROLE_LANGUAGES[role_name_lower]

    return "EN-US"


def generate_progress_bar(used: int, total: int, length: int = 16) -> str:
    pct = min(used / total, 1.0) if total > 0 else 0.0
    filled = int(length * pct)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {pct * 100:.1f}%"


def protect_text_for_deepl(text: str) -> str:
    """
    Escape HTML first, then wrap custom emotes in notranslate spans so
    DeepL's tag_handling=html leaves them intact.
    """
    escaped = html.escape(text, quote=False)
    return EMOTE_REGEX.sub(r'<span class="notranslate">\1</span>', escaped)


def restore_text_from_deepl(text: str) -> str:
    # Remove the protective spans we added
    text = re.sub(r'</?span[^>]*>', '', text)
    # Unescape any entities DeepL may have left
    return html.unescape(text)


# ------------------------------------------------------------------------------
# DeepL Client Helpers (timeouts + error handling)
# ------------------------------------------------------------------------------


def require_session(bot_instance) -> aiohttp.ClientSession:
    if bot_instance.session is None or bot_instance.session.closed:
        raise RuntimeError("HTTP session is not available")
    return bot_instance.session


def _deepl_headers() -> dict:
    return {
        "Authorization": f"DeepL-Auth-Key {DEEPL_AUTH_KEY.strip()}",
        "Content-Type": "application/json",
    }


async def _deepl_translate_one(
    session: aiohttp.ClientSession,
    protected_text: str,
    target_lang: str,
) -> Optional[tuple[str, str, int]]:
    """
    Single-target DeepL call.
    Returns (translated_text, detected_source, billed_characters) or None.
    """
    url = f"{DEEPL_BASE_URL}/v2/translate"
    body = {
        "text": [protected_text],
        "target_lang": target_lang,
        "tag_handling": "html",
    }
    sem = DEEPL_SEMAPHORE or asyncio.Semaphore(3)
    try:
        async with sem:
            async with session.post(
                url, headers=_deepl_headers(), json=body
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    translation = data.get("translations", [{}])[0]
                    translated = restore_text_from_deepl(
                        translation.get("text", "")
                    )
                    detected = translation.get(
                        "detected_source_language", "Unknown"
                    )
                    billed = translation.get(
                        "billed_characters", len(protected_text)
                    )
                    return translated, detected, billed
                elif resp.status == 456:
                    log.warning("DeepL quota exceeded (456)")
                elif resp.status == 429:
                    log.warning("DeepL rate limit (429) – backing off")
                    await asyncio.sleep(1.5)
                elif resp.status in {401, 403}:
                    log.error("DeepL authentication failed (%s)", resp.status)
                else:
                    body_text = await resp.text()
                    log.error(
                        "DeepL error %s for %s: %s",
                        resp.status,
                        target_lang,
                        body_text[:300],
                    )
    except asyncio.TimeoutError:
        log.warning("DeepL request timed out for target %s", target_lang)
    except Exception as e:
        log.exception("DeepL translation error for %s: %s", target_lang, e)
    return None


async def translate_text(
    session: aiohttp.ClientSession,
    text: str,
    target_langs: list[str],
) -> list[tuple[str, str, str, int]]:
    """
    Translate `text` into each language in `target_langs` in parallel
    (bounded by DEEPL_SEMAPHORE).
    Returns list of (translated_text, target_code, detected_source, confidence).
    Confidence is always 100 (DeepL does not expose a score).
    """
    if not target_langs:
        return []

    target_langs = [normalize_deepl_code(t) for t in target_langs]

    # Soft local quota check
    requested_chars = len(text) * len(target_langs)
    current_usage = await asyncio.to_thread(get_monthly_character_usage)
    if current_usage + requested_chars > DEEPL_MONTHLY_LIMIT:
        log.warning(
            "Translation skipped: local monthly quota would be exceeded "
            "(%s + %s > %s)",
            current_usage,
            requested_chars,
            DEEPL_MONTHLY_LIMIT,
        )
        return []

    protected_text = protect_text_for_deepl(text)

    async def _one(target: str):
        result = await _deepl_translate_one(session, protected_text, target)
        if result is None:
            return None
        translated, detected, billed = result
        await asyncio.to_thread(log_character_usage, billed)
        return translated, target, detected, 100

    gathered = await asyncio.gather(
        *[_one(t) for t in target_langs], return_exceptions=True
    )

    results: list[tuple[str, str, str, int]] = []
    for item in gathered:
        if isinstance(item, Exception):
            log.error("Parallel translate task failed: %s", item)
            continue
        if item is not None:
            results.append(item)
    return results


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
    confidence: int = 100,
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
        text=f"Original: {src_name} ({src_code.upper()}) • Provider: DeepL"
    )
    return embed


# ------------------------------------------------------------------------------
# Persistent UI Views
# ------------------------------------------------------------------------------


class GlobalTranslateView(discord.ui.View):
    """
    Persistent view attached to the bot's "Translate Here" reply.
    The original message is recovered via message.reference.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Translate / 翻譯",
        style=discord.ButtonStyle.secondary,
        emoji="🌐",
        custom_id="global_translate_button_v1",
    )
    async def translate_callback(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not interaction.message:
            await interaction.response.send_message(
                "Unable to retrieve message.", ephemeral=True
            )
            return

        # Resolve the original user message via the reply reference
        target_msg = interaction.message
        if (
            interaction.message.reference
            and interaction.message.reference.message_id
        ):
            try:
                target_msg = await interaction.channel.fetch_message(
                    interaction.message.reference.message_id
                )
            except discord.NotFound:
                await interaction.response.send_message(
                    "Original message was deleted.", ephemeral=True
                )
                return
            except Exception as e:
                log.warning("Failed to fetch referenced message: %s", e)
                await interaction.response.send_message(
                    "Could not retrieve the original message.", ephemeral=True
                )
                return

        cleaned_text = strip_untranslatable_content(target_msg.content or "")
        if not cleaned_text:
            await interaction.response.send_message(
                "Cannot translate empty text or non-text elements.",
                ephemeral=True,
            )
            return

        # Consistent language resolution (DB → role → EN-US)
        user_lang = await get_effective_user_language_async(interaction.user)

        # If the user has never set a preference and has no matching role,
        # still show the picker (get_effective returns EN-US by default,
        # so we only prompt when the DB entry is missing AND no role matched).
        db_lang = await asyncio.to_thread(
            get_user_language_db, interaction.user.id
        )
        if db_lang is None and not any(
            role.name.strip().lower() in ROLE_LANGUAGES
            for role in getattr(interaction.user, "roles", [])
        ):
            guide_embed = discord.Embed(
                title="🌐 Set Target Language / 設定語言",
                description=(
                    "Please choose your target language below "
                    "to complete translation:"
                ),
                color=discord.Color.gold(),
            )
            prompt_view = LanguagePromptView(cleaned_text, target_msg.author)
            await interaction.response.send_message(
                embed=guide_embed, view=prompt_view, ephemeral=True
            )
            return

        result = await translate_single(
            require_session(interaction.client), cleaned_text, user_lang
        )

        if result is None:
            await interaction.response.send_message(
                "⚠️ Translation failed or limit reached. Please try again later.",
                ephemeral=True,
            )
            return

        translated, src_lang, confidence = result
        embed = build_translation_embed(
            author=target_msg.author,
            translated_text=translated,
            src_code=src_lang,
            target_code=user_lang,
            confidence=confidence,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class LanguagePromptSelect(discord.ui.Select):
    """
    Discord Select menus are capped at 25 options.
    LANGUAGES is ordered so the 25 most common languages appear here;
    the remaining five (FI, EL, HU, RO, BG) are still available via /setlanguage.
    """

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
            content=(
                f"✅ Preferred language set to **{lang_name}** "
                f"(`{selected_lang}`):"
            ),
            embed=embed,
            ephemeral=True,
        )


class LanguagePromptView(discord.ui.View):

    def __init__(
        self, original_text: str, author: discord.User | discord.Member
    ):
        super().__init__(timeout=180)
        self.add_item(LanguagePromptSelect(original_text, author))


# ------------------------------------------------------------------------------
# Bot Setup & Unified Error Handlers
# ------------------------------------------------------------------------------


class TranslationBot(commands.Bot):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        global DEEPL_SEMAPHORE
        timeout = aiohttp.ClientTimeout(total=20)
        self.session = aiohttp.ClientSession(timeout=timeout)
        DEEPL_SEMAPHORE = asyncio.Semaphore(3)
        await asyncio.to_thread(init_db)
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


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        msg = (
            "⛔ You need the **Manage Channels** permission "
            "to execute this command."
        )
    elif isinstance(error, app_commands.CheckFailure):
        msg = "⛔ You do not have permission to execute this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"⏳ Slow down! Try again in {error.retry_after:.1f}s."
    else:
        log.exception("App command error: %s", error)
        msg = "⚠️ An unexpected error occurred while processing your request."

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.NotOwner):
        await ctx.send("⛔ Only the bot owner can run this command.", delete_after=8)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(
            "⛔ You lack the required permissions for this command.",
            delete_after=8,
        )
    else:
        log.exception("Prefix command error: %s", error)


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
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("DeepL endpoint: %s", DEEPL_BASE_URL)


@bot.command()
@commands.is_owner()
async def sync(ctx: commands.Context):
    if ctx.guild is None:
        await ctx.send("This command can only be executed within a server.")
        return
    
    # 1. Wipe old global commands
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync() 
    
    # 2. Copy current code commands to this server and sync
    bot.tree.copy_global_to(guild=ctx.guild)
    synced = await bot.tree.sync(guild=ctx.guild)
    
    await ctx.send(f"🧹 Cleared global commands. Synced {len(synced)} clean command(s) to this server.")


# ------------------------------------------------------------------------------
# Slash Commands
# ------------------------------------------------------------------------------


@bot.tree.command(
    name="setglobalchat",
    description="Toggle or query global language chat mode for a channel",
)
@app_commands.describe(
    channel="Target text channel (defaults to current)",
    action="enable / disable / status (default: toggle)",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="toggle", value="toggle"),
        app_commands.Choice(name="enable", value="enable"),
        app_commands.Choice(name="disable", value="disable"),
        app_commands.Choice(name="status", value="status"),
    ]
)
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.checks.cooldown(1, 3.0)
async def set_global_chat(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    action: app_commands.Choice[str] | None = None,
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
    current = settings.get("is_global", False)
    act = (action.value if action else "toggle").lower()

    if act == "status":
        status_str = "enabled" if current else "disabled"
        await interaction.response.send_message(
            f"🌐 Global Language Chat is currently **{status_str}** "
            f"for {target_channel.mention}.",
            ephemeral=True,
        )
        return

    if act == "enable":
        new_status = True
    elif act == "disable":
        new_status = False
    else:
        new_status = not current

    await asyncio.to_thread(
        set_global_chat_channel_db, target_channel.id, new_status
    )
    status_str = "enabled" if new_status else "disabled"
    await interaction.response.send_message(
        f"🌐 Global Language Chat mode has been **{status_str}** "
        f"for {target_channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="setlanguage",
    description="Set or clear your personal target language for translations",
)
@app_commands.describe(
    language="Target language (leave empty to clear preference)",
)
@app_commands.autocomplete(language=language_autocomplete)
@app_commands.checks.cooldown(1, 3.0)
async def set_user_language(
    interaction: discord.Interaction,
    language: str | None = None,
):
    if language is None:
        await asyncio.to_thread(clear_user_language_db, interaction.user.id)
        await interaction.response.send_message(
            "Your preferred translation language has been cleared. "
            "Role-based or default (EN-US) will be used.",
            ephemeral=True,
        )
        return

    language = normalize_deepl_code(language)
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
    description="Add a rule to auto-translate a source language in a channel",
)
@app_commands.describe(
    channel="Channel to configure",
    detectlanguage="Source language to detect",
    translatedlanguage="Language to translate into",
)
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.autocomplete(
    detectlanguage=language_autocomplete,
    translatedlanguage=language_autocomplete,
)
@app_commands.checks.cooldown(1, 3.0)
async def translate_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    detectlanguage: str,
    translatedlanguage: str,
):
    detectlanguage = normalize_deepl_code(detectlanguage)
    translatedlanguage = normalize_deepl_code(translatedlanguage)

    if (
        detectlanguage not in VALID_LANGUAGE_CODES
        or translatedlanguage not in VALID_LANGUAGE_CODES
    ):
        await interaction.response.send_message(
            "Invalid language selection. Please choose valid options "
            "from the list.",
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
    name="removechannelrule",
    description="Remove a specific auto-translate rule from a channel",
)
@app_commands.describe(
    channel="Channel to configure",
    detectlanguage="Source language of the rule",
    translatedlanguage="Target language of the rule",
)
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.autocomplete(
    detectlanguage=language_autocomplete,
    translatedlanguage=language_autocomplete,
)
@app_commands.checks.cooldown(1, 3.0)
async def remove_channel_rule(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    detectlanguage: str,
    translatedlanguage: str,
):
    detectlanguage = normalize_deepl_code(detectlanguage)
    translatedlanguage = normalize_deepl_code(translatedlanguage)

    await asyncio.to_thread(
        remove_custom_channel_translation_db,
        channel.id,
        detectlanguage,
        translatedlanguage,
    )
    src_name = get_clean_lang_name(detectlanguage)
    target_name = get_clean_lang_name(translatedlanguage)
    await interaction.response.send_message(
        f"Removed rule for {channel.mention}: **{src_name}** ➔ **{target_name}** "
        f"(if it existed).",
        ephemeral=True,
    )


@bot.tree.command(
    name="clearchannelrules",
    description="Clear all auto-translate rules and defaults for a channel",
)
@app_commands.describe(channel="Channel to clear")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.checks.cooldown(1, 5.0)
async def clear_channel_rules(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
):
    await asyncio.to_thread(clear_channel_rules_db, channel.id)
    await interaction.response.send_message(
        f"All translation rules and defaults cleared for {channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="listchannelrules",
    description="List current auto-translate rules for a channel",
)
@app_commands.describe(channel="Channel to inspect (defaults to current)")
@app_commands.checks.cooldown(1, 3.0)
async def list_channel_rules(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
):
    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message(
            "Please specify a valid text channel.", ephemeral=True
        )
        return

    settings = await asyncio.to_thread(
        get_cached_channel_settings, target.id
    )
    rules = settings.get("rules", [])
    defaults = settings.get("defaults", [])
    is_global = settings.get("is_global", False)

    lines = [
        f"**Channel:** {target.mention}",
        f"**Global chat mode:** {'enabled' if is_global else 'disabled'}",
        "",
        "**Custom rules:**",
    ]
    if rules:
        for _, src, tgt in rules:
            lines.append(
                f"• {get_clean_lang_name(src)} (`{src}`) ➔ "
                f"{get_clean_lang_name(tgt)} (`{tgt}`)"
            )
    else:
        lines.append("• (none)")

    lines.append("")
    lines.append("**Default target languages:**")
    if defaults:
        for d in defaults:
            lines.append(f"• {get_clean_lang_name(d)} (`{d}`)")
    else:
        lines.append("• (none)")

    embed = discord.Embed(
        title="Channel Translation Settings",
        description="\n".join(lines),
        color=discord.Color.purple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="stats",
    description="View DeepL character usage and quota (local tracker)",
)
@app_commands.checks.cooldown(1, 5.0)
async def stats_command(interaction: discord.Interaction):
    used = await asyncio.to_thread(get_monthly_character_usage)
    remaining = max(0, DEEPL_MONTHLY_LIMIT - used)
    progress_bar = generate_progress_bar(used, DEEPL_MONTHLY_LIMIT)

    plan = "Free" if (DEEPL_AUTH_KEY or "").endswith(":fx") else "Pro"
    embed = discord.Embed(
        title="📊 DeepL Translation Usage Tracker",
        color=discord.Color.purple(),
    )
    embed.add_field(
        name="Monthly Character Usage (local tracker)",
        value=f"`{progress_bar}`",
        inline=False,
    )
    embed.add_field(name="Used Characters", value=f"**{used:,}**", inline=True)
    embed.add_field(
        name="Remaining Capacity", value=f"**{remaining:,}**", inline=True
    )
    embed.add_field(
        name="Plan / Soft Limit",
        value=f"**{plan}** · {DEEPL_MONTHLY_LIMIT:,} chars",
        inline=False,
    )
    embed.set_footer(
        text=(
            "Local tracker may differ slightly from the DeepL dashboard. "
            "Free plan hard limit is 500 000 / month."
        )
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="help",
    description="Show how to use the translation bot",
)
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 Translation Bot Help",
        color=discord.Color.purple(),
        description=(
            "Translate messages with DeepL. "
            "Set a preferred language, configure channels, or use reactions."
        ),
    )
    embed.add_field(
        name="Personal",
        value=(
            "`/setlanguage` – set or clear your target language\n"
            "Right-click a message → **Apps → Translate**\n"
            "React with a flag emoji (🇺🇸 🇰🇷 🇯🇵 …) for a quick translation"
        ),
        inline=False,
    )
    embed.add_field(
        name="Channel (Manage Channels)",
        value=(
            "`/setglobalchat` – toggle 🌐 prompt mode\n"
            "`/translatechannel` – add auto-translate rule\n"
            "`/removechannelrule` – remove one rule\n"
            "`/clearchannelrules` – clear all rules\n"
            "`/listchannelrules` – inspect current settings"
        ),
        inline=False,
    )
    embed.add_field(
        name="Other",
        value="`/stats` – character usage\n`/help` – this message",
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.context_menu(name="Translate")
@app_commands.checks.cooldown(1, 3.0)
async def translate_context_menu(
    interaction: discord.Interaction, message: discord.Message
):
    cleaned_content = strip_untranslatable_content(message.content or "")
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
    # Always process prefix commands first so !sync etc. stay responsive
    await bot.process_commands(message)

    if message.author.bot or not message.guild:
        return

    if not is_worth_translating(message.content or ""):
        return

    # Threads inherit parent channel settings
    channel_id = resolve_channel_id(message.channel)

    settings = await asyncio.to_thread(
        get_cached_channel_settings, channel_id
    )
    is_global = settings.get("is_global", False)

    # Global mode only adds a reaction — never bill DeepL, never throttle
    if is_global:
        try:
            await message.add_reaction("🌐")
        except Exception as e:
            log.debug(
                "Could not add 🌐 reaction in %s: %s", message.channel.id, e
            )
        return

    defaults = settings.get("defaults", [])
    rules = settings.get("rules", [])
    if not (defaults or rules):
        return

    # Rate-limit only when we are about to call DeepL
    if not _check_cooldown(message.author.id, message.channel.id):
        return

    text_to_translate = strip_untranslatable_content(message.content)
    if not text_to_translate:
        return

    # Collect every target that *might* apply (rules + defaults).
    # One parallel batch; each response includes detected_source_language,
    # so we filter after the fact and never do a separate probe call.
    candidate_targets: list[str] = []
    for _, _rule_src, rule_target in rules:
        candidate_targets.append(rule_target)
    for def_lang in defaults:
        candidate_targets.append(def_lang)

    seen: set[str] = set()
    ordered_targets: list[str] = []
    for t in candidate_targets:
        nt = normalize_deepl_code(t)
        if nt not in seen:
            seen.add(nt)
            ordered_targets.append(nt)

    if not ordered_targets:
        return

    translations = await translate_text(
        require_session(bot), text_to_translate, ordered_targets
    )
    if not translations:
        return

    # All successful results share the same detected source (same input text)
    detected_src = translations[0][2]

    # Which targets should actually be posted?
    allowed: set[str] = set()
    for _, rule_src, rule_target in rules:
        if is_lang_match(detected_src, rule_src):
            allowed.add(normalize_deepl_code(rule_target))
    for def_lang in defaults:
        if not is_lang_match(detected_src, def_lang):
            allowed.add(normalize_deepl_code(def_lang))

    for translated_text, target_code, detected_code, score in translations:
        if target_code not in allowed:
            continue  # candidate that didn't match after detection
        embed = build_translation_embed(
            author=message.author,
            translated_text=translated_text,
            src_code=detected_code,
            target_code=target_code,
            confidence=score,
        )
        try:
            await message.reply(embed=embed, mention_author=False)
        except Exception as e:
            log.warning("Failed to reply with translation: %s", e)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if bot.user and payload.user_id == bot.user.id:
        return

    emoji_str = str(payload.emoji)
    if emoji_str != "🌐" and emoji_str not in FLAG_TRANSLATIONS:
        return

    # Prefer cache
    user = bot.get_user(payload.user_id)
    if user is None:
        try:
            user = await bot.fetch_user(payload.user_id)
        except Exception:
            return
    if user.bot:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            return

    try:
        message = await channel.fetch_message(payload.message_id)
    except Exception as e:
        log.debug("Could not fetch reacted message: %s", e)
        return

    if not message or not message.content:
        return

    text_to_translate = strip_untranslatable_content(message.content)
    if not text_to_translate:
        return

    # 🌐 prompt is free (no DeepL call) — skip cooldown
    if emoji_str == "🌐":
        clean_prompted_messages_cache()
        clean_cooldown_caches()
        if payload.message_id in PROMPTED_MESSAGES:
            return
        PROMPTED_MESSAGES[payload.message_id] = datetime.now(
            timezone.utc
        ).timestamp()
        view = GlobalTranslateView()
        try:
            await message.reply(
                content="🌐 **Translate Here • 點擊翻譯 • 翻訳 • 번역**",
                view=view,
                mention_author=False,
            )
        except Exception as e:
            log.warning("Failed to send translate prompt: %s", e)
        return

    # Flag emoji → actual translation — apply rate limit
    if not _check_cooldown(payload.user_id, payload.channel_id):
        return

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
        try:
            await message.reply(embed=embed, mention_author=False)
        except Exception as e:
            log.warning("Failed to reply with flag translation: %s", e)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)