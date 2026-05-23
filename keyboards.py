from __future__ import annotations

from collections import Counter

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


MENU_BUTTONS = [
    "🎒 Сумка",
    "🖼 Картинка",
    "⚙ Настройки",
    "заново",
]


def _grid(buttons: list[str], cols: int = 2) -> list[list[KeyboardButton]]:
    rows: list[list[KeyboardButton]] = []
    current: list[KeyboardButton] = []
    for text in buttons:
        current.append(KeyboardButton(text=text))
        if len(current) == cols:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return rows


def _menu_rows() -> list[list[KeyboardButton]]:
    return _grid(MENU_BUTTONS, cols=2)


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=_menu_rows(),
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Пиши действие или просто продолжай историю",
    )


def gender_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👨 Мужской"), KeyboardButton(text="👩 Женский")],
            [KeyboardButton(text="🌀 Нейтральный")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def auto_manual_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🎲 Придумай сам"), KeyboardButton(text="✍️ Я сам")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="да"), KeyboardButton(text="заново")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def game_over_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="заново")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def inventory_keyboard(inventory: list[str]) -> ReplyKeyboardMarkup:
    counts = Counter(inventory)
    rows = _menu_rows()
    for item in counts:
        rows.append([KeyboardButton(text=f"🍞 Есть {item}")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)
