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
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Violence'** or **'Terrorism'**. These get the fastest human "
            "review priority."
        ),
        "email_subject": "Violence/terrorism content report",
    },
    "copyright": {
        "label": "📄 Copyright / Piracy",
        "reason": (
            "This channel/message is distributing copyrighted material "
            "(movies, books, software, courses) without authorization."
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
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Other'** and paste the description generated below into the "
            "text box."
        ),
        "email_subject": "Illegal activity report",
    },
}

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
        "2️⃣ Main violation ki category poochunga.\n"
        "3️⃣ Main ek ready report text + exact in-app steps bana kar dunga.\n\n"
        "⚠️ *Important:* Agar content child sexual abuse material (CSAM) se "
        "related hai, to is bot ka use na karein — seedha "
        "[NCMEC CyberTipline](https://report.cybertip.org) ya apni local "
        "police cyber cell ko report karein. Telegram bhi CSAM ko "
        "sabse top priority par automated + human review deta hai jab "
        "seedha unke abuse channel se report kiya jaaye.",
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def handle_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch forwarded messages or plain t.me links."""
    msg = update.message
    user_id = update.effective_user.id

    forward_origin = getattr(msg, "forward_origin", None)
    link = None
    source_desc = None

    if msg.text and ("t.me/" in msg.text or "telegram.me/" in msg.text):
        link = msg.text.strip()
        source_desc = f"Link submitted: {link}"
    elif forward_origin is not None:
        # Try to build a human-readable description of the forwarded source
        chat = getattr(forward_origin, "chat", None)
        sender_name = getattr(forward_origin, "sender_user_name", None) or getattr(
            forward_origin, "sender_chat", None
        )
        if chat is not None:
            uname = f"@{chat.username}" if getattr(chat, "username", None) else chat.title
            source_desc = f"Forwarded from channel/chat: {uname} (id: {chat.id})"
        else:
            source_desc = f"Forwarded message (origin: {sender_name or 'unknown'})"
    else:
        await msg.reply_text(
            "Mujhe ya to *forward ki hui post* bhejein, ya us post ka "
            "*t.me link* paste karein.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pending_evidence[user_id] = {
        "source_desc": source_desc,
        "link": link,
        "captured_at": captured_at,
        "raw_text": msg.text or msg.caption or "(media/no text content)",
    }

    keyboard = [
        [InlineKeyboardButton(v["label"], callback_data=f"cat:{k}")]
        for k, v in CATEGORIES.items()
    ]
    await msg.reply_text(
        "Evidence capture ho gayi ✅\n\nAb violation ki *category* choose karein:",
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
    cat = CATEGORIES[cat_key]
    ev = pending_evidence[user_id]

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


def build_report_text(cat: dict, ev: dict) -> str:
    lines = [
        f"Category: {cat['label']}",
        f"Reason: {cat['reason']}",
        f"Source: {ev['source_desc']}",
    ]
    if ev.get("link"):
        lines.append(f"Link: {ev['link']}")
    lines.append(f"Captured at: {ev['captured_at']}")
    excerpt = (ev.get("raw_text") or "")[:300]
    if excerpt:
        lines.append(f"Content excerpt: {excerpt}")
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
        MessageHandler(filters.FORWARDED | filters.TEXT, handle_incoming)
    )
    app.add_handler(CallbackQueryHandler(handle_category_choice, pattern=r"^cat:"))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
