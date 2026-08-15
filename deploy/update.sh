#!/usr/bin/env bash
# Serverni GitHub'dagi oxirgi holatga keltiradi.
#
#   ssh -i ~/.ssh/growth-up ubuntu@158.178.149.128 /opt/anon-bot/deploy/update.sh
#
# growth-up dagi update.sh dan soddaroq: bu yerda frontend ham, migratsiya
# skriptlari ham yo'q — sxema o'zgarishi anon_bot.py ning init_db() ichida,
# ALTER TABLE lar xatoni yutib yuboradigan tarzda bajariladi.

set -euo pipefail

APP_DIR="/opt/anon-bot"
cd "$APP_DIR"

ESKI=${ANON_ESKI:-$(git rev-parse HEAD)}
git pull -q origin main
YANGI=$(git rev-parse HEAD)

# Skript o'zini yangilagan bo'lishi mumkin — bash faylni bo'lak-bo'lak o'qiydi,
# almashtirilgan fayl o'rtasidan davom etish jim xatolarga olib keladi.
# Shuning uchun yangilanish bo'lsa yangi nusxa boshidan ishga tushiriladi.
if [ "$ESKI" != "$YANGI" ] && [ -z "${ANON_ESKI:-}" ]; then
    export ANON_ESKI="$ESKI"
    exec "$APP_DIR/deploy/update.sh" "$@"
fi

if [ "$ESKI" = "$YANGI" ] && [ "${1:-}" != "--force" ]; then
    echo "Yangilanish yo'q — kod oxirgi holatda ($(git log --oneline -1))"
    echo "Baribir qayta o'rnatish kerak bo'lsa: $0 --force"
    exit 0
fi

if [ "${1:-}" = "--force" ]; then
    echo "--force: bog'liqliklar qayta o'rnatiladi"
    OZGARGAN=$(git ls-files)
else
    echo "Yangilanish: $(git log --oneline "$ESKI..$YANGI" | wc -l) ta commit"
    git log --oneline "$ESKI..$YANGI"
    echo
    OZGARGAN=$(git diff --name-only "$ESKI" "$YANGI")
fi

if echo "$OZGARGAN" | grep -q "^requirements.txt"; then
    echo "→ Python bog'liqliklari yangilanyapti"
    .venv/bin/pip install -q -r requirements.txt
fi

if echo "$OZGARGAN" | grep -q "^deploy/anon-bot.service"; then
    echo "→ systemd xizmati yangilanyapti"
    sudo cp deploy/anon-bot.service /etc/systemd/system/
    sudo systemctl daemon-reload
fi

# Bazadan zaxira — sxema o'zgarishi init_db() da bo'lgani uchun qayta
# ishga tushirishning o'zi migratsiya hisoblanadi.
if [ -f anon_qa_bot.db ]; then
    ZAXIRA="anon_qa_bot.db.$(date +%Y%m%d-%H%M%S).bak"
    echo "→ Zaxira: $ZAXIRA"
    sqlite3 anon_qa_bot.db ".backup '$ZAXIRA'"
    ls -t anon_qa_bot.db.*.bak 2>/dev/null | tail -n +8 | xargs -r rm --
fi

echo "→ Qayta ishga tushirilyapti"
sudo systemctl restart anon-bot

# Ko'tarilishini kutamiz. Bot startda Telegramga menyu buyruqlarini
# o'rnatadi — 1 GB xotirali serverda bu bir necha soniya oladi.
for i in $(seq 1 30); do
    if ! systemctl is-active --quiet anon-bot; then
        echo "XATO: xizmat ko'tarilmadi. Sabab:"
        sudo journalctl -u anon-bot -n 20 --no-pager
        exit 1
    fi
    if journalctl -u anon-bot --since "-2min" | grep -q "Bot is starting"; then
        echo "TAYYOR — bot ishga tushdi ($i soniyada)"
        exit 0
    fi
    sleep 1
done

echo "XATO: 30 soniyada bot ishga tushmadi. Jurnal:"
sudo journalctl -u anon-bot -n 30 --no-pager
exit 1
