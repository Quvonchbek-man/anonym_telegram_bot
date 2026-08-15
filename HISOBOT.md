# Anonim Telegram bot — kod tahlili hisoboti

**Repo:** `github.com/Quvonchbek-man/anonym_telegram_bot` (commit `2d73622`)
**Sana:** 2026-08-15
**Hajm:** `anon_bot.py` 1768 qator · `bot.py` 2213 · `translations.py` 272 · `demo.py` 1
**Status:** tahlil qilindi → topilmalar tuzatildi (rate-limitdan tashqari — u ataylab qo'shilmadi)

> **Bir jumlada:** repodagi bot umuman ishga tushmas edi (sintaksis xatosi), va unda muallifning shaxsiy Telegram ID'si default super-admin sifatida qotirib qo'yilgan edi.

## Asosiy me'moriy o'zgarish: suhbat jurnali butunlay olib tashlandi

Talab: *foydalanuvchi xabarlari 7 kun saqlanmasin, lekin eski xabarlarga javob berish ishlab tursin.*

Avval `message_mappings` jadvali "qaysi xabar kimdan kelgan"ni 7 kun saqlab turardi va `cleanup_old_mappings` uni o'chirardi — shu sababli 7 kundan eski xabarga javob berib bo'lmasdi.

Endi bu jadval **umuman yo'q**. Kerakli `sender_id` xabarga biriktirilgan "🔄 Javob berish" tugmasining `callback_data` sida (`reply_<id>`) turadi, ya'ni **Telegram'ning o'zida** saqlanadi. `resolve_sender_from_reply()` uni javob berilayotgan xabardan o'qib oladi.

Natija: bot diskka hech narsa yozmaydi **va** javob berish muddatsiz ishlaydi — ikkala talab bir yechim bilan bajarildi. Eski bazalarda qolgan jadval `DROP TABLE IF EXISTS message_mappings` orqali migratsiyada o'chiriladi.

---

## A. Kritik — bot ishga tushmaydi

### A1. Sintaksis xatosi ×2 — `anon_bot.py:844` va `anon_bot.py:899`

`handle_addadmin_command` va `handle_deladmin_command` funksiyalarida funksiya tanasi `try:` bilan ochilmagan, lekin oxirida `except Exception as e:` turibdi:

```
$ python -c "import ast; ast.parse(open('anon_bot.py',encoding='utf-8').read())"
  File "<unknown>", line 844
    except Exception as e:
    ^^^^^^
SyntaxError: invalid syntax
```

Ya'ni GitHub'dagi kod **hech qachon ishga tushmaydi** — `python anon_bot.py` darhol yiqiladi. Oxirgi commit (`2d73622` — "prevent banning Super Admin, add username resolution to addadmin/deladmin") aynan shu ikki funksiyaga tegib, ishga tushirib ko'rilmasdan push qilingan.

### A2. `ADMIN_IDS` env o'qiladi, lekin ishlatilmaydi — `anon_bot.py:22-28`

`.env` da `ADMIN_IDS=111,222` yozilsa ham hech narsa bo'lmaydi: `is_admin()` faqat DB'dagi `is_admin` ustunini tekshiradi. `ADMIN_IDS` o'zgaruvchisi butun faylda boshqa hech qayerda uchramaydi (faqat 22–28-qatorlar). Commit `870572b` xabari "fix is_admin bug for env configured ADMIN_IDS" deydi — lekin tuzatilmagan, o'lik kod qolib ketgan.

---

## B. Xavfsizlik va maxfiylik

### B1. Muallifning shaxsiy Telegram ID'si default qiymat — `anon_bot.py:19`

```python
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "<OWNER_CHAT_ID>"))
```

Kimdir bu repoyi klon qilib `SUPER_ADMIN_ID` ni qo'ymasa, **muallif o'sha botning super-admini bo'lib qoladi** — ban, broadcast, barcha shikoyatlarni o'qish huquqi bilan. Default umuman bo'lmasligi, `.env` da majburiy qilinishi kerak.

### B2. Fiktiv foydalanuvchi qatori qattiq yozilgan — `anon_bot.py:170`

```python
cursor.execute("INSERT OR IGNORE INTO users (chat_id, username, link_code, is_admin) "
               "VALUES (?, '<username>', '<LINK_CODE>', 2)", (SUPER_ADMIN_ID,))
```

Har qanday deploy'da `t.me/<bot>?start=<LINK_CODE>` havolasi orqali yuborilgan anonim xabarlar `<OWNER_CHAT_ID>` ga ketadi. Qo'shimcha: bu "foydalanuvchi" `/start` bosmagan bo'lsa ham `users` jadvalida turadi → `/stats` noto'g'ri, broadcast unga urinib xato qaytaradi.

### B3. `/history` anonimlikni ochib beradi — `anon_bot.py:1158-1229`

`/help` matnida (`:945`) "Oddiy anonim xabarlar matni bazada saqlanmaydi" deyilgan — bu to'g'ri. Lekin `message_mappings` jadvalida **kim kimga yozgani** (`sender_id` ↔ `recipient_id` + vaqt) 7 kun saqlanadi va istalgan admin `/history <chat_id>` bilan buni ko'radi. Texnik jihatdan javob berish uchun kerak, ammo `/help` dagi maxfiylik va'dasi amaliyot bilan to'liq mos emas — foydalanuvchiga metadata saqlanishi ham aytilishi kerak.

### B4. Oddiy admin boshqa adminni ban qila oladi — `anon_bot.py:706`

`/ban` da faqat `SUPER_ADMIN_ID` himoyalangan. `is_admin >= 1` bo'lgan har qanday admin boshqa adminni global ban qila oladi.

### B5. Fayl kengaytmasi filtri zaif — `anon_bot.py:557`

```python
blocked_exts = ['.apk', '.exe', '.msi', '.bat', '.cmd', '.sh', '.com', '.vbs', '.js', '.scr', '.pif']
```

`.jar`, `.ps1`, `.msix`, `.lnk`, `.iso` yo'q; MIME turi tekshirilmaydi. Eng muhimi — bu tekshiruv **faqat anonim xabar yo'lida** bor: `/support` (`:1021`) va `/broadcast` (`:1633`) orqali istalgan fayl filtrsiz o'tadi.

### B6. Rate-limit yo'q

Bir foydalanuvchi bir qabul qiluvchiga cheksiz xabar yuborishi mumkin — flood himoyasi umuman yo'q. Repo tavsifida "rate-limited" deyilgan, lekin `time.sleep(0.05)` faqat broadcast siklida (`:1639`).

---

## C. Mantiqiy buglar

### C1. "❌ Bekor qilish" tugmasi foydalanuvchi username'ini buzadi — `anon_bot.py:1402`, `:1708`

```python
send_user_link(call.message)   # call.message — BOTNING xabari!
```

`send_user_link` ichida (`:525-526`) `message.from_user.username` olinadi. Callback'da `call.message.from_user` — bu **bot**, foydalanuvchi emas. Natijada `register_user()` foydalanuvchining username'ini **bot username'iga almashtirib yuboradi**.

Oqibati: `/ban @username`, `/unban @username`, `/addadmin @username` noto'g'ri odamni topadi yoki umuman topmaydi; DB'da bir nechta foydalanuvchi bir xil (bot) username bilan yotib qoladi.

To'g'risi: `call.from_user` ishlatilishi kerak (`call.message.from_user` emas).

### C2. Media bilan javob berish ishlamaydi — `anon_bot.py:1286`

```python
@bot.message_handler(func=lambda message: message.reply_to_message is not None and ...)
```

pyTelegramBotAPI'da `content_types` ko'rsatilmasa **default `['text']`**. Kelgan anonim xabarga rasm/video/voice bilan "reply" qilinsa, bu handler ishlamaydi — xabar `handle_writing_mode` ga tushadi va `active_target_id` bo'lmasa, javob o'rniga foydalanuvchiga **o'z havolasi** yuboriladi. Xuddi shu muammo `handle_admin_support_reply` (`:1348`) da ham bor.

### C3. Admin support rejimi anonim xabar yuborishni o'g'irlaydi — `anon_bot.py:1348-1360`

`active_support_target` o'rnatilgan admin shu payt kimgadir anonim xabar yozmoqchi bo'lsa, matn `handle_admin_support_reply` ga tushadi va **support so'ragan foydalanuvchiga** ketadi. Handler `handle_writing_mode` dan oldin ro'yxatdan o'tgan va `active_target_id` ni umuman tekshirmaydi.

### C4. 7 kundan eski xabarga javob jimgina yo'qoladi — `:1710-1722` + `:1389-1391`

`cleanup_old_mappings(days=7)` mapping'ni o'chiradi. Foydalanuvchi eski xabarga javob yozsa, hech qanday xato xabari chiqmaydi — o'rniga `send_user_link()` ishlab, unga o'z havolasi yuboriladi. Foydalanuvchi uchun butunlay tushunarsiz xatti-harakat.

### C5. `/stats` va `/history` da DB ulanishi sizib chiqadi — `:1081-1099`, `:1177-1202`

Bu ikki funksiyada `conn.close()` `finally` blokida emas — o'rtada exception bo'lsa ulanish ochiq qoladi. Faylning qolgan qismida `try/finally` to'g'ri qo'llanilgan, faqat shu ikkitasi istisno.

### C6. Har bir kelgan xabar uchun ~6-7 ta alohida SQLite ulanishi

Filtr lambdalar (`:1286`, `:1348`) DB so'rovi qiladi — ular **har bir xabar uchun** ishlaydi. `:1348` dagi lambda `get_user()` ni ikki marta chaqiradi, keyin handler ichida yana bir marta. Yuklama ostida WAL bo'lsa ham `database is locked` ga olib keladi.

### C7. Holat lug'atlari hech qachon tozalanmaydi — `:982`, `:1139`

`USER_SUPPORT_STATES[chat_id] = False` (`pop` emas) — `/support` bosgan har bir foydalanuvchi xotirada abadiy qoladi. Xuddi shu `ADMIN_BROADCAST_STATES` da. Sekin o'suvchi memory leak.

### C8. Broadcast ban qilinganlarga ham yuboriladi — `:1622`

`SELECT chat_id FROM users` — `banned_users` filtri yo'q. Shuningdek Telegram 429 (`retry_after`) javobiga qayta urinish yo'q: shunchaki `fail += 1`, ya'ni rate-limit'ga tushgan foydalanuvchilar e'lonni olmaydi.

### C9. `users.username` da UNIQUE yo'q — `:372`

`get_user_by_username` birinchi topilganini qaytaradi. C1 bilan birgalikda `/ban @user` noto'g'ri odamni bloklashi mumkin.

### C10. `init_db()` faqat `if __name__ == '__main__'` ichida — `:1727`

Modul sifatida import qilinsa jadvallar yaratilmaydi.

---

## D. Keraksiz narsalar

### D1. `bot.py` (2213 qator, 85 KB) — mutlaqo boshqa loyihaning kodi

Bu Instagram/YouTube yuklab oluvchi + Shazam boti (`yt_dlp`, `shazamio`, `pydub`, `imageio_ffmpeg`, `TRANSLATIONS` import qiladi). Anonim botga hech qanday aloqasi yo'q, `anon_bot.py` uni import qilmaydi, `requirements.txt` da uning bironta ham dependency'si yo'q — ya'ni bu fayl bu repoda baribir ishlamaydi. Kod allaqachon alohida repoda: `instagram-youtube-shazam-bot`. **O'chirilishi kerak.**

### D2. `translations.py` (272 qator) — faqat `bot.py` ishlatadi

`anon_bot.py` da `TRANSLATIONS` so'zi 0 marta uchraydi. D1 bilan birga ketadi.

### D3. `demo.py` — bitta qator: `import numpy as np`

Hech kim import qilmaydi, `numpy` `requirements.txt` da ham yo'q. Tasodifan qo'shilgan.

### D4. `.gitignore` o'z source fayllarini ignore qilmoqchi — lekin kech

```
bot.py
demo.py
math_bot.py
translations.py
```

Bu fayllar allaqachon track qilingan, shuning uchun `.gitignore` ularga ta'sir qilmaydi — ular baribir repoda turibdi (ignore qilish uchun `git rm --cached` kerak). `.gitignore` boshqa loyihadan ko'chirilgani ko'rinib turibdi: `cookies.txt`, `temp_downloads/`, `math_bot.py`, `user_settings.db` — anonim botda bunday narsalar yo'q.

### D5. Git tarixi ikki loyihadan aralashgan

7 ta commitning 2 tasi anonim botga umuman tegishli emas:
- `f0f6337` — "dummy HTTP server for Render free tier"
- `3b0f353` — "multi-language support ... using persistent SQLite"

Ikkalasi ham yuklovchi botning commitlari.

### D6. `ThreadSafeDict` (`:34-61`) — ortiqcha murakkablik

CPython'da `dict` amallari GIL ostida allaqachon atomik. Bu klass hech qanday poyga holatini bartaraf etmaydi — `get`-keyin-`set` ketma-ketligi baribir atomik emas. Oddiy `dict` yetarli, 28 qator ortiqcha.

### D7. `mark_support_resolved()` (`:303-312`) — o'lik kod

Hech qayerda chaqirilmaydi; o'rniga `:1366` da xuddi shu SQL qo'lda yozilgan.

### D8. Takrorlanuvchi kod bloklari

- `set_active_target(x, None)` + qo'lda `UPDATE users SET last_target_id = ?` — 4 marta aynan bir xil takrorlangan (`:565-572`, `:648-654`, `:1310-1316`, `:1334-1340`). Ammo `set_active_target` **allaqachon** `last_target_id` ni yozadi (`:400-403`) — ya'ni bu bloklar butunlay ortiqcha.
- `handle_location` va `handle_contact` (`:1300-1346`) — 46 qator, bir-biriga deyarli aynan. Bitta handler `content_types=['location','venue','contact']` bilan yetarli.
- `/ban`, `/unban`, `/addadmin`, `/deladmin` da "ID yoki @username ni yechish" bloki 4 marta copy-paste (`:690-704`, `:731-745`, `:800-814`, `:859-873`).

### D9. Loyiha hujjatlari yo'q

README.md, LICENSE, `.env.example` — hech biri yo'q. GitHub tavsifida sanab o'tilgan imkoniyatlar (`dynamic admin hierarchy`, `support ticketing system`, `fallback link sharing`) hech qayerda hujjatlashtirilmagan.

### D10. `requirements.txt` versiyalari qotirilmagan

`pyTelegramBotAPI` va `python-dotenv` versiyasiz — kelajakdagi breaking change botni to'xtatishi mumkin.

### D11. Mayda tozalanmagan joylar

| Joy | Nima |
|---|---|
| `:773` | `import html` funksiya ichida — yuqoriga chiqarilsin |
| `:1053` | `time = row['timestamp']` — `time` modulini soyalaydi |
| `:1217` | `f"\n🔄 ..."` — placeholder yo'q, keraksiz f-string |
| `:1358` | `except Exception as e` — `e` ishlatilmaydi |
| `:484` | `share_url` da `link_code` URL-encode qilinmagan (hozir ishlaydi, lekin mo'rt) |

---

## Bajarilgan ishlar jadvali

| # | Topilma | Holat | Qanday hal qilindi |
|---|---|---|---|
| A1 | Sintaksis xatosi ×2 | ✅ | `try:` siz `except` bloklari olib tashlandi |
| A2 | `ADMIN_IDS` ishlamaydi | ✅ | `is_admin()` ga ulandi + startda DB'ga belgilanadi |
| B1 | Muallif ID'si default | ✅ | Default olib tashlandi, `.env` majburiy; bo'sh qiymat aniq xato beradi |
| B2 | Soxta qator + ochiq `link_code` | ✅ | `INSERT` olib tashlandi; mavjud bazadagi ochiq kod yangilandi |
| B3 | `/history` anonimlikni ochadi | ✅ | `/userinfo` bilan almashtirildi — faqat sonlar, aloqa ro'yxati yo'q |
| B4 | Admin adminni ban qiladi | ✅ | Faqat Super Admin qila oladi |
| B5 | Zaif fayl filtri | ✅ | `BLOCKED_DOC_EXTS` kengaytirildi (`.ps1`, `.jar`, `.lnk`, `.iso` …) |
| B6 | Rate-limit yo'q | ⬜ | **Ataylab qo'shilmadi** — loyiha egasi kerak emas dedi |
| C1 | "Bekor qilish" username'ni buzadi | ✅ | `send_user_link(chat_id, username)` + `call.from_user` |
| C2 | Media bilan javob ishlamaydi | ✅ | `content_types` qo'shildi; tugmalar mediyaning o'ziga ham biriktiriladi |
| C3 | Support/anonim to'qnashuvi | ✅ | `active_target_id` bor bo'lsa support handleri aralashmaydi |
| C4 | Eski xabarga javob yo'qoladi | ✅ | Jurnal olib tashlandi — javob muddatsiz ishlaydi |
| C5 | `conn.close()` sizishi | ✅ | `/stats` `try/finally` ga o'tkazildi, `/history` qayta yozildi |
| C6 | Xabar boshiga 6-7 DB ulanishi | ✅ | Reply filtri DB'ga umuman tegmaydi; support filtri 3 → 1 so'rov |
| C7 | Holat lug'atlari o'sadi | ✅ | `False` o'rniga `pop()` |
| C8 | Broadcast: banlanganlar + 429 | ✅ | `NOT IN banned_users` + `retry_after` bilan qayta urinish |
| C9 | `username` indekssiz | ✅ | `idx_users_username` qo'shildi |
| C10 | `init_db` faqat `__main__` da | ✅ | Modul darajasiga ko'chirildi |
| D1 | `bot.py` — begona loyiha | ✅ | O'chirildi |
| D2 | `translations.py` | ✅ | O'chirildi |
| D3 | `demo.py` | ✅ | O'chirildi |
| D4 | `.gitignore` noto'g'ri | ✅ | Qaytadan yozildi |
| D5 | Git tarixi aralashgan | ⬜ | Tarixni qayta yozish kerak (`filter-repo`) — alohida ish |
| D6 | `ThreadSafeDict` ortiqcha | ✅ | Oddiy `dict` ga almashtirildi (28 qator kamaydi) |
| D7 | `mark_support_resolved` o'lik | ✅ | `mark_support_resolved_for_user()` qilib ishga solindi |
| D8 | Takrorlanuvchi bloklar | ✅ | `resolve_target_arg()`, `remember_last_target()`; location+contact birlashtirildi |
| D9 | Hujjat yo'q | ✅ | `README.md`, `.env.example` yozildi |
| D10 | Versiyalar qotirilmagan | ✅ | `==4.26.0`, `==1.0.1` |
| D11 | Mayda tozalashlar | ✅ | `import html` yuqoriga, `time` soyalanishi, ishlatilmagan o'zgaruvchilar |

Qo'shimcha: `/reports` da anonim jo'natuvchining xom `chat_id` si o'rniga barqaror taxallus (`anon#a1b2c3`) ko'rsatiladi — bloklash tugmasi baribir ishlaydi. `/help` dagi maxfiylik matni endi nima saqlanishini aniq aytadi.

### Qayta tekshirish

```bash
python -m pyflakes anon_bot.py
```

Hech qanday ogohlantirish bermasligi kerak.
