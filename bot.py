"""
Telegram Illegal-Content Report Assistant Bot
----------------------------------------------
User forwards a suspicious post (or sends a t.me link) to this bot.
Bot reads the message, matches it against known violation categories,
pulls out concrete signals (links, handles, phone numbers, amounts,
matched trigger words), and generates:
  1. A ready-to-paste report description (for Telegram's in-app "Report" flow)
  2. Exact guidance on which in-app option / section to tap
  3. A fallback email template for abuse@telegram.org, referencing the
     specific Telegram ToS / EU DSA clause that applies
  4. A general note that EU DSA notices may require the reporter's name,
     contact info, and a "clear and convincing explanation" (per DSA
     Art. 16) — the bot fills in the explanation, the human still adds
     their own contact details, since that's personal info the bot has
     no business inventing.

Two reports for two different forwarded messages in the SAME category will
NOT be identical — each pulls its own links/handles/keywords out of the
specific message, so the report reflects what's actually in front of you,
not a copy-pasted boilerplate.

This bot does NOT file the report itself — Telegram doesn't expose a public
API for filing abuse reports (this is intentional on their part, to stop
report-spam abuse). What it does is prepare a clean, well-structured report
so a human filing it in-app (or via email) gives Telegram's moderators
everything they need to act on it quickly.

IMPORTANT: Child sexual abuse material (CSAM) is deliberately NOT included
as a selectable category with an auto-generated report. That must be
reported directly to NCMEC (https://report.cybertip.org), Telegram's own
stopCA@telegram.org, or your local police cyber-crime cell. The bot does
not build a report template for it, does not echo back any content
excerpt, and does not store anything about it — see csam_guidance_text().
"""

import logging
import re
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
# Category definitions
# ---------------------------------------------------------------------------
# tos_clause / dsa_note text below is paraphrased from Telegram's publicly
# posted Terms of Service and Telegram's DSA transparency page as of this
# bot's writing. Telegram can and does update these pages, so if a report
# ever gets disputed, double-check the live wording at telegram.org/tos
# before quoting it as exact text — treat tos_clause as "the clause this
# maps to," not a guaranteed verbatim quote.
CATEGORIES = {
    "scam": {
        "label": "💳 Scam / Fraud",
        "reason": (
            "This channel/message is being used to run a financial scam or "
            "fraud scheme (fake investment, phishing, fake job offers, "
            "impersonation for money, etc.)."
        ),
        "tos_clause": "Telegram ToS: prohibits using the service to send spam or scam users.",
        "in_app": (
            "Open the chat/channel → tap the channel name at the top → tap "
            "the ⋮ (three-dot) menu → **Report** → choose **'Scam'** or "
            "**'Fraud'** as the reason."
        ),
        "email_subject": "Scam/Fraud channel report",
        "requested_action": (
            "Immediate suspension of the channel/account and removal of "
            "the linked payment/contact details to prevent further victims."
        ),
    },
    "drugs": {
        "label": "💊 Drugs / Illegal Sale",
        "reason": (
            "This channel/message is advertising or facilitating the sale "
            "of illegal drugs or controlled substances."
        ),
        "tos_clause": "Telegram ToS prohibits illegal goods sale; also violates local narcotics law.",
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Illegal Goods'** (shown as 'Illegal drugs' in some app "
            "versions)."
        ),
        "email_subject": "Illegal goods (drugs) channel report",
        "requested_action": (
            "Removal of the channel/message and account-level action, as "
            "this facilitates an ongoing criminal transaction, not just a "
            "policy violation."
        ),
    },
    "weapons": {
        "label": "🔫 Weapons / Explosives",
        "reason": (
            "This channel/message is advertising, selling, or providing "
            "instructions for weapons, firearms, or explosives."
        ),
        "tos_clause": "Telegram ToS prohibits illegal goods sale; also violates local firearms/explosives law.",
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Illegal Goods'** (weapons fall under this in most app "
            "versions)."
        ),
        "email_subject": "Illegal weapons channel report",
        "requested_action": (
            "Urgent review — weapons/explosives content carries direct "
            "physical-harm risk; requesting expedited removal and account "
            "action."
        ),
    },
    "violence": {
        "label": "☠️ Violence",
        "reason": (
            "This channel/message contains content that incites violence "
            "or organizes real-world harm against people."
        ),
        "tos_clause": "Telegram ToS: prohibits promoting violence on publicly viewable channels/bots.",
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Violence'**. This gets fast human-review priority."
        ),
        "email_subject": "Violence content report",
        "requested_action": (
            "High-priority review requested — content promoting violence "
            "poses real-world risk. Requesting expedited takedown."
        ),
    },
    "terrorism": {
        "label": "💣 Terrorism / Extremist Content",
        "reason": (
            "This channel/message supports, promotes, recruits for, or "
            "facilitates a terrorist organization or terrorism-related "
            "activity."
        ),
        "tos_clause": (
            "Telegram ToS / DSA prohibited-content list: terrorism-related "
            "content and calls for violence."
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Violence'** or **'Terrorism'** where shown. This gets the "
            "fastest human-review priority."
        ),
        "email_subject": "Terrorism / extremist content report",
        "requested_action": (
            "Highest-priority review requested — terrorism-related content "
            "poses immediate real-world risk. Requesting expedited takedown "
            "and account action."
        ),
    },
    "non_consensual": {
        "label": "🔞 Non-Consensual Sexual Material",
        "reason": (
            "This channel/message contains sexual images or videos of a "
            "person shared without their consent (revenge porn / leaked "
            "private content)."
        ),
        "tos_clause": (
            "Telegram ToS / DSA prohibited-content list: non-consensual "
            "publication of sexual material."
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Personal data'** or **'Pornography'** depending on app "
            "version, and specify 'non-consensual' in the text box."
        ),
        "email_subject": "Non-consensual sexual material report",
        "requested_action": (
            "Urgent removal of the material and account action — this is "
            "an active privacy violation against the person depicted, not "
            "just a policy breach."
        ),
    },
    "doxxing": {
        "label": "🪪 Doxxing / Personal Info Exposure",
        "reason": (
            "This channel/message publishes someone's private personal "
            "details (address, phone, ID numbers, workplace, etc.) in a "
            "way intended to intimidate, harass, or expose them."
        ),
        "tos_clause": (
            "Telegram ToS / DSA prohibited-content list: publishing private "
            "personal data to intimidate or bully."
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Personal data'**."
        ),
        "email_subject": "Doxxing / personal data exposure report",
        "requested_action": (
            "Removal of the personal data and account-level action against "
            "the poster."
        ),
    },
    "impersonation": {
        "label": "🎭 Fake Account / Impersonation",
        "reason": (
            "This account/channel falsely presents itself as another "
            "person, organization, or entity."
        ),
        "tos_clause": "Telegram ToS: accounts/channels may be marked FAKE for impersonation.",
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Fake account'**."
        ),
        "email_subject": "Impersonation / fake account report",
        "requested_action": "Account should be marked FAKE or removed, and the impersonated party protected.",
    },
    "harassment": {
        "label": "😡 Harassment / Targeted Abuse",
        "reason": (
            "This channel/message contains threatening, targeted, or "
            "seriously abusive behavior directed at a specific person."
        ),
        "tos_clause": "Telegram ToS's general prohibited-use list covers targeted abuse/harassment.",
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Other'** and describe the harassment in the text box."
        ),
        "email_subject": "Harassment / targeted abuse report",
        "requested_action": "Review and account action against the sender for targeted harassment.",
    },
    "misinformation": {
        "label": "🎭 Harmful Misinformation / Deepfake",
        "reason": (
            "This channel/message is spreading harmful misinformation, "
            "including a manipulated or deepfake image/video presented as "
            "real."
        ),
        "tos_clause": (
            "Telegram ToS: prohibits spreading harmful misinformation, "
            "including harmful deepfake images or videos."
        ),
        "in_app": (
            "Open the chat/channel → ⋮ menu → **Report** → choose "
            "**'Other'** and reference the misinformation/deepfake clause "
            "of Telegram's ToS in the text box."
        ),
        "email_subject": "Misinformation / deepfake content report",
        "requested_action": (
            "Removal of the manipulated media and a warning/strike on the "
            "distributing account, per Telegram's own deepfake clause."
        ),
    },
    "copyright": {
        "label": "📄 Copyright / Piracy",
        "reason": (
            "This channel/message is distributing copyrighted material "
            "(movies, books, software, courses) without authorization."
        ),
        "tos_clause": "Telegram ToS: prohibits violating copyright and IP rights.",
        "in_app": (
            "In-app 'Report' does NOT have a copyright option. You must "
            "email abuse@telegram.org directly (template below) or use "
            "Telegram's copyright form."
        ),
        "email_subject": "DMCA / copyright infringement report",
        "requested_action": "Takedown of the infringing content under DMCA / applicable copyright law.",
    },
    "spam": {
        "label": "🚫 Spam / Bot Abuse",
        "reason": (
            "This channel/account is sending unsolicited spam, running "
            "fake engagement, or mass-adding users without consent."
        ),
        "tos_clause": "Telegram ToS: prohibits using the service to send spam or scam users.",
        "in_app": "Open the chat/channel → ⋮ menu → **Report** → choose **'Spam'**.",
        "email_subject": "Spam report",
        "requested_action": "Account-level restriction to stop further mass messaging.",
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
        "requested_action": "Manual review requested — please assess against the closest applicable policy.",
    },
}

# ---------------------------------------------------------------------------
# Keyword hints — used to auto-suggest AND to score which category best
# matches the specific forwarded message. This is a best-effort guess; the
# user always confirms or picks a different category via the buttons, so a
# wrong guess costs nothing.
# ---------------------------------------------------------------------------
KEYWORDS = {
    "scam": ["invest", "guaranteed return", "double your money", "forex",
              "crypto profit", "trading signal", "loan approved", "lottery",
              "prize", "job offer", "work from home", "otp", "kyc update"],
    "drugs": ["weed", "mdma", "cocaine", "charas", "ganja", "heroin",
               "meth", "drugs available", "stuff available"],
    "weapons": ["pistol", "rifle", "gun for sale", "ammunition", "explosive"],
    "violence": ["kill", "attack", "riot", "beat up", "lynch"],
    "terrorism": ["bomb threat", "terrorist", "jihad call", "isis", "recruit for",
                   "martyrdom operation"],
    "non_consensual": ["leaked video", "leaked pics", "her nudes", "revenge porn",
                          "without her consent", "mms leaked"],
    "doxxing": ["home address", "her number is", "his number is", "aadhar number",
                 "leaked details", "personal info of"],
    "impersonation": ["official account of", "verified", "impersonat", "fake profile of"],
    "harassment": ["kill yourself", "we know where you live", "stalk", "threat to"],
    "misinformation": ["deepfake", "fake news", "morphed video",
                         "ai generated fake", "fake video of"],
    "copyright": ["movie link", "leaked movie", "pirated", "cracked",
                   "course leak", "paid course free"],
    "spam": ["join for free", "click here to win", "limited offer",
              "subscribe now", "mass dm"],
}


def matched_keywords(text: str, cat_key: str) -> list[str]:
    """Which keyword(s) from a given category actually hit in this text."""
    if not text:
        return []
    lowered = text.lower()
    return [w for w in KEYWORDS.get(cat_key, []) if w in lowered]


def detect_category(text: str) -> tuple[str | None, list[str]]:
    """Score every category by how many of its keywords hit this specific
    message, return the best match and the exact words that matched (so
    the generated report can quote them). Ties broken by category order
    above (roughly severity order)."""
    if not text:
        return None, []
    best_key, best_hits = None, []
    for k in KEYWORDS:
        hits = matched_keywords(text, k)
        if len(hits) > len(best_hits):
            best_key, best_hits = k, hits
    return best_key, best_hits


# Regex patterns to pull concrete, reportable details out of the message
# text/caption — phone numbers, payment amounts, links, @handles — so the
# report reflects what's actually in THIS message instead of being a
# generic paragraph every time.
PHONE_RE = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\d{10}\b")
URL_RE = re.compile(r"(?:https?://\S+|(?:t|telegram)\.me/\S+|www\.\S+)")
AMOUNT_RE = re.compile(r"(?:₹|\$|Rs\.?|INR|USD)\s?\d[\d,]*(?:\.\d+)?")
HANDLE_RE = re.compile(r"@\w{4,}")


def extract_signals(text: str) -> dict[str, list[str]]:
    if not text:
        return {"phones": [], "urls": [], "amounts": [], "handles": []}
    return {
        "phones": list(dict.fromkeys(PHONE_RE.findall(text)))[:5],
        "urls": list(dict.fromkeys(URL_RE.findall(text)))[:5],
        "amounts": list(dict.fromkeys(AMOUNT_RE.findall(text)))[:5],
        "handles": list(dict.fromkeys(HANDLE_RE.findall(text)))[:5],
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
        "How to use:\n"
        "1️⃣ *Forward* the post you want to report, or paste its t.me "
        "link here.\n"
        "2️⃣ I'll read it, match it against known violation types, and "
        "suggest the closest category — confirm or change it.\n"
        "3️⃣ I'll generate a report tailored to THIS message (not a "
        "generic template) + exact in-app steps + an email draft.\n\n"
        "🛑 *Child safety concern?* If the content appears to involve "
        "sexual abuse/exploitation of a minor, tap the "
        "*'🛑 Child Safety Concern'* button in the menu directly — I won't "
        "process the content itself, only give you the correct reporting "
        "channels (NCMEC / Telegram / police).",
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
            "Please send me a *forwarded post* (text, photo, video, "
            "sticker, or GIF), or paste the post's *t.me link*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if media_note:
        source_desc += f" — Content type: {media_note}"

    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    raw_text = msg.text or msg.caption or "(no text/caption — media content, review manually before reporting)"

    pending_evidence[user_id] = {
        "source_desc": source_desc,
        "link": link,
        "captured_at": captured_at,
        "media_note": media_note,
        "raw_text": raw_text,
    }

    guess_key, guess_hits = detect_category(raw_text)
    pending_evidence[user_id]["guess_hits"] = guess_hits

    keyboard = [
        [InlineKeyboardButton(v["label"], callback_data=f"cat:{k}")]
        for k, v in CATEGORIES.items()
    ]
    # CSAM always shown as its own button, never auto-selected.
    keyboard.append(
        [InlineKeyboardButton("🛑 Child Safety Concern", callback_data="cat:csam")]
    )

    if guess_key:
        hits_str = ", ".join(f"'{h}'" for h in guess_hits)
        header = (
            f"Evidence captured ✅\n\n"
            f"🔎 Detected category (best guess): *{CATEGORIES[guess_key]['label']}*\n"
            f"Matched on: {hits_str}\n"
            f"If this looks right, tap that button below, or pick a "
            f"different category:"
        )
    else:
        header = (
            "Evidence captured ✅\n\n"
            "Couldn't auto-detect the category from the text. Please choose "
            "manually — media-only posts (no caption) always need a manual "
            "pick since I don't scan image/video content itself:"
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
            "⚠️ The evidence seems to have expired. Please forward the "
            "post again or send the link."
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
    report_text = build_report_text(cat, ev, cat_key)
    email_text = build_email_text(cat, ev, cat_key)

    guidance = (
        f"📋 *Report ready — {cat['label']}*\n\n"
        f"*Report description (copy this into Telegram's report box):*\n"
        f"```\n{report_text}\n```\n\n"
        f"*Where to tap in the app:*\n{cat['in_app']}\n\n"
        f"*If in-app reporting doesn't resolve it (channel reappears, or "
        f"large-scale operation, or you want to file under EU DSA), email "
        f"abuse@telegram.org — draft below. Add your name + contact info "
        f"before sending; EU DSA notices need that from you personally, "
        f"I can't fill it in for you:*\n"
        f"```\n{email_text}\n```"
    )

    await query.edit_message_text(guidance, parse_mode=ParseMode.MARKDOWN)
    del pending_evidence[user_id]


def csam_guidance_text() -> str:
    """Static, multi-channel reporting guidance. No user content is echoed
    back here — nothing about the specific post is included on purpose."""
    return (
        "🛑 *Child Safety Concern — report directly, don't forward it any "
        "further*\n\n"
        "Do not forward this content to anyone else, and don't save or "
        "download it — only report it. Reporting in *more than one place* "
        "gets faster action:\n\n"
        "*1. Inside Telegram (do this immediately):*\n"
        "Open the chat/channel → ⋮ menu → *Report* → select *'Child "
        "abuse'*. Telegram treats this category as highest priority.\n\n"
        "*2. Telegram's dedicated child-safety inbox:*\n"
        "stopCA@telegram.org\n\n"
        "*3. NCMEC CyberTipline (international, the standard route):*\n"
        "https://report.cybertip.org\n\n"
        "*4. India — National Cyber Crime Reporting Portal:*\n"
        "https://cybercrime.gov.in (or helpline 1930)\n\n"
        "*5. Local police cyber cell:*\n"
        "Also file a complaint with your city's cyber crime cell — this "
        "creates an important legal record.\n\n"
        "*6. If UK-related:*\n"
        "Internet Watch Foundation — https://report.iwf.org.uk\n\n"
        "Save the channel/message link (screenshot or t.me link) wherever "
        "you report it, as evidence — but do not forward the actual "
        "content itself."
    )


def build_report_text(cat: dict, ev: dict, cat_key: str) -> str:
    """Structured, category-specific report pulled together from THIS
    message's actual content — not a static paragraph reused every time."""
    signals = extract_signals(ev["raw_text"])
    hits = ev.get("guess_hits") or matched_keywords(ev["raw_text"], cat_key)

    lines = [
        cat["reason"],
        "",
        f"Source: {ev['source_desc']}",
        f"Captured at: {ev['captured_at']} (UTC)",
    ]
    if ev.get("link"):
        lines.append(f"Link: {ev['link']}")
    if hits:
        lines.append(f"Matched terms in message: {', '.join(hits)}")
    if signals["urls"]:
        lines.append(f"Linked URLs in message: {', '.join(signals['urls'])}")
    if signals["handles"]:
        lines.append(f"Handles mentioned: {', '.join(signals['handles'])}")
    if signals["phones"]:
        lines.append(f"Phone numbers mentioned: {', '.join(signals['phones'])}")
    if signals["amounts"]:
        lines.append(f"Amounts mentioned: {', '.join(signals['amounts'])}")

    lines += [
        "",
        f"Policy basis: {cat['tos_clause']}",
        "",
        f"Requested action: {cat['requested_action']}",
    ]
    return "\n".join(lines)


def build_email_text(cat: dict, ev: dict, cat_key: str) -> str:
    report_body = build_report_text(cat, ev, cat_key)
    return (
        f"To: abuse@telegram.org\n"
        f"Subject: {cat['email_subject']}\n\n"
        f"Hello Telegram Trust & Safety team,\n\n"
        f"I am reporting the following content, which I believe violates "
        f"Telegram's Terms of Service"
        f"{' and may be reportable under the EU Digital Services Act' if True else ''}:\n\n"
        f"{report_body}\n\n"
        f"[Your name]\n"
        f"[Your contact email/phone — required for EU DSA notices per "
        f"Article 16]\n\n"
        f"Thank you for your attention to this report."
    )


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION
             | filters.Sticker.ALL | filters.Document.ALL) & ~filters.COMMAND,
            handle_incoming,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_category_choice, pattern=r"^cat:"))

    logger.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
