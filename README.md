# OpenJarvis Telegram Bot (full, Railway)

Chat, voice-in/voice-out, web search, deep research, image generation, document Q&A.

## Deploy
1. Push this folder to GitHub.
2. railway.com > New Project > Deploy from GitHub repo.
3. Variables: see `.env.example` (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, GROQ_API_KEY, ALLOWED_USER_IDS).
4. Optional: add a Volume mounted at `/data` so memory/documents survive redeploys.

## Commands
/start /help /research /image /remember /facts /clearfacts /agent /model /voice /voices /forget /memory
Send a voice note -> voice reply. Send a document -> indexed into memory.

## Free voices
hi-IN-SwaraNeural, hi-IN-MadhurNeural, en-IN-NeerjaNeural, en-IN-PrabhatNeural,
en-US-JennyNeural, en-US-GuyNeural  (set `TTS_VOICE`)

## Groups
Add the bot to a group. It replies when someone says its name (`BOT_NAMES`, default "baddie"),
@mentions it, or replies to its message. In BotFather run /setprivacy -> your bot -> Disable,
otherwise Telegram will not show it normal group messages.

## Access levels
- Owner (`OWNER_ID`): everything, including shell/code/files/browser.
- Approved (`ALLOWED_USER_IDS`): all features (voice, images, documents, research, photos, settings, memory).
- Everyone else (`PUBLIC_ACCESS=true`): text chat and questions only (web search, calculator, weather),
  `PUBLIC_DAILY_LIMIT` messages per day. They cannot see your documents or memory.
  Set `PUBLIC_ACCESS=false` to block strangers completely.

## MongoDB
Set `MONGODB_URI` (MongoDB Atlas free M0 cluster; allow 0.0.0.0/0 in Network Access).
Stores chat history, saved facts and per-user settings. Without it the bot uses RAM only.
Document (PDF) memory is separate and needs a Railway Volume at `/data`.

## Language
Bot messages (commands/help/status/errors) are English. Conversation (text + voice) is Hinglish.

## Owner-only power tools
Set `OWNER_ID` (your Telegram ID). Only that user gets shell, code run, file read/write and browser
tools (agent uses them from plain chat), plus `/sh` and `/getfile`. Without `OWNER_ID` they stay off.
Photo understanding (Gemini vision) works for every allowed user.

## Not included
Gmail/Calendar digest (needs your OAuth), local models (Railway has no GPU).
