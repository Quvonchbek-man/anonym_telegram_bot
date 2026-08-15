# Serverga o'rnatish

Oracle Cloud Always Free (Ubuntu 24.04) — `growth-up` turgan o'sha server.
Bot faqat Telegramga **chiquvchi** ulanish qiladi (polling), shuning uchun
port ochish, domen, Caddy va HTTPS **kerak emas**.

Bir marta bajariladi; keyingi yangilanishlar — pastdagi «Yangilash» bo'limi.

## 0. Muhim: bitta vaqtda faqat bitta nusxa

Telegram bitta tokenga faqat bitta polling ulanishiga ruxsat beradi. Server
nusxasi ko'tarilishidan oldin uy kompyuteridagi bot **to'xtatilgan** bo'lsin,
aks holda ikkalasi ham `409 Conflict` bilan uzilib turadi.

## 1. O'rnatish

```bash
ssh -i ~/.ssh/growth-up ubuntu@158.178.149.128
```

```bash
sudo mkdir -p /opt/anon-bot && sudo chown ubuntu:ubuntu /opt/anon-bot
git clone https://github.com/Quvonchbek-man/anonym_telegram_bot.git /opt/anon-bot
cd /opt/anon-bot
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
```

## 2. `.env` — serverda qo'lda yoziladi

Repoda yo'q va bo'lmasligi ham kerak:

```bash
cp .env.example .env && nano .env
```

Majburiylari: `ANON_BOT_TOKEN` va `SUPER_ADMIN_ID`. Ikkinchisi bo'sh
qolsa bot ataylab ishga tushmaydi — indamay noto'g'ri egaga ishlashdan ko'ra
aniq xato bergani yaxshi.

```bash
chmod 600 .env
```

## 3. Bazani ko'chirish (ixtiyoriy)

Uy kompyuteridagi foydalanuvchilarni saqlab qolish uchun, **ikkala nusxa ham
to'xtatilgan holda**:

```bash
scp -i ~/.ssh/growth-up anon_qa_bot.db ubuntu@158.178.149.128:/opt/anon-bot/
```

Ko'chirmasangiz server bo'sh bazadan boshlaydi va foydalanuvchilarning
shaxsiy havolalari yangilanadi — ya'ni eski havolalar ishlamay qoladi.

## 4. Xizmat sifatida ishga tushirish

```bash
sudo cp /opt/anon-bot/deploy/anon-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anon-bot
journalctl -u anon-bot -f
```

Kutilgan chiqish:

```
Database initialized successfully.
Anonymous Q&A Bot is starting...
```

Tekshirish: Telegram'da botga `/start` yozing.

---

## Yangilash

O'z kompyuteringizdan, bitta buyruq:

```bash
ssh -i ~/.ssh/growth-up ubuntu@158.178.149.128 /opt/anon-bot/deploy/update.sh
```

Skript `git pull` qiladi, `requirements.txt` o'zgargan bo'lsa bog'liqliklarni
yangilaydi, bazadan zaxira oladi va xizmatni qayta ishga tushiradi. Kod
o'zgarmagan bo'lsa hech narsa qilmaydi (`--force` majburlaydi).

Sxema o'zgarishi uchun alohida migratsiya skripti yo'q: `init_db()` dagi
`ALTER TABLE` lar xatoni yutib yuboradi, ya'ni har ishga tushishda o'zi
moslashadi.

## Nosozlik

| Belgi | Qayerga qarash |
|---|---|
| Bot javob bermaydi | `journalctl -u anon-bot -n 50` |
| `409 Conflict` jurnalda | Bot boshqa joyda ham ishlayapti (uy kompyuteri?) |
| `SUPER_ADMIN_ID topilmadi` | `.env` to'ldirilmagan |
| `/adminpage` ruxsat bermaydi | `.env` dagi `SUPER_ADMIN_ID` sizniki emas |
| Xizmat qayta-qayta yiqilyapti | `systemctl status anon-bot`, keyin jurnal |

## Zaxira

Baza kichkina (foydalanuvchilar + bloklar; suhbat jurnali umuman
saqlanmaydi). `update.sh` har yangilanishda zaxira oladi va oxirgi 7 tasini
qoldiradi. Kunlik zaxira ham kerak bo'lsa:

```bash
crontab -e
# 0 3 * * * cd /opt/anon-bot && sqlite3 anon_qa_bot.db ".backup 'anon_qa_bot.db.$(date +\%Y\%m\%d).bak'"
```
