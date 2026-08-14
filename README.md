# Telegram Illegal-Content Report Assistant

A bot that helps you build a **clean, well-structured report** for Telegram's
in-app abuse reporting system. Forward a suspicious post (or send its
`t.me` link), pick a category, and it hands you back:

- A copy-paste report description
- The exact in-app steps (which menu, which button) for that category
- A fallback email draft for `abuse@telegram.org`

**It does not file the report for you** — Telegram has no public API for
submitting abuse reports (this is intentional, to prevent report-spam). A
human still has to tap "Report" in the app or send the email. This bot just
makes sure that report is complete and well-formatted, which makes it much
more likely to get acted on.

**Not for CSAM.** Child sexual abuse material must go straight to
[NCMEC's CyberTipline](https://report.cybertip.org) or your local police
cyber-crime cell — not through a general-purpose bot. The bot tells users
this in `/start`.

## 1. Create the bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. `/newbot` → follow the prompts → copy the token it gives you

## 2. Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

## 3. Deploy on Railway

1. [railway.app](https://railway.app) → New Project → **Deploy from GitHub repo**
2. Select your repo
3. Go to the service → **Variables** tab → add:
   - `BOT_TOKEN` = the token from BotFather
4. Railway will auto-detect the `Procfile` and run `python bot.py` as a worker
5. Deploy — check the **Logs** tab for `Bot starting...`

Railway's free tier (~$5/month trial credit, no card needed initially) is
enough for a low-traffic bot like this one running on long-polling.

## 4. Test it

- Open your bot in Telegram, hit `/start`
- Forward a post from any public channel, or paste a `t.me/...` link
- Pick a category → get your report text + instructions

## Notes / possible improvements

- Currently stores one pending report per user in memory — fine for
  personal/low-traffic use. For multi-user scale, swap `pending_evidence`
  dict for SQLite or Redis so it survives restarts.
- Copyright category has no in-app report option on Telegram — the bot
  correctly routes that one to the email template only.
- You can extend `CATEGORIES` in `bot.py` with more categories or
  region-specific reporting bodies (e.g. India's cybercrime portal
  cybercrime.gov.in) if you want a local reporting path alongside
  Telegram's own.
