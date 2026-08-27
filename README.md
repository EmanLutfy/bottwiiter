# X Profile Telegram Bot — Vercel Webhook Version

Bot Telegram yang sama fungsinya (paste link/username X → dapat nama,
bio, website, logo PNG), tapi versi ni jalan sebagai **webhook** di
Vercel — tak perlu proses berjalan 24/7 di laptop korang, dan orang lain
boleh terus guna bot ni bila-bila masa.

## Kenapa webhook (bukan versi bot.py yang polling)

Versi awal (`bot.py`, guna `run_polling()`) perlukan proses Python yang
hidup **berterusan** — sesuai untuk laptop/VPS, tapi TAK sesuai untuk
Vercel (serverless), sebab Vercel cuma jalankan kod bila ada request
masuk, lepas tu proses "mati". Webhook plak — **Telegram sendiri** yang
hantar (`POST`) setiap mesej terus ke satu URL bila-bila ada orang mesej
bot — ini padan 100% dengan cara serverless berfungsi, dan boleh jalan
percuma di Vercel.

**Tak boleh guna DUA-DUA mod sekali gus** — Telegram cuma benarkan SATU
sahaja pada satu masa (webhook ATAU polling, bukan kedua-dua). Kalau nak
kembali guna versi `bot.py` (polling) di laptop, kena `deleteWebhook`
dulu (rujuk bawah).

## Langkah 1 — Dapatkan token bot (sama macam sebelum ni)

Kalau dah ada token dari @BotFather (dari setup bot.py awal), boleh guna
token yang sama.

## Langkah 2 — Deploy ke Vercel

Struktur folder ni sengaja **tiada `vercel.json`** — Vercel automatik
kesan `api/index.py` dan reachable terus di `/api/index` (zero-config,
paling predictable, elak isu routing yang pernah jadi kat web version).

### Cara CLI

```bash
cd x_profile_telegram_webhook
npm install -g vercel   # sekali je kalau belum ada
vercel login
vercel --prod
```

### Cara Dashboard (import GitHub)

1. Push folder ni ke repo GitHub baru (atau upload terus macam sebelum
   ni).
2. https://vercel.com → **Add New** → **Project** → import repo tu →
   **Deploy**.

Lepas deploy, korang akan dapat URL macam
`https://nama-app-korang.vercel.app`.

## Langkah 3 — Set Environment Variables

Dalam Vercel: **Project → Settings → Environment Variables**, tambah:

| Key | Value |
|---|---|
| `BOT_TOKEN` | token dari @BotFather |
| `WEBHOOK_SECRET` | (optional tapi disyorkan) satu string rawak korang cipta sendiri, contoh `openssl rand -hex 20` — untuk sahkan request memang dari Telegram |

Lepas tambah env var, **Redeploy** (Deployments → `...` → Redeploy) —
env var baru cuma applied lepas redeploy.

## Langkah 4 — Daftarkan webhook (SEKALI je)

Guna browser atau terminal, panggil (ganti `<TOKEN>`, `<PROJECT>`, dan
`<SECRET>` — buang bahagian `&secret_token=...` kalau tak set
`WEBHOOK_SECRET`):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<PROJECT>.vercel.app/api/index&secret_token=<SECRET>"
```

Response patut `{"ok":true,"result":true,"description":"Webhook was set"}`.

Boleh check status webhook bila-bila masa:

```bash
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```

## Langkah 5 — Test

Pergi Telegram, mesej bot tu (`/start`, atau terus paste link X). Kalau
tak reply, check **Vercel Dashboard → project → Logs** untuk lihat
request masuk & ralat (kalau ada) — sama macam cara debug web version
sebelum ni.

## Nak kembali guna bot.py (polling) di laptop?

Padam webhook dulu (Telegram tak benarkan webhook + polling serentak):

```bash
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```

Lepas tu boleh `python bot.py` seperti biasa.

## Nota sama macam versi-versi sebelum ni

- Scraping X guna fallback chain (og-tags → syndication API → nitter),
  timeout dikecilkan sesuai untuk had masa function serverless.
- Setiap reply ada baris kecil "sumber: ..." untuk senang debug kalau ada
  akaun yang asyik "tak jumpa".
- Terma Perkhidmatan X: sesuai kegunaan peribadi/kecil; besar-besaran/
  komersial pertimbang API rasmi X.
