import logging
import os
from datetime import datetime, time as dtime
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo  # стандартная библиотека, без доп. зависимостей

from telegram import (
    Update,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============ НАСТРОЙКИ ============

BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ = ZoneInfo("Europe/Moscow")  # твой локальный часовой пояс (UTC+3)

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в переменных окружения.")

# ============ ЛОГИРОВАНИЕ ============

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# глушим болтливый httpx, чтобы логи не заспамливались
logging.getLogger("httpx").setLevel(logging.WARNING)

# ============ ХРАНЕНИЕ СОСТОЯНИЯ ============

users_status: Dict[int, Dict[str, Any]] = {}

cats_feeding: Dict[str, Dict[str, Optional[datetime]]] = {
    "cassiy": {"name": "⚫ Кассий", "dry": None, "wet": None},
    "bulik": {"name": "🟠 Булик", "dry": None, "wet": None},
    "grom": {"name": "🟤 Гром", "dry": None, "wet": None},
    "klava": {"name": "🟡 Клава", "dry": None},  # только сухой
}

# ============ КЛАВИАТУРЫ ============


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🏠 Я дома", "🚶 Я ушёл"],
            ["❓ Кто дома", "🐾 Статус котов"],
            ["🐱 Меню котов"],
        ],
        resize_keyboard=True,
    )


def cats_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["⚫ Кассий 🍖", "⚫ Кассий 💧"],
            ["🟠 Булик 🍖", "🟠 Булик 💧"],
            ["🟤 Гром 🍖", "🟤 Гром 💧"],
            ["🟡 Клава 🍖"],
            ["⬅️ Назад"],
        ],
        resize_keyboard=True,
    )


# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============


def format_dt(dt: Optional[datetime]) -> str:
    """Красиво формируем время. Если None — ставим тире."""
    return dt.strftime("%H:%M %d.%m") if dt else "—"


def get_home_status_text() -> str:
    if not users_status:
        return "Пока никто не отмечался."

    home = []
    away = []

    for info in users_status.values():
        name = info["name"]
        status = info["status"]
        time = format_dt(info["updated_at"])

        if status == "home":
            home.append(f"• {name} (с {time})")
        else:
            away.append(f"• {name} (с {time})")

    text = "🏠 *Дома:*\n" + ("\n".join(home) if home else "никого") + "\n\n"
    text += "🚶 *Вне дома:*\n" + ("\n".join(away) if away else "никого")
    return text


def get_cats_status_text() -> str:
    lines = ["🐾 *Кормление котов:*", ""]
    for key, data in cats_feeding.items():
        lines.append(data["name"] + ":")
        lines.append(f"  • сухой 🍖: {format_dt(data['dry'])}")
        if key != "klava":
            lines.append(f"  • влажный 💧: {format_dt(data['wet'])}")
        lines.append("")
    return "\n".join(lines).strip()


def reset_cats_feeding() -> None:
    """Сбрасываем только историю кормления (каждую полночь)."""
    for key, data in cats_feeding.items():
        data["dry"] = None
        if "wet" in data:
            data["wet"] = None
    logger.info("Сброшены отметки кормления котов (полночь).")


# ============ JOB ДЛЯ ПОЛНОЧИ ============


async def reset_cats_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    reset_cats_feeding()


# ============ HANDLERS ============


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.message is None:
        return

    user = update.effective_user
    users_status[user.id] = {
        "name": user.first_name or user.username or str(user.id),
        "status": "home",
        "updated_at": datetime.now(TZ),
    }

    await update.message.reply_text(
        "Привет! Бот запущен 🐾\n\n"
        "Используй меню ниже.",
        reply_markup=main_keyboard(),
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    # ——— статус жильца
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

    # ——— Меню котов
    if text == "🐱 Меню котов":
        await update.message.reply_text("Меню котов 🐱", reply_markup=cats_keyboard())
        return

    if text == "⬅️ Назад":
        await update.message.reply_text("Главное меню", reply_markup=main_keyboard())
        return

    if text == "🐾 Статус котов":
        await update.message.reply_markdown(
            get_cats_status_text(),
            reply_markup=main_keyboard(),
        )
        return

    # ——— Кормление котов
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
        cat, feed_type = mapping[text]
        cats_feeding[cat][feed_type] = now

        feed_text = "сухим (🍖)" if feed_type == "dry" else "влажным (💧)"

        await update.message.reply_text(
            f"{cats_feeding[cat]['name']} накормлен {feed_text} в {now.strftime('%H:%M %d.%m')}",
            reply_markup=cats_keyboard(),
        )
        return

    # ——— неизвестное
    await update.message.reply_text("Не понял 🤔", reply_markup=main_keyboard())


# ============ ЗАПУСК ============


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Хэндлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Джоб на полночь по Europe/Moscow — сбрасываем только кормление
    job_queue = app.job_queue
    job_queue.run_daily(
        reset_cats_job,
        time=dtime(hour=0, minute=0, second=0, tzinfo=TZ),
        name="reset_cats_daily",
    )

    # Чуть уменьшаем частоту опроса Telegram
    app.run_polling(
        poll_interval=2.0,  # пауза между запросами getUpdates
        timeout=10,         # long polling до 10 сек
    )


if __name__ == "__main__":
    main()    "grom": {"name": "🟤 Гром", "dry": None, "wet": None},
    "klava": {"name": "🟡 Клава", "dry": None},

# ============ КЛАВИАТУРЫ ============

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🏠 Я дома", "🚶 Я ушёл"],
            ["❓ Кто дома", "🐾 Статус котов"],
            ["🐱 Меню котов"],
        ],
        resize_keyboard=True,
    )

def cats_keyboard() -> ReplyKeyboardMarkup:
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

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def format_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%H:%M %d.%m") if dt else "—"

def get_home_status_text() -> str:
    if not users_status:
        return "Пока никто не отмечался."

    home = []
    away = []

    for info in users_status.values():
        name = info["name"]
        status = info["status"]
        time = format_dt(info["updated_at"])

        if status == "home":
            home.append(f"• {name} (с {time})")
        else:
            away.append(f"• {name} (с {time})")

    text = "🏠 *Дома:*\n" + ("\n".join(home) if home else "никого") + "\n\n"
    text += "🚶 *Вне дома:*\n" + ("\n".join(away) if away else "никого")
    return text

def get_cats_status_text() -> str:
    lines = ["🐾 *Кормление котов:*", ""]
    for key, data in cats_feeding.items():
        lines.append(data["name"] + ":")
        lines.append(f"  • сухой 🍖: {format_dt(data['dry'])}")
        if key != "klava":
            lines.append(f"  • влажный 💧: {format_dt(data['wet'])}")
        lines.append("")
    return "\n".join(lines).strip()

# ============ HANDLERS ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    users_status[user.id] = {
        "name": user.first_name or user.username,
        "status": "home",
        "updated_at": datetime.now(),
    }

    await update.message.reply_text(
        "Привет! Бот запущен 🐾\n\n"
        "Используй меню ниже.",
        reply_markup=main_keyboard(),
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user = update.effective_user

    if user.id not in users_status:
        users_status[user.id] = {
            "name": user.first_name or user.username,
            "status": "unknown",
            "updated_at": datetime.now(),
        }

    # ——— статус жильца
    if text == "🏠 Я дома":
        users_status[user.id]["status"] = "home"
        users_status[user.id]["updated_at"] = datetime.now()
        await update.message.reply_text("Отмечено 🏠", reply_markup=main_keyboard())
        return

    if text == "🚶 Я ушёл":
        users_status[user.id]["status"] = "away"
        users_status[user.id]["updated_at"] = datetime.now()
        await update.message.reply_text("Отмечено 🚶", reply_markup=main_keyboard())
        return

    if text == "❓ Кто дома":
        await update.message.reply_markdown(
            get_home_status_text(),
            reply_markup=main_keyboard(),
        )
        return

    # ——— Меню котов
    if text == "🐱 Меню котов":
        await update.message.reply_text("Меню котов 🐱", reply_markup=cats_keyboard())
        return

    if text == "⬅️ Назад":
        await update.message.reply_text("Главное меню", reply_markup=main_keyboard())
        return

    if text == "🐾 Статус котов":
        await update.message.reply_markdown(
            get_cats_status_text(),
            reply_markup=main_keyboard(),
        )
        return

    # ——— Кормление котов
    now = datetime.now()

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
        cat, feed_type = mapping[text]
        cats_feeding[cat][feed_type] = now

        feed_text = "сухим (🍖)" if feed_type == "dry" else "влажным (💧)"

        await update.message.reply_text(
            f"{cats_feeding[cat]['name']} накормлен {feed_text} в {now.strftime('%H:%M %d.%m')}",
            reply_markup=cats_keyboard(),
        )
        return

    # ——— неизвестное
    await update.message.reply_text("Не понял 🤔", reply_markup=main_keyboard())

# ============ ЗАПУСК ============

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.run_polling()

if __name__ == "__main__":
    main()



