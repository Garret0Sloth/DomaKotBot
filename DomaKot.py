import logging
import os
from datetime import datetime, time as dtime
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo

import asyncpg
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

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

# ========= СОСТОЯНИЕ В ПАМЯТИ =========

# текущий статус жильцов (приход/уход)
users_status: Dict[int, Dict[str, Any]] = {}

# последнее кормление котов за сегодня
cats_feeding: Dict[str, Dict[str, Any]] = {
    "cassiy": {"label": "⚫ Кассий", "dry_time": None, "dry_by": None, "wet_time": None, "wet_by": None},
    "bulik": {"label": "🟠 Булик", "dry_time": None, "dry_by": None, "wet_time": None, "wet_by": None},
    "grom":   {"label": "🟤 Гром",   "dry_time": None, "dry_by": None, "wet_time": None, "wet_by": None},
    "klava":  {"label": "🟡 Клава",  "dry_time": None, "dry_by": None},  # только сухой
}

# пул соединений с БД
db_pool: Optional[asyncpg.Pool] = None

# ========= КЛАВИАТУРЫ =========

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🏠 Я дома", "🚶 Я ушёл"],
            ["❓ Кто дома", "🐾 История кормлений"],
            ["🐱 Меню котов"],
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

def format_dt(dt: Optional[datetime]) -> str:
    return dt.astimezone(TZ).strftime("%H:%M %d.%m") if dt else "—"

def get_home_status_text() -> str:
    if not users_status:
        return "Пока никто не отмечался."

    home, away = [], []

    for info in users_status.values():
        name = info["name"]
        status = info["status"]
        time_str = format_dt(info["updated_at"])

        if status == "home":
            home.append(f"• {name} (с {time_str})")
        else:
            away.append(f"• {name} (с {time_str})")

    text = "🏠 *Дома:*\n" + ("\n".join(home) if home else "никого") + "\n\n"
    text += "🚶 *Вне дома:*\n" + ("\n".join(away) if away else "никого")
    return text

def get_cats_status_text() -> str:
    """История кормлений за сегодня (по последнему кормлению каждого типа)."""
    lines = ["🐾 *Кормление котов (за сегодня):*", ""]
    for key, data in cats_feeding.items():
        lines.append(data["label"] + ":")

        # сперва влажный
        if key != "klava":
            wet_line = "  • 💧: " + (format_dt(data["wet_time"]) if data["wet_time"] else "—")
            if data.get("wet_by"):
                wet_line += f" ({data['wet_by']})"
            lines.append(wet_line)

        # потом сухой
        dry_line = "  • 🍖: " + (format_dt(data["dry_time"]) if data["dry_time"] else "—")
        if data.get("dry_by"):
            dry_line += f" ({data['dry_by']})"
        lines.append(dry_line)

        lines.append("")

    return "\n".join(lines).strip()

# ========= РАБОТА С БД =========

async def setup_db() -> None:
    """Создаём таблицу, если её ещё нет."""
    if db_pool is None:
        logger.warning("DB pool не инициализирован, пропускаю setup_db.")
        return
    async with db_pool.acquire() as conn:
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

async def load_last_feedings() -> None:
    """Подтягиваем последнее кормление за сегодня из БД (на случай рестарта бота)."""
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
                   AND fed_at::date = (NOW() AT TIME ZONE $2)::date
              ORDER BY fed_at DESC
                 LIMIT 1;
                """,
                cat_code, "Europe/Moscow",
            )
            if row:
                state["dry_time"] = row["fed_at"]
                state["dry_by"] = row["fed_by_name"]

            # влажный (кроме Клавы)
            if cat_code == "klava":
                continue
            row = await conn.fetchrow(
                """
                SELECT fed_at, fed_by_name
                  FROM feedings
                 WHERE cat_code = $1
                   AND feed_type = 'wet'
                   AND fed_at::date = (NOW() AT TIME ZONE $2)::date
              ORDER BY fed_at DESC
                 LIMIT 1;
                """,
                cat_code, "Europe/Moscow",
            )
            if row:
                state["wet_time"] = row["fed_at"]
                state["wet_by"] = row["fed_by_name"]

async def reset_feedings_midnight() -> None:
    """Полночь: очищаем сегодняшние кормления."""
    for state in cats_feeding.values():
        state["dry_time"] = None
        state["dry_by"] = None
        if "wet_time" in state:
            state["wet_time"] = None
            state["wet_by"] = None

    if db_pool is not None:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM feedings;")

async def reset_feedings_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await reset_feedings_midnight()

async def post_init(app: Application) -> None:
    """Вызывается один раз при старте приложения."""
    global db_pool
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.warning("DATABASE_URL не задан, БД использоваться не будет.")
        return

    db_pool = await asyncpg.create_pool(dsn=db_url)
    await setup_db()
    await load_last_feedings()
    logger.info("БД инициализирована.")

# ========= HANDLERS =========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user is None or update.message is None:
        return

    user = update.effective_user
    users_status[user.id] = {
        "name": user.first_name or user.username or str(user.id),
        "status": "home",
        "updated_at": datetime.now(TZ),
    }

    await update.message.reply_text(
        "Привет! Бот запущен 🐾\n\nИспользуй меню ниже.",
        reply_markup=main_keyboard(),
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    text = update.message.text
    user = update.effective_user

    if user.id not in users_status:
        users_status[user.id] = {
            "name": user.first_name or user.username or str(user.id),
            "status": "unknown",
            "updated_at": datetime.now(TZ),
        }

    # --- жильцы ---
    if text == "🏠 Я дома":
        users_status[user.id]["status"] = "home"
        users_status[user.id]["updated_at"] = datetime.now(TZ)
        await update.message.reply_text("Отмечено 🏠", reply_markup=main_keyboard())
        return

    if text == "🚶 Я ушёл":
        users_status[user.id]["status"] = "away"
        users_status[user.id]["updated_at"] = datetime.now(TZ)
        await update.message.reply_text("Отмечено 🚶", reply_markup=main_keyboard())
        return

    if text == "❓ Кто дома":
        await update.message.reply_markdown(
            get_home_status_text(),
            reply_markup=main_keyboard(),
        )
        return

    # --- меню котов ---
    if text == "🐱 Меню котов":
        await update.message.reply_text("Меню котов 🐱", reply_markup=cats_keyboard())
        return

    if text == "⬅️ Назад":
        await update.message.reply_text("Главное меню", reply_markup=main_keyboard())
        return

    if text == "🐾 История кормлений":
        await update.message.reply_markdown(
            get_cats_status_text(),
            reply_markup=main_keyboard(),
        )
        return

    # --- кормление котов ---
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
        user_name = users_status[user.id]["name"]

        if feed_type == "dry":
            state["dry_time"] = now
            state["dry_by"] = user_name
        else:
            state["wet_time"] = now
            state["wet_by"] = user_name

        # пишем в БД
        if db_pool is not None:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO feedings (cat_code, feed_type, fed_at, fed_by_id, fed_by_name) "
                    "VALUES ($1, $2, $3, $4, $5);",
                    cat_code,
                    feed_type,
                    now,
                    user.id,
                    user_name,
                )

        await update.message.reply_text(
            f"{state['label']} накормлен "
            f"{'🍖' if feed_type == 'dry' else '💧'} "
            f"в {now.strftime('%H:%M %d.%m')} ({user_name})",
            reply_markup=cats_keyboard(),
        )
        return

    # --- неизвестный текст ---
    await update.message.reply_text("Не понял 🤔", reply_markup=main_keyboard())

# ========= ЗАПУСК =========

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # джоба на полночь по Москве — чистим кормления
    job_queue = app.job_queue
    job_queue.run_daily(
        reset_feedings_job,
        time=dtime(hour=0, minute=0, second=0, tzinfo=TZ),
        name="reset_feedings",
    )

    app.run_polling(poll_interval=2.0, timeout=10)

if __name__ == "__main__":
    main()
