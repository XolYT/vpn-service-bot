"""VPN Service Bot template. Payments and 3x-ui provisioning are integration points."""
import asyncio
import logging
import os
import sqlite3
from html import escape
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DATABASE_PATH", "vpn_bot.sqlite3")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Keep temporary Telegram API failures visible without stopping the bot."""
    if isinstance(context.error, (TimedOut, NetworkError)):
        logging.warning("Telegram API is temporarily unavailable: %s", context.error)
        return
    logging.exception("Unhandled error while processing an update", exc_info=context.error)


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY, name TEXT NOT NULL, days INTEGER NOT NULL, price INTEGER NOT NULL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS payment_methods(id INTEGER PRIMARY KEY, name TEXT NOT NULL, instructions TEXT NOT NULL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, discount INTEGER NOT NULL CHECK(discount BETWEEN 1 AND 100), max_uses INTEGER NOT NULL, uses INTEGER DEFAULT 0, expires_at TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS user_promos(user_id INTEGER PRIMARY KEY, code TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, method_id INTEGER NOT NULL, amount INTEGER NOT NULL, promo_code TEXT, status TEXT DEFAULT 'new', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS subscriptions(user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL, connection TEXT, device_limit INTEGER NOT NULL DEFAULT 3);
        -- Kept independently from subscriptions, so removing access never resets a trial.
        CREATE TABLE IF NOT EXISTS trial_uses(user_id INTEGER PRIMARY KEY, activated_at TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_state(admin_id INTEGER PRIMARY KEY, action TEXT NOT NULL, payload TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Lightweight migration for databases created by earlier template versions.
        subscription_columns = {row[1] for row in con.execute("PRAGMA table_info(subscriptions)")}
        if "device_limit" not in subscription_columns:
            con.execute("ALTER TABLE subscriptions ADD COLUMN device_limit INTEGER NOT NULL DEFAULT 3")
        if not con.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
            con.executemany("INSERT INTO plans(name, days, price) VALUES(?,?,?)", [("1 месяц", 30, 299), ("3 месяца", 90, 799), ("1 год", 365, 2499)])
        con.commit()


def is_admin(user_id): return user_id in ADMIN_IDS
def rub(n): return f"{n:,}".replace(",", " ") + " ₽"
def main_kb(user_id=None):
    keys = [
        [InlineKeyboardButton("🛒 Купить VPN", callback_data="buy")],
        [InlineKeyboardButton("📋 Тарифы", callback_data="plans"), InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
        [InlineKeyboardButton("🎁 Пробный период — 3 дня", callback_data="trial")],
        [InlineKeyboardButton("🎁 Промокод", callback_data="promo")],
    ]
    if user_id is not None and is_admin(user_id):
        keys.append([InlineKeyboardButton("⚙️ Открыть админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(keys)
def admin_kb(): return InlineKeyboardMarkup([[InlineKeyboardButton("📦 Тарифы", callback_data="a_plans"), InlineKeyboardButton("💳 Способы оплаты", callback_data="a_methods")], [InlineKeyboardButton("🎁 Промокоды", callback_data="a_promos"), InlineKeyboardButton("🧾 Заказы", callback_data="a_orders")], [InlineKeyboardButton("📊 Статистика", callback_data="a_stats")], [InlineKeyboardButton("⬅️ Пользовательское меню", callback_data="home")]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Добро пожаловать в VPN Service. Выберите действие:", reply_markup=main_kb(update.effective_user.id))

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await update.effective_message.reply_text("⚙️ Админ-панель", reply_markup=admin_kb())

def profile_text(uid, username=None):
    with closing(db()) as con: row = con.execute("SELECT expires_at, connection, device_limit FROM subscriptions WHERE user_id=?", (uid,)).fetchone()
    nickname = f"@{escape(username)}" if username else "не указан"
    identity = f"Никнейм: {nickname}\nID: <code>{uid}</code>"
    if not row: return f"👤 Мой профиль\n\n{identity}\n\nПодписки пока нет."
    expires = datetime.fromisoformat(row['expires_at']).astimezone().strftime("%d.%m.%Y %H:%M")
    result = f"👤 Мой профиль\n\n{identity}\n\nПодписка действует до: {expires}\nЛимит устройств: {row['device_limit']}"
    return result + (f"\n\nВаш ключ:\n<code>{escape(row['connection'])}</code>" if row['connection'] else "\n\nКлюч выдаст администратор после подключения 3x-ui.")

async def show_plans(query, purchase=False):
    with closing(db()) as con: rows = con.execute("SELECT * FROM plans WHERE active=1 ORDER BY price").fetchall()
    if not rows: return await query.edit_message_text("Тарифов пока нет.", reply_markup=main_kb(query.from_user.id))
    keys = [[InlineKeyboardButton(f"{r['name']} — {rub(r['price'])}", callback_data=(f"pickplan:{r['id']}" if purchase else "plans"))] for r in rows]
    keys.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
    title = "Выберите тариф:" if purchase else "Доступные тарифы:"
    await query.edit_message_text(title, reply_markup=InlineKeyboardMarkup(keys))

async def create_order_payment(query, plan_id, method_id):
    """Integration point for automatic payment providers: create invoice here."""
    uid = query.from_user.id
    with closing(db()) as con:
        plan = con.execute("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,)).fetchone()
        method = con.execute("SELECT * FROM payment_methods WHERE id=? AND active=1", (method_id,)).fetchone()
        promo = con.execute("SELECT code,discount FROM user_promos JOIN promos USING(code) WHERE user_id=? AND active=1 AND uses<max_uses AND (expires_at IS NULL OR expires_at>=date('now'))", (uid,)).fetchone()
        if not plan or not method: return await query.edit_message_text("Этот вариант больше недоступен.", reply_markup=main_kb(uid))
        amount = plan['price'] * (100 - (promo['discount'] if promo else 0)) // 100
        cur = con.execute("INSERT INTO orders(user_id,plan_id,method_id,amount,promo_code,created_at) VALUES(?,?,?,?,?,?)", (uid, plan_id, method_id, amount, promo['code'] if promo else None, datetime.now(UTC).isoformat()))
        # A promo is reserved by the first created order and cannot discount later ones.
        if promo:
            con.execute("UPDATE promos SET uses=uses+1 WHERE code=?", (promo['code'],))
            con.execute("DELETE FROM user_promos WHERE user_id=?", (uid,))
        con.commit(); oid = cur.lastrowid
    text = f"Тариф: {plan['name']}\nК оплате: {rub(amount)}\n\n{method['instructions']}\n\nПосле оплаты нажмите кнопку — администратор проверит платёж."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid:{oid}")], [InlineKeyboardButton("⬅️ В меню", callback_data="home")]]))

def provision_vpn(user_id, days, order_id, device_limit=3):
    """Connect 3x-ui API here. Set 3x-ui client's limitIp to device_limit (3 by default)."""
    return None

def confirm_order(order_id):
    with closing(db()) as con:
        order = con.execute("SELECT o.*, p.days FROM orders o JOIN plans p ON p.id=o.plan_id WHERE o.id=? AND o.status IN ('new','waiting')", (order_id,)).fetchone()
        if not order: return None
        old = con.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (order['user_id'],)).fetchone()
        base = max(datetime.now(UTC), datetime.fromisoformat(old['expires_at'])) if old else datetime.now(UTC)
        device_limit = 3
        connection = provision_vpn(order['user_id'], order['days'], order_id, device_limit)
        con.execute("INSERT INTO subscriptions(user_id,expires_at,connection,device_limit) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, connection=COALESCE(excluded.connection, subscriptions.connection), device_limit=excluded.device_limit", (order['user_id'], (base + timedelta(days=order['days'])).isoformat(), connection, device_limit))
        con.execute("UPDATE orders SET status='paid' WHERE id=?", (order_id,))
        con.commit(); return order

def activate_trial(user_id):
    """Grant a three-day trial once for one Telegram account."""
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=3)
    with closing(db()) as con:
        try:
            # Serialize simultaneous button presses before testing the unique key.
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO trial_uses(user_id,activated_at,expires_at) VALUES(?,?,?)", (user_id, now.isoformat(), expires_at.isoformat()))
        except sqlite3.IntegrityError:
            con.rollback()
            return None
        # Do not consume the trial when a current subscriber presses the button.
        if con.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND expires_at>?", (user_id, now.isoformat())).fetchone():
            con.rollback()
            return False
        connection = provision_vpn(user_id, 3, "trial", 3)
        con.execute("INSERT INTO subscriptions(user_id,expires_at,connection,device_limit) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, connection=COALESCE(excluded.connection, subscriptions.connection), device_limit=excluded.device_limit", (user_id, expires_at.isoformat(), connection, 3))
        con.commit()
    return expires_at

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer(); data = q.data; uid = q.from_user.id
    if data == "home": return await q.edit_message_text("Главное меню:", reply_markup=main_kb(uid))
    if data == "plans": return await show_plans(q)
    if data == "buy": return await show_plans(q, True)
    if data == "trial":
        try:
            expires_at = activate_trial(uid)
        except Exception:
            logging.exception("Could not activate trial for user %s", uid)
            return await q.edit_message_text("Не удалось включить пробный период. Попробуйте позже или обратитесь к администратору.", reply_markup=main_kb(uid))
        if expires_at is None:
            return await q.edit_message_text("Пробный период уже был использован для этого аккаунта.", reply_markup=main_kb(uid))
        if expires_at is False:
            return await q.edit_message_text("У вас уже есть активная подписка. Пробный период сохранён и будет доступен после её окончания.", reply_markup=main_kb(uid))
        return await q.edit_message_text("🎁 Пробный период активирован на 3 дня — до " + expires_at.astimezone().strftime("%d.%m.%Y %H:%M") + ".\n\n" + profile_text(uid, q.from_user.username), parse_mode="HTML", reply_markup=main_kb(uid))
    if data == "profile":
        try:
            return await q.edit_message_text(profile_text(uid, q.from_user.username), parse_mode="HTML", reply_markup=main_kb(uid))
        except BadRequest as exc:
            # Telegram rejects an attempt to replace a message with identical text.
            if "not modified" in str(exc).lower():
                return await q.answer("Профиль уже открыт.", show_alert=True)
            raise
    if data == "promo":
        context.user_data['awaiting_promo'] = True
        return await q.edit_message_text("Отправьте промокод одним сообщением.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="home")]]))
    if data.startswith("pickplan:"):
        pid = int(data.split(":")[1])
        with closing(db()) as con: methods = con.execute("SELECT * FROM payment_methods WHERE active=1").fetchall()
        if not methods: return await q.edit_message_text("Способы оплаты ещё не добавлены.", reply_markup=main_kb(uid))
        return await q.edit_message_text("Выберите способ оплаты:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(m['name'], callback_data=f"method:{pid}:{m['id']}")] for m in methods] + [[InlineKeyboardButton("⬅️ Назад", callback_data="buy")]]))
    if data.startswith("method:"):
        _, p, m = data.split(":"); return await create_order_payment(q, int(p), int(m))
    if data.startswith("paid:"):
        oid = int(data.split(":")[1])
        with closing(db()) as con: con.execute("UPDATE orders SET status='waiting' WHERE id=? AND user_id=? AND status='new'", (oid,uid)); con.commit()
        for aid in ADMIN_IDS: await context.bot.send_message(aid, f"Пользователь @{q.from_user.username or uid} отметил заказ №{oid} как оплаченный.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{oid}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{oid}")]]))
        return await q.edit_message_text("Заявка отправлена на проверку. Мы уведомим вас.", reply_markup=main_kb(uid))
    if data == "admin" or data in ("addplan", "addmethod", "addpromo", "clearstats_confirm", "clearstats_cancel") or data.startswith("a_") or data.startswith(("confirm:", "reject:", "delplan:", "delmethod:", "delpromo:", "editplan:", "editpromo:")):
        if not is_admin(uid): return
        if data == "admin": return await q.edit_message_text("⚙️ Админ-панель", reply_markup=admin_kb())
        if data == "a_plans":
            with closing(db()) as con: rows=con.execute("SELECT * FROM plans").fetchall()
            keys=[[InlineKeyboardButton(f"✏️ {x['name']} ({rub(x['price'])})",callback_data=f"editplan:{x['id']}"), InlineKeyboardButton("❌",callback_data=f"delplan:{x['id']}")] for x in rows]+[[InlineKeyboardButton("➕ Добавить тариф",callback_data="addplan"),InlineKeyboardButton("⬅️ Назад",callback_data="admin")]]
            return await q.edit_message_text("Тарифы. ✏️ — изменить, ❌ — удалить.",reply_markup=InlineKeyboardMarkup(keys))
        if data == "a_methods":
            with closing(db()) as con: rows=con.execute("SELECT * FROM payment_methods").fetchall()
            keys=[[InlineKeyboardButton(f"❌ {x['name']}",callback_data=f"delmethod:{x['id']}")] for x in rows]+[[InlineKeyboardButton("➕ Добавить способ",callback_data="addmethod"),InlineKeyboardButton("⬅️ Назад",callback_data="admin")]]
            return await q.edit_message_text("Способы оплаты.",reply_markup=InlineKeyboardMarkup(keys))
        if data == "a_promos":
            with closing(db()) as con: rows=con.execute("SELECT * FROM promos").fetchall()
            keys=[[InlineKeyboardButton(f"✏️ {x['code']} — {x['discount']}% ({x['uses']}/{x['max_uses']})",callback_data=f"editpromo:{x['code']}"), InlineKeyboardButton("❌",callback_data=f"delpromo:{x['code']}")] for x in rows]+[[InlineKeyboardButton("➕ Добавить промокод",callback_data="addpromo"),InlineKeyboardButton("⬅️ Назад",callback_data="admin")]]
            return await q.edit_message_text("Промокоды. ✏️ — изменить, ❌ — удалить.",reply_markup=InlineKeyboardMarkup(keys))
        if data == "a_orders":
            with closing(db()) as con: rows=con.execute("SELECT o.*,p.name FROM orders o JOIN plans p ON p.id=o.plan_id WHERE status='waiting' ORDER BY id DESC").fetchall()
            keys=[[InlineKeyboardButton(f"№{x['id']} — {x['name']} — {rub(x['amount'])}",callback_data=f"confirm:{x['id']}")] for x in rows] or [[InlineKeyboardButton("Нет ожидающих заказов",callback_data="admin")]]
            keys.append([InlineKeyboardButton("⬅️ Назад",callback_data="admin")]); return await q.edit_message_text("Ожидают подтверждения:",reply_markup=InlineKeyboardMarkup(keys))
        if data == "a_stats":
            with closing(db()) as con:
                reset_at = con.execute("SELECT value FROM app_settings WHERE key='stats_reset_at'").fetchone()
                since = reset_at['value'] if reset_at else '1970-01-01T00:00:00+00:00'
                customers = con.execute("SELECT COUNT(DISTINCT user_id) FROM orders WHERE created_at >= ?", (since,)).fetchone()[0]
                total_orders = con.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (since,)).fetchone()[0]
                paid_orders = con.execute("SELECT COUNT(*) FROM orders WHERE status='paid' AND created_at >= ?", (since,)).fetchone()[0]
                waiting = con.execute("SELECT COUNT(*) FROM orders WHERE status='waiting' AND created_at >= ?", (since,)).fetchone()[0]
                revenue = con.execute("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status='paid' AND created_at >= ?", (since,)).fetchone()[0]
                active = con.execute("SELECT COUNT(*) FROM subscriptions WHERE expires_at > ?", (datetime.now(UTC).isoformat(),)).fetchone()[0]
                promo_uses = con.execute("SELECT COUNT(*) FROM orders WHERE status='paid' AND promo_code IS NOT NULL AND created_at >= ?", (since,)).fetchone()[0]
            text = ("📊 Статистика\n\n"
                    f"Клиентов: {customers}\n"
                    f"Всего заказов: {total_orders}\n"
                    f"Оплачено: {paid_orders}\n"
                    f"Ожидают проверки: {waiting}\n"
                    f"Активных подписок: {active}\n"
                    f"Использований промокодов: {promo_uses}\n\n"
                    f"Подтверждённая выручка: {rub(revenue)}")
            return await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧹 Очистить статистику", callback_data="a_clearstats")], [InlineKeyboardButton("🚫 Удалить подписку", callback_data="a_remove_sub")], [InlineKeyboardButton("⬅️ Назад", callback_data="admin")]]))
        if data == "a_clearstats":
            return await q.edit_message_text("Сбросить показатели статистики с текущего момента? История заказов и подписки не удаляются.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Сбросить", callback_data="clearstats_confirm"), InlineKeyboardButton("Отмена", callback_data="clearstats_cancel")]]))
        if data == "clearstats_cancel":
            return await q.edit_message_text("Сброс отменён.", reply_markup=admin_kb())
        if data == "clearstats_confirm":
            with closing(db()) as con:
                con.execute("INSERT INTO app_settings(key,value) VALUES('stats_reset_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now(UTC).isoformat(),))
                con.commit()
            return await q.edit_message_text("✅ Статистика сброшена. Новые показатели будут считаться с этого момента.", reply_markup=admin_kb())
        if data == "a_remove_sub":
            context.user_data['admin_action'] = 'remove_sub'
            return await q.edit_message_text("Отправьте числовой Telegram ID пользователя, чью подписку нужно удалить. Это действие нельзя отменить.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="admin")]]))
        if data.startswith("editplan:"):
            context.user_data['admin_action'] = data
            return await q.edit_message_text("Отправьте новые значения: название | дней | цена\nНапример: 1 месяц | 30 | 299")
        if data.startswith("editpromo:"):
            context.user_data['admin_action'] = data
            return await q.edit_message_text("Отправьте новые значения: скидка% | лимит | дата YYYY-MM-DD (или -)")
        if data in ("addplan","addmethod","addpromo"):
            action=data[3:]; context.user_data['admin_action']=action
            prompts={'plan':'Отправьте: название | дней | цена\nНапример: 1 месяц | 30 | 299','method':'Отправьте: название | инструкция для оплаты','promo':'Отправьте: КОД | скидка% | лимит | дата YYYY-MM-DD (или -)'}
            return await q.edit_message_text(prompts[action])
        if data.startswith("del"):
            kind, ident=data.split(":",1); table={'delplan':'plans','delmethod':'payment_methods','delpromo':'promos'}[kind]; col='code' if kind=='delpromo' else 'id'
            with closing(db()) as con: con.execute(f"DELETE FROM {table} WHERE {col}=?",(ident,));con.commit()
            return await q.edit_message_text("Удалено.",reply_markup=admin_kb())
        oid=int(data.split(":")[1])
        if data.startswith("reject:"):
            with closing(db()) as con: con.execute("UPDATE orders SET status='rejected' WHERE id=?",(oid,));con.commit()
            # Do not attach another admin panel to every order notification.
            return await q.edit_message_text(f"Заказ №{oid} отклонён.")
        try: order=confirm_order(oid)
        except Exception as exc: return await q.edit_message_text(f"Ошибка выдачи VPN: {exc}")
        if order:
            await context.bot.send_message(order['user_id'], "✅ Оплата подтверждена! " + profile_text(order['user_id']), parse_mode="HTML")
        # The primary /admin message remains the single place for navigation.
        return await q.edit_message_text(f"Заказ №{oid} подтверждён.")

async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text=update.effective_message.text.strip(); uid=update.effective_user.id
    # Promocodes are the only customer text input; it is enabled explicitly by
    # the corresponding button and consumed after a single message.
    if context.user_data.pop('awaiting_promo',False):
        code=text.upper()
        with closing(db()) as con:
            promo=con.execute("SELECT * FROM promos WHERE code=? AND active=1 AND uses<max_uses AND (expires_at IS NULL OR expires_at>=date('now'))",(code,)).fetchone()
            if promo: con.execute("INSERT INTO user_promos(user_id,code) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET code=excluded.code",(uid,code));con.commit()
        return await update.effective_message.reply_text("Промокод применён: скидка " + str(promo['discount']) + "% на следующий заказ." if promo else "Промокод недействителен или закончился.",reply_markup=main_kb(uid))
    action=context.user_data.pop('admin_action',None)
    # Client messages are deliberately ignored: customer interaction happens via
    # inline buttons only. Text input remains available only during an admin flow.
    if not action or not is_admin(uid): return
    if action == 'remove_sub':
        try:
            target_id = int(text)
            with closing(db()) as con:
                deleted = con.execute("DELETE FROM subscriptions WHERE user_id=?", (target_id,)).rowcount
                con.commit()
            result = f"✅ Подписка пользователя <code>{target_id}</code> удалена." if deleted else f"У пользователя <code>{target_id}</code> нет подписки."
            return await update.effective_message.reply_text(result, parse_mode="HTML", reply_markup=admin_kb())
        except ValueError:
            return await update.effective_message.reply_text("Нужен числовой Telegram ID. Повторите действие через /admin.")
    parts=[x.strip() for x in text.split('|')]
    try:
        with closing(db()) as con:
            if action.startswith('editplan:'): con.execute("UPDATE plans SET name=?,days=?,price=? WHERE id=?",(parts[0],int(parts[1]),int(parts[2]),int(action.split(':',1)[1])))
            elif action.startswith('editpromo:'): con.execute("UPDATE promos SET discount=?,max_uses=?,expires_at=? WHERE code=?",(int(parts[0].replace('%','')),int(parts[1]),None if parts[2]=='-' else parts[2],action.split(':',1)[1]))
            elif action=='plan': con.execute("INSERT INTO plans(name,days,price) VALUES(?,?,?)",(parts[0],int(parts[1]),int(parts[2])))
            elif action=='method': con.execute("INSERT INTO payment_methods(name,instructions) VALUES(?,?)",(parts[0],parts[1]))
            else: con.execute("INSERT INTO promos(code,discount,max_uses,expires_at) VALUES(?,?,?,?)",(parts[0].upper(),int(parts[1].replace('%','')),int(parts[2]),None if parts[3]=='-' else parts[3]))
            con.commit()
        await update.effective_message.reply_text("✅ Сохранено.",reply_markup=admin_kb())
    except (IndexError, ValueError, sqlite3.IntegrityError):
        await update.effective_message.reply_text("Не удалось сохранить. Проверьте формат и уникальность кода, затем повторите действие через /admin.")

def main():
    if not TOKEN or TOKEN == 'put_your_token_here': raise RuntimeError('Заполните BOT_TOKEN в .env')
    if not ADMIN_IDS: raise RuntimeError('Заполните ADMIN_IDS в .env')
    # Python 3.14 no longer creates the main asyncio loop implicitly.
    # python-telegram-bot 21.x still expects one when run_polling() starts.
    asyncio.set_event_loop(asyncio.new_event_loop())
    init_db()
    # The library defaults to a 5-second HTTP read timeout. That is too tight for
    # unstable routes to api.telegram.org, especially during long polling.
    builder = (
        Application.builder()
        .token(TOKEN)
        .connect_timeout(20)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(45)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(10)
    )
    # A proxy is often the biggest latency improvement where api.telegram.org
    # is throttled or blocked. It must serve both getUpdates and outgoing edits.
    if TELEGRAM_PROXY:
        builder.proxy(TELEGRAM_PROXY).get_updates_proxy(TELEGRAM_PROXY)
    app = builder.build()
    app.add_handler(CommandHandler('start',start)); app.add_handler(CommandHandler('admin',admin)); app.add_handler(CallbackQueryHandler(callback)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_input))
    app.add_error_handler(log_error)
    app.run_polling(timeout=30, allowed_updates=Update.ALL_TYPES)
if __name__=='__main__': main()
