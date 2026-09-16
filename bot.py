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

# Load environment variables from .env file
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
AZURE_TRANSLATE_KEY = os.getenv("AZURE_TRANSLATE_KEY")
AZURE_TRANSLATE_REGION = os.getenv("AZURE_TRANSLATE_REGION")
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

AZURE_MONTHLY_LIMIT = 2_000_000

# Supported language choices
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

# Role-based default language mapping
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

# Regex matching untranslatable Discord noise
EMOTE_REGEX = re.compile(r"(<a?:[a-zA-Z0-9_]+:\d+>|:[a-zA-Z0-9_]+:)")
CODE_BLOCK_REGEX = re.compile(r"```[\s\S]*?```|`[^`]+`")
URL_REGEX = re.compile(r"https?://\S+|www\.\S+")
MENTION_REGEX = re.compile(r"<@!?&?\d+>|<#\d+>")

# ------------------------------------------------------------------------------
# Database Helpers (Turso / libSQL)
# ------------------------------------------------------------------------------


def get_db_connection():
    return libsql.connect(
        database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN
    )


def init_db():
    conn = get_db_connection()

    # Automatic schema migration for existing channel_languages table
    try:
        res = conn.execute("PRAGMA table_info(channel_languages)").fetchall()
        if res:
            pk_count = sum(1 for col in res if col[5] > 0)
            if pk_count == 1:
                # Old single primary key detected; migrate to composite primary key (channel_id, target_lang)
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS channel_languages_migrated (channel_id TEXT, target_lang TEXT, PRIMARY KEY (channel_id, target_lang))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO channel_languages_migrated SELECT channel_id, target_lang FROM channel_languages"
                )
                conn.execute("DROP TABLE channel_languages")
                conn.execute(
                    "ALTER TABLE channel_languages_migrated RENAME TO channel_languages"
                )
                print("Successfully migrated channel_languages table schema.")
    except Exception as e:
        print(f"Database migration check: {e}")

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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_channel_translations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            src_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL
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
    conn.close()


def add_channel_language_db(channel_id: int, target_lang: str):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO channel_languages (channel_id, target_lang)
        VALUES (?, ?)
    """,
        (str(channel_id), target_lang),
    )
    conn.commit()
    conn.close()


def remove_channel_language_db(channel_id: int, target_lang: str):
    conn = get_db_connection()
    conn.execute(
        """
        DELETE FROM channel_languages WHERE channel_id = ? AND target_lang = ?
    """,
        (str(channel_id), target_lang),
    )
    conn.commit()
    conn.close()


def get_channel_languages_db(channel_id: int) -> list[str]:
    conn = get_db_connection()
    res = conn.execute(
        "SELECT target_lang FROM channel_languages WHERE channel_id = ?",
        (str(channel_id),),
    )
    rows = res.fetchall()
    conn.close()
    return [r[0] for r in rows]


def log_character_usage(char_count: int):
    if char_count <= 0:
        return
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO character_usage (month_year, char_count)
        VALUES (?, ?)
        ON CONFLICT(month_year) DO UPDATE SET char_count = char_count + excluded.char_count
    """,
        (current_month, char_count),
    )
    conn.commit()
    conn.close()


def get_monthly_character_usage() -> int:
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn = get_db_connection()
    res = conn.execute(
        "SELECT char_count FROM character_usage WHERE month_year = ?",
        (current_month,),
    )
    row = res.fetchone()
    conn.close()
    return row[0] if row else 0


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
    conn = get_db_connection()
    res = conn.execute(
        "SELECT 1 FROM user_help_seen WHERE user_id = ?", (str(user_id),)
    )
    row = res.fetchone()
    conn.close()
    return row is not None


def mark_user_seen_help(user_id: int):
    conn = get_db_connection()
    conn.execute(
        "INSERT OR IGNORE INTO user_help_seen (user_id) VALUES (?)",
        (str(user_id),),
    )
    conn.commit()
    conn.close()


def add_custom_channel_translation_db(
    channel_id: int, src_lang: str, target_lang: str
):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO custom_channel_translations (channel_id, src_lang, target_lang)
        VALUES (?, ?, ?)
    """,
        (str(channel_id), src_lang, target_lang),
    )
    conn.commit()
    conn.close()


def get_custom_channel_translations_db(
    channel_id: int,
) -> list[tuple[int, str, str]]:
    conn = get_db_connection()
    res = conn.execute(
        "SELECT id, src_lang, target_lang FROM custom_channel_translations WHERE channel_id = ? ORDER BY id ASC",
        (str(channel_id),),
    )
    rows = res.fetchall()
    conn.close()
    return rows


def delete_custom_channel_translation_by_index_db(
    channel_id: int, index: int
) -> tuple[str, str] | None:
    rules = get_custom_channel_translations_db(channel_id)
    if 1 <= index <= len(rules):
        rule_id, src_lang, target_lang = rules[index - 1]
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM custom_channel_translations WHERE id = ?",
            (rule_id,),
        )
        conn.commit()
        conn.close()
        return src_lang, target_lang
    return None


# ------------------------------------------------------------------------------
# Content Stripper & Helpers
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


def get_effective_user_language(user: discord.User | discord.Member) -> str:
    db_lang = get_user_language_db(user.id)
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


# ------------------------------------------------------------------------------
# Azure API Integration
# ------------------------------------------------------------------------------


def protect_emotes_html(text: str) -> str:
    return EMOTE_REGEX.sub(r'<span class="notranslate">\1</span>', text)


def restore_emotes_html(text: str) -> str:
    return (
        text.replace('<span class="notranslate">', "")
        .replace("</span>", "")
        .replace('<span class="notranslate" >', "")
    )


async def detect_text_language(text: str) -> tuple[str, int]:
    if not AZURE_TRANSLATE_KEY or not AZURE_TRANSLATE_REGION:
        return "Unknown", 0

    protected_text = protect_emotes_html(text)
    url = "https://api.cognitive.microsofttranslator.com/detect?api-version=3.0"
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
                    detected_info = data[0]
                    detected_code = detected_info.get("language", "Unknown")
                    score = detected_info.get("score", 1.0)
                    return detected_code, int(score * 100)
    except Exception as e:
        print(f"Language detection error: {e}")
    return "Unknown", 0


async def translate_text(text: str, target_lang: str) -> tuple[str, str, int]:
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

                    log_character_usage(len(protected_text))

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
# UI Components for /setchannellanguage
# ------------------------------------------------------------------------------


class AddLanguageSelect(discord.ui.Select):
    def __init__(self, parent_view):
        options = [
            discord.SelectOption(label=name, value=code)
            for name, code in list(LANGUAGES.items())[:25]
        ]
        super().__init__(
            placeholder="Select a language to add...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message(
                "You are not allowed to control this menu.", ephemeral=True
            )
            return

        selected_lang = self.values[0]
        add_channel_language_db(self.parent_view.channel.id, selected_lang)
        await self.parent_view.refresh(interaction)


class RemoveLanguageSelect(discord.ui.Select):
    def __init__(self, parent_view, current_langs: list[str]):
        options = []
        for lang_code in current_langs:
            name = get_clean_lang_name(lang_code)
            options.append(
                discord.SelectOption(
                    label=f"{name} ({lang_code})", value=lang_code
                )
            )
        super().__init__(
            placeholder="Select a language to remove...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message(
                "You are not allowed to control this menu.", ephemeral=True
            )
            return

        selected_lang = self.values[0]
        remove_channel_language_db(self.parent_view.channel.id, selected_lang)
        await self.parent_view.refresh(interaction)


class ChannelLanguageView(discord.ui.View):
    def __init__(self, channel: discord.TextChannel, author_id: int):
        super().__init__(timeout=120)
        self.channel = channel
        self.author_id = author_id
        self.state = "main"
        self.build_components()

    def build_components(self):
        self.clear_items()
        current_langs = get_channel_languages_db(self.channel.id)

        if self.state == "main":
            add_btn = discord.ui.Button(
                label="Add Language",
                style=discord.ButtonStyle.primary,
                emoji="➕",
            )
            add_btn.callback = self.on_add_click
            self.add_item(add_btn)

            rem_btn = discord.ui.Button(
                label="Remove Language",
                style=discord.ButtonStyle.danger,
                emoji="➖",
                disabled=len(current_langs) == 0,
            )
            rem_btn.callback = self.on_remove_click
            self.add_item(rem_btn)

        elif self.state == "add":
            self.add_item(AddLanguageSelect(self))
            back_btn = discord.ui.Button(
                label="Back / Cancel", style=discord.ButtonStyle.secondary
            )
            back_btn.callback = self.on_back_click
            self.add_item(back_btn)

        elif self.state == "remove":
            if current_langs:
                self.add_item(RemoveLanguageSelect(self, current_langs))
            back_btn = discord.ui.Button(
                label="Back / Cancel", style=discord.ButtonStyle.secondary
            )
            back_btn.callback = self.on_back_click
            self.add_item(back_btn)

    def build_embed(self) -> discord.Embed:
        current_langs = get_channel_languages_db(self.channel.id)
        embed = discord.Embed(
            title=f"⚙️ Target Languages for #{self.channel.name}",
            color=discord.Color.purple(),
        )
        if current_langs:
            lang_list = "\n".join(
                [
                    f"• **{get_clean_lang_name(code)}** (`{code}`)"
                    for code in current_langs
                ]
            )
            embed.add_field(
                name="Active Target Languages", value=lang_list, inline=False
            )
        else:
            embed.add_field(
                name="Active Target Languages",
                value="*No target languages currently set.*",
                inline=False,
            )
        embed.set_footer(
            text="Use the controls below to add or remove default target languages."
        )
        return embed

    async def refresh(self, interaction: discord.Interaction):
        self.state = "main"
        self.build_components()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self
        )

    async def on_add_click(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "You are not allowed to control this menu.", ephemeral=True
            )
            return
        self.state = "add"
        self.build_components()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self
        )

    async def on_remove_click(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "You are not allowed to control this menu.", ephemeral=True
            )
            return
        self.state = "remove"
        self.build_components()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self
        )

    async def on_back_click(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "You are not allowed to control this menu.", ephemeral=True
            )
            return
        self.state = "main"
        self.build_components()
        await interaction.response.edit_message(
            embed=self.build_embed(), view=self
        )


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
    bot.tree.copy_global_to(guild=ctx.guild)
    synced = await bot.tree.sync(guild=ctx.guild)
    await ctx.send(f"Synced {len(synced)} command(s) to this server.")


# ------------------------------------------------------------------------------
# Slash Commands & Context Menu App
# ------------------------------------------------------------------------------


@bot.tree.command(
    name="setchannellanguage",
    description="Manage default target auto-translation languages for a channel",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def set_channel_language(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
):
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Please specify a text channel.", ephemeral=True
        )
        return

    view = ChannelLanguageView(target_channel, interaction.user.id)
    embed = view.build_embed()
    await interaction.response.send_message(
        embed=embed, view=view, ephemeral=True
    )


@bot.tree.command(
    name="stats",
    description="View real-time Azure AI character usage and monthly free limit progress",
)
async def stats_command(interaction: discord.Interaction):
    used = get_monthly_character_usage()
    remaining = max(0, AZURE_MONTHLY_LIMIT - used)
    progress_bar = generate_progress_bar(used, AZURE_MONTHLY_LIMIT)

    embed = discord.Embed(
        title="📊 Azure Translation Usage & Quota Tracker",
        color=discord.Color.purple(),
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
    embed.add_field(
        name="Monthly Limit", value=f"**{AZURE_MONTHLY_LIMIT:,}**", inline=True
    )
    embed.set_footer(
        text="Usage resets on the 1st of every month • Azure AI Free Tier"
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="help",
    description="View instructions and guide for using the Translator bot",
)
async def help_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    user_id = interaction.user.id
    is_first_time = not has_user_seen_help(user_id)
    target_lang = get_effective_user_language(interaction.user)

    title = "🌐 Translator Bot Guide"
    description = "Here is how to use translation features across the server!"

    fields = [
        (
            "👤 Personal Target Language Settings",
            "• **`/setlanguage`**: Override your preferred translation language.\n"
            "• **`/mylanguage`**: Check your current active target language.\n"
            "• **Role Auto-Detection**: If you haven't set a language manually, the bot automatically checks your server roles to pick your default!",
        ),
        (
            "🚩 Quick Flag Reactions (Public)",
            "React to any message with a flag to translate it directly in chat for everyone:\n"
            "• 🇹🇼 ➔ **Traditional Chinese** (`zh-Hant`)\n"
            "• 🇰🇷 ➔ **Korean** (`ko`)\n"
            "• 🇷🇺 ➔ **Russian** (`ru`)\n"
            "• 🇺🇸 ➔ **English** (`en`)\n"
            "• 🌐 ➔ Sends a **private DM** translation using your personal target language.",
        ),
        (
            "🖱️ Context Menu Translation",
            "Right-click any message ➔ **Apps** ➔ **Translate** to receive a private translation card.",
        ),
        (
            "⚙️ Admin & Utility Commands",
            "• **`/stats`**: View current monthly character usage against the 2,000,000 Azure limit.\n"
            "• **`/setchannellanguage`**: Add/remove automatic channel-wide target languages.\n"
            "• **`/translatechannel`**: Add custom language-specific translation rules.\n"
            "• **`/checkchannel`**: View active channel translation rules.\n"
            "• **`/clearchannel`**: Remove a custom translation rule by index.",
        ),
    ]

    footer_text = "Powered by Azure AI • Emote & Short Message Filter Active"

    if target_lang.lower() != "en":
        title, _, _ = await translate_text(title, target_lang)
        description, _, _ = await translate_text(description, target_lang)
        footer_text, _, _ = await translate_text(footer_text, target_lang)

        async def translate_field(name: str, val: str):
            t_name, _, _ = await translate_text(name, target_lang)
            t_val, _, _ = await translate_text(val, target_lang)
            return t_name, t_val

        fields = await asyncio.gather(
            *[translate_field(n, v) for n, v in fields]
        )

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.purple(),
    )

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
        mark_user_seen_help(user_id)

    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)

    embed.set_footer(text=footer_text)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="translatechannel",
    description="Set a rule to translate a specific detected language into a target language for a channel",
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
    add_custom_channel_translation_db(
        channel.id, detectlanguage, translatedlanguage
    )
    src_name = get_clean_lang_name(detectlanguage)
    target_name = get_clean_lang_name(translatedlanguage)
    await interaction.response.send_message(
        f"Added custom translation rule for {channel.mention}: **{src_name}** (`{detectlanguage}`) ➔ **{target_name}** (`{translatedlanguage}`).",
        ephemeral=True,
    )


@bot.tree.command(
    name="checkchannel",
    description="Display translation settings and custom rules configured for a channel",
)
async def check_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
):
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Please run this command in a text channel or specify a text channel parameter.",
            ephemeral=True,
        )
        return

    default_langs = get_channel_languages_db(target_channel.id)
    custom_rules = get_custom_channel_translations_db(target_channel.id)

    embed = discord.Embed(
        title=f"⚙️ Translation Rules for #{target_channel.name}",
        color=discord.Color.purple(),
    )

    if default_langs:
        def_names = ", ".join(
            [f"**{get_clean_lang_name(l)}** (`{l}`)" for l in default_langs]
        )
        embed.add_field(
            name="🌐 Default Channel Target Languages",
            value=def_names,
            inline=False,
        )
    else:
        embed.add_field(
            name="🌐 Default Channel Target Languages",
            value="*None set (Use `/setchannellanguage` to configure)*",
            inline=False,
        )

    if custom_rules:
        rules_text = ""
        for idx, (_, src, target) in enumerate(custom_rules, start=1):
            src_name = get_clean_lang_name(src)
            target_name = get_clean_lang_name(target)
            rules_text += f"`[{idx}]` **{src_name}** (`{src}`) ➔ **{target_name}** (`{target}`)\n"
        embed.add_field(
            name="🔀 Custom Language Rules (`/translatechannel`)",
            value=rules_text,
            inline=False,
        )
    else:
        embed.add_field(
            name="🔀 Custom Language Rules (`/translatechannel`)",
            value="*No custom rules set*",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="clearchannel",
    description="Remove a custom translation rule from a channel using its index number",
)
@app_commands.checks.has_permissions(manage_channels=True)
async def clear_channel(
    interaction: discord.Interaction,
    index: int,
    channel: discord.TextChannel | None = None,
):
    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Please run this command in a text channel or specify a text channel parameter.",
            ephemeral=True,
        )
        return

    deleted_rule = delete_custom_channel_translation_by_index_db(
        target_channel.id, index
    )
    if deleted_rule:
        src_lang, target_lang = deleted_rule
        src_name = get_clean_lang_name(src_lang)
        target_name = get_clean_lang_name(target_lang)
        await interaction.response.send_message(
            f"Removed rule `[{index}]` from {target_channel.mention}: **{src_name}** (`{src_lang}`) ➔ **{target_name}** (`{target_lang}`).",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"Invalid rule index `[{index}]` for {target_channel.mention}. Use `/checkchannel` to list active rules and their indices.",
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


# Context Menu Translate App
@bot.tree.context_menu(name="Translate")
async def translate_context_menu(
    interaction: discord.Interaction, message: discord.Message
):
    cleaned_content = strip_untranslatable_content(message.content)
    if not cleaned_content:
        await interaction.response.send_message(
            "Cannot translate empty text, URLs, code blocks, or pure emojis.",
            ephemeral=True,
        )
        return

    target_lang = get_effective_user_language(interaction.user)
    translated, src_lang, score = await translate_text(
        cleaned_content, target_lang
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

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    if not is_worth_translating(message.content):
        await bot.process_commands(message)
        return

    text_to_translate = strip_untranslatable_content(message.content)

    custom_rules = get_custom_channel_translations_db(message.channel.id)
    default_target_langs = get_channel_languages_db(message.channel.id)

    if not custom_rules and not default_target_langs:
        await bot.process_commands(message)
        return

    detected_lang, _ = await detect_text_language(text_to_translate)

    target_langs = []

    # 1. Collect targets from custom channel rules
    if custom_rules:
        for _, src, target in custom_rules:
            if is_lang_match(detected_lang, src):
                if target not in target_langs:
                    target_langs.append(target)

    # 2. Collect targets from default channel languages
    if default_target_langs:
        for default_lang in default_target_langs:
            if not is_lang_match(detected_lang, default_lang):
                if default_lang not in target_langs:
                    target_langs.append(default_lang)

    # 3. Translate and send embed cards for all applicable targets
    for target_lang in target_langs:
        translated, src_lang, confidence = await translate_text(
            text_to_translate, target_lang
        )

        if not is_lang_match(src_lang, target_lang):
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

        text_to_translate = strip_untranslatable_content(message.content)
        if not text_to_translate:
            return

        if emoji_str in FLAG_TRANSLATIONS:
            target_lang = FLAG_TRANSLATIONS[emoji_str]
            translated, src_lang, confidence = await translate_text(
                text_to_translate, target_lang
            )

            embed = build_translation_embed(
                author=message.author,
                translated_text=translated,
                src_code=src_lang,
                target_code=target_lang,
                confidence=confidence,
            )
            await channel.send(embed=embed)

        elif emoji_str == "🌐":
            guild = (
                bot.get_guild(payload.guild_id) if payload.guild_id else None
            )
            member = guild.get_member(user.id) if guild else user
            target_lang = get_effective_user_language(member or user)

            translated, src_lang, confidence = await translate_text(
                text_to_translate, target_lang
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