# Molty Royale AI Agent Bot

Bot otomatis buat Molty Royale. Isinya sudah mencakup setup akun, wallet/identity, join game, gameplay lewat WebSocket, strategi combat, settlement, learning antar match, dan dashboard buat mantau bot secara live.

> [!NOTE]
> Mode saat ini sengaja dibuat cocok dengan flow server v1.5.2:
> 1. `SKILL_VERSION` diset ke `1.5.2` di `bot/config.py`.
> 2. `USE_V160_JOIN` default-nya `false`, jadi bot pakai join flow lama kecuali kamu nyalakan sendiri.
> 3. Log sekarang lebih jelas buat cek state, combat, guardian, dan learning.

## Mulai Cepat

```bash
# 1. Install dependency
pip install -r requirements.txt

# 2. Copy template env
cp .env.example .env

# 3. Jalanin bot
python -m bot.main
```

Pas pertama kali jalan, bot akan bikin atau restore credential di folder `dev-agent/`. Jaga baik-baik file `.env` dan folder `dev-agent/`, karena bisa berisi API key dan private key wallet.

## Dashboard Web

Bot punya dashboard web buat mantau kondisi live.

Buka ini kalau jalan lokal:

```text
http://localhost:8080
```

Yang bisa dipantau:
- metric live: agent, playing, dead, Moltz, sMoltz, CROSS
- status agent: HP/EP, inventory, enemy, item di region
- live log dari runtime bot
- endpoint import/export state dashboard

## Dashboard Learning

Dashboard learning sekarang bisa dicek langsung dari dashboard web lewat menu `Learning`.

Buka dashboard web:

```text
http://localhost:8080
```

Lalu klik menu `Learning` di sidebar. Di situ kamu bisa lihat Strategy DNA, summary performa, recent matches, dan rekomendasi.

Versi terminalnya masih tetap ada kalau kamu mau cek cepat dari command line.

Jalankan dari root project:

```bash
python -m bot.learning.dashboard
```

File yang dibaca:

```text
data/match_history.json
data/strategy_dna.json
```

Catatan:
- `match_history.json` dibuat/diisi setelah game selesai.
- `strategy_dna.json` dibuat/disimpan saat hasil match direkam.
- Evolusi DNA mulai jalan setelah data match cukup, saat ini minimal 5 match.
- Kalau belum ada DNA tersimpan, dashboard akan tampilkan default strategy DNA.
- Fitness dihitung dari placement, kill, survival time, dan damage dealt.

## Update Strategi

Strategi sekarang arahnya lebih survival-first, tapi tetap bisa agresif kalau kondisinya masuk.

Yang sudah dioptimasi:
- Combat punya batas HP aman, jadi DNA lama yang terlalu agresif tidak bisa maksa bot fight saat HP rendah.
- Default `combat_hp_threshold`, `ready_for_war_hp`, dan `danger_flee_hp` dibuat lebih aman.
- Guardian farming sekarang lebih pilih-pilih. Bot butuh weapon yang oke, HP/heal cukup, weather aman, dan tidak ada player dekat sebelum farming guardian.
- Movement late game tidak terlalu anti balik ke region lama, jadi bot bisa rotasi ke posisi yang lebih aman/strategis.
- Data learning dibersihkan sebelum disimpan: placement, kill, survival time, damage, dan reward dikonversi jadi angka.
- DNA snapshot juga disanitasi sebelum diadopsi, jadi snapshot lama yang terlalu agresif tidak bikin strategi mundur lagi.
- Logger Windows sudah dibuat lebih tahan Unicode supaya log tidak crash di console.

## Konfigurasi

| Env Variable | Default | Keterangan |
|---|---:|---|
| `ROOM_MODE` | `free` | Pilihan room: `free`, `auto`, atau `paid` |
| `ADVANCED_MODE` | `true` | Auto-manage Owner EOA dan whitelist |
| `LOG_LEVEL` | `INFO` | Bisa `DEBUG`, `INFO`, atau `WARNING` |
| `PORT` / `DASHBOARD_PORT` | `8080` | Port dashboard web |
| `USE_V160_JOIN` | `false` | Nyalakan unified WebSocket join v1.6.0 |
| `AGGRESSION_LEVEL` | `aggressive` | Bisa `aggressive`, `balanced`, atau `passive` |
| `HP_CRITICAL_THRESHOLD` | `35` | Ambang HP untuk heal darurat |
| `HP_MODERATE_THRESHOLD` | `70` | Ambang HP untuk heal normal |
| `GUARDIAN_FARM_MIN_HP` | `50` | Minimal HP dasar buat guardian farming |
| `COMBAT_MIN_EP` | `8` | Minimal EP sebelum action combat |
| `ENABLE_MEMORY` | `true` | Nyalakan memory antar game |

## Docker

```bash
docker build -t molty-bot .
docker run --env-file .env -p 8080:8080 -it molty-bot
```

Kalau pakai `docker-compose.yml`, folder `dev-agent/` akan dimount ke container dan memory `.molty-royale` disimpan di named volume.

## Deploy ke Railway

### Step 1: Push ke GitHub

```bash
git init
git add .
git commit -m "Molty Royale AI Agent"
git remote add origin https://github.com/YOUR_USER/molty5.git
git push -u origin main
```

### Step 2: Connect di Railway

1. Buat project baru di Railway dari GitHub.
2. Pilih repo bot ini.
3. Di service settings, generate public domain buat dashboard.

### Step 3: Set Variable di Railway

Variable yang perlu kamu isi:

| Variable | Contoh | Keterangan |
|---|---|---|
| `AGENT_NAME` | `YourBotName` | Nama agent, maksimal 50 karakter |
| `ADVANCED_MODE` | `true` | Auto-generate Owner EOA |
| `ROOM_MODE` | `free` | `free`, `auto`, atau `paid` |
| `LOG_LEVEL` | `INFO` | Level log |
| `RAILWAY_API_TOKEN` | token value | Dipakai buat simpan credential otomatis ke Railway variables |

Variable yang auto-generated:

| Variable | Keterangan |
|---|---|
| `API_KEY` | API key dari proses create account |
| `AGENT_WALLET_ADDRESS` | Address Agent EOA |
| `AGENT_PRIVATE_KEY` | Private key Agent EOA |
| `OWNER_EOA` | Address Owner EOA |
| `OWNER_PRIVATE_KEY` | Private key Owner EOA |

## Struktur Project

```text
bot/
|-- main.py              # Entry point
|-- heartbeat.py         # Lifecycle/state machine utama
|-- api_client.py        # REST API client
|-- state_router.py      # Deteksi readiness akun/game
|-- credentials.py       # Helper credential lokal
|-- dashboard/           # Dashboard web
|-- setup/               # Setup akun, wallet, whitelist, identity
|-- game/                # Join flow, WebSocket engine, settlement
|-- strategy/            # Combat, loot, movement, guardian logic
|-- learning/            # Strategy DNA dan dashboard learning terminal
|-- memory/              # Memory antar game
|-- web3/                # EIP-712, contract, wallet management
`-- utils/               # Logger, rate limiter, Railway sync
```

## Command Berguna

```bash
# Jalanin bot + dashboard web
python -m bot.main

# Buka dashboard learning di terminal
python -m bot.learning.dashboard

# Cek syntax tanpa nulis bytecode
python -B -m py_compile bot/learning/strategy_dna.py bot/learning/dashboard.py

# Compile semua module bot
python -m compileall bot
```

Kalau di Windows muncul error permission di `__pycache__`, pakai command dengan `python -B ...` atau tutup proses Python yang mungkin masih megang file `.pyc`.
