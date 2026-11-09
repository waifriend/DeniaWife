"""Discord translation bot:
Turso is used for storing server configurations. No personal or server data is stored.
Translation provider: Azure Translator.
"""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import emoji
import libsql
import requests

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("translator")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name} in .env")
    return value


TOKEN = required("DISCORD_TOKEN")
AZURE_KEY = required("AZURE_TRANSLATOR_KEY")
AZURE_REGION = required("AZURE_TRANSLATOR_REGION")  # e.g. "eastus", "global", "westeurope"
TURSO_URL = required("TURSO_DATABASE_URL")
TURSO_KEY = required("TURSO_AUTH_TOKEN")
GLOBE = "🌐"

# Internal codes (unchanged from previous version)
LANGUAGES = {
    "AR": "Arabic", "BG": "Bulgarian", "CS": "Czech", "DA": "Danish",
    "DE": "German", "EL": "Greek", "EN-US": "English (US)",
    "EN-GB": "English (UK)", "ES": "Spanish", "ET": "Estonian",
    "FI": "Finnish", "FR": "French", "HE": "Hebrew", "HI": "Hindi",
    "HU": "Hungarian", "ID": "Indonesian", "IT": "Italian",
    "JA": "Japanese", "KO": "Korean", "LT": "Lithuanian", "LV": "Latvian",
    "NB": "Norwegian Bokmål", "NL": "Dutch", "PL": "Polish",
    "PT-BR": "Portuguese (Brazil)", "PT-PT": "Portuguese (Portugal)",
    "RO": "Romanian", "RU": "Russian", "SK": "Slovak", "SL": "Slovenian",
    "SV": "Swedish", "TH": "Thai", "TR": "Turkish", "UK": "Ukrainian",
    "VI": "Vietnamese", "ZH-HANS": "Chinese (Simplified)",
    "ZH-HANT": "Chinese (Traditional)",
}
ALIASES = {
    "EN": "EN-US", "KR": "KO", "JP": "JA", "CN": "ZH-HANS",
    "ZH": "ZH-HANS", "ZH-CN": "ZH-HANS", "ZH-SG": "ZH-HANS",
    "ZH-TW": "ZH-HANT", "ZH-HK": "ZH-HANT", "TW": "ZH-HANT",
    "NO": "NB", "PT": "PT-BR", "ES-ES": "ES", "ES-419": "ES",
}
FLAGS = {
    "🇩🇪": "DE", "🇦🇹": "DE", "🇬🇧": "EN-GB", "🇺🇸": "EN-US",
    "🇦🇺": "EN-GB", "🇫🇷": "FR", "🇪🇸": "ES", "🇲🇽": "ES",
    "🇮🇹": "IT", "🇯🇵": "JA", "🇰🇷": "KO", "🇨🇳": "ZH-HANS",
    "🇹🇼": "ZH-HANT", "🇭🇰": "ZH-HANT", "🇮🇩": "ID", "🇧🇷": "PT-BR",
    "🇵🇹": "PT-PT", "🇷🇺": "RU", "🇺🇦": "UK", "🇳🇱": "NL",
    "🇵🇱": "PL", "🇹🇷": "TR", "🇸🇪": "SV", "🇩🇰": "DA",
    "🇳🇴": "NB", "🇫🇮": "FI", "🇨🇿": "CS", "🇬🇷": "EL",
    "🇷🇴": "RO", "🇭🇺": "HU", "🇧🇬": "BG", "🇸🇰": "SK",
    "🇸🇮": "SL", "🇪🇪": "ET", "🇱🇻": "LV", "🇱🇹": "LT",
    "🇸🇦": "AR", "🇻🇳": "VI", "🇹🇭": "TH", "🇮🇳": "HI", "🇮🇱": "HE",
}

# Internal code → Azure Translator code
TO_AZURE = {
    "AR": "ar", "BG": "bg", "CS": "cs", "DA": "da", "DE": "de",
    "EL": "el", "EN-US": "en", "EN-GB": "en", "ES": "es", "ET": "et",
    "FI": "fi", "FR": "fr", "HE": "he", "HI": "hi", "HU": "hu",
    "ID": "id", "IT": "it", "JA": "ja", "KO": "ko", "LT": "lt",
    "LV": "lv", "NB": "nb", "NL": "nl", "PL": "pl",
    "PT-BR": "pt", "PT-PT": "pt-pt", "RO": "ro", "RU": "ru",
    "SK": "sk", "SL": "sl", "SV": "sv", "TH": "th", "TR": "tr",
    "UK": "uk", "VI": "vi", "ZH-HANS": "zh-Hans", "ZH-HANT": "zh-Hant",
}

# Azure code → preferred internal code (for detected_source_lang)
FROM_AZURE = {
    "ar": "AR", "bg": "BG", "cs": "CS", "da": "DA", "de": "DE",
    "el": "EL", "en": "EN-US", "es": "ES", "et": "ET", "fi": "FI",
    "fr": "FR", "he": "HE", "hi": "HI", "hu": "HU", "id": "ID",
    "it": "IT", "ja": "JA", "ko": "KO", "lt": "LT", "lv": "LV",
    "nb": "NB", "nl": "NL", "pl": "PL", "pt": "PT-BR", "pt-pt": "PT-PT",
    "ro": "RO", "ru": "RU", "sk": "SK", "sl": "SL", "sv": "SV",
    "th": "TH", "tr": "TR", "uk": "UK", "vi": "VI",
    "zh-Hans": "ZH-HANS", "zh-Hant": "ZH-HANT", "zh-CN": "ZH-HANS",
    "zh-TW": "ZH-HANT", "zh": "ZH-HANS",
}


def canonical(value: str) -> str:
    value = value.strip().upper().replace("_", "-")
    for code, name in LANGUAGES.items():
        if value == name.upper():
            return code
    return ALIASES.get(value, value)


def family(code: str) -> str:
    return code.upper().split("-")[0]


def unchanged_translation(original: str, translated: str) -> bool:
    """Ignore whitespace differences when checking for redundant output."""
    def normalize(text):
        return re.sub(r"\s+", " ", text).strip()
    return normalize(original) == normalize(translated)


class Store:
    """Serialize all remote SQL on one worker; never block Discord's event loop."""
    def __init__(self):
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turso")
        self.conn = None

    async def sql(self, statement, args=()):
            def execute():
                for attempt in range(2):
                    if self.conn is None:
                        self.conn = libsql.connect(TURSO_URL, auth_token=TURSO_KEY)
                    try:
                        cursor = self.conn.execute(statement, args)
                        rows = cursor.fetchall() if statement.lstrip().upper().startswith("SELECT") else []
                        self.conn.commit()
                        return rows
                    except Exception:
                        try:
                            if self.conn:
                                self.conn.rollback()
                        except Exception:
                            pass
                        self.conn = None  # Drop stale connection so it reconnects fresh on retry
                        if attempt == 1:
                            raise
            return await asyncio.get_running_loop().run_in_executor(self.worker, execute)

    async def setup(self):
        for statement in (
            "CREATE TABLE IF NOT EXISTS tr2_users (user_id TEXT PRIMARY KEY, language TEXT NOT NULL, origin TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tr2_channels (guild_id TEXT NOT NULL, channel_id TEXT PRIMARY KEY, language TEXT, is_global INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS tr2_rules (guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, PRIMARY KEY(channel_id,source,target))",
            "CREATE TABLE IF NOT EXISTS tr2_prompts (original_id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL, channel_id TEXT NOT NULL)",
        ):
            await self.sql(statement)

    async def user_language(self, uid, locale=None):
        """Replace a provisional join/reaction default only when a real locale arrives."""
        loc = locale.value if locale and hasattr(locale, "value") else (locale or "EN-US")
        language = canonical(loc)
        if language not in engine.targets:
            language = canonical(language.split("-")[0])
        if language not in engine.targets:
            language = "EN-US"
        origin = "locale" if locale else "provisional"
        await self.sql(
            "INSERT INTO tr2_users VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language, origin=excluded.origin "
            "WHERE tr2_users.origin='provisional' AND excluded.origin='locale'",
            (str(uid), language, origin),
        )
        return (await self.sql("SELECT language,origin FROM tr2_users WHERE user_id=?", (str(uid),)))[0]

    async def close(self):
        if self.conn:
            await asyncio.get_running_loop().run_in_executor(self.worker, self.conn.close)
        self.worker.shutdown(wait=True)


# Protect code, URLs/domains, Discord syntax, email addresses and every Unicode emoji.
PROTECTED = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`|https?://[^\s<>]+|www\.[^\s<>]+"
    r"|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"
    r"|\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[^\s<>]*)?"
    r"|<a?:\w+:\d+>|<[@#][!&]?\d+>|</[^>]+>|<t:[^>]+>|@everyone|@here"
)


def prepare(text):
    """Return XML with immutable slots, plus text suitable for skip heuristics."""
    spans = [(m.start(), m.end()) for m in PROTECTED.finditer(text)]
    spans += [(m["match_start"], m["match_end"]) for m in emoji.emoji_list(text)]
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, visible, slots, position = [], [], [], 0
    for start, end in merged:
        plain = text[position:start]
        parts += [escape(plain), f"<keep>{len(slots)}</keep>"]
        visible.append(plain)
        slots.append(text[start:end])
        position = end
    parts.append(escape(text[position:]))
    visible.append(text[position:])
    return "<root>" + "".join(parts) + "</root>", "".join(visible), slots


def eligible(text):
    """Conservative heuristic, not a promise to recognize every nonsense string."""
    if not text.strip() or text.lstrip().startswith(("/", "!")):
        return False
    plain = prepare(text)[1]
    letters = "".join(c for c in plain if c.isalpha())
    if not letters or len(set(letters.casefold())) == 1 and len(letters) > 4:
        return False
    if re.search(r"(.)\1{7,}", letters):
        return False
    if re.fullmatch(r"[a-zA-Z\s]+", plain.strip()) and len(letters) > 5:
        if not re.search(r"[aeiouy]", letters, re.I):
            return False
    return True


@dataclass(frozen=True)
class Translation:
    text: str
    source: str
    target: str


class Engine:
    """Azure Translator, bounded TTL caching and serialized cache-fill requests."""

    def __init__(self):
        self.endpoint = "https://api.cognitive.microsofttranslator.com"
        self.targets = {}
        self.sources = set()
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()

    async def setup(self):
        # Load supported languages from Azure (no auth required for /languages)
        def fetch_languages():
            resp = requests.get(
                f"{self.endpoint}/languages",
                params={"api-version": "3.0", "scope": "translation"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("translation", {})
            targets = {}
            sources = set()
            for code, info in data.items():
                # Prefer our internal codes when possible
                internal = FROM_AZURE.get(code) or FROM_AZURE.get(code.lower())
                if internal:
                    targets[internal] = info.get("name", internal)
                    sources.add(family(internal))
                else:
                    # Fallback: keep Azure code upper-cased for unknown languages
                    upper = code.upper().replace("-", "-")
                    targets[upper] = info.get("name", upper)
                    sources.add(family(upper))
            return targets, sources

        self.targets, self.sources = await asyncio.to_thread(fetch_languages)
        # Ensure our preferred codes are present
        for code, name in LANGUAGES.items():
            self.targets.setdefault(code, name)
            self.sources.add(family(code))
        LANGUAGES.update(self.targets)
        if "EN-US" not in self.targets:
            raise RuntimeError("Azure language list did not include English")

    def target(self, value):
        code = canonical(value)
        if code not in self.targets:
            raise ValueError(f"Unsupported target language: {value}. Use command autocomplete.")
        return code

    def source(self, value):
        code = family(canonical(value))
        if code not in self.sources:
            raise ValueError(f"Unsupported source language: {value}.")
        return code

    def _to_azure(self, internal: str) -> str:
        return TO_AZURE.get(internal, internal.lower())

    def _from_azure(self, azure_code: str) -> str:
        return FROM_AZURE.get(azure_code, azure_code.upper())

    async def translate(self, text, target):
        if not eligible(text):
            raise ValueError("This message has no suitable text to translate.")
        target = self.target(target)
        key = (text, target)
        async with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() - cached[0] < 900:
                self.cache.move_to_end(key)
                return cached[1]

            xml, _, slots = prepare(text)
            azure_target = self._to_azure(target)

            def call_azure():
                headers = {
                    "Ocp-Apim-Subscription-Key": AZURE_KEY,
                    "Ocp-Apim-Subscription-Region": AZURE_REGION,
                    "Content-Type": "application/json",
                    "X-ClientTraceId": str(uuid.uuid4()),
                }
                params = {
                    "api-version": "3.0",
                    "to": azure_target,
                }
                body = [{"text": xml}]
                resp = requests.post(
                    f"{self.endpoint}/translate",
                    params=params,
                    headers=headers,
                    json=body,
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()

            data = await asyncio.to_thread(call_azure)
            if not data or not data[0].get("translations"):
                raise ValueError("Azure returned an empty translation.")

            translated_xml = data[0]["translations"][0]["text"]
            detected = data[0].get("detectedLanguage", {}).get("language", "en")
            detected_internal = self._from_azure(detected)

            # Restore protected slots (same algorithm as before)
            root = ET.fromstring(f"<root>{translated_xml}</root>" if not translated_xml.startswith("<root>") else translated_xml)
            found = []
            for node in root.iter("keep"):
                index = int(node.text or "-1")
                if not 0 <= index < len(slots):
                    raise ValueError("Protected content could not be restored safely.")
                found.append(index)
                node.text = slots[index]
            if sorted(found) != list(range(len(slots))):
                raise ValueError("Translation changed protected content; refusing unsafe output.")

            final_text = "".join(root.itertext())
            translated = Translation(final_text, detected_internal, target)

            self.cache[key] = (time.monotonic(), translated)
            self.cache.move_to_end(key)
            while len(self.cache) > 512:
                self.cache.popitem(last=False)
            return translated


store = Store()
engine = Engine()


def embeds(message, result):
    """Split long outputs without silently truncating the original translation."""
    text = result.text
    chunks = [text[i : i + 3500] for i in range(0, len(text), 3500)] or ["(empty)"]
    if len(chunks) > 10 or len(text) > 5400:
        raise ValueError("Translation exceeds Discord output limits.")
    output = []
    for index, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=LANGUAGES.get(result.target, result.target),
            description=chunk,
            color=0xB05CC6,
            url=message.jump_url,
        )
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        if index == 0:
            embed.set_thumbnail(url=message.author.display_avatar.url)
        embed.set_footer(
            text=f"Original language: {result.source} • Provider: Azure • {index + 1}/{len(chunks)}"
        )
        output.append(embed)
    return output


async def private_error(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


class Tree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Do not initialize a language during read-only inspection.
        if (
            interaction.command is None
            or interaction.command.name != "checkuserlanguage"
        ):
            await store.user_language(
                interaction.user.id,
                interaction.locale.value,
            )
        return True

    async def on_error(self, interaction, error):
        cause = getattr(error, "original", error)
        log.error("Slash command failed", exc_info=(type(cause), cause, cause.__traceback__))
        if isinstance(cause, (ValueError, app_commands.CheckFailure)):
            text = str(cause)
        else:
            text = "The operation failed. Check the bot logs, Azure quota, and Turso connection."
        await private_error(interaction, text)


class TranslateView(discord.ui.View):
    """A stateless persistent button; the reply reference identifies its source."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Translate / 翻譯",
        emoji=GLOBE,
        custom_id="tr2:translate",
        style=discord.ButtonStyle.secondary,
    )
    async def translate_button(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            target, _ = await store.user_language(interaction.user.id, interaction.locale.value)
            prompt = interaction.message
            rows = await store.sql(
                "SELECT original_id FROM tr2_prompts WHERE prompt_id=? AND channel_id=?",
                (str(prompt.id), str(interaction.channel_id)),
            )
            if not rows or not interaction.guild or not isinstance(interaction.user, discord.Member):
                raise ValueError("This translation prompt is no longer registered.")
            perms = interaction.channel.permissions_for(interaction.user)
            if not (perms.view_channel and perms.read_message_history):
                raise ValueError("You no longer have access to this message.")
            original = await interaction.channel.fetch_message(int(rows[0][0]))
            result = await engine.translate(original.content, target)
            await interaction.followup.send(embeds=embeds(original, result), ephemeral=True)
        except ValueError as error:
            await private_error(interaction, str(error))
        except Exception:
            log.exception("Translation button failed")
            await private_error(
                interaction,
                "Translation failed. The message may be deleted, or the provider unavailable.",
            )


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            tree_cls=Tree,
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.prompt_lock = asyncio.Lock()
        self.cooldowns = OrderedDict()

    async def setup_hook(self):
        await engine.setup()
        await store.setup()
        self.add_view(TranslateView())
        self.add_view(LanguageSelectView())

    def limited(self, key, seconds=5):
        now = time.monotonic()
        old = self.cooldowns.get(key, 0)
        if now - old < seconds:
            return True
        self.cooldowns[key] = now
        self.cooldowns.move_to_end(key)
        while len(self.cooldowns) > 4096:
            self.cooldowns.popitem(last=False)
        return False

    async def close(self):
        try:
            await super().close()
        finally:
            await store.close()


class LanguageSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name, value=code)
            for code, name in list(LANGUAGES.items())[:25]
        ]
        super().__init__(
            placeholder="Choose your preferred language...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="tr2:language_select",
        )

    async def callback(self, interaction: discord.Interaction):
        code = self.values[0]
        await store.sql(
            "INSERT INTO tr2_users VALUES(?,?,'manual') "
            "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,origin='manual'",
            (str(interaction.user.id), code),
        )
        await interaction.response.send_message(
            f"Your language has been set to {LANGUAGES[code]} (`{code}`).", ephemeral=True
        )


class LanguageSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(LanguageSelect())



bot = Bot()


async def language_choices(interaction, current):
    return [
        app_commands.Choice(name=f"{LANGUAGES.get(c, c)} ({c})"[:100], value=c)
        for c in sorted(engine.targets)
        if current.casefold() in f"{c} {LANGUAGES.get(c, c)}".casefold()
    ][:25]


async def source_choices(interaction, current):
    return [
        app_commands.Choice(name=f"{LANGUAGES.get(c, c)} ({c})"[:100], value=c)
        for c in sorted(engine.sources)
        if current.casefold() in f"{c} {LANGUAGES.get(c, c)}".casefold()
    ][:25]


@bot.tree.command(description="Set your personal translation language.")
@app_commands.autocomplete(language=language_choices)
async def setselflanguage(interaction: discord.Interaction, language: str):
    code = engine.target(language)
    await store.sql(
        "INSERT INTO tr2_users VALUES(?,?,'manual') "
        "ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,origin='manual'",
        (str(interaction.user.id), code),
    )
    await interaction.followup.send(
        f"Your language is now {LANGUAGES[code]} ({code}).", ephemeral=True
    )


@bot.tree.command(description="Show your assigned translation language.")
async def selflanguage(interaction: discord.Interaction):
    code, origin = await store.user_language(interaction.user.id, interaction.locale.value)
    await interaction.followup.send(
        f"Your language: {LANGUAGES.get(code, code)} ({code}). Assignment: {origin}.",
        ephemeral=True,
    )


@bot.tree.command(description="Set one automatic language for a channel; use off to clear.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(language=language_choices)
async def setchannellanguage(
    interaction: discord.Interaction, channel: discord.TextChannel, language: str
):
    code = None if language.lower() == "off" else engine.target(language)
    await store.sql(
        "INSERT INTO tr2_channels(guild_id,channel_id,language) VALUES(?,?,?) "
        "ON CONFLICT(channel_id) DO UPDATE SET language=excluded.language",
        (str(interaction.guild_id), str(channel.id), code),
    )
    await interaction.followup.send(
        f"{channel.mention}: {code or 'automatic channel translation disabled'}.",
        ephemeral=True,
    )


@bot.tree.command(description="Enable or disable globe reactions in a channel.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def setglobalchannel(
    interaction: discord.Interaction, channel: discord.TextChannel, enabled: bool = True
):
    await store.sql(
        "INSERT INTO tr2_channels(guild_id,channel_id,is_global) VALUES(?,?,?) "
        "ON CONFLICT(channel_id) DO UPDATE SET is_global=excluded.is_global",
        (str(interaction.guild_id), str(channel.id), int(enabled)),
    )
    await interaction.followup.send(
        f"{channel.mention}: global mode {'enabled' if enabled else 'disabled'}.",
        ephemeral=True,
    )


@bot.tree.command(
    description="Translate a source language into a target; enabled:false removes the rule."
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.autocomplete(
    targeted_language=language_choices, translated_language=language_choices
)
async def translate(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    targeted_language: str,
    translated_language: str,
    enabled: bool = True,
):
    source = engine.source(targeted_language)
    target = engine.target(translated_language)

    args = (str(interaction.guild_id), str(channel.id), source, target)
    if enabled:
        if family(source) == family(target):
            raise ValueError("Choose two different languages for a translation rule.")
        await store.sql("INSERT OR IGNORE INTO tr2_rules VALUES(?,?,?,?)", args)
    else:
        await store.sql(
            "DELETE FROM tr2_rules WHERE guild_id=? AND channel_id=? AND source=? AND target=?",
            args,
        )
    await interaction.followup.send(
        f"{channel.mention}: {source} → {target} {'enabled' if enabled else 'removed'}.",
        ephemeral=True,
    )


@bot.tree.command(description="List channel languages, global channels and translation rules.")
@app_commands.guild_only()
async def listslanguage(interaction: discord.Interaction):
    rows = await store.sql(
        "SELECT channel_id,language,is_global FROM tr2_channels WHERE guild_id=?",
        (str(interaction.guild_id),),
    )
    rules = await store.sql(
        "SELECT channel_id,source,target FROM tr2_rules WHERE guild_id=? ORDER BY channel_id,source,target",
        (str(interaction.guild_id),),
    )
    lines = ["Channel settings:"]
    for cid, language, glob in rows:
        channel = interaction.guild.get_channel(int(cid))
        if channel and channel.permissions_for(interaction.user).view_channel:
            lines.append(
                f"{channel.mention}: language={language or 'none'}; global={bool(glob)}"
            )
    lines.append("Translation rules:")
    for cid, source, target in rules:
        channel = interaction.guild.get_channel(int(cid))
        if channel and channel.permissions_for(interaction.user).view_channel:
            lines.append(f"{channel.mention}: {source} → {target}")
    pages, page = [], ""
    for line in lines:
        if len(page) + len(line) > 1800:
            pages.append(page)
            page = ""
        page += line + "\n"
    pages.append(page)
    if len(pages) > 4:
        import io
        await interaction.followup.send(
            file=discord.File(
                io.BytesIO("\n".join(lines).encode()), filename="channel-settings.txt"
            ),
            ephemeral=True,
        )
    else:
        for page in pages:
            await interaction.followup.send(page, ephemeral=True)


@bot.command(name="sync")
@commands.is_owner()
@commands.guild_only()
@commands.max_concurrency(1, per=commands.BucketType.default, wait=False)
async def sync_commands(ctx: commands.Context):
    """Publish current global commands and clear legacy commands in this server."""
    tree = ctx.bot.tree
    if not tree.get_commands(guild=None):
        await ctx.reply(
            "Sync cancelled: no global commands are loaded.",
            mention_author=False,
        )
        return
    try:
        synced = await tree.sync()
    except discord.HTTPException:
        log.exception("Global command sync failed")
        await ctx.reply(
            "Global sync failed. Check the bot logs.",
            mention_author=False,
        )
        return
    previous_guild_commands = tree.get_commands(guild=ctx.guild)
    try:
        tree.clear_commands(guild=ctx.guild)
        await tree.sync(guild=ctx.guild)
    except discord.HTTPException:
        for command in previous_guild_commands:
            tree.add_command(command, guild=ctx.guild, override=True)
        log.exception("Legacy command cleanup failed in guild %s", ctx.guild.id)
        await ctx.reply(
            f"Published {len(synced)} global commands, but could not clear "
            "the old server-specific commands. Check the logs and retry !sync.",
            mention_author=False,
        )
        return
    await ctx.reply(
        f"Published {len(synced)} global commands and cleared this bot's "
        "old server-specific slash/context-menu commands in this server.",
        mention_author=False,
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NotOwner):
        await ctx.reply(
            "Only the bot application owner can run !sync.", mention_author=False
        )
    else:
        log.error(
            "Prefix command failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        await ctx.reply("Command failed; check the bot logs.", mention_author=False)


@bot.event
async def on_ready():
    log.info(
        "Online as %s. Run !sync as the bot owner to publish commands.", bot.user
    )


@bot.event
async def on_member_join(member):
    if member.bot:
        return
    await store.user_language(member.id)
    try:
        embed = discord.Embed(
            title="Welcome! Please choose your language",
            description="Please select your preferred translation language using the dropdown menu below.",
            color=0xB05CC6,
        )
        await member.send(embed=embed, view=LanguageSelectView())
    except discord.Forbidden:
        log.info("DM blocked for new member %s", member.id)
    except Exception:
        log.exception("Failed to send language selection DM to %s", member.id)


@bot.event
async def on_message(message):
    if message.author.bot or message.webhook_id:
        return
    await bot.process_commands(message)
    if not message.guild:
        return
    try:
        await store.user_language(message.author.id)
        cid = str(message.channel.id)
        settings = await store.sql(
            "SELECT language,is_global FROM tr2_channels WHERE channel_id=?", (cid,)
        )
        language, glob = settings[0] if settings else (None, 0)
        if glob:
            try:
                await message.add_reaction(GLOBE)
            except discord.HTTPException:
                log.warning("Cannot add global reaction in channel %s", cid)
        if not eligible(message.content):
            return
        rules = await store.sql(
            "SELECT source,target FROM tr2_rules WHERE channel_id=? ORDER BY source,target",
            (cid,),
        )
        if not language and not rules:
            return
        probe_target = language or rules[0][1]
        probe = await engine.translate(message.content, probe_target)
        targets = set()
        if language and family(language) != family(probe.source):
            targets.add(language)
        targets.update(
            target
            for source, target in rules
            if family(source) == family(probe.source)
            and family(target) != family(probe.source)
        )
        for target in sorted(targets):
            try:
                result = (
                    probe
                    if target == probe_target
                    else await engine.translate(message.content, target)
                )
                if unchanged_translation(message.content, result.text):
                    continue
                await message.reply(
                    embeds=embeds(message, result),
                    mention_author=False,
                )
            except Exception:
                log.exception("Automatic translation target %s failed", target)
    except Exception:
        log.exception("Message processing failed for %s", message.id)


@bot.event
async def on_raw_reaction_add(payload):
    if not payload.guild_id or payload.user_id == bot.user.id:
        return
    symbol = str(payload.emoji)
    if symbol != GLOBE and symbol not in FLAGS:
        return
    try:
        guild = bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = payload.member or await guild.fetch_member(payload.user_id)
        if member.bot:
            return
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(
            payload.channel_id
        )
        perms = channel.permissions_for(member)
        if not (perms.view_channel and perms.read_message_history):
            return
        if bot.limited((member.id, symbol), 5):
            return
        await store.user_language(member.id)
        original = await channel.fetch_message(payload.message_id)
        if original.author.bot or original.webhook_id:
            return
        if symbol == GLOBE:
            settings = await store.sql(
                "SELECT is_global FROM tr2_channels WHERE channel_id=?",
                (str(channel.id),),
            )
            if not settings or not settings[0][0]:
                return
            async with bot.prompt_lock:
                rows = await store.sql(
                    "SELECT prompt_id FROM tr2_prompts WHERE original_id=?",
                    (str(original.id),),
                )
                if rows:
                    try:
                        await channel.fetch_message(int(rows[0][0]))
                        return
                    except discord.NotFound:
                        pass
                prompt = await original.reply(
                    "🌐 Translate Here • 點擊翻譯 • 翻訳 • 번역",
                    view=TranslateView(),
                    mention_author=False,
                )
                await store.sql(
                    "INSERT INTO tr2_prompts VALUES(?,?,?) "
                    "ON CONFLICT(original_id) DO UPDATE SET prompt_id=excluded.prompt_id,channel_id=excluded.channel_id",
                    (str(original.id), str(prompt.id), str(channel.id)),
                )
        else:
            if not eligible(original.content):
                return
            target = engine.target(FLAGS[symbol])
            result = await engine.translate(original.content, target)
            try:
                await member.send(embeds=embeds(original, result))
            except discord.Forbidden:
                log.info("DM blocked for user %s", member.id)
    except Exception:
        log.exception("Reaction translation failed for message %s", payload.message_id)


@bot.tree.command(
    name="checkuserlanguage",
    description="Administrator: check a member's assigned translation language.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user="Member whose assigned language you want to check")
async def checkuserlanguage(interaction: discord.Interaction, user: discord.Member):
    """Read the stored language without creating an assignment for the target."""
    rows = await store.sql(
        "SELECT language FROM tr2_users WHERE user_id = ?",
        (str(user.id),),
    )
    if not rows or rows[0][0] is None:
        await interaction.followup.send("NULL", ephemeral=True)
        return
    language = rows[0][0]
    await interaction.followup.send(
        f"{user.mention}: {LANGUAGES.get(language, language)} (`{language}`)",
        ephemeral=True,
    )

@bot.tree.command(
    name="sendlanguagerequest",
    description="Administrator: send a language selection DM to a member.",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(user="Member to send the language selection request to")
async def sendlanguagerequest(interaction: discord.Interaction, user: discord.Member):
    if user.bot:
        raise ValueError("Cannot send a language request to a bot.")
    try:
        embed = discord.Embed(
            title="Language Selection Request",
            description="Please select your preferred translation language using the dropdown menu below.",
            color=0xB05CC6,
        )
        await user.send(embed=embed, view=LanguageSelectView())
        await interaction.followup.send(
            f"Successfully sent language selection request to {user.mention}.",
            ephemeral=True,
        )
    except discord.Forbidden:
        raise ValueError(f"Could not send DM to {user.mention}. Their DMs might be closed.")




if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
