# Kod xaritasi — anon-bot

Butun bot **bitta faylda**: `anon_bot.py` (~1680 qator). Modullarga bo'lish
ataylab qilinmagan — mantiq bitta ekranga sig'adigan darajada sodda va
pyTelegramBotAPI dekoratorlari fayl bo'ylab tartib bilan joylashgan.

## Fayllar

| Fayl | Nima |
|---|---|
| `anon_bot.py` | Butun bot: konfig, DB, handlerlar, ishga tushirish |
| `deploy/anon-bot.service` | systemd birligi (server) |
| `deploy/update.sh` | `git pull` + zaxira + restart |
| `deploy/SERVER.md` | Bir martalik o'rnatish qo'llanmasi |
| `HISOBOT.md` | Kod tahlili: topilgan 23 ta muammo va ular qanday hal qilingani |
| `.env` | Token va SUPER_ADMIN_ID. **Repoda yo'q**, serverda qo'lda yoziladi |

## Buzib bo'lmaydigan qoidalar

1. **Suhbat jurnali saqlanmaydi.** `message_mappings` kabi jadval qaytadan
   kiritilmasin. Kim kimga yozgani diskka yozilmasligi — loyihaning asosiy
   va'dasi, `/help` da foydalanuvchiga aytilgan.
2. **Javob berish `callback_data` orqali ishlaydi.** `resolve_sender_from_reply()`
   javob berilayotgan xabarning inline tugmasidan `reply_<id>` ni o'qiydi.
   Shu sababli tugmalar **har bir** anonim xabarga biriktirilishi shart —
   media nusxasiga ham, sarlavha xabariga ham. Tugmani olib tashlash =
   o'sha xabarga javob berish imkoniyatini o'ldirish.
3. **Rate-limit ataylab yo'q.** Loyiha egasining qarori. Qayta taklif qilinmasin.
4. **Maxfiy qiymat kodga yozilmaydi.** `SUPER_ADMIN_ID` ning default qiymati
   yo'q — yo'qligida bot ataylab ishga tushmaydi.
5. **Callbackda `call.from_user`**, hech qachon `call.message.from_user` —
   ikkinchisi bot, foydalanuvchi emas. Bir marta shu sabab DB'dagi
   username'lar bot username'iga almashib ketgan edi.

## Baza (SQLite, `anon_qa_bot.db`)

Sxema `init_db()` (`:64`) da. Alembic yo'q — yangi ustun `ALTER TABLE` bilan,
xatoni yutadigan siklda qo'shiladi, ya'ni har ishga tushishda o'zi moslashadi.

| Jadval | Nima saqlaydi |
|---|---|
| `users` | chat_id, username, link_code, faol/oxirgi nishon, is_admin (0/1/2), sent_count, received_count |
| `blocked_users` | (owner_id, blocked_id) — shaxsiy bloklar |
| `banned_users` | global banlar (admin qo'yadi) |
| `reports` | shikoyatlar: kim, kimga, matn, status |
| `support_messages` | `/support` murojaatlari: matn, status |

`is_admin`: `0` oddiy, `1` admin, `2` super admin.

## Asosiy funksiyalar

| Funksiya | Qator | Vazifa |
|---|---|---|
| `forward_anonymous_message()` | 585 | Anonim xabarni yetkazish — barcha yo'llar shu yerdan o'tadi |
| `resolve_sender_from_reply()` | 478 | Javob → jo'natuvchi (bazasiz) |
| `resolve_target_arg()` | 377 | `chat_id` yoki `@username` → foydalanuvchi (4 ta admin buyrug'i ishlatadi) |
| `remember_last_target()` | 439 | Faol nishonni tozalab, oxirgisini eslab qolish |
| `anon_tag()` | 464 | `chat_id` → `anon#a1b2c3` taxallus (`/reports` uchun) |
| `is_blocked_document()` | 459 | Xavfli fayl kengaytmasi filtri |
| `bump_message_counters()` | 255 | Sonli hisoblagichlar (jurnal emas) |

## Handlerlar tartibi — MUHIM

pyTelegramBotAPI handlerlarni **ro'yxatdan o'tish tartibida** sinaydi.
Fayldagi joylashuv = ustuvorlik. Hozirgi tartib:

1. Buyruqlar (`/start`, `/ban`, …) — `:694`–`:1224`
2. `handle_reply_messages` (`:1225`) — javob berilgan xabar tugmasi bo'lsa
3. `handle_unsupported_content` (`:1238`) — lokatsiya/kontakt
4. `handle_admin_support_reply` (`:1266`) — admin support javobi
5. `handle_writing_mode` (`:1282`) — qolgan hammasi

**4-chi 5-chidan oldin turishi shart**, aks holda admin support javobi oddiy
anonim xabar sifatida ketadi. Va u `active_target_id` ni tekshiradi — admin
shu payt anonim xabar yozayotgan bo'lsa aralashmaydi (`_is_admin_support_reply`).

`content_types` ko'rsatilmasa pyTelegramBotAPI **faqat `text`** ni oladi —
media qabul qilishi kerak bo'lgan har bir handlerda uni yozish shart.

## Holat (2026-08-15)

Serverda ishlaydi: Oracle Cloud `158.178.149.128`, `/opt/anon-bot`,
systemd xizmati `anon-bot` (`enabled`, avtoyuklanadi). `growth-up` bilan
bir serverda, bir-biriga xalaqit qilmaydi.

Yangilash: `ssh -i ~/.ssh/growth-up ubuntu@158.178.149.128 /opt/anon-bot/deploy/update.sh`
