# Anonim Savol-Javob Bot

Telegram uchun anonim xabar boti: har bir foydalanuvchi shaxsiy havola oladi, o'sha havola orqali unga anonim savol/xabar yuborish mumkin. Qabul qiluvchi kim yozganini bilmaydi, lekin javob bera oladi.

pyTelegramBotAPI + SQLite. Tashqi servis, navbat yoki keshsiz — bitta fayl, bitta protsess.

## Maxfiylik prinsipi

Bot **suhbat jurnalini yuritmaydi**:

- Anonim xabarlar matni bazaga **yozilmaydi**.
- Kim kimga yozgani ham **saqlanmaydi** — `message_mappings` kabi jadval yo'q.
- Javob berish uchun kerakli "bu xabar kimdan" ma'lumoti xabarning inline tugmasidagi `callback_data` da turadi (`reply_<id>`), ya'ni **Telegram'ning o'zida**. Shu sababli javob berish muddatsiz ishlaydi va bot tomonda hech narsa saqlanmaydi.
- Bazada saqlanadigan yagona narsalar: foydalanuvchi profili (`chat_id`, username, shaxsiy havola kodi), bloklash ro'yxatlari, sonli hisoblagichlar, hamda **foydalanuvchi o'zi yuborgan** shikoyat va `/support` murojaatlari matni.
- `/reports` da adminlar anonim jo'natuvchining xom `chat_id` sini ko'rmaydi — barqaror taxallus ko'rsatiladi (`anon#a1b2c3`).

## O'rnatish

```bash
pip install -r requirements.txt
cp .env.example .env
```

`.env` ni to'ldiring:

| O'zgaruvchi | Majburiy | Izoh |
|---|---|---|
| `ANON_BOT_TOKEN` | ha | @BotFather dan olinadi |
| `SUPER_ADMIN_ID` | ha | Bot egasining chat_id si (@userinfobot) |
| `ADMIN_IDS` | yo'q | Vergul bilan ajratilgan qo'shimcha admin ID lari |

Ishga tushirish:

```bash
python anon_bot.py
```

Baza (`anon_qa_bot.db`) birinchi ishga tushishda avtomatik yaratiladi.

## Buyruqlar

**Hamma uchun**

| Buyruq | Vazifasi |
|---|---|
| `/start` | Shaxsiy havolani olish |
| `/support` | Administratorga murojaat yuborish |
| `/help` | Yordam |

**Administratorlar**

| Buyruq | Vazifasi |
|---|---|
| `/adminpage` | Boshqaruv paneli |
| `/stats` | Umumiy statistika |
| `/reports` | Ko'rib chiqilmagan shikoyatlar |
| `/broadcast` | Barcha a'zolarga e'lon |
| `/ban <id\|@user> [sabab]` | Global bloklash |
| `/unban <id\|@user>` | Blokdan chiqarish |
| `/banned` | Bloklanganlar ro'yxati |
| `/userinfo <id\|@user>` | Foydalanuvchi haqida (faqat sonlar, suhbat tarixi yo'q) |

**Super Admin**

| Buyruq | Vazifasi |
|---|---|
| `/addadmin <id\|@user>` | Admin tayinlash |
| `/deladmin <id\|@user>` | Adminlikdan bo'shatish |

## Xavfsizlik

- Ishga tushuvchi fayllar (`.exe`, `.apk`, `.bat`, `.ps1`, `.jar` va h.k.) uzatilmaydi.
- Lokatsiya va kontakt uzatilmaydi.
- Har bir qabul qiluvchi jo'natuvchini bloklashi mumkin — bloklangan odam unga boshqa yeta olmaydi va buni bilmaydi.
- Global ban adminlar tomonidan qo'yiladi; oddiy admin boshqa adminni bloklay olmaydi.

## Kod tahlili

Loyihaning batafsil kod tahlili va topilgan muammolar ro'yxati: [HISOBOT.md](HISOBOT.md).
