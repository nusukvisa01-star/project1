import asyncio
import sys

# 1. EVENT LOOP FIX — Python 3.14 da WindowsSelectorEventLoopPolicy deprecated
# Pyrogram importidan OLDIN yangi loop o'rnatilishi shart
try:
    loop = asyncio.get_event_loop()
    if loop.is_closed():
        raise RuntimeError("Loop closed")
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import re
import logging
import random
import string
from datetime import datetime, timedelta
import aiosqlite

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, FloodWait
from pyrogram.handlers import MessageHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= SOZLAMALAR =================
BOT_TOKEN = "8336829201:AAHxq5EvQm7Cdf0Tx08An_OF0DoMLrsd4gA"
SUPER_ADMIN_ID = 5724592490
DB_NAME = "bot_database.db"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

BOT_ID = 0
active_userbots = {}
temp_clients = {}

# ================= HOLATLAR =================
class AdminState(StatesGroup):
    waiting_for_minutes = State()

class UserState(StatesGroup):
    waiting_for_promo = State()

class AuthState(StatesGroup):
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()

class PostState(StatesGroup):
    post_msg = State()
    type = State()
    interval = State()
    count_limit = State()
    char_limit = State()
    groups = State()

# ================= BAZA BILAN ISHLASH =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY, duration_mins INTEGER, is_used BOOLEAN DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, expire_date TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            user_id INTEGER PRIMARY KEY, api_id INTEGER, api_hash TEXT, session_string TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, post_msg_id INTEGER, type TEXT,
            time_interval INTEGER DEFAULT 0, last_run TIMESTAMP,
            count_limit INTEGER DEFAULT 0, char_limit INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active')""")
        await db.execute("""CREATE TABLE IF NOT EXISTS task_groups (
            task_id INTEGER, group_id TEXT, current_count INTEGER DEFAULT 0)""")
        await db.commit()

# ================= ID NORMALIZATSIYA (TO'LIQ TO'G'IRLANGAN) =================
def normalize_group_id(input_str: str):
    """
    Barcha Telegram guruh/kanal formatlarini qo'llab-quvvatlaydi:

    ✅ Qo'llab-quvvatlanadigan formatlar:
      - https://t.me/username          → @username
      - t.me/username                  → @username
      - https://t.me/+AbCdEfGh123      → invite link (to'g'ridan-to'g'ri)
      - https://t.me/joinchat/AbCdEf   → invite link (to'g'ridan-to'g'ri)
      - https://t.me/c/1234567890/5    → -1001234567890
      - @username                       → @username
      - -1001234567890                  → -1001234567890 (supergroup/channel)
      - -1234567                        → -1234567 (basic group, -100 YO'Q!)
      - 1234567890                      → int (raw ID)
      - 1001234567890                   → -1001234567890 (100-prefixli)
    """
    input_str = input_str.strip()

    # 1. Invite link: t.me/+HASH yoki t.me/joinchat/HASH
    # Bu formatlar Pyrogram tomonidan to'g'ridan-to'g'ri qabul qilinadi
    if re.search(r't\.me/(\+|joinchat/)', input_str):
        if not input_str.startswith("http"):
            return "https://" + input_str
        return input_str

    # 2. Private supergroup/channel havolasi: t.me/c/CHANNEL_ID/...
    match_private = re.search(r't\.me/c/(\d+)', input_str)
    if match_private:
        return int(f"-100{match_private.group(1)}")

    # 3. Oddiy t.me/username havolasi
    if "t.me/" in input_str:
        username = input_str.split("t.me/")[-1].split("/")[0].strip()
        if username:
            return f"@{username}"

    # 4. @username formatida
    if input_str.startswith("@"):
        return input_str

    # 5. -100xxxxxxxxxx → supergroup yoki kanal (to'liq format)
    if input_str.startswith("-100") and input_str[4:].isdigit():
        return int(input_str)

    # 6. -xxxxxxx → BASIC GROUP (minus bor, lekin -100 EMAS!)
    # MUHIM: Basic group ID manfiy butun son, -100 prefiksi YO'Q
    # Masalan: -1234567 → basic group
    if input_str.startswith("-") and not input_str.startswith("-100"):
        clean = input_str[1:]
        if clean.isdigit():
            return int(input_str)  # O'zgartirmasdan qaytaramiz!

    # 7. Faqat musbat raqamlar - o'zgartirmasdan qaytaramiz
    if input_str.isdigit():
        return int(input_str)

    # 8. Boshqa holatlar (faqat letters, noma'lum) - as-is
    return input_str


# ================= PEER RESOLVER (KESH MUAMMOSINI HAL QILADI) =================
async def resolve_and_cache(client: Client, chat_id):
    """
    Pyrogram in_memory=True da peer keshda bo'lmasa ValueError chiqaradi.
    Bu funksiya chatni oldindan resolve qilib keshga saqlaydi.
    """
    try:
        return await client.get_chat(int(chat_id))
    except ValueError:
        # Peer keshda yo'q — join_chat orqali resolve qilamiz
        try:
            return await client.join_chat(int(chat_id))
        except Exception:
            return None
    except Exception:
        return None


# ================= SAFE FORWARD =================
async def safe_forward(client, chat_id, p_id):
    """Aynan Bot bilan bo'lgan chatdan xabarni guruhga forward qiladi"""
    try:
        # Avval peer ni resolve qilib keshga olamiz
        await resolve_and_cache(client, chat_id)
        await client.forward_messages(
            chat_id=int(chat_id),
            from_chat_id=BOT_ID,
            message_ids=p_id
        )
        await asyncio.sleep(1.5)
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        await client.forward_messages(
            chat_id=int(chat_id),
            from_chat_id=BOT_ID,
            message_ids=p_id
        )
    except Exception as e:
        logging.error(f"Forward error in {chat_id}: {e}")


# ================= USERBOT HANDLER =================
async def userbot_message_handler(client: Client, message):
    if not message.text and not message.caption:
        return
    user_id = getattr(client, "owner_id", None)
    if not user_id:
        return

    # chat.id ni xavfsiz olamiz — ba'zan ValueError chiqishi mumkin
    try:
        chat_id = str(message.chat.id)
    except Exception:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """SELECT t.id, t.post_msg_id, t.count_limit, t.char_limit, tg.current_count
               FROM tasks t JOIN task_groups tg ON t.id = tg.task_id
               WHERE t.user_id = ? AND t.type = 'count' AND t.status = 'active' AND tg.group_id = ?""",
            (user_id, chat_id)
        ) as cursor:
            tasks = await cursor.fetchall()
            for task in tasks:
                t_id, post_id, limit, char_lim, current = task
                text_len = len(message.text or message.caption or "")
                if text_len >= char_lim:
                    new_c = current + 1
                    if new_c >= limit:
                        await safe_forward(client, chat_id, post_id)
                        await db.execute(
                            "UPDATE task_groups SET current_count = 0 WHERE task_id = ? AND group_id = ?",
                            (t_id, chat_id)
                        )
                    else:
                        await db.execute(
                            "UPDATE task_groups SET current_count = ? WHERE task_id = ? AND group_id = ?",
                            (new_c, t_id, chat_id)
                        )
            await db.commit()


async def time_based_job():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, user_id, post_msg_id, time_interval, last_run FROM tasks WHERE type='time' AND status='active'"
        ) as cursor:
            tasks = await cursor.fetchall()
            for task in tasks:
                t_id, u_id, p_id, interval, last_run_str = task
                run = False
                if not last_run_str:
                    run = True
                else:
                    last = datetime.strptime(last_run_str, "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - last).total_seconds() / 60 >= interval:
                        run = True

                if run and u_id in active_userbots:
                    client = active_userbots[u_id]
                    async with db.execute(
                        "SELECT group_id FROM task_groups WHERE task_id=?", (t_id,)
                    ) as g_cursor:
                        groups = await g_cursor.fetchall()
                        for g in groups:
                            await safe_forward(client, g[0], p_id)
                    await db.execute("UPDATE tasks SET last_run=? WHERE id=?", (now, t_id))
            await db.commit()


async def start_userbot(user_id, api_id, api_hash, session_string):
    client = Client(
        f"s_{user_id}",
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        in_memory=True
    )
    client.owner_id = user_id
    client.add_handler(MessageHandler(userbot_message_handler))
    try:
        await client.start()
        active_userbots[user_id] = client

        # Barcha saqlangan guruhlarni keshga yuklaymiz (Peer id invalid oldini olish uchun)
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT DISTINCT tg.group_id FROM task_groups tg "
                "JOIN tasks t ON t.id = tg.task_id WHERE t.user_id = ?",
                (user_id,)
            ) as cursor:
                groups = await cursor.fetchall()
                for (gid,) in groups:
                    try:
                        await client.get_chat(int(gid))
                        await asyncio.sleep(0.2)
                    except Exception as e:
                        logging.warning(f"Pre-cache failed for {gid}: {e}")

        return True
    except Exception as e:
        logging.error(f"Userbot start error: {e}")
        return False


async def load_all_userbots():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT user_id, api_id, api_hash, session_string FROM sessions"
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                await start_userbot(row[0], row[1], row[2], row[3])


# ================= AIOGRAM HANDLERS =================
async def check_subscription(user_id):
    if user_id == SUPER_ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT expire_date FROM users WHERE user_id=?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] and datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S") > datetime.now():
                return True
    return False


def get_main_menu(user_id):
    kb = [
        [
            InlineKeyboardButton(text="⚙️ Ulash", callback_data="acc_connect"),
            InlineKeyboardButton(text="🔌 Uzish", callback_data="acc_disconnect")
        ],
        [
            InlineKeyboardButton(text="📝 Yangi Post", callback_data="post_new"),
            InlineKeyboardButton(text="🗂 Postlarim", callback_data="post_my")
        ]
    ]
    if user_id == SUPER_ADMIN_ID:
        kb.insert(0, [InlineKeyboardButton(text="🎟 Promokod", callback_data="admin_promo")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.message(Command("start"), StateFilter("*"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    if await check_subscription(message.from_user.id):
        await message.answer("Xush kelibsiz!", reply_markup=get_main_menu(message.from_user.id))
    else:
        await message.answer("Botdan foydalanish uchun promokod yuboring:")
        await state.set_state(UserState.waiting_for_promo)


@dp.callback_query(F.data == "cancel_all", StateFilter("*"))
async def cancel_all(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Asosiy menyu:", reply_markup=get_main_menu(call.from_user.id))


# ================= POST YARATISH =================
@dp.callback_query(F.data == "post_new")
async def post_new(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id not in active_userbots:
        return await call.answer("Akkaunt ulanmagan!", show_alert=True)
    await call.message.edit_text(
        "Tarqatmoqchi bo'lgan post xabarini yuboring (Rasm, matn, video...):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_all")]
        ])
    )
    await state.set_state(PostState.post_msg)


@dp.message(PostState.post_msg)
async def post_msg_catch(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    client = active_userbots.get(user_id)

    msg_wait = await message.answer("Post tahlil qilinmoqda... ⏳")

    userbot_msg_id = None
    try:
        await asyncio.sleep(0.5)
        async for msg in client.get_chat_history(BOT_ID, limit=5):
            if msg.outgoing:
                userbot_msg_id = msg.id
                break
    except Exception as e:
        logging.error(f"Post ID search error: {e}")

    final_id = userbot_msg_id if userbot_msg_id else message.message_id
    await state.update_data(post_msg_id=final_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏳ Vaqt bo'yicha", callback_data="p_time"),
            InlineKeyboardButton(text="💬 Sanoq bo'yicha", callback_data="p_count")
        ]
    ])
    await msg_wait.edit_text("Post qabul qilindi! Endi tarqatish turini tanlang:", reply_markup=kb)
    await state.set_state(PostState.type)


@dp.callback_query(PostState.type)
async def post_type(call: types.CallbackQuery, state: FSMContext):
    pt = "time" if call.data == "p_time" else "count"
    await state.update_data(type=pt)
    if pt == "time":
        await call.message.edit_text("Interval (daqiqa):")
        await state.set_state(PostState.interval)
    else:
        await call.message.edit_text("Sanoq (xabarlar soni):")
        await state.set_state(PostState.count_limit)


@dp.message(PostState.interval)
async def p_interval(message: types.Message, state: FSMContext):
    await state.update_data(interval=int(message.text), count_limit=0, char_limit=0)
    await message.answer("Guruhlarni kiriting (Link, ID yoki Username, har birini yangi satrda yoki bo'sh joy bilan):")
    await state.set_state(PostState.groups)


@dp.message(PostState.count_limit)
async def p_count_lim(message: types.Message, state: FSMContext):
    await state.update_data(count_limit=int(message.text))
    await message.answer("Bitta xabarda minimal harf soni:")
    await state.set_state(PostState.char_limit)


@dp.message(PostState.char_limit)
async def p_char_lim(message: types.Message, state: FSMContext):
    await state.update_data(char_limit=int(message.text), interval=0)
    await message.answer("Guruhlarni kiriting (bo'sh joy, vergul yoki yangi satr bilan):")
    await state.set_state(PostState.groups)


@dp.message(PostState.groups)
async def p_groups(message: types.Message, state: FSMContext):
    client = active_userbots.get(message.from_user.id)
    raw = re.split(r'[\s,;\n]+', message.text.strip())
    valid = []
    failed = []

    m = await message.answer("Guruhlar tahlil qilinmoqda... ⏳")

    for r in raw:
        r = r.strip()
        if not r:
            continue

        target = normalize_group_id(r)
        chat = None

        # Bir necha usul bilan urinib koramiz
        attempts = [target]

        # Agar raqam bolsa, turli formatlarni sinaymiz
        if isinstance(target, int) and target > 0:
            attempts = [target, int(f"-100{target}"), int(f"-{target}")]
        elif isinstance(target, int) and str(abs(target)).startswith("100"):
            attempts = [target]
        elif isinstance(target, int):
            attempts = [target, int(f"-100{abs(target)}")]

        for attempt in attempts:
            try:
                chat = await client.get_chat(attempt)
                break
            except Exception:
                await asyncio.sleep(0.2)
                continue

        if chat:
            valid.append(str(chat.id))
            logging.info(f"✅ Topildi: '{chat.title}' | ID: {chat.id}")
        else:
            logging.warning(f"❌ Topilmadi: '{r}'")
            failed.append(r)

        await asyncio.sleep(0.3)

    if not valid:
        fail_preview = "\n".join(f"• {f}" for f in failed[:5])
        return await m.edit_text(
            f"❌ Hech qanday guruh topilmadi!\n\nTopilmaganlar:\n{fail_preview}",
            reply_markup=get_main_menu(message.from_user.id)
        )

    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        c = await db.execute(
            "INSERT INTO tasks (user_id, post_msg_id, type, time_interval, count_limit, char_limit) VALUES (?,?,?,?,?,?)",
            (
                message.from_user.id,
                data['post_msg_id'],
                data['type'],
                data['interval'],
                data['count_limit'],
                data['char_limit']
            )
        )
        tid = c.lastrowid
        for v in set(valid):
            await db.execute("INSERT INTO task_groups (task_id, group_id) VALUES (?,?)", (tid, v))
        await db.commit()

    result_text = f"✅ Tayyor! {len(valid)} ta guruhga tarqatish boshlandi."
    if failed:
        result_text += f"\n⚠️ {len(failed)} ta topilmadi: {', '.join(failed[:3])}"
        if len(failed) > 3:
            result_text += f" va yana {len(failed) - 3} ta"

    await m.edit_text(result_text, reply_markup=get_main_menu(message.from_user.id))
    await state.clear()


@dp.callback_query(F.data == "post_my")
async def post_my(call: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, type, status FROM tasks WHERE user_id=?", (call.from_user.id,)
        ) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        return await call.message.edit_text(
            "Sizda faol postlar yo'q.",
            reply_markup=get_main_menu(call.from_user.id)
        )
    kb = []
    for r in rows:
        kb.append([InlineKeyboardButton(
            text=f"ID: {r[0]} | {r[1]} | {r[2]}",
            callback_data=f"mng_{r[0]}"
        )])
    kb.append([InlineKeyboardButton(text="Ortga", callback_data="cancel_all")])
    await call.message.edit_text(
        "Sizning postlaringiz:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )


@dp.callback_query(F.data.startswith("mng_"))
async def mng_task(call: types.CallbackQuery):
    tid = call.data.split("_")[1]
    kb = [
        [InlineKeyboardButton(text="⏸/▶️ Holat", callback_data=f"tog_{tid}")],
        [InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"del_{tid}")],
        [InlineKeyboardButton(text="Ortga", callback_data="post_my")]
    ]
    await call.message.edit_text(
        f"Vazifa #{tid} ni boshqarish:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )


@dp.callback_query(F.data.startswith("tog_"))
async def tog_task(call: types.CallbackQuery):
    tid = call.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT status FROM tasks WHERE id=?", (tid,)) as cursor:
            res = await cursor.fetchone()
            if not res:
                return
            new = "paused" if res[0] == "active" else "active"
            await db.execute("UPDATE tasks SET status=? WHERE id=?", (new, tid))
            await db.commit()
    await call.answer(f"Holat: {new}")
    await post_my(call)


@dp.callback_query(F.data.startswith("del_"))
async def del_task(call: types.CallbackQuery):
    tid = call.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        await db.execute("DELETE FROM task_groups WHERE task_id=?", (tid,))
        await db.commit()
    await call.answer("O'chirildi")
    await post_my(call)


# ================= AUTH JARAYONI =================
@dp.callback_query(F.data == "acc_connect")
async def acc_conn_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id in active_userbots:
        return await call.answer("Akkaunt ulangan!", show_alert=True)
    await call.message.edit_text(
        "API ID kiriting:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor", callback_data="cancel_all")]
        ])
    )
    await state.set_state(AuthState.api_id)


@dp.message(AuthState.api_id)
async def auth_api_id(message: types.Message, state: FSMContext):
    await state.update_data(api_id=int(message.text))
    await message.answer("API HASH kiriting:")
    await state.set_state(AuthState.api_hash)


@dp.message(AuthState.api_hash)
async def auth_api_hash(message: types.Message, state: FSMContext):
    await state.update_data(api_hash=message.text)
    await message.answer("Telefon raqamingiz (+998...):")
    await state.set_state(AuthState.phone)


@dp.message(AuthState.phone)
async def auth_phone_num(message: types.Message, state: FSMContext):
    data = await state.get_data()
    phone = message.text.strip().replace(" ", "").replace("+", "")
    msg = await message.answer("Kutib turing, kod yuborilmoqda... ⏳")
    client = Client(f"t_{message.from_user.id}", data["api_id"], data["api_hash"], in_memory=True, device_model="Samsung Galaxy S25 Ultra", system_version="Android 15", app_version="11.2.0", lang_code="uz")
    await client.connect()
    try:
        sc = await client.send_code(phone)
        temp_clients[message.from_user.id] = {"c": client, "p": phone, "h": sc.phone_code_hash}
        await msg.edit_text("📱 Kod Telegram ilovangizga yuborildi.\n\nKodni orasida bo'sh joy qoldirib yozing (Masalan: 1 2 3 4 5):")
        await state.set_state(AuthState.code)
    except Exception as e:
        await msg.edit_text(f"❌ Xatolik: {e}")
        await state.clear()


@dp.message(AuthState.code)
async def auth_verify_code(message: types.Message, state: FSMContext):
    code = message.text.replace(" ", "")
    u = temp_clients.get(message.from_user.id)
    try:
        await u['c'].sign_in(u['p'], u['h'], code)
        ss = await u['c'].export_session_string()
        data = await state.get_data()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                (message.from_user.id, data['api_id'], data['api_hash'], ss)
            )
            await db.commit()
        await start_userbot(message.from_user.id, data['api_id'], data['api_hash'], ss)
        await message.answer("✅ Muvaffaqiyatli ulandi!", reply_markup=get_main_menu(message.from_user.id))
        await state.clear()
    except SessionPasswordNeeded:
        await message.answer("2FA parolni kiriting:")
        await state.set_state(AuthState.password)
    except Exception as e:
        await message.answer(f"Xato: {e}")


@dp.message(AuthState.password)
async def auth_2fa(message: types.Message, state: FSMContext):
    u = temp_clients.get(message.from_user.id)
    data = await state.get_data()
    try:
        await u['c'].check_password(message.text)
        ss = await u['c'].export_session_string()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?)",
                (message.from_user.id, data['api_id'], data['api_hash'], ss)
            )
            await db.commit()
        await start_userbot(message.from_user.id, data['api_id'], data['api_hash'], ss)
        await message.answer("✅ Muvaffaqiyatli ulandi!", reply_markup=get_main_menu(message.from_user.id))
        await state.clear()
    except Exception as e:
        await message.answer(f"Xato: {e}")


# ================= ADMIN VA PROMO =================
@dp.message(UserState.waiting_for_promo)
async def promo_check(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT duration_mins FROM promocodes WHERE code=? AND is_used=0",
            (message.text.strip(),)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                exp = (datetime.now() + timedelta(minutes=row[0])).strftime("%Y-%m-%d %H:%M:%S")
                await db.execute(
                    "INSERT OR REPLACE INTO users VALUES (?,?)",
                    (message.from_user.id, exp)
                )
                await db.execute(
                    "UPDATE promocodes SET is_used=1 WHERE code=?",
                    (message.text.strip(),)
                )
                await db.commit()
                await message.answer(
                    "✅ Promokod faollandi!",
                    reply_markup=get_main_menu(message.from_user.id)
                )
                await state.clear()
            else:
                await message.answer("❌ Noto'g'ri yoki ishlatilgan kod!")


@dp.callback_query(F.data == "admin_promo")
async def adm_p(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("Necha daqiqa:")
    await state.set_state(AdminState.waiting_for_minutes)


@dp.message(AdminState.waiting_for_minutes)
async def adm_p_res(message: types.Message, state: FSMContext):
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO promocodes VALUES (?,?,0)", (code, int(message.text)))
        await db.commit()
    await message.answer(f"Kod: `{code}`", reply_markup=get_main_menu(message.from_user.id))
    await state.clear()


@dp.callback_query(F.data == "acc_disconnect")
async def acc_disc(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid in active_userbots:
        await active_userbots[uid].stop()
        del active_userbots[uid]
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            await db.execute("UPDATE tasks SET status='paused' WHERE user_id=?", (uid,))
            await db.commit()
        await call.message.edit_text("Uzildi.", reply_markup=get_main_menu(uid))
    else:
        await call.answer("Ulanmagan")


# ================= START =================
async def main():
    global BOT_ID
    await init_db()
    me = await bot.get_me()
    BOT_ID = me.id
    await load_all_userbots()
    scheduler.add_job(time_based_job, "interval", minutes=1)
    scheduler.start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

