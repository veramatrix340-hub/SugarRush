"""Bot de Telegram para Gem Rush.

Instalar:  pip install "python-telegram-bot>=21"
Ejecutar:
    export BOT_TOKEN="el_token_nuevo_de_BotFather"
    export GAME_URL="https://tu-sitio.com/gem-rush.html"   # debe ser https
    python bot.py
"""
import os
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
GAME_URL = os.environ["GAME_URL"]

db = sqlite3.connect("game.db")
db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, coins INTEGER DEFAULT 0)")
db.execute("CREATE TABLE IF NOT EXISTS referrals (invited_id INTEGER PRIMARY KEY, inviter_id INTEGER)")
db.commit()

REF_BONUS = 50


def play_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎮 Jugar", web_app=WebAppInfo(url=GAME_URL))]]
    )


def register_referral(inviter_id: int, invited_id: int) -> bool:
    """Guarda el referido una sola vez. Devuelve True si es nuevo."""
    if inviter_id == invited_id:
        return False
    db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (invited_id,))
    cur = db.execute(
        "INSERT OR IGNORE INTO referrals (invited_id, inviter_id) VALUES (?, ?)",
        (invited_id, inviter_id),
    )
    if cur.rowcount:
        db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (inviter_id,))
        db.execute("UPDATE users SET coins = coins + ? WHERE id = ?", (REF_BONUS, inviter_id))
    db.commit()
    return bool(cur.rowcount)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user.id,))
    db.commit()

    if context.args and context.args[0].startswith("ref_"):
        try:
            inviter = int(context.args[0][4:])
            if register_referral(inviter, user.id):
                await context.bot.send_message(
                    inviter, f"🎉 {user.first_name} se unió con tu enlace. ¡+{REF_BONUS} 🪙!"
                )
        except (ValueError, Exception):
            pass

    await update.message.reply_text(
        f"¡Hola {user.first_name}! 🍓 Combina frutas, gana monedas 🪙 y canjea premios.",
        reply_markup=play_keyboard(),
    )


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{update.effective_user.id}"
    await update.message.reply_text(f"Tu enlace de invitación:\n{link}\n\nGanas {REF_BONUS} 🪙 por cada amigo.")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = db.execute("SELECT coins FROM users WHERE id = ?", (update.effective_user.id,)).fetchone()
    await update.message.reply_text(f"Tienes {row[0] if row else 0} 🪙")


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("jugar", start))
    app.add_handler(CommandHandler("invitar", invite))
    app.add_handler(CommandHandler("monedas", balance))
    app.run_polling()


if __name__ == "__main__":
    main()
