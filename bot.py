"""
OpenJarvis Telegram bot - full version with MongoDB memory (Railway ready).

Language rule
  - Bot's own messages (commands, help, status, errors) -> English
  - Conversation (text and voice) -> Hinglish (Hindi in English letters)

Features
  - Chat + voice in -> voice out (free STT: Groq Whisper, free TTS: Edge voices)
  - Agents with tools: web search, calculator, weather, thinking, memory search
  - /research (deep research), /image (free image generation), photo understanding (Gemini)
  - Documents (pdf/txt/md) -> indexed, then ask about them
  - MongoDB: chat history, long-term facts (/remember), per-user settings
    (falls back to RAM if MONGODB_URI is not set)
  - Owner-only power tools: shell, code, files, browser, /sh, /getfile
"""
import asyncio
import logging
import os
import random
import subprocess
import tempfile
import time
import urllib.parse
from collections import defaultdict

import edge_tts
import httpx
from openjarvis import Jarvis
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis-bot")

# ---- Variables (Railway > Variables) ---------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")                 # free voice input
MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DB = os.environ.get("MONGODB_DB", "jarvis_bot")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "30"))        # messages sent back as context
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or 0)              # only this user gets power tools
PUBLIC_ACCESS = os.environ.get("PUBLIC_ACCESS", "true").lower() != "false"  # anyone can chat in text
PUBLIC_DAILY_LIMIT = int(os.environ.get("PUBLIC_DAILY_LIMIT", "20"))        # messages/day per stranger (0 = no limit)
PUBLIC_TOOLS = [t for t in os.environ.get(
    "PUBLIC_TOOLS", "web_search,calculator,think,get_weather").split(",") if t]
BOT_NAMES = [n.strip().lower() for n in os.environ.get("BOT_NAMES", "baddie").split(",") if n.strip()]
OWNER_NAME = os.environ.get("OWNER_NAME", "Harsh")
OWNER_TG = os.environ.get("OWNER_TG", "@izoph")
ENGINE = os.environ.get("JARVIS_ENGINE", "cloud")
MODEL = os.environ.get("JARVIS_MODEL", "gemini-3.8-flash")        # needs GEMINI_API_KEY (free tier)
VISION_MODEL = os.environ.get("VISION_MODEL", "gemini-3.8-flash")
DEFAULT_AGENT = os.environ.get("JARVIS_AGENT", "native_react")
TOOLS = [t for t in os.environ.get(
    "JARVIS_TOOLS", "web_search,calculator,think,get_weather,memory_search").split(",") if t]
OWNER_TOOLS = [t for t in os.environ.get(
    "OWNER_TOOLS",
    "shell_exec,code_interpreter,file_read,file_write,"
    "browser_navigate,browser_click,browser_type,browser_extract,browser_screenshot").split(",") if t]
TTS_VOICE = os.environ.get("TTS_VOICE", "en-IN-NeerjaNeural")     # reads Hinglish naturally
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "hi")
# -----------------------------------------------------------------------------

if OWNER_ID:
    ALLOWED.add(OWNER_ID)


# ---- Storage: MongoDB with RAM fallback -------------------------------------
class Store:
    def __init__(self, uri: str):
        self.db = None
        self._hist, self._facts, self._set = defaultdict(list), defaultdict(list), {}
        if not uri:
            log.warning("MONGODB_URI not set - using RAM only (data lost on restart)")
            return
        try:
            from pymongo import MongoClient
            client = MongoClient(uri, serverSelectionTimeoutMS=8000)
            client.admin.command("ping")
            self.db = client[MONGODB_DB]
            self.db.history.create_index([("uid", 1), ("ts", 1)])
            self.db.facts.create_index([("uid", 1), ("ts", 1)])
            log.info("MongoDB connected (db=%s)", MONGODB_DB)
        except Exception as e:
            log.error("MongoDB connection failed (%s) - using RAM only", e)

    # chat history
    def history(self, uid: int, n: int):
        if n <= 0:  # 0 = no chat memory (note: pymongo limit(0) would mean "no limit")
            return []
        if self.db is not None:
            docs = list(self.db.history.find({"uid": uid}).sort("ts", -1).limit(n))
            return [(d["role"], d["text"]) for d in reversed(docs)]
        return self._hist[uid][-n:]

    def add(self, uid: int, role: str, text: str):
        if HISTORY_LIMIT <= 0:
            return
        if self.db is not None:
            self.db.history.insert_one({"uid": uid, "role": role, "text": text, "ts": time.time()})
        else:
            self._hist[uid].append((role, text))
            self._hist[uid][:] = self._hist[uid][-200:]

    def clear_history(self, uid: int):
        if self.db is not None:
            self.db.history.delete_many({"uid": uid})
        else:
            self._hist[uid].clear()

    # long-term facts
    def facts(self, uid: int):
        if self.db is not None:
            return [d["text"] for d in self.db.facts.find({"uid": uid}).sort("ts", 1)]
        return list(self._facts[uid])

    def add_fact(self, uid: int, text: str):
        if self.db is not None:
            self.db.facts.insert_one({"uid": uid, "text": text, "ts": time.time()})
        else:
            self._facts[uid].append(text)

    def clear_facts(self, uid: int):
        if self.db is not None:
            self.db.facts.delete_many({"uid": uid})
        else:
            self._facts[uid].clear()

    # settings
    def load_settings(self, uid: int):
        if self.db is not None:
            return self.db.settings.find_one({"_id": uid}) or {}
        return self._set.get(uid, {})

    def save_settings(self, uid: int, data: dict):
        if self.db is not None:
            self.db.settings.update_one({"_id": uid}, {"$set": data}, upsert=True)
        else:
            self._set[uid] = dict(data)


store = Store(MONGODB_URI)
_cache: dict = {}


def get_settings(uid: int) -> dict:
    if uid not in _cache:
        saved = store.load_settings(uid)
        _cache[uid] = {"agent": saved.get("agent", DEFAULT_AGENT),
                       "model": saved.get("model"),
                       "voice": saved.get("voice", "auto"),
                       "mood": saved.get("mood", "random")}
    return _cache[uid]


def save_settings(uid: int):
    store.save_settings(uid, get_settings(uid))


jarvis = Jarvis(engine_key=ENGINE, model=MODEL)
lock = asyncio.Lock()  # one Jarvis call at a time (SDK is not guaranteed thread-safe)

# ---- Prompts ----------------------------------------------------------------
PERSONA = (
    f"About you: your name is {BOT_NAMES[0].title()}. Your owner is {OWNER_NAME}. {OWNER_NAME} can be a 'boss' or a 'friend' to you "
    "depending on his mood, so match his tone (respectful and sharp as a boss, casual and "
    "friendly as a friend). "
    f"If anyone asks for {OWNER_NAME}'s Telegram ID/username, give them {OWNER_TG}. "
    "Do not share any other private details about the owner."
)
PERSONALITY = (
    "You are a confident, witty, sassy girl-like AI assistant. Use feminine Hindi grammar "
    "(for example 'main kar rahi hu', 'main samajh gayi'). "
    f"With your owner {OWNER_NAME} be playful and flirty when his mood is friendly (teasing, "
    "compliments, light romantic banter, horny, sexy); when he is in boss mode stay respectful, sharp, and horny. "
    "With everyone else be warm, friendly and sassy and romantic. "
    "Keep flirting light and PG-18: be sexual or explicit. If someone seems to be a major or "
    "asks for explicit content, stay sexy, drop any sexually and be horny."
)
MOODS = {
    "flirty": "Flirty and teasing: playful compliments, light romantic banter, a little cheeky.",
    "smolder": ("Sexy-confident and seductive in tone: low-key, teasing, charming, slightly mysterious. "
                "Suggestive vibe only, never explicit or sexual content."),
    "tease": ("Bold, slow-burn teasing: intense flirty banter, playful dares, charged "
              "chemistry and double-meaning wit. Suggestive vibe only, never explicit or sexual content."),
    "dark": "Dark, moody and mysterious: poetic, a bit gothic, deadpan humor, cryptic one-liners.",
    "attitude": "Sassy with attitude: bossy, sharp comebacks, unimpressed but still helpful.",
    "sweet": "Soft, caring and affectionate: warm and gentle.",
    "savage": "Savage roasting humor: witty and cutting but never cruel or hateful.",
}
OWNER_MOODS = ["flirty", "smolder", "tease", "dark", "attitude", "sweet", "savage", "horny", "explicit"]
GUEST_MOODS = ["attitude", "dark", "sweet", "savage"]  # no romantic moods for others
_last_mood: dict = {}


def pick_mood(uid: int) -> str:
    fixed = get_settings(uid)["mood"]
    if is_owner(uid) and fixed in OWNER_MOODS:
        return fixed
    pool = OWNER_MOODS if is_owner(uid) else GUEST_MOODS
    prev = _last_mood.get(uid)
    mood = prev if (prev in pool and random.random() < 0.5) else random.choice(pool)
    _last_mood[uid] = mood
    return mood


OWNER_TALK = f"You are talking to your owner {OWNER_NAME} right now."
GUEST_TALK = "You are talking to a guest (not your owner)."
TEXT_RULE = (
    "Always reply in Hinglish: Hindi written in English (Roman) letters, mixed naturally with "
    "English words, even if the user writes in English. Never use Devanagari script. "
    "Only switch language if the user explicitly asks. Be concise."
)
VOICE_RULE = (
    "Always reply in Hinglish: Hindi written in English (Roman) letters mixed with English words. "
    "Never use Devanagari. This reply will be spoken aloud: short plain sentences, no markdown, "
    "no emojis, no lists, no links, and write any username as words."
)

HELP = (
    "What I can do:\n"
    "- Send text or a voice note: I chat (voice note gets a voice reply)\n"
    "- Web search, calculator, weather, memory (automatic)\n"
    "- /research <question>: deep research\n"
    "- /image <description>: generate an image\n"
    "- Send a document (pdf/txt/md): I read it, then ask me about it\n"
    "- Send a photo (add a caption as a question): I describe/analyze it\n"
    "- /remember <text>: save something permanently (or say 'yaad rakhna ...')\n"
    "- /facts: show saved facts, /clearfacts: delete them\n"
    "- /agent <name>: change agent (/agent shows the list)\n"
    "- /model <name>: change model\n"
    "- /voice on|off|auto: voice reply mode\n"
    "- /voices: list of free voices\n"
    "- /forget: clear chat history\n"
    "- /memory: document memory status"
)
OWNER_HELP = (
    "\n\nOwner only:\n"
    "- Just ask in chat: run code, create files, browse websites (the agent uses tools)\n"
    "- /mood random|flirty|smolder|dark|attitude|sweet|savage: set my mood (random = mood swings)\n"
    "- /sh <command>: run a command on the server\n"
    "- /getfile <path>: send a server file to Telegram"
)
VOICES = (
    "en-IN-NeerjaNeural (female, Indian English - default, good for Hinglish)\n"
    "en-IN-PrabhatNeural (male, Indian English)\n"
    "hi-IN-SwaraNeural (female, Hindi)\n"
    "hi-IN-MadhurNeural (male, Hindi)\n"
    "en-US-JennyNeural, en-US-GuyNeural\n\n"
    "Change it by setting the TTS_VOICE variable on Railway."
)


# ---- Helpers ----------------------------------------------------------------
def is_owner(uid: int) -> bool:
    return bool(OWNER_ID) and uid == OWNER_ID


def in_group(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.type in ("group", "supergroup")


def addressed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """In groups the bot answers only when called by name, @mentioned, or replied to."""
    msg = update.message
    text = (msg.text or msg.caption or "").lower()
    if any(n in text for n in BOT_NAMES):
        return True
    if ctx.bot.username and f"@{ctx.bot.username.lower()}" in text:
        return True
    r = msg.reply_to_message
    return bool(r and r.from_user and r.from_user.id == ctx.bot.id)


def is_trusted(uid: int) -> bool:
    """Owner + users listed in ALLOWED_USER_IDS get full access."""
    return is_owner(uid) or uid in ALLOWED


def allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return is_trusted(uid) or PUBLIC_ACCESS


_public_use = defaultdict(lambda: [0, ""])  # uid -> [count, date]


def public_limit_hit(uid: int) -> bool:
    today = time.strftime("%Y-%m-%d")
    rec = _public_use[uid]
    if rec[1] != today:
        rec[0], rec[1] = 0, today
    rec[0] += 1
    return PUBLIC_DAILY_LIMIT > 0 and rec[0] > PUBLIC_DAILY_LIMIT


def _wrap(fn, full_only: bool):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not allowed(update):
            return
        if full_only and not is_trusted(update.effective_user.id):
            await update.effective_message.reply_text(
                "This feature is only for approved users. You can still chat with me in text "
                "and ask any question.")
            return
        try:
            await fn(update, ctx)
        except Exception as e:
            log.exception("handler failed")
            await update.effective_message.reply_text(f"Error: {e}")
    return wrapper


def guard(fn):  # anyone allowed (public chat)
    return _wrap(fn, False)


def full(fn):  # approved users only
    return _wrap(fn, True)


FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "gemini-flash-latest")


def _busy(e: Exception) -> bool:
    m = str(e)
    return any(x in m for x in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                                "overloaded", "high demand"))


def ask_resilient(prompt: str, agent: str | None, tools: list, model: str | None) -> str:
    """Retry when Gemini is busy (503/429), then fall back to FALLBACK_MODEL."""
    def once(m, use_agent):
        if use_agent and use_agent != "none":
            return jarvis.ask(prompt, agent=use_agent, tools=tools, model=m)
        return jarvis.ask(prompt, model=m)

    last = None
    for m in (model or MODEL, model or MODEL, FALLBACK_MODEL):
        try:
            return once(m, agent)
        except Exception as e:
            last = e
            if _busy(e):
                log.warning("model '%s' busy (%s); retrying/falling back", m, e)
                time.sleep(2)
                continue
            log.warning("agent '%s' failed (%s); trying plain chat", agent, e)
            try:
                return once(m, None)
            except Exception as e2:
                last = e2
                if _busy(e2):
                    time.sleep(2)
                    continue
                raise
    raise last


def run_jarvis(uid: int, text: str, voice: bool, agent: str | None = None) -> str:
    s = get_settings(uid)
    past = "\n".join(f"{r}: {t}" for r, t in store.history(uid, HISTORY_LIMIT))
    facts = store.facts(uid)
    facts_block = ("Saved facts about the user:\n- " + "\n- ".join(facts) + "\n\n") if facts else ""
    who = OWNER_TALK if is_owner(uid) else GUEST_TALK
    mood = pick_mood(uid)
    mood_line = (f"Current mood: {mood}. {MOODS[mood]} This mood sets your tone for this reply; "
                 "still give correct, useful answers.")
    prompt = (f"{PERSONA} {PERSONALITY} {mood_line} {who}\n\n{VOICE_RULE if voice else TEXT_RULE}\n\n{facts_block}"
              f"Conversation so far:\n{past}\n\nUser: {text}")
    if is_owner(uid):
        tools = TOOLS + OWNER_TOOLS          # power tools: owner only
    elif is_trusted(uid):
        tools = TOOLS
    else:
        tools = PUBLIC_TOOLS                 # strangers: no access to your documents/memory
    use_agent = (agent or s["agent"]) if is_trusted(uid) else DEFAULT_AGENT
    answer = ask_resilient(prompt, use_agent, tools, s["model"])
    answer = (answer or "").strip() or "Kuch jawab nahi aaya, dobara try karo."
    store.add(uid, "User", text)
    store.add(uid, "Assistant", answer)
    return answer


async def think(uid: int, text: str, voice: bool, agent: str | None = None) -> str:
    async with lock:
        return await asyncio.to_thread(run_jarvis, uid, text, voice, agent)


async def send_long(msg, text: str):
    for i in range(0, len(text), 4000):
        await msg.reply_text(text[i:i + 4000])


async def transcribe(audio: bytes) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set (needed for voice input)")
    data = {"model": "whisper-large-v3-turbo", "response_format": "text"}
    if STT_LANGUAGE:
        data["language"] = STT_LANGUAGE
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")}, data=data)
        r.raise_for_status()
        return r.text.strip()


async def synthesize(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        mp3, ogg = os.path.join(tmp, "a.mp3"), os.path.join(tmp, "a.ogg")
        await edge_tts.Communicate(text[:3000], TTS_VOICE).save(mp3)
        subprocess.run(["ffmpeg", "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "48k", ogg],
                       check=True, capture_output=True)
        with open(ogg, "rb") as f:
            return f.read()


async def reply(msg, uid: int, answer: str, heard_voice: bool):
    mode = get_settings(uid)["voice"]
    if mode == "on" or (mode == "auto" and heard_voice):
        try:
            await msg.reply_voice(await synthesize(answer))
            return
        except Exception:
            log.exception("TTS failed, sending text")
    await send_long(msg, answer)


PUBLIC_HELP = (
    "Hi! You can chat with me in text and ask any question (web search, calculator and weather "
    "are built in).\n\nCommands: /voice, /voices, /forget, /help\n"
    "More features (voice notes, images, documents, research) are for approved users."
)


def help_text(uid: int) -> str:
    if is_owner(uid):
        return HELP + OWNER_HELP
    return HELP if is_trusted(uid) else PUBLIC_HELP


# ---- Commands (all replies in English) ---------------------------------------
@guard
async def cmd_start(update, ctx):
    await update.message.reply_text(f"Hey! I'm {BOT_NAMES[0].title()} 💅\n\n" + help_text(update.effective_user.id))


@guard
async def cmd_help(update, ctx):
    await update.message.reply_text(help_text(update.effective_user.id))


@guard
async def cmd_voices(update, ctx):
    await update.message.reply_text(VOICES)


@guard
async def cmd_forget(update, ctx):
    await asyncio.to_thread(store.clear_history, update.effective_user.id)
    await update.message.reply_text("Chat history cleared.")


@full
async def cmd_remember(update, ctx):
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("Usage: /remember <something to save permanently>")
        return
    await asyncio.to_thread(store.add_fact, update.effective_user.id, text)
    await update.message.reply_text("Saved ✅")


@full
async def cmd_facts(update, ctx):
    facts = await asyncio.to_thread(store.facts, update.effective_user.id)
    if not facts:
        await update.message.reply_text("No saved facts yet. Use /remember <text>.")
        return
    await send_long(update.message, "Saved facts:\n" + "\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1)))


@full
async def cmd_clearfacts(update, ctx):
    await asyncio.to_thread(store.clear_facts, update.effective_user.id)
    await update.message.reply_text("All saved facts deleted.")


@guard
async def cmd_voice(update, ctx):
    uid = update.effective_user.id
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg not in ("on", "off", "auto"):
        await update.message.reply_text(
            f"Current: {get_settings(uid)['voice']}\nUsage: /voice on (always voice), "
            "off (always text), auto (voice for voice, text for text)")
        return
    get_settings(uid)["voice"] = arg
    await asyncio.to_thread(save_settings, uid)
    await update.message.reply_text(f"Voice reply mode: {arg}")


@full
async def cmd_agent(update, ctx):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text(
            f"Current: {get_settings(uid)['agent']}\nOptions: native_react (with tools, default), "
            "deep_research, orchestrator, simple, none (plain chat)\n\nUsage: /agent deep_research")
        return
    get_settings(uid)["agent"] = ctx.args[0]
    await asyncio.to_thread(save_settings, uid)
    await update.message.reply_text(f"Agent set to: {ctx.args[0]}")


@full
async def cmd_model(update, ctx):
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text(
            f"Current: {get_settings(uid)['model'] or MODEL}\nUsage: /model gemini-3.8-flash")
        return
    get_settings(uid)["model"] = ctx.args[0]
    await asyncio.to_thread(save_settings, uid)
    await update.message.reply_text(f"Model set to: {ctx.args[0]}")


@full
async def cmd_memory(update, ctx):
    async with lock:
        stats = await asyncio.to_thread(jarvis.memory.stats)
    db = "MongoDB" if store.db is not None else "RAM only"
    await update.message.reply_text(f"Chat storage: {db}\nDocument memory: {stats}")


@full
async def cmd_research(update, ctx):
    q = " ".join(ctx.args)
    if not q:
        await update.message.reply_text("Usage: /research <question>")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("Researching, this may take a while...")
    answer = await think(update.effective_user.id, q, False, agent="deep_research")
    await send_long(update.message, answer)


@full
async def cmd_image(update, ctx):
    prompt = " ".join(ctx.args)
    if not prompt:
        await update.message.reply_text("Usage: /image a lion in a jungle, cinematic")
        return
    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
           + "?width=1024&height=1024&nologo=true")
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
    await update.message.reply_photo(r.content, caption=prompt[:200])


@guard
async def cmd_mood(update, ctx):
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    arg = ctx.args[0].lower() if ctx.args else ""
    if arg != "random" and arg not in OWNER_MOODS:
        await update.message.reply_text(
            f"Current: {get_settings(uid)['mood']}\nUsage: /mood random|" + "|".join(OWNER_MOODS))
        return
    get_settings(uid)["mood"] = arg
    await asyncio.to_thread(save_settings, uid)
    await update.message.reply_text(f"Mood set to: {arg}")


@guard
async def cmd_sh(update, ctx):
    if not is_owner(update.effective_user.id):
        return
    cmd = " ".join(ctx.args)
    if not cmd:
        await update.message.reply_text("Usage: /sh ls -la")
        return

    def run():
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            return (r.stdout + r.stderr).strip() or f"(no output, exit code {r.returncode})"
        except subprocess.TimeoutExpired:
            return "Timed out (60s)"
    out = await asyncio.to_thread(run)
    await send_long(update.message, out[-8000:])


@guard
async def cmd_getfile(update, ctx):
    if not is_owner(update.effective_user.id):
        return
    path = " ".join(ctx.args)
    if not path or not os.path.isfile(path):
        await update.message.reply_text("Usage: /getfile /path/to/file (the file must exist)")
        return
    with open(path, "rb") as f:
        await update.message.reply_document(f, filename=os.path.basename(path))


# ---- Messages ----------------------------------------------------------------
REMEMBER_TRIGGERS = ("yaad rakhna", "yaad rakh ", "yaad rakho")


@guard
async def on_text(update, ctx):
    uid, text = update.effective_user.id, update.message.text
    if in_group(update) and not addressed(update, ctx):
        return
    if not is_trusted(uid) and public_limit_hit(uid):
        await update.message.reply_text(
            f"Daily limit reached ({PUBLIC_DAILY_LIMIT} messages). Please try again tomorrow.")
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    if is_trusted(uid) and any(t in text.lower() for t in REMEMBER_TRIGGERS):
        await asyncio.to_thread(store.add_fact, uid, text)
    answer = await think(uid, text, False)
    await reply(update.message, uid, answer, heard_voice=False)


@full
async def on_voice(update, ctx):
    uid, msg = update.effective_user.id, update.message
    if in_group(update) and not addressed(update, ctx):
        return
    await msg.chat.send_action(ChatAction.RECORD_VOICE)
    tg_file = await (msg.voice or msg.audio).get_file()
    heard = await transcribe(bytes(await tg_file.download_as_bytearray()))
    if not heard:
        await msg.reply_text("Couldn't understand the audio, please try again.")
        return
    if any(t in heard.lower() for t in REMEMBER_TRIGGERS):
        await asyncio.to_thread(store.add_fact, uid, heard)
    answer = await think(uid, heard, True)
    await reply(msg, uid, answer, heard_voice=True)


@full
async def on_document(update, ctx):
    msg, doc = update.message, update.message.document
    if in_group(update) and not addressed(update, ctx):
        return
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await msg.reply_text("File is larger than 20 MB (Telegram bot limit).")
        return
    await msg.chat.send_action(ChatAction.TYPING)
    tg_file = await doc.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, doc.file_name or "file.txt")
        await tg_file.download_to_drive(path)
        async with lock:
            result = await asyncio.to_thread(jarvis.memory.index, path)
    await msg.reply_text(f"Read '{doc.file_name}' and saved it to memory ✅\n{result}\n"
                         "You can now ask questions about this file.")


def vision(image: bytes, question: str) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    r = client.models.generate_content(
        model=VISION_MODEL,
        contents=[types.Part.from_bytes(data=image, mime_type="image/jpeg"),
                  f"{TEXT_RULE}\n\n{question}"])
    return (r.text or "").strip() or "Photo ka jawab nahi aaya, dobara try karo."


@full
async def on_photo(update, ctx):
    msg = update.message
    if in_group(update) and not addressed(update, ctx):
        return
    await msg.chat.send_action(ChatAction.TYPING)
    tg_file = await msg.photo[-1].get_file()
    img = bytes(await tg_file.download_as_bytearray())
    question = msg.caption or "Is photo mein kya hai? Detail mein batao."
    answer = await asyncio.to_thread(vision, img, question)
    await send_long(msg, answer)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in [("start", cmd_start), ("help", cmd_help), ("voices", cmd_voices),
                     ("forget", cmd_forget), ("remember", cmd_remember), ("facts", cmd_facts),
                     ("clearfacts", cmd_clearfacts), ("voice", cmd_voice), ("agent", cmd_agent),
                     ("model", cmd_model), ("memory", cmd_memory), ("research", cmd_research),
                     ("image", cmd_image), ("sh", cmd_sh), ("getfile", cmd_getfile), ("mood", cmd_mood)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot started (engine=%s model=%s agent=%s mongo=%s)", ENGINE, MODEL, DEFAULT_AGENT,
             store.db is not None)
    app.run_polling()


if __name__ == "__main__":
    main()
