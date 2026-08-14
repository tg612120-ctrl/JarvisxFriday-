"""
Telegram Illegal-Content Report Assistant Bot
----------------------------------------------
User forwards a suspicious post (or sends a t.me link) to this bot.
Bot asks which category the violation falls under, then generates:
  1. A ready-to-paste report description (for Telegram's in-app "Report" flow)
  2. Exact guidance on which in-app option / section to tap
  3. A fallback email template for abuse@telegram.org when in-app reporting
     isn't enough (e.g. the channel keeps reappearing, or it's a large-scale
     operation Telegram's automated triage might miss)

This bot does NOT file the report itself — Telegram doesn't expose a public
API for filing abuse reports (this is intentional on their part, to stop
report-spam abuse). What it does is prepare a clean, well-structured report
so a human filing it in-app (or via email) gives Telegram's moderators
everything they need to act on it quickly.

IMPORTANT: Child sexual abuse material (CSAM) is deliberately NOT included
as a selectable category here. That must be reported directly to NCMEC
(https://report.cybertip.org) or your local police cyber-crime cell, not
routed through a generic bot. See handle_start() for the message shown.
"""

import logging
from datetime import datetime, timezone

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN")  # set this in Railway's Variables tab

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category definitions: label -> (report_reason_text, in_app_guidance, email_tag)
# ---------------------------------------------------------------------------
CATEGORIES = {
    "scam": {
        "label": "💳 Scam / Fraud",
        "reason": (
            "This channel/message is being used to run a financial scam or "
            "fraud scheme (fake investment, phishing, fake job offers, "
            "impersonation for money, etc.)."
        ),
        "tos_clause": (
            "Telegram ToS: \"you agree not to... Use our service to send "
            "spam or scam users.\""
        ),
        "in_app": (
            "Open the chat/channel → tap the channel name at the top → tap "
            "the ⋮ (three-dot) menu → **Report** → choose **'Scam'** or "
            "**'Fraud'** as the reason."
        ),
        "email_subject": "Scam/Fraud channel report",
    },
    "drugs": {
        "label": "💊 Drugs / Illegal Sale",
        "reason": (
            "This channel/message is advertising or facilitating the sale "
            "of illegal drugs or controlled substances."
        ),
        "tos_clause": (
            "Telegram ToS prohibits use of the service for illegal goods "
            "sale; also violates local narcotics law."
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Illegal Goods'** (shown as 'Illegal drugs' in some app "
            "versions)."
        ),
        "email_subject": "Illegal goods (drugs) channel report",
    },
    "weapons": {
        "label": "🔫 Weapons / Explosives",
        "reason": (
            "This channel/message is advertising, selling, or providing "
            "instructions for weapons, firearms, or explosives."
        ),
        "tos_clause": (
            "Telegram ToS prohibits use of the service for illegal goods "
            "sale; also violates local firearms/explosives law."
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Illegal Goods'** (weapons fall under this in most app "
            "versions)."
        ),
        "email_subject": "Illegal weapons channel report",
    },
    "violence": {
        "label": "☠️ Violence / Terrorism",
        "reason": (
            "This channel/message contains content that incites violence, "
            "promotes terrorism, or organizes real-world harm against "
            "people."
        ),
        "tos_clause": (
            "Telegram ToS: \"you agree not to... Promote violence on "
            "publicly viewable Telegram channels, bots, etc.\""
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Violence'** or **'Terrorism'**. These get the fastest human "
            "review priority."
        ),
        "email_subject": "Violence/terrorism content report",
    },
    "misinformation": {
        "label": "🎭 Harmful Misinformation / Deepfake",
        "reason": (
            "This channel/message is spreading harmful misinformation, "
            "including a manipulated or deepfake image/video presented as "
            "real."
        ),
        "tos_clause": (
            "Telegram ToS: \"you agree not to... Spread harmful "
            "misinformation (including harmful deepfake images or "
            "videos).\""
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Other'** and reference the misinformation/deepfake clause "
            "of Telegram's ToS in the text box."
        ),
        "email_subject": "Misinformation / deepfake content report",
    },
    "copyright": {
        "label": "📄 Copyright / Piracy",
        "reason": (
            "This channel/message is distributing copyrighted material "
            "(movies, books, software, courses) without authorization."
        ),
        "tos_clause": (
            "Telegram ToS: \"you agree not to... Violate copyright and "
            "intellectual property rights.\""
        ),
        "in_app": (
            "In-app 'Report' does NOT have a copyright option. You must "
            "email abuse@telegram.org directly (template below) or use "
            "Telegram's copyright form."
        ),
        "email_subject": "DMCA / copyright infringement report",
    },
    "spam": {
        "label": "🚫 Spam / Bot Abuse",
        "reason": (
            "This channel/account is sending unsolicited spam, running "
            "fake engagement, or mass-adding users without consent."
        ),
        "tos_clause": (
            "Telegram ToS: \"you agree not to... Use our service to send "
            "spam or scam users.\""
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Spam'**."
        ),
        "email_subject": "Spam report",
    },
    "other": {
        "label": "❓ Other Illegal Activity",
        "reason": (
            "This channel/message appears to involve illegal activity not "
            "covered by the standard categories — describe it briefly when "
            "you submit."
        ),
        "tos_clause": (
            "Telegram ToS's general prohibited-use list — cite the closest "
            "matching clause, or applicable local law, in your description."
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Other'** and paste the description generated below into the "
            "text box."
        ),
        "email_subject": "Illegal activity report",
    },
}

# Simple keyword hints used to auto-suggest a category from message text.
# This is just a best-effort guess — the user always confirms or picks a
# different category before anything is generated, so a wrong guess costs
# nothing.
KEYWORDS = {
    "scam": ["invest", "guaranteed return", "double your money", "forex",
              "crypto profit", "trading signal", "loan approved", "lottery",
              "prize", "job offer", "work from home", "otp", "kyc update"],
    "drugs": ["weed", "mdma", "cocaine", "charas", "ganja", "heroin",
               "meth", "drugs available", "stuff available"],
    "weapons": ["pistol", "rifle", "gun for sale", "ammunition", "explosive"],
    "violence": ["kill", "attack", "bomb threat", "terrorist", "jihad call",
                  "riot"],
    "misinformation": ["deepfake", "fake news", "morphed video",
                         "ai generated fake", "fake video of"],
    "copyright": ["movie link", "leaked movie", "pirated", "cracked",
                   "course leak", "paid course free"],
    "spam": ["join for free", "click here to win", "limited offer",
              "subscribe now", "mass dm"],
}


def detect_category(text: str) -> str | None:
    """Best-effort keyword guess. Returns a CATEGORIES key or None."""
    if not text:
        return None
    lowered = text.lower()
    for cat_key, words in KEYWORDS.items():
        if any(w in lowered for w in words):
            return cat_key
    return None


# In-memory store of the last forwarded/linked evidence per user.
# For a single-user or low-traffic bot this is fine; for scale, swap for
# Redis/SQLite.
pending_evidence: dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Illegal Content Report Assistant*\n\n"
        "Kaise use karein:\n"
        "1️⃣ Jis post ko report karna hai use *forward* kar dein, "
        "ya uska t.me link yahin paste kar dein.\n"
        "2️⃣ Main category guess kar ke suggest karunga — confirm ya "
        "change kar sakte hain.\n"
        "3️⃣ Main ek ready report text + exact in-app steps bana kar dunga.\n\n"
        "🛑 *Child safety concern?* Agar content mein kisi bacche ka sexual "
        "abuse/exploitation involved lagta hai, to menu mein seedha "
        "*'🛑 Child Safety Concern'* button dabayein — main content ko "
        "process nahi karunga, sirf sahi reporting channels bata dunga.",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def handle_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch forwarded messages, plain t.me links, or forwarded media
    (photo / video / sticker / GIF / document).

    NOTE: we never download or persist the actual media file — only its
    Telegram-assigned file_id and type are noted, for the report text. The
    file itself stays on Telegram's servers; this bot doesn't touch it.
    """
    msg = update.message
    user_id = update.effective_user.id

    forward_origin = getattr(msg, "forward_origin", None)
    link = None
    source_desc = None
    media_note = None

    # Identify media type (without downloading content)
    if msg.photo:
        media_note = "Photo"
    elif msg.video:
        media_note = "Video"
    elif msg.animation:
        media_note = "GIF/Animation"
    elif msg.sticker:
        media_note = "Sticker"
    elif msg.document:
        media_note = "Document/File"

    if msg.text and ("t.me/" in msg.text or "telegram.me/" in msg.text):
        link = msg.text.strip()
        source_desc = f"Link submitted: {link}"
    elif forward_origin is not None:
        chat = getattr(forward_origin, "chat", None)
        sender_name = getattr(forward_origin, "sender_user_name", None) or getattr(
            forward_origin, "sender_chat", None
        )
        if chat is not None:
            uname = f"@{chat.username}" if getattr(chat, "username", None) else chat.title
            source_desc = f"Forwarded from channel/chat: {uname} (id: {chat.id})"
        else:
            source_desc = f"Forwarded message (origin: {sender_name or 'unknown'})"
    elif media_note:
        source_desc = f"{media_note} sent directly (no forward metadata / not forwarded)"
    else:
        await msg.reply_text(
            "Mujhe *forward ki hui post* bhejein (text, photo, video, "
            "sticker ya GIF), ya us post ka *t.me link* paste karein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if media_note:
        source_desc += f" — Content type: {media_note}"

    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pending_evidence[user_id] = {
        "source_desc": source_desc,
        "link": link,
        "captured_at": captured_at,
        "media_note": media_note,
        "raw_text": msg.text or msg.caption or "(no text/caption — media content, "
                                                  "review manually before reporting)",
    }

    guess = detect_category(pending_evidence[user_id]["raw_text"])

    keyboard = [
        [InlineKeyboardButton(v["label"], callback_data=f"cat:{k}")]
        for k, v in CATEGORIES.items()
    ]
    # CSAM always shown as its own button, never auto-selected.
    keyboard.append(
        [InlineKeyboardButton("🛑 Child Safety Concern", callback_data="cat:csam")]
    )

    if guess:
        header = (
            f"Evidence capture ho gayi ✅\n\n"
            f"🔎 Detected category (best guess): *{CATEGORIES[guess]['label']}*\n"
            f"Sahi lage to niche wahi button dabayein, ya doosri category "
            f"choose karein:"
        )
    else:
        header = (
            "Evidence capture ho gayi ✅\n\n"
            "Category auto-detect nahi ho payi. Manually choose karein:"
        )

    await msg.reply_text(
        header,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_category_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in pending_evidence:
        await query.edit_message_text(
            "⚠️ Evidence expire ho gaya lagta hai. Post ko dobara forward "
            "karein ya link bhejein."
        )
        return

    cat_key = query.data.split(":", 1)[1]
    ev = pending_evidence[user_id]

    if cat_key == "csam":
        # Deliberately does NOT build a report template or include any
        # content excerpt. We do not want this bot (or its hosting) to
        # process, store, or describe suspected CSAM content in any way —
        # the only correct move is to route the person to the proper
        # channels immediately.
        await query.edit_message_text(
            csam_guidance_text(),
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
        del pending_evidence[user_id]
        return

    cat = CATEGORIES[cat_key]
    report_text = build_report_text(cat, ev)
    email_text = build_email_text(cat, ev)

    guidance = (
        f"📋 *Report ready — {cat['label']}*\n\n"
        f"*Report description (copy this into Telegram's report box):*\n"
        f"```\n{report_text}\n```\n\n"
        f"*Where to tap in the app:*\n{cat['in_app']}\n\n"
        f"*If in-app reporting doesn't resolve it (channel reappears, or "
        f"large-scale operation), email abuse@telegram.org — draft below:*\n"
        f"```\n{email_text}\n```"
    )

    await query.edit_message_text(guidance, parse_mode=ParseMode.MARKDOWN)
    del pending_evidence[user_id]


def csam_guidance_text() -> str:
    """Static, multi-channel reporting guidance. No user content is echoed
    back here — nothing about the specific post is included on purpose."""
    return (
        "🛑 *Child Safety Concern — report directly, don't forward further*\n\n"
        "Is content ko kisi aur ko forward na karein, na hi save/download "
        "karein — sirf report karein. Report *ek se zyada jagah* karne se "
        "action jaldi hota hai:\n\n"
        "*1. Telegram ke andar (turant karein):*\n"
        "Chat/channel open karein → ⋮ menu → *Report* → *'Child abuse'* "
        "option select karein. Telegram is category ko sabse high priority "
        "deta hai.\n\n"
        "*2. NCMEC CyberTipline (international, sabse standard route):*\n"
        "https://report.cybertip.org\n\n"
        "*3. India — National Cyber Crime Reporting Portal:*\n"
        "https://cybercrime.gov.in (ya helpline 1930)\n\n"
        "*4. Local police cyber cell:*\n"
        "Apne shehar ki cyber crime cell mein bhi complaint file karein — "
        "yeh legal record ke liye zaroori hota hai.\n\n"
        "*5. Agar UK-related hai:*\n"
        "Internet Watch Foundation — https://report.iwf.org.uk\n\n"
        "Har jagah channel/message ka link save kar lein (screenshot ya "
        "t.me link) taaki report karte waqt evidence ke roop mein de sakein "
        "— lekin actual content forward na karein."
    )


def build_report_text(cat: dict, ev: dict) -> str:
    lines = [
        f"Category: {cat['label']}",
        f"Reason: {cat['reason']}",
        f"ToS basis: {cat['tos_clause']}",
        f"Source: {ev['source_desc']}",
    ]
    if ev.get("link"):
        lines.append(f"Link: {ev['link']}")
    lines.append(f"Captured at: {ev['captured_at']}")
    excerpt = (ev.get("raw_text") or "")[:300]
    if excerpt:
        lines.append(f"Content excerpt/caption: {excerpt}")
    lines.append(
        "Requesting review and removal under Telegram's Terms of Service "
        "and applicable law."
    )
    return "\n".join(lines)


def build_email_text(cat: dict, ev: dict) -> str:
    subject = cat["email_subject"]
    body = (
        f"Subject: {subject}\n\n"
        f"To: abuse@telegram.org\n\n"
        f"Hello Telegram Trust & Safety team,\n\n"
        f"I am reporting the following for review:\n\n"
        f"{build_report_text(cat, ev)}\n\n"
        f"Please investigate and take appropriate action (removal / ban) "
        f"in line with Telegram's Terms of Service.\n\n"
        f"Thank you."
    )
    return body


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN environment variable is not set. "
            "Add it in Railway → Variables (or your local .env)."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("help", handle_start))
    app.add_handler(
        MessageHandler(
            filters.FORWARDED
            | filters.TEXT
            | filters.PHOTO
            | filters.VIDEO
            | filters.ANIMATION
            | filters.Sticker.ALL
            | filters.Document.ALL,
            handle_incoming,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_category_choice, pattern=r"^cat:"))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
