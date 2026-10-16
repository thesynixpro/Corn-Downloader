"""Runtime bridge between pure UI cards and python-telegram-bot."""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode

from .ui import Button, Card

logger = logging.getLogger("uvd_bot.tgui")


def to_markup(card: Card) -> InlineKeyboardMarkup | None:
    if not card.buttons:
        return None
    keyboard = [
        [InlineKeyboardButton(text=button.label, callback_data=button.data) for button in row]
        for row in card.buttons
    ]
    return InlineKeyboardMarkup(keyboard)


async def reply_card(message: Message, card: Card) -> Message:
    return await message.reply_text(
        card.text,
        reply_markup=to_markup(card),
        parse_mode=ParseMode.HTML if card.parse_mode == "HTML" else None,
        disable_web_page_preview=True,
    )


async def edit_card(message: Message, card: Card) -> bool:
    try:
        await message.edit_text(
            card.text,
            reply_markup=to_markup(card),
            parse_mode=ParseMode.HTML if card.parse_mode == "HTML" else None,
            disable_web_page_preview=True,
        )
        return True
    except Exception as exc:
        logger.debug("Card edit suppressed: %s", type(exc).__name__)
        return False


async def answer_query(query, text: str | None = None, alert: bool = False) -> None:
    try:
        await query.answer(text=text, show_alert=alert)
    except Exception:
        pass
