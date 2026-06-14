# VoidBot — Setup

Bot di collegamento account VoidCyber ↔ Discord.
Comandi: `/link`, `/unlink`, `/rank`, `/leaderboard`.

---

## 1. Database (VoidCyber)

Applica la migration al DB D1 di produzione:

```bash
npx wrangler d1 execute voidcyber-db --remote --file=migrations/023_discord_link.sql
```

Aggiunge `discord_id` + `discord_username` alla tabella `users` e crea la
tabella `discord_link_codes`.

## 2. Secret condiviso

Genera una stringa segreta:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Mettila **uguale** in due posti:

- **Vercel** → progetto VoidCyber → Settings → Environment Variables →
  `DISCORD_BOT_SECRET` (Production). È lo stesso posto dove hai `AUTH_SECRET`,
  `CLOUDFLARE_API_TOKEN`, ecc. Dopo averla aggiunta, fai un nuovo deploy (Redeploy).
- **Bot** → file `.env` → `DISCORD_BOT_SECRET`.

Senza questo secret le API `/api/discord/*` rispondono `403`.

> Nota: il sito gira su **Vercel**; su Cloudflare c'è solo il database **D1**,
> che l'app raggiunge via REST API. Per questo le env var (incluse quelle
> Cloudflare) vanno su Vercel. Il comando `wrangler d1 execute` del punto 1 è
> solo accesso da CLI al database — non c'entra con l'hosting.

## 3. Discord Developer Portal

- **Bot → Privileged Gateway Intents** → attiva **Server Members Intent**.
  (Non serve Message Content.)
- Invita il bot con i permessi: **Manage Roles** (e i permessi base di un bot).

## 4. Gerarchia ruoli (importante!)

Server → Impostazioni → Ruoli: trascina il ruolo del **bot SOPRA** tutti i 10
ruoli rank e sopra il ruolo "linked". Discord non permette di assegnare ruoli
che stanno più in alto del ruolo del bot.

## 5. Avvio del bot

```bash
cd "Discord bot"
pip install -r requirements.txt
# crea il file .env partendo da .env.example e compila i valori
python "bot di discord.py"
```

Se vedi `✅ VoidBot online` e `✅ Comandi slash sincronizzati`, è tutto ok.

---

## Come funziona il collegamento

1. L'utente va sul suo profilo VoidCyber → sezione **// Discord** → genera un
   codice monouso (8 caratteri, scade in 10 minuti).
2. Su Discord scrive `/link CODICE`.
3. Il bot manda codice + Discord ID a VoidCyber, che verifica e salva il link.
4. Il bot assegna subito il ruolo del rank + il ruolo "linked".
5. Un task in background (ogni 15 min) tiene i ruoli allineati all'XP del sito.
   Anche `/rank` e `/link` aggiornano il ruolo all'istante.

Un codice è usabile **una sola volta** e un account Discord può essere collegato
a **un solo** profilo VoidCyber (e viceversa). Per scollegare: `/unlink`.
