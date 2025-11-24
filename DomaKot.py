import logging
import os
from datetime import datetime, time as dtime
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo

import asyncpg
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========= НАСТРОЙКИ =========

BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ = ZoneInfo("Europe/Moscow")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в переменных окружения.")

# ========= ЛОГИ =========

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ========= ПАМЯТЬ =========

# users_status[user_id] = {name, status, updated_at, gender}
users_status: Dict[int, Dict[str, Any]] = {}

# Состояние "за сегодня"
cats_feeding: Dict[str, Dict[str, Any]] = {
    "cassiy": {"label": "⚫ Кассий", "dry_time": None, "dry_by": None, "wet_time": None, "wet_by": None},
    "bulik": {"label": "🟠 Булик", "dry_time": None, "dry_by": None, "wet_time": None, "wet_by": None},
    "grom":  {"label": "🟤 Гром",  "dry_time": None, "dry_by": None, "wet_time": None, "wet_by": None},
    "klava": {"label": "🟡 Клава", "dry_time": None, "dry_by": None},  # только сухой
}

db_pool: Optional[asyncpg.Pool] = None

# ========= КЛАВИАТУРЫ =========


def main_keyboard(gender: Optional[str] = None) -> ReplyKeyboardMarkup:
    away_caption = "🚶 Я ушёл" if gender != "f" else "🚶 Я ушла"
    return ReplyKeyboardMarkup(
        [
            ["🏠 Я дома", away_caption],
            ["❓ Кто дома", "🐾 История кормлений"],
            ["🐱 Меню котов", "🏆 Рейтинг"],
        ],
        resize_keyboard=True,
    )


def cats_keyboard() -> ReplyKeyboardMarkup:
    # сначала 💧 (влажный), потом 🍖 (сухой)
    return ReplyKeyboardMarkup(
        [
            ["⚫ Кассий 💧", "⚫ Кассий 🍖"],
            ["🟠 Булик 💧", "🟠 Булик 🍖"],
            ["🟤 Гром 💧", "🟤 Гром 🍖"],
            ["🟡 Клава 🍖"],
            ["⬅️ Назад"],
        ],
        resize_keyboard=True,
    )


# ========= ВСПОМОГАТЕЛЬНЫЕ =========


def get_user_gender(user_id: int) -> Optional[str]:
    return users_status.get(user_id, {}).get("gender")


def format_dt(dt: Optional[datetime]) -> str:
    return dt.astimezone(TZ).strftime("%H:%M %d.%m") if dt else "—"


def get_home_status_text() -> str:
    if not users_status:
        return "Пока никто не отмечался."

    home, away = [], []
    for info in users_status.values():
        name = info["name"]
        status = info["status"]
        t = format_dt(info["updated_at"])
        if status == "home":
            home.append(f"• {name} (с {t})")
        else:
            away.append(f"• {name} (с {t})")

    text = "🏠 *Дома:*\n" + ("\n".join(home) if home else "никого") + "\n\n"
    text += "🚶 *Вне дома:*\n" + ("\n".join(away) if away else "никого")
    return text


def get_cats_status_text() -> str:
    lines = ["🐾 *Кормление котов (за сегодня):*", ""]
    for key, data in cats_feeding.items():
        lines.append(data["label"] + ":")

        # влажный
        if key != "klava":
            if data["wet_time"]:
                line = f"  • 💧 {format_dt(data['wet_time'])}"
                if data["wet_by"]:
                    line += f" ({data['wet_by']})"
            else:
                line = "  • 💧 —"
            lines.append(line)

        # сухой
        if data["dry_time"]:
            line = f"  • 🍖 {format_dt(data['dry_time'])}"
            if data["dry_by"]:
                line += f" ({data['dry_by']})"
        else:
            line = "  • 🍖 —"
        lines.append(line)
        lines.append("")

    return "\n".join(lines).strip()


# ========= БАЗА ДАННЫХ =========


async def setup_db() -> None:
    if db_pool is None:
        return
    async with db_pool.acquire() as conn:
        # История кормлений
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedings (
                id SERIAL PRIMARY KEY,
                cat_code TEXT NOT NULL,
                feed_type TEXT NOT NULL CHECK (feed_type IN ('dry', 'wet')),
                fed_at TIMESTAMPTZ NOT NULL,
                fed_by_id BIGINT NOT NULL,
                fed_by_name TEXT NOT NULL
            );
            """
        )
        # Пользователи
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                display_name TEXT,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            );
            """
        )
        # Пол пользователя
        await conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS gender TEXT
            CHECK (gender IN ('m','f'));
            """
        )


async def ensure_user_record(
    user_id: int, username: Optional[str], display_name: str
) -> Optional[str]:
    """Создаём/обновляем запись пользователя и возвращаем его gender (если есть)."""
    if db_pool is None:
        return None
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username, display_name)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
              SET username = EXCLUDED.username,
                  display_name = COALESCE(EXCLUDED.display_name, users.display_name);
            """,
            user_id,
            username,
            display_name,
        )

        # если админов ещё нет — делаем этого пользователя админом
        admins_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_admin = TRUE;")
        if admins_count == 0:
            await conn.execute("UPDATE users SET is_admin = TRUE WHERE user_id = $1;", user_id)
            logger.info("Пользователь %s назначен первым админом", user_id)

        row = await conn.fetchrow("SELECT gender FROM users WHERE user_id = $1;", user_id)
        return row["gender"] if row else None


async def is_admin(user_id: int) -> bool:
    if db_pool is None:
        return False
    async with db_pool.acquire() as conn:
        val = await conn.fetchval(
            "SELECT is_admin FROM users WHERE user_id = $1 AND is_active = TRUE;",
            user_id,
        )
        return bool(val)


async def load_last_feedings_today() -> None:
    """Подтягиваем последнее кормление за сегодня для статуса."""
    if db_pool is None:
        return
    async with db_pool.acquire() as conn:
        for cat_code, state in cats_feeding.items():
            # сухой
            row = await conn.fetchrow(
                """
                SELECT fed_at, fed_by_name
                  FROM feedings
                 WHERE cat_code = $1
                   AND feed_type = 'dry'
                   AND (fed_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date
              ORDER BY fed_at DESC
                 LIMIT 1;
                """,
                cat_code,
                "Europe/Moscow",
            )
            if row:
                state["dry_time"] = row["fed_at"]
                state["dry_by"] = row["fed_by_name"]

            if cat_code == "klava":
                continue

            row = await conn.fetchrow(
                """
                SELECT fed_at, fed_by_name
                  FROM feedings
                 WHERE cat_code = $1
                   AND feed_type = 'wet'
                   AND (fed_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date
              ORDER BY fed_at DESC
                 LIMIT 1;
                """,
                cat_code,
                "Europe/Moscow",
            )
            if row:
                state["wet_time"] = row["fed_at"]
                state["wet_by"] = row["fed_by_name"]


async def reset_feedings_today() -> None:
    """Полночь: очищаем только 'состояние за сегодня', историю в БД не трогаем."""
    for state in cats_feeding.values():
        state["dry_time"] = None
        state["dry_by"] = None
        if "wet_time" in state:
            state["wet_time"] = None
            state["wet_by"] = None
    logger.info("Сброшено состояние кормлений за сегодня.")


async def reset_feedings_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await reset_feedings_today()


async def post_init(app: Application) -> None:
    """Старт приложения: подключаем БД, создаём таблицы, подтягиваем сегодняшние кормления."""
    global db_pool
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.warning("DATABASE_URL не задан, БД использоваться не будет.")
        return

    db_pool = await asyncpg.create_pool(dsn=db_url)
    await setup_db()
    await load_last_feedings_today()
    logger.info("БД инициализирована.")


# ========= HANDLERS: БАЗОВЫЕ =========


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return

    user = update.effective_user
    name = user.first_name or user.username or str(user.id)

    gender = await ensure_user_record(user.id, user.username, name)

    users_status[user.id] = {
        "name": name,
        "status": "home",
        "updated_at": datetime.now(TZ),
        "gender": gender,
    }

    await update.message.reply_text(
        "Привет! Бот запущен 🐾\n\n"
        "Можно указать пол командой /setgender, тогда кнопка будет «ушёл» или «ушла» 🙂",
        reply_markup=main_keyboard(gender),
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    text = update.message.text
    user = update.effective_user
    name = user.first_name or user.username or str(user.id)

    if user.id not in users_status:
        gender = await ensure_user_record(user.id, user.username, name)
        users_status[user.id] = {
            "name": name,
            "status": "unknown",
            "updated_at": datetime.now(TZ),
            "gender": gender,
        }

    gender = get_user_gender(user.id)

    # ---- жильцы ----
    if text == "🏠 Я дома":
        users_status[user.id]["status"] = "home"
        users_status[user.id]["updated_at"] = datetime.now(TZ)
        await update.message.reply_text(
            "Отмечено: ты дома 🏠",
            reply_markup=main_keyboard(gender),
        )
        return

    if text in ("🚶 Я ушёл", "🚶 Я ушла"):
        users_status[user.id]["status"] = "away"
        users_status[user.id]["updated_at"] = datetime.now(TZ)
        word = "ушёл" if gender != "f" else "ушла"
        await update.message.reply_text(
            f"Отмечено: ты {word} 🚶",
            reply_markup=main_keyboard(gender),
        )
        return

    if text == "❓ Кто дома":
        await update.message.reply_markdown(
            get_home_status_text(),
            reply_markup=main_keyboard(gender),
        )
        return

    # ---- меню котов / история / рейтинг ----
    if text == "🐱 Меню котов":
        await update.message.reply_text("Меню котов 🐱", reply_markup=cats_keyboard())
        return

    if text == "⬅️ Назад":
        await update.message.reply_text("Главное меню", reply_markup=main_keyboard(gender))
        return

    if text == "🐾 История кормлений":
        await send_history_today(update, context)
        return

    if text == "🏆 Рейтинг":
        await send_rating(update, context)
        return

    # ---- кормление котов ----
    now = datetime.now(TZ)

    mapping = {
        "⚫ Кассий 🍖": ("cassiy", "dry"),
        "⚫ Кассий 💧": ("cassiy", "wet"),
        "🟠 Булик 🍖": ("bulik", "dry"),
        "🟠 Булик 💧": ("bulik", "wet"),
        "🟤 Гром 🍖": ("grom", "dry"),
        "🟤 Гром 💧": ("grom", "wet"),
        "🟡 Клава 🍖": ("klava", "dry"),
    }

    if text in mapping:
        cat_code, feed_type = mapping[text]
        state = cats_feeding[cat_code]

        display_name = users_status[user.id]["name"]

        if feed_type == "dry":
            state["dry_time"] = now
            state["dry_by"] = display_name
        else:
            state["wet_time"] = now
            state["wet_by"] = display_name

        if db_pool is not None:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO feedings (cat_code, feed_type, fed_at, fed_by_id, fed_by_name)
                    VALUES ($1, $2, $3, $4, $5);
                    """,
                    cat_code,
                    feed_type,
                    now,
                    user.id,
                    display_name,
                )

        await update.message.reply_text(
            f"{state['label']} накормлен "
            f"{'🍖' if feed_type == 'dry' else '💧'} "
            f"в {now.strftime('%H:%M %d.%m')} ({display_name})",
            reply_markup=cats_keyboard(),
        )
        return

    await update.message.reply_text("Не понял 🤔", reply_markup=main_keyboard(gender))


# ========= HANDLERS: ИСТОРИЯ И РЕЙТИНГ =========


async def send_history_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    gender = get_user_gender(update.effective_user.id) if update.effective_user else None

    if db_pool is None:
        await update.message.reply_text("База данных не настроена 😿", reply_markup=main_keyboard(gender))
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT fed_at, cat_code, feed_type, fed_by_name
              FROM feedings
             WHERE (fed_at AT TIME ZONE $1)::date = (NOW() AT TIME ZONE $1)::date
          ORDER BY fed_at DESC
             LIMIT 20;
            """,
            "Europe/Moscow",
        )

    if not rows:
        await update.message.reply_text(
            "Сегодня ещё никого не кормили 🐾",
            reply_markup=main_keyboard(gender),
        )
        return

    cat_names = {k: v["label"] for k, v in cats_feeding.items()}
    lines = ["📜 *История кормлений за сегодня:*", ""]
    for r in rows:
        cat_label = cat_names.get(r["cat_code"], r["cat_code"])
        emoji = "🍖" if r["feed_type"] == "dry" else "💧"
        lines.append(
            f"{r['fed_at'].astimezone(TZ).strftime('%H:%M')} — {cat_label} {emoji} ({r['fed_by_name']})"
        )

    await update.message.reply_markdown(
        "\n".join(lines),
        reply_markup=main_keyboard(gender),
    )


async def send_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    gender = get_user_gender(update.effective_user.id) if update.effective_user else None

    if db_pool is None:
        await update.message.reply_text("База данных не настроена 😿", reply_markup=main_keyboard(gender))
        return

    chat_user = update.effective_user
    uid = chat_user.id if chat_user else None

    async with db_pool.acquire() as conn:
        top_rows = await conn.fetch(
            """
            SELECT fed_by_id,
                   fed_by_name,
                   COUNT(*) AS cnt
              FROM feedings
          GROUP BY fed_by_id, fed_by_name
          ORDER BY cnt DESC
             LIMIT 10;
            """
        )

        user_row = None
        total_people = 0
        if uid is not None:
            rows = await conn.fetch(
                """
                SELECT fed_by_id,
                       fed_by_name,
                       COUNT(*) AS cnt,
                       RANK() OVER (ORDER BY COUNT(*) DESC) AS rnk
                  FROM feedings
              GROUP BY fed_by_id, fed_by_name
                """
            )
            total_people = len(rows)
            for r in rows:
                if r["fed_by_id"] == uid:
                    user_row = r
                    break

    if not top_rows:
        await update.message.reply_text(
            "Пока никто ещё не кормил котов 🐾",
            reply_markup=main_keyboard(gender),
        )
        return

    lines = ["🏆 *Рейтинг кормильцев:*", ""]
    for i, r in enumerate(top_rows, start=1):
        lines.append(f"{i}. {r['fed_by_name']} — {r['cnt']} раз")

    if user_row:
        lines.append("")
        lines.append(
            f"Твоё место: {user_row['rnk']} из {total_people}, "
            f"{user_row['cnt']} кормлений"
        )
    else:
        lines.append("")
        lines.append("Ты ещё ни разу не кормил(а) котов 😼")

    await update.message.reply_markdown(
        "\n".join(lines),
        reply_markup=main_keyboard(gender),
    )


# ========= HANDLERS: АДМИНКА =========


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может смотреть список пользователей.")
        return

    if db_pool is None:
        await update.message.reply_text("База данных не настроена.")
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id, display_name, username, is_admin, is_active, gender
              FROM users
          ORDER BY is_admin DESC, is_active DESC, display_name;
            """
        )

    if not rows:
        await update.message.reply_text("Пользователей пока нет.")
        return

    lines = ["👥 *Пользователи:*", ""]
    for r in rows:
        flags = []
        if r["is_admin"]:
            flags.append("admin")
        if not r["is_active"]:
            flags.append("inactive")
        if r["gender"] == "m":
            flags.append("m")
        elif r["gender"] == "f":
            flags.append("f")
        flag_str = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"• {r['display_name']} — `{r['user_id']}`{flag_str}")

    await update.message.reply_markdown("\n".join(lines))


async def setadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может назначать админов.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /setadmin <user_id>")
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    if db_pool is None:
        await update.message.reply_text("База данных не настроена.")
        return

    async with db_pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE users SET is_admin = TRUE WHERE user_id = $1 AND is_active = TRUE;",
            uid,
        )

    if res.endswith("0"):
        await update.message.reply_text("Пользователь не найден или не активен.")
    else:
        await update.message.reply_text(f"Пользователь {uid} назначен админом.")


async def deluser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может удалять пользователей.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /deluser <user_id>")
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    if db_pool is None:
        await update.message.reply_text("База данных не настроена.")
        return

    async with db_pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE users SET is_active = FALSE WHERE user_id = $1;",
            uid,
        )

    users_status.pop(uid, None)

    if res.endswith("0"):
        await update.message.reply_text("Пользователь не найден.")
    else:
        await update.message.reply_text(f"Пользователь {uid} деактивирован.")


async def setname_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None:
        return
    if not await is_admin(update.effective_user.id):
        await update.message.reply_text("Только админ может менять имена.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Использование: /setname <user_id> <Новое имя>")
        return

    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом.")
        return

    new_name = " ".join(context.args[1:])

    if db_pool is None:
        await update.message.reply_text("База данных не настроена.")
        return

    async with db_pool.acquire() as conn:
        res = await conn.execute(
            "UPDATE users SET display_name = $2 WHERE user_id = $1;",
            uid,
            new_name,
        )
        # Обновляем имя во всей истории кормлений
        await conn.execute(
            "UPDATE feedings SET fed_by_name = $2 WHERE fed_by_id = $1;",
            uid,
            new_name,
        )

    if res.endswith("0"):
        await update.message.reply_text("Пользователь не найден.")
        return

    if uid in users_status:
        users_status[uid]["name"] = new_name

    await update.message.reply_text(f"Имя пользователя {uid} изменено на: {new_name}")


# ========= HANDLER: УСТАНОВКА ПОЛА =========


def parse_gender_arg(arg: str) -> Optional[str]:
    a = arg.lower()
    if a in ("m", "м", "муж", "мужчина", "парень", "male", "man"):
        return "m"
    if a in ("f", "ж", "жен", "женщина", "девушка", "female", "woman", "girl"):
        return "f"
    return None


async def setgender_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return

    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Использование: /setgender <пол>\n"
            "Например: /setgender м  или  /setgender ж"
        )
        return

    gender = parse_gender_arg(context.args[0])
    if gender is None:
        await update.message.reply_text(
            "Не понял пол. Варианты: м / ж / m / f / мужчина / женщина."
        )
        return

    if db_pool is not None:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET gender = $2 WHERE user_id = $1;",
                user.id,
                gender,
            )

    if user.id in users_status:
        users_status[user.id]["gender"] = gender
    else:
        # на всякий случай
        users_status[user.id] = {
            "name": user.first_name or user.username or str(user.id),
            "status": "unknown",
            "updated_at": datetime.now(TZ),
            "gender": gender,
        }

    word = "мужской" if gender == "m" else "женский"
    await update.message.reply_text(
        f"Пол установлен: {word}. Кнопка теперь будет «я ушёл/ушла» с нужным окончанием 🙂",
        reply_markup=main_keyboard(gender),
    )


# ========= ЗАПУСК =========


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("setadmin", setadmin_cmd))
    app.add_handler(CommandHandler("deluser", deluser_cmd))
    app.add_handler(CommandHandler("setname", setname_cmd))
    app.add_handler(CommandHandler("setgender", setgender_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    job_queue = app.job_queue
    job_queue.run_daily(
        reset_feedings_job,
        time=dtime(hour=0, minute=0, second=0, tzinfo=TZ),
        name="reset_feedings_today",
    )

    app.run_polling(poll_interval=2.0, timeout=10)


if __name__ == "__main__":
    main()
