# VoidBot — Setup

VoidCyber ↔ Discord account linking bot.
Commands: `/link`, `/unlink`, `/rank`, `/leaderboard`.

---

## 1. Database (VoidCyber)

Apply the migration to the D1 production database:

```bash
npx wrangler d1 execute voidcyber-db --remote --file=migrations/023_discord_link.sql
```

This adds `discord_id` + `discord_username` to the `users` table and creates the `discord_link_codes` table.

## 2. Shared Secret

Generate a random secret string:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set the **same value** in two places:

- **Vercel** → VoidCyber project → Settings → Environment Variables → `DISCORD_BOT_SECRET` (Production). After adding it, trigger a new deploy (Redeploy).
- **Bot** → `.env` file → `DISCORD_BOT_SECRET`.

Without this secret all `/api/discord/*` API calls will return `403`.

> Note: the site runs on **Vercel**; Cloudflare only hosts the **D1** database,
> which the app reaches via REST API. All env vars (including Cloudflare ones)
> go on Vercel. The `wrangler d1 execute` command in step 1 is just CLI access
> to the database — it has nothing to do with hosting.

## 3. Discord Developer Portal

- **Bot → Privileged Gateway Intents** → enable **Server Members Intent**.
  (Message Content Intent is not needed.)
- Invite the bot with the following permissions: **Manage Roles** (plus standard bot permissions).

## 4. Role Hierarchy (important!)

Server → Settings → Roles: drag the **bot's role ABOVE** all 10 rank roles and above the "linked" role. Discord does not allow bots to assign roles that are higher than their own role.

## 5. Starting the bot

```bash
cd "Discord bot"
pip install -r requirements.txt
# create the .env file from .env.example and fill in the values
python main.py
```

If you see `✅ VoidBot online` and slash commands synced, everything is working.

---

## How account linking works

1. The user goes to their VoidCyber profile → **// Discord** section → generates a one-time code (8 characters, expires in 10 minutes).
2. On Discord they run `/link CODE`.
3. The bot sends the code + Discord ID to VoidCyber, which verifies and saves the link.
4. The bot immediately assigns the rank role + the "linked" role.
5. A background task (every 15 min) keeps roles in sync with the site's XP. `/rank` and `/link` also sync instantly.

A code can only be used **once** and a Discord account can only be linked to **one** VoidCyber profile (and vice versa). To unlink: `/unlink`.
