import logging
from datetime import datetime
from typing import Dict, Any, Optional

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

BOT_TOKEN = "8354267156:AAH4u8FVXkWh0kr5AsRr6c2xPzT5OZmG7Xw"

# ============ ЛОГИРОВАНИЕ ============

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ============ ХРАНЕНИЕ СОСТОЯНИЯ ============

users_status: Dict[int, Dict[str, Any]] = {}

cats_feeding: Dict[str, Dict[str, Optional[datetime]]] = {
    "cassiy": {"name": "⚫ Кассий", "dry": None, "wet": None},
    "bulik": {"name": "🟠 Булик", "dry": None, "wet": None},
    "grom": {"name": "🟤 Гром", "dry": None, "wet": None},
    "klava": {"name": "🟡 Клава", "dry": None},
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
