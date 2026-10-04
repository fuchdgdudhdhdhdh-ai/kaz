import asyncio
import html
import logging
import os
from datetime import datetime, timezone

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("casino")

# ───────────────────────── НАСТРОЙКИ ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
PLATEGA_MERCHANT_ID = os.environ["PLATEGA_MERCHANT_ID"]
PLATEGA_SECRET = os.environ["PLATEGA_SECRET"]
PLATEGA_METHOD = int(os.getenv("PLATEGA_METHOD", "2"))  # 2 = СБП QR
PLATEGA_URL = "https://app.platega.io"
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
RETURN_URL = os.getenv("RETURN_URL", "https://t.me")
PORT = int(os.getenv("PORT", "10000"))

MIN_BET = int(os.getenv("MIN_BET", "50"))
MAX_BET = int(os.getenv("MAX_BET", "0"))  # 0 = без лимита
DAILY_BONUS = 500
MIN_DEPOSIT = 500

MULT_EVEN_ODD = 1.85
MULT_HIGH_LOW = 1.85
MULT_EXACT = 4.0
MULT_DUEL = 1.85
MULT_PVP = 1.85
MULT_HIT = {"basket": 1.85, "football": 1.4}
MULT_DARTS_CENTER = 2.5
MULT_DARTS_COLOR = 1.85

# какие значения dice считаются попаданием
HIT_VALUES = {"basket": {4, 5}, "football": {3, 4, 5}}

# дартс: 1 = мимо, 6 = центр, кольца чередуются белое/красное.
# Если на практике цвета окажутся наоборот, просто поменяйте множества местами.
DARTS_CENTER = {6}
DARTS_WHITE = {2, 4}
DARTS_RED = {3, 5}

DISC = {
    "dice": ("🎲", "Кубик"),
    "basket": ("🏀", "Баскетбол"),
    "football": ("⚽", "Футбол"),
    "darts": ("🎯", "Дартс"),
}
WALLETS = {"coins": "монет", "donate": "донат-монет"}
WALLET_TITLE = {"coins": "🪙 Игровые монеты", "donate": "💎 Донат-монеты"}
THROW_DELAY = 4.2  # ждём окончания анимации

dp = Dispatcher()
pool: asyncpg.Pool
http: aiohttp.ClientSession


# ───────────────────────── БАЗА ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id BIGINT PRIMARY KEY,
    username TEXT,
    name TEXT,
    coins BIGINT NOT NULL DEFAULT 0 CHECK (coins >= 0),
    donate BIGINT NOT NULL DEFAULT 0 CHECK (donate >= 0),
    wallet TEXT NOT NULL DEFAULT 'coins',
    last_bonus TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS payments(
    id TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    amount BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pvp(
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    creator_id BIGINT NOT NULL,
    creator_name TEXT NOT NULL,
    discipline TEXT NOT NULL,
    stake BIGINT NOT NULL,
    wallet TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS withdrawals(
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    amount BIGINT NOT NULL,
    details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", "\u00a0")


def mention(user) -> str:
    return f'<a href="tg://user?id={user.id}">{html.escape(user.full_name)}</a>'


async def get_user(tg_user):
    return await pool.fetchrow(
        """INSERT INTO users(id, username, name) VALUES($1,$2,$3)
           ON CONFLICT (id) DO UPDATE SET username=EXCLUDED.username, name=EXCLUDED.name
           RETURNING *""",
        tg_user.id, tg_user.username, tg_user.full_name,
    )


def _w(wallet: str) -> str:
    assert wallet in WALLETS
    return wallet


async def debit(uid: int, wallet: str, amount: int) -> bool:
    col = _w(wallet)
    row = await pool.fetchrow(
        f"UPDATE users SET {col}={col}-$2 WHERE id=$1 AND {col}>=$2 RETURNING id", uid, amount
    )
    return row is not None


async def credit(uid: int, wallet: str, amount: int):
    if amount <= 0:
        return
    col = _w(wallet)
    await pool.execute(f"UPDATE users SET {col}={col}+$2 WHERE id=$1", uid, amount)


async def balance(uid: int, wallet: str) -> int:
    col = _w(wallet)
    return await pool.fetchval(f"SELECT {col} FROM users WHERE id=$1", uid)


# ───────────────────────── УТИЛИТЫ ИГРЫ ─────────────────────────
def parse_stake(raw: str | None):
    try:
        s = int(raw)
    except (TypeError, ValueError):
        return None
    if s < MIN_BET or (MAX_BET and s > MAX_BET):
        return None
    return s


def stake_error() -> str:
    lim = f" и не больше {fmt(MAX_BET)}" if MAX_BET else ""
    return f"Ставка — целое число от {fmt(MIN_BET)}{lim}."


async def throw(bot: Bot, chat_id: int, emoji: str) -> int:
    msg = await bot.send_dice(chat_id, emoji=emoji)
    await asyncio.sleep(THROW_DELAY)
    return msg.dice.value


async def start_bet(message: Message, raw_stake: str | None):
    """Проверка ставки и списание. Возвращает (user, stake, wallet) или None."""
    u = await get_user(message.from_user)
    stake = parse_stake(raw_stake)
    if stake is None:
        await message.reply(stake_error())
        return None
    wallet = u["wallet"]
    if not await debit(u["id"], wallet, stake):
        bal = await balance(u["id"], wallet)
        await message.reply(
            f"Недостаточно средств. {WALLET_TITLE[wallet]}: <b>{fmt(bal)}</b>\n"
            f"Сменить кошелёк: /wallet"
        )
        return None
    return u, stake, wallet


def result_text(won: bool, stake: int, payout: int, bal: int, wallet: str, push=False) -> str:
    unit = WALLETS[wallet]
    if push:
        return f"🤝 Ничья, ставка возвращена.\nБаланс: <b>{fmt(bal)}</b> {unit}"
    if won:
        return f"✅ <b>Победа!</b> +{fmt(payout - stake)} {unit}\nБаланс: <b>{fmt(bal)}</b> {unit}"
    return f"❌ <b>Проигрыш</b> −{fmt(stake)} {unit}\nБаланс: <b>{fmt(bal)}</b> {unit}"


# ───────────────────────── ОБЩИЕ КОМАНДЫ ─────────────────────────
HELP = f"""🎰 <b>Игровой бот — все команды</b>

<b>💼 Аккаунт</b>
/balance — посмотреть баланс
/bonus — забрать {DAILY_BONUS} монет (раз в сутки)
/wallet — выбрать, с чего играть: 🪙 игровые или 💎 донат-монеты
/deposit <code>сумма</code> — пополнить донат-монеты (от {MIN_DEPOSIT} ₽, 1 монета = 1 ₽)
   Пример: <code>/deposit 500</code>
/withdraw <code>сумма реквизиты</code> — вывод донат-монет (только в личке бота)
   Пример: <code>/withdraw 1000 СБП +79990000000 Сбербанк</code>

<b>🎲 Кубик</b> — формат: <code>/dice ставка выбор</code>
• <code>/dice 100 чет</code> или <code>нечет</code> — x{MULT_EVEN_ODD}
• <code>/dice 100 больше</code> (4–6) или <code>меньше</code> (1–3) — x{MULT_HIGH_LOW}
• <code>/dice 100 5</code> — точное число от 1 до 6 — x{MULT_EXACT:g}

<b>🤖 Дуэль с ботом</b>
• <code>/duel 100</code> — у кого выпадет больше на кубике, тот победил, x{MULT_DUEL} (ничья — возврат)

<b>🏀 Баскетбол</b>
• <code>/basket 100</code> — ставка на попадание, x{MULT_HIT['basket']}

<b>⚽ Футбол</b>
• <code>/football 100</code> — ставка на гол, x{MULT_HIT['football']}

<b>🎯 Дартс</b> — формат: <code>/darts ставка выбор</code>
• <code>/darts 100 центр</code> — попасть в центр, x{MULT_DARTS_CENTER:g}
• <code>/darts 100 белое</code> или <code>красное</code> — попасть в белую/красную зону, x{MULT_DARTS_COLOR}

<b>⚔️ PvP с другим игроком</b>
• <code>/pvp 100</code> — бот предложит выбрать дисциплину (кубик, баскетбол, футбол, дартс) и создаст игру. Кто первым нажмёт «Принять вызов» — тот и играет. Победитель получает x{MULT_PVP}, при ничьей ставки возвращаются.

Минимальная ставка — <b>{MIN_BET}</b>. Ставка — целое число."""


@dp.message(Command("start", "help"))
async def cmd_help(message: Message):
    await get_user(message.from_user)
    await message.answer(HELP)


@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_bot_added(event: ChatMemberUpdated):
    if event.chat.type in ("group", "supergroup"):
        await event.bot.send_message(event.chat.id, "👋 Всем привет! Я игровой бот.\n\n" + HELP)


@dp.message(Command("balance", "bal"))
async def cmd_balance(message: Message):
    u = await get_user(message.from_user)
    active = u["wallet"]
    await message.reply(
        f"👤 {mention(message.from_user)}\n"
        f"🪙 Игровые монеты: <b>{fmt(u['coins'])}</b>\n"
        f"💎 Донат-монеты: <b>{fmt(u['donate'])}</b> (≈ {fmt(u['donate'])} ₽, доступны к выводу)\n"
        f"Ставки сейчас идут с: <b>{WALLET_TITLE[active]}</b>"
    )


@dp.message(Command("wallet"))
async def cmd_wallet(message: Message):
    u = await get_user(message.from_user)
    kb = InlineKeyboardBuilder()
    for w, title in WALLET_TITLE.items():
        mark = "✅ " if u["wallet"] == w else ""
        kb.button(text=mark + title, callback_data=f"wallet:{u['id']}:{w}")
    kb.adjust(1)
    await message.reply("С какого кошелька делать ставки?", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("wallet:"))
async def cb_wallet(cb: CallbackQuery):
    _, uid, w = cb.data.split(":")
    if cb.from_user.id != int(uid):
        return await cb.answer("Это не ваше меню", show_alert=True)
    await get_user(cb.from_user)
    await pool.execute("UPDATE users SET wallet=$2 WHERE id=$1", cb.from_user.id, _w(w))
    await cb.message.edit_text(f"Теперь ставки идут с: <b>{WALLET_TITLE[w]}</b>")
    await cb.answer()


@dp.message(Command("bonus"))
async def cmd_bonus(message: Message):
    u = await get_user(message.from_user)
    now = datetime.now(timezone.utc)
    row = await pool.fetchrow(
        """UPDATE users SET coins=coins+$2, last_bonus=$3
           WHERE id=$1 AND (last_bonus IS NULL OR last_bonus <= $3::timestamptz - interval '24 hours')
           RETURNING coins""",
        u["id"], DAILY_BONUS, now,
    )
    if row:
        return await message.reply(f"🎁 +{DAILY_BONUS} монет! Баланс: <b>{fmt(row['coins'])}</b>")
    last = await pool.fetchval("SELECT last_bonus FROM users WHERE id=$1", u["id"])
    left = int(24 * 3600 - (now - last).total_seconds())
    h, m = left // 3600, (left % 3600) // 60
    await message.reply(f"⏳ Бонус уже получен. Следующий через {h} ч {m} мин.")


# ───────────────────────── КУБИК ─────────────────────────
def parse_choice(raw: str | None):
    if not raw:
        return None
    t = raw.lower().replace("ё", "е")
    if t in ("чет", "even"):
        return "even"
    if t in ("нечет", "odd"):
        return "odd"
    if t in ("больше", "more", "high"):
        return "high"
    if t in ("меньше", "less", "low"):
        return "low"
    if t.isdigit() and 1 <= int(t) <= 6:
        return int(t)
    return None


@dp.message(Command("dice"))
async def cmd_dice(message: Message, command: CommandObject):
    args = (command.args or "").split()
    choice = parse_choice(args[1]) if len(args) > 1 else None
    if len(args) < 2 or choice is None:
        return await message.reply(
            "Формат: <code>/dice ставка чет|нечет|больше|меньше|1-6</code>\n"
            "Пример: <code>/dice 100 чет</code>"
        )
    started = await start_bet(message, args[0])
    if not started:
        return
    u, stake, wallet = started
    try:
        value = await throw(message.bot, message.chat.id, "🎲")
    except Exception:
        await credit(u["id"], wallet, stake)
        raise

    if choice == "even":
        won, mult = value % 2 == 0, MULT_EVEN_ODD
    elif choice == "odd":
        won, mult = value % 2 == 1, MULT_EVEN_ODD
    elif choice == "high":
        won, mult = value >= 4, MULT_HIGH_LOW
    elif choice == "low":
        won, mult = value <= 3, MULT_HIGH_LOW
    else:
        won, mult = value == choice, MULT_EXACT
    payout = int(stake * mult) if won else 0
    await credit(u["id"], wallet, payout)
    bal = await balance(u["id"], wallet)
    await message.reply(f"🎲 Выпало: <b>{value}</b>\n" + result_text(won, stake, payout, bal, wallet))


# ───────────────────────── ДУЭЛЬ С БОТОМ ─────────────────────────
@dp.message(Command("duel"))
async def cmd_duel(message: Message, command: CommandObject):
    started = await start_bet(message, (command.args or "").split()[0] if command.args else None)
    if not started:
        return
    u, stake, wallet = started
    try:
        mine = await throw(message.bot, message.chat.id, "🎲")
        theirs = await throw(message.bot, message.chat.id, "🎲")
    except Exception:
        await credit(u["id"], wallet, stake)
        raise
    if mine == theirs:
        await credit(u["id"], wallet, stake)
        bal = await balance(u["id"], wallet)
        return await message.reply(
            f"Вы: <b>{mine}</b> · Бот: <b>{theirs}</b>\n" + result_text(False, stake, 0, bal, wallet, push=True)
        )
    won = mine > theirs
    payout = int(stake * MULT_DUEL) if won else 0
    await credit(u["id"], wallet, payout)
    bal = await balance(u["id"], wallet)
    await message.reply(
        f"Вы: <b>{mine}</b> · Бот: <b>{theirs}</b>\n" + result_text(won, stake, payout, bal, wallet)
    )


# ───────────────────────── БАСКЕТБОЛ / ФУТБОЛ / ДАРТС ─────────────────────────
async def solo_hit(message: Message, command: CommandObject, disc: str):
    emoji, title = DISC[disc]
    raw = (command.args or "").split()
    started = await start_bet(message, raw[0] if raw else None)
    if not started:
        return
    u, stake, wallet = started
    try:
        value = await throw(message.bot, message.chat.id, emoji)
    except Exception:
        await credit(u["id"], wallet, stake)
        raise
    won = value in HIT_VALUES[disc]
    payout = int(stake * MULT_HIT[disc]) if won else 0
    await credit(u["id"], wallet, payout)
    bal = await balance(u["id"], wallet)
    verdict = "Попадание!" if won else "Мимо!"
    await message.reply(f"{emoji} {title}: <b>{verdict}</b>\n" + result_text(won, stake, payout, bal, wallet))


@dp.message(Command("basket"))
async def cmd_basket(message: Message, command: CommandObject):
    await solo_hit(message, command, "basket")


@dp.message(Command("football"))
async def cmd_football(message: Message, command: CommandObject):
    await solo_hit(message, command, "football")


def parse_darts_choice(raw: str | None):
    if not raw:
        return None
    t = raw.lower().replace("ё", "е")
    if t in ("центр", "яблочко", "center", "bull"):
        return "center"
    if t in ("белое", "белый", "белая", "white"):
        return "white"
    if t in ("красное", "красный", "красная", "red"):
        return "red"
    return None


@dp.message(Command("darts"))
async def cmd_darts(message: Message, command: CommandObject):
    args = (command.args or "").split()
    choice = parse_darts_choice(args[1]) if len(args) > 1 else None
    if choice is None:
        return await message.reply(
            "Формат: <code>/darts ставка центр|белое|красное</code>\n"
            f"• центр — x{MULT_DARTS_CENTER:g}\n"
            f"• белое / красное — x{MULT_DARTS_COLOR}\n"
            "Пример: <code>/darts 100 красное</code>"
        )
    started = await start_bet(message, args[0])
    if not started:
        return
    u, stake, wallet = started
    try:
        value = await throw(message.bot, message.chat.id, "🎯")
    except Exception:
        await credit(u["id"], wallet, stake)
        raise
    if choice == "center":
        won, mult = value in DARTS_CENTER, MULT_DARTS_CENTER
    elif choice == "white":
        won, mult = value in DARTS_WHITE, MULT_DARTS_COLOR
    else:
        won, mult = value in DARTS_RED, MULT_DARTS_COLOR
    payout = int(stake * mult) if won else 0
    await credit(u["id"], wallet, payout)
    bal = await balance(u["id"], wallet)
    if value in DARTS_CENTER:
        where = "центр 🎯"
    elif value in DARTS_WHITE:
        where = "белая зона ⚪"
    elif value in DARTS_RED:
        where = "красная зона 🔴"
    else:
        where = "мимо"
    await message.reply(f"🎯 Дартс: <b>{where}</b>\n" + result_text(won, stake, payout, bal, wallet))


# ───────────────────────── PVP ─────────────────────────
@dp.message(Command("pvp"))
async def cmd_pvp(message: Message, command: CommandObject):
    u = await get_user(message.from_user)
    raw = (command.args or "").split()
    stake = parse_stake(raw[0] if raw else None)
    if stake is None:
        return await message.reply(stake_error() + "\nПример: <code>/pvp 100</code>")
    bal = await balance(u["id"], u["wallet"])
    if bal < stake:
        return await message.reply(f"Недостаточно средств ({WALLET_TITLE[u['wallet']]}: {fmt(bal)}).")
    kb = InlineKeyboardBuilder()
    for key, (emoji, title) in DISC.items():
        kb.button(text=f"{emoji} {title}", callback_data=f"pvpnew:{u['id']}:{stake}:{key}")
    kb.adjust(2)
    await message.reply(f"Ставка <b>{fmt(stake)}</b>. Выберите дисциплину:", reply_markup=kb.as_markup())


def lobby_kb(lobby_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Принять вызов", callback_data=f"pvpjoin:{lobby_id}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"pvpcancel:{lobby_id}"),
    ]])


@dp.callback_query(F.data.startswith("pvpnew:"))
async def cb_pvp_new(cb: CallbackQuery):
    _, uid, stake, disc = cb.data.split(":")
    uid, stake = int(uid), int(stake)
    if cb.from_user.id != uid:
        return await cb.answer("Это не ваше меню", show_alert=True)
    u = await get_user(cb.from_user)
    wallet = u["wallet"]
    if not await debit(uid, wallet, stake):
        return await cb.answer("Недостаточно средств", show_alert=True)
    lobby_id = await pool.fetchval(
        """INSERT INTO pvp(chat_id, creator_id, creator_name, discipline, stake, wallet)
           VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
        cb.message.chat.id, uid, cb.from_user.full_name, disc, stake, wallet,
    )
    emoji, title = DISC[disc]
    await cb.message.edit_text(
        f"⚔️ <b>PvP · {emoji} {title}</b>\n"
        f"Игрок: {mention(cb.from_user)}\n"
        f"Ставка: <b>{fmt(stake)}</b> {WALLETS[wallet]}\n"
        f"Победителю: <b>{fmt(int(stake * MULT_PVP))}</b>\n\n"
        f"Ждём соперника…",
        reply_markup=lobby_kb(lobby_id),
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("pvpcancel:"))
async def cb_pvp_cancel(cb: CallbackQuery):
    lobby_id = int(cb.data.split(":")[1])
    row = await pool.fetchrow(
        """UPDATE pvp SET status='cancelled'
           WHERE id=$1 AND status='open' AND creator_id=$2 RETURNING *""",
        lobby_id, cb.from_user.id,
    )
    if not row:
        return await cb.answer("Отменить может только создатель, пока игра не началась", show_alert=True)
    await credit(row["creator_id"], row["wallet"], row["stake"])
    await cb.message.edit_text("✖️ PvP отменено, ставка возвращена.")
    await cb.answer()


@dp.callback_query(F.data.startswith("pvpjoin:"))
async def cb_pvp_join(cb: CallbackQuery):
    lobby_id = int(cb.data.split(":")[1])
    lobby = await pool.fetchrow("SELECT * FROM pvp WHERE id=$1", lobby_id)
    if not lobby or lobby["status"] != "open":
        return await cb.answer("Игра уже недоступна", show_alert=True)
    if lobby["creator_id"] == cb.from_user.id:
        return await cb.answer("Нельзя играть с самим собой", show_alert=True)
    await get_user(cb.from_user)
    # занимаем лобби атомарно
    row = await pool.fetchrow(
        "UPDATE pvp SET status='playing' WHERE id=$1 AND status='open' RETURNING *", lobby_id
    )
    if not row:
        return await cb.answer("Игра уже занята", show_alert=True)
    stake, wallet = row["stake"], row["wallet"]
    if not await debit(cb.from_user.id, wallet, stake):
        await pool.execute("UPDATE pvp SET status='open' WHERE id=$1", lobby_id)
        return await cb.answer(
            f"Нужно {fmt(stake)} на кошельке «{WALLET_TITLE[wallet]}»", show_alert=True
        )
    await cb.answer()

    disc = row["discipline"]
    emoji, title = DISC[disc]
    bot = cb.bot
    chat_id = row["chat_id"]
    creator_id = row["creator_id"]
    joiner_id = cb.from_user.id
    await cb.message.edit_text(
        f"⚔️ <b>PvP · {emoji} {title}</b>\n"
        f"{html.escape(row['creator_name'])} vs {mention(cb.from_user)}\n"
        f"Ставка: <b>{fmt(stake)}</b> {WALLETS[wallet]} — бросаем!"
    )
    try:
        a = await throw(bot, chat_id, emoji)
        b = await throw(bot, chat_id, emoji)
    except Exception:
        await credit(creator_id, wallet, stake)
        await credit(joiner_id, wallet, stake)
        await pool.execute("UPDATE pvp SET status='error' WHERE id=$1", lobby_id)
        raise

    await pool.execute("UPDATE pvp SET status='done' WHERE id=$1", lobby_id)
    names = f"{html.escape(row['creator_name'])}: <b>{a}</b> · {html.escape(cb.from_user.full_name)}: <b>{b}</b>"
    if a == b:
        await credit(creator_id, wallet, stake)
        await credit(joiner_id, wallet, stake)
        text = f"{names}\n🤝 Ничья — ставки возвращены."
    else:
        winner_id, winner_name = (
            (creator_id, html.escape(row["creator_name"])) if a > b else (joiner_id, mention(cb.from_user))
        )
        payout = int(stake * MULT_PVP)
        await credit(winner_id, wallet, payout)
        text = f"{names}\n🏆 Победил {winner_name}: +{fmt(payout - stake)} {WALLETS[wallet]}"
    await bot.send_message(chat_id, text)


# ───────────────────────── ПЛАТЕЖИ (PLATEGA) ─────────────────────────
def platega_headers():
    return {
        "X-MerchantId": PLATEGA_MERCHANT_ID,
        "X-Secret": PLATEGA_SECRET,
        "Content-Type": "application/json",
    }


@dp.message(Command("deposit"))
async def cmd_deposit(message: Message, command: CommandObject):
    u = await get_user(message.from_user)
    raw = (command.args or "").split()
    try:
        amount = int(raw[0])
    except (IndexError, ValueError):
        return await message.reply(f"Формат: <code>/deposit сумма</code> (от {MIN_DEPOSIT} ₽)")
    if amount < MIN_DEPOSIT:
        return await message.reply(f"Минимальное пополнение — {MIN_DEPOSIT} ₽.")

    body = {
        "paymentMethod": PLATEGA_METHOD,
        "paymentDetails": {"amount": amount, "currency": "RUB"},
        "description": f"Пополнение донат-монет, user {u['id']}",
        "return": RETURN_URL,
        "failedUrl": RETURN_URL,
        "payload": str(u["id"]),
    }
    try:
        async with http.post(f"{PLATEGA_URL}/transaction/process", json=body, headers=platega_headers()) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or "transactionId" not in data:
                log.error("Platega create error %s: %s", r.status, data)
                return await message.reply("Не удалось создать платёж, попробуйте позже.")
    except Exception:
        log.exception("Platega request failed")
        return await message.reply("Платёжная система недоступна, попробуйте позже.")

    await pool.execute(
        "INSERT INTO payments(id, user_id, chat_id, amount) VALUES($1,$2,$3,$4)",
        data["transactionId"], u["id"], message.chat.id, amount,
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=data["redirect"])]])
    await message.reply(
        f"Счёт на <b>{amount} ₽</b> создан. После оплаты придёт <b>{fmt(amount)}</b> 💎 донат-монет.",
        reply_markup=kb,
    )


async def platega_status(tx_id: str):
    try:
        async with http.get(f"{PLATEGA_URL}/transaction/{tx_id}", headers=platega_headers()) as r:
            if r.status == 200:
                return (await r.json(content_type=None)).get("status")
    except Exception:
        log.exception("Platega status failed")
    return None


async def platega_callback(request: web.Request):
    if (
        request.headers.get("X-MerchantId") != PLATEGA_MERCHANT_ID
        or request.headers.get("X-Secret") != PLATEGA_SECRET
    ):
        return web.Response(status=401)
    data = await request.json()
    tx_id, status = data.get("id"), data.get("status")
    log.info("Platega callback: %s %s", tx_id, status)
    if status == "CONFIRMED" and tx_id:
        # перепроверяем статус напрямую в Platega
        if await platega_status(tx_id) == "CONFIRMED":
            row = await pool.fetchrow(
                """UPDATE payments SET status='CONFIRMED'
                   WHERE id=$1 AND status<>'CONFIRMED' RETURNING *""",
                tx_id,
            )
            if row:  # зачисляем один раз, сумма берётся из нашей БД
                await credit(row["user_id"], "donate", row["amount"])
                try:
                    await request.app["bot"].send_message(
                        row["chat_id"],
                        f'✅ <a href="tg://user?id={row["user_id"]}">Игрок</a>, оплата получена: '
                        f"+<b>{fmt(row['amount'])}</b> 💎 донат-монет.",
                    )
                except Exception:
                    log.exception("notify failed")
    elif status == "CANCELED" and tx_id:
        await pool.execute("UPDATE payments SET status='CANCELED' WHERE id=$1 AND status='PENDING'", tx_id)
    return web.Response(text="ok")


# ───────────────────────── ВЫВОД ─────────────────────────
@dp.message(Command("withdraw"))
async def cmd_withdraw(message: Message, command: CommandObject):
    if message.chat.type != "private":
        return await message.reply("Вывод оформляется только в личке с ботом (чтобы не светить реквизиты).")
    u = await get_user(message.from_user)
    parts = (command.args or "").split(maxsplit=1)
    try:
        amount = int(parts[0])
        details = parts[1].strip()
    except (IndexError, ValueError):
        return await message.reply("Формат: <code>/withdraw сумма реквизиты</code>\nПример: <code>/withdraw 1000 СБП +79990000000 Сбербанк</code>")
    if amount <= 0:
        return await message.reply("Сумма должна быть положительной.")
    if not await debit(u["id"], "donate", amount):
        return await message.reply(f"Недостаточно донат-монет: {fmt(u['donate'])}.")
    wid = await pool.fetchval(
        "INSERT INTO withdrawals(user_id, amount, details) VALUES($1,$2,$3) RETURNING id",
        u["id"], amount, details,
    )
    await message.reply(f"Заявка #{wid} на вывод {fmt(amount)} ₽ принята. Администратор обработает её вручную.")
    if ADMIN_ID:
        await message.bot.send_message(
            ADMIN_ID,
            f"💸 Заявка #{wid}\nПользователь: {mention(message.from_user)} (<code>{u['id']}</code>)\n"
            f"Сумма: {fmt(amount)} ₽\nРеквизиты: {html.escape(details)}",
        )


# ───────────────────────── ЗАПУСК ─────────────────────────
async def health(_):
    return web.Response(text="ok")


async def main():
    global pool, http
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/platega/callback", platega_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("HTTP server on :%s", PORT)

    await bot.set_my_commands([
        BotCommand(command="help", description="Все команды и как их вводить"),
        BotCommand(command="balance", description="Мой баланс"),
        BotCommand(command="bonus", description="Ежедневный бонус"),
        BotCommand(command="wallet", description="Выбрать кошелёк"),
        BotCommand(command="dice", description="Кубик: /dice ставка чет|нечет|больше|меньше|1-6"),
        BotCommand(command="duel", description="Дуэль с ботом"),
        BotCommand(command="basket", description="Баскетбол"),
        BotCommand(command="football", description="Футбол"),
        BotCommand(command="darts", description="Дартс: центр|белое|красное"),
        BotCommand(command="pvp", description="PvP с игроком"),
        BotCommand(command="deposit", description="Пополнить донат-монеты"),
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await http.close()
        await pool.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
