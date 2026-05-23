from __future__ import annotations

import asyncio
from html import escape
import logging
import re
import random
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message, ReplyKeyboardRemove
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from config import settings
from database import GameDatabase
from keyboards import (
    auto_manual_keyboard,
    confirm_keyboard,
    game_over_keyboard,
    gender_keyboard,
    inventory_keyboard,
    main_keyboard,
)
from llm import LLMError, LLMService, build_system_prompt


logging.basicConfig(level=logging.INFO)
router = Router()


def normalize(text: str) -> str:
    text = text.casefold().strip()
    text = re.sub(r"^[\W_]+", "", text, flags=re.UNICODE)
    return " ".join(text.split())


def is_restart(text: str) -> bool:
    norm = normalize(text)
    return norm in {"заново", "сброс", "restart", "reset", "начать заново"}


def is_image_request(text: str) -> bool:
    norm = normalize(text)
    return norm in {"картинка", "покажи картинку", "опиши подробнее", "покажи подробнее", "изобрази"} or norm.startswith(
        "опиши подробнее"
    )


def is_inventory(text: str) -> bool:
    norm = normalize(text)
    return norm in {
        "инвентарь",
        "рюкзак",
        "сумка",
        "открой инвентарь",
        "покажи инвентарь",
        "открой рюкзак",
        "покажи рюкзак",
        "открой сумку",
        "покажи сумку",
    }


def is_settings(text: str) -> bool:
    norm = normalize(text)
    return norm in {"настройки", "открой настройки", "покажи настройки", "⚙ настройки"}


def is_yes(text: str) -> bool:
    norm = normalize(text)
    return norm in {"да", "yes", "ок", "окей", "поехали", "подтверждаю"}


def is_auto_choice(text: str) -> bool:
    norm = normalize(text)
    return norm in {"придумай сам", "бот сам", "сгенерируй", "авто", "автоматически", "🎲 придумай сам"}


def is_manual_choice(text: str) -> bool:
    norm = normalize(text)
    return norm in {"я сам", "вручную", "сам", "✍ я сам", "✍️ я сам"}


def normalize_gender(text: str) -> str:
    norm = normalize(text)
    if norm in {"мужской", "мужчина", "парень", "👨 мужской"}:
        return "мужской"
    if norm in {"женский", "женщина", "девушка", "👩 женский"}:
        return "женский"
    if norm in {"нейтральный", "другое", "другой", "🌀 нейтральный"}:
        return "нейтральный"
    return ""


def is_rename_request(text: str) -> bool:
    norm = normalize(text)
    return norm in {
        "сменить имя",
        "переименуй",
        "переименовать",
        "новое имя",
        "новое прозвище",
        "прозвище",
        "имя спутника",
        "✏ имя",
    }


def get_creation_stage(user: dict[str, object]) -> str:
    if not user.get("player_name"):
        return "player_name"
    if not user.get("companion_gender"):
        return "companion_gender"
    if not user.get("companion_name"):
        if not user.get("companion_name_mode"):
            return "companion_name_mode"
        return "companion_name"
    if not user.get("companion_personality"):
        if not user.get("companion_personality_mode"):
            return "companion_personality_mode"
        return "companion_personality"
    if not user.get("companion_appearance"):
        if not user.get("companion_appearance_mode"):
            return "companion_appearance_mode"
        return "companion_appearance"
    if not user.get("setting"):
        if not user.get("setting_mode"):
            return "setting_mode"
        return "setting"
    return "confirm"


def render_inventory(inventory: list[str]) -> str:
    if not inventory:
        return "🎒 Бесконечная сумка пуста."
    lines = ["🎒 Бесконечная сумка:"]
    counts: dict[str, int] = {}
    for item in inventory:
        counts[item] = counts.get(item, 0) + 1
    for name, count in counts.items():
        lines.append(f"- {name} x{count}")
    lines.append("Чтобы съесть предмет, напиши: 🍞 Есть [предмет]")
    lines.append("Если предмет появился в истории, он сам окажется в сумке.")
    return "\n".join(lines)


def render_settings() -> str:
    return (
        "⚙ Настройки:\n"
        "- Текстовый режим: включён\n"
        "- Нижнее меню: включено\n"
        "- Автокартинки: включены при новых локациях и предметах\n"
        "- Переименование спутника: напиши 'сменить имя'\n"
        "Если хочешь, я могу позже добавить переключатели."
    )


def render_creation_summary(user: dict[str, object]) -> str:
    gender = user.get("companion_gender") or "не указан"
    appearance = user.get("companion_appearance") or "не описана"
    return (
        "Отлично! Мир создан.\n"
        f"{user['player_name']} и {user['companion_name']} — {user['setting']}\n"
        f"Пол: {gender}\n"
        f"Характер: {user['companion_personality']}\n"
        f"Внешность: {appearance}\n"
        "Всё верно? Напиши 'да' чтобы начать, или 'заново' чтобы пересоздать."
    )


def short_location(text: str, limit: int = 180) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def build_scene_image_prompt(user: dict[str, object], scene_text: str) -> str:
    setting = user.get("setting") or "неизвестный мир"
    scene = scene_text or user.get("last_scene_description") or user.get("current_location") or setting
    return f"Иллюстрация: {setting}. Сейчас: {scene}. Атмосферная сцена, без текста и интерфейса."


def build_item_image_prompt(user: dict[str, object], item_name: str) -> str:
    setting = user.get("setting") or "неизвестный мир"
    return f"Иллюстрация предмета: {item_name}. Мир: {setting}. Крупный план, атмосферно, без текста и интерфейса."


def scene_caption(reply_scene: str, location_name: str = "") -> str:
    if reply_scene:
        return reply_scene
    if location_name:
        return f"Новая локация: {location_name}"
    return "Новая сцена."


def item_caption(reply_item_description: str, item_name: str) -> str:
    if reply_item_description:
        return reply_item_description
    return f"Новый предмет: {item_name}"


def render_story_plain(scene: str, dialogue: str, companion_name: str) -> str:
    return f"{scene}\n\n{companion_name}: {dialogue}"


def render_story_html(scene: str, dialogue: str, companion_name: str) -> str:
    return (
        f"<blockquote><i>{escape(scene)}</i></blockquote>\n\n"
        f"<b>{escape(companion_name)}:</b> {escape(dialogue)}"
    )


MALE_NAME_FALLBACKS = ["Артём", "Илья", "Макс", "Никита", "Данил"]
FEMALE_NAME_FALLBACKS = ["Алиса", "Ирина", "Кира", "Мира", "Лада"]
NEUTRAL_NAME_FALLBACKS = ["Саша", "Женя", "Тёма", "Дана", "Лёва"]


def fallback_companion_name(gender: str) -> str:
    if gender == "женский":
        return random.choice(FEMALE_NAME_FALLBACKS)
    if gender == "нейтральный":
        return random.choice(NEUTRAL_NAME_FALLBACKS)
    return random.choice(MALE_NAME_FALLBACKS)


def fallback_companion_personality(gender: str) -> str:
    if gender == "женский":
        return "Спокойная и наблюдательная, говорит мягко, но умеет быть очень прямой. Она быстро замечает настроение собеседника и не любит пустых разговоров."
    if gender == "нейтральный":
        return "Спокойный, внимательный и немного загадочный. Говорит коротко, но всегда по делу, и легко подстраивается под настроение ситуации."
    return "Спокойный и внимательный, с лёгкой иронией в голосе. Он любит замечать детали и не торопится с выводами."


def fallback_companion_appearance(gender: str) -> str:
    if gender == "женский":
        return "Невысокая, с живым взглядом, тёплой одеждой и парой заметных деталей, которые сразу запоминаются."
    if gender == "нейтральный":
        return "Опрятный, с мягкими чертами лица, удобной одеждой и спокойной, запоминающейся манерой держаться."
    return "Среднего роста, с внимательным взглядом и практичной одеждой, в которой чувствуется привычка к дороге."


def fallback_setting() -> str:
    return "Вы оказались в живом мире, где дорога, случайные встречи и мелкие детали быстро превращаются в историю."


async def auto_generate_creation_value(llm: LLMService, user: dict[str, object], kind: str) -> str:
    gender = user.get("companion_gender") or "нейтральный"
    player_name = user.get("player_name") or "путник"
    companion_name = user.get("companion_name") or fallback_companion_name(str(gender))
    personality = user.get("companion_personality") or ""
    appearance = user.get("companion_appearance") or ""

    prompts = {
        "companion_name": (
            f"Придумай одно короткое русское имя для спутника. Пол: {gender}. "
            "Верни только имя без пояснений и кавычек."
        ),
        "companion_personality": (
            f"Опиши в 2-3 предложениях характер, манеру речи и короткую историю спутника. "
            f"Пол: {gender}. Имя: {companion_name}. Верни только готовый текст."
        ),
        "companion_appearance": (
            f"Опиши в 1-2 предложениях внешность, одежду и заметную деталь спутника. "
            f"Пол: {gender}. Имя: {companion_name}. Характер: {personality or 'не задан'}. "
            "Верни только готовый текст."
        ),
        "setting": (
            f"Придумай атмосферное описание мира и текущей ситуации для текстовой RPG. "
            f"Игрок: {player_name}. Спутник: {companion_name}. Пол: {gender}. "
            f"Характер: {personality or 'не задан'}. Внешность: {appearance or 'не задана'}. "
            "Верни 2-3 предложения только с описанием мира и сцены."
        ),
    }

    max_tokens = 64 if kind == "companion_name" else 160
    try:
        return await llm.generate_text(prompts[kind], max_output_tokens=max_tokens, temperature=0.9)
    except Exception:
        logging.exception("Auto generation failed for %s", kind)
        if kind == "companion_name":
            return fallback_companion_name(str(gender))
        if kind == "companion_personality":
            return fallback_companion_personality(str(gender))
        if kind == "companion_appearance":
            return fallback_companion_appearance(str(gender))
        return fallback_setting()


def build_webhook_url() -> str:
    if not settings.webhook_base_url:
        raise RuntimeError("WEBHOOK_BASE_URL or SPACE_HOST is missing")
    return f"{settings.webhook_base_url.rstrip('/')}{settings.webhook_path}"


async def healthcheck(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def send_generated_image(
    message: Message,
    llm: LLMService,
    prompt: str,
    caption: str,
    *,
    reply_markup=None,
    report_errors: bool = True,
) -> bool:
    try:
        image_bytes = await llm.generate_image_bytes(prompt)
    except Exception:
        if report_errors:
            logging.exception("Image generation failed")
            await message.answer("Спутник задумался, повтори", reply_markup=main_keyboard())
        else:
            logging.exception("Automatic image generation failed")
        return False

    await message.answer_photo(
        BufferedInputFile(image_bytes, filename="scene.png"),
        caption=caption[:1000],
        reply_markup=reply_markup or main_keyboard(),
    )
    return True


async def send_scene_art(
    message: Message,
    llm: LLMService,
    user: dict[str, object],
    reply_scene: str,
    location_name: str,
    scene_prompt: str,
    *,
    report_errors: bool = True,
) -> bool:
    prompt = scene_prompt or build_scene_image_prompt(user, location_name or reply_scene)
    return await send_generated_image(
        message,
        llm,
        prompt,
        scene_caption(reply_scene, location_name),
        report_errors=report_errors,
    )


async def send_item_art(
    message: Message,
    llm: LLMService,
    user: dict[str, object],
    item_name: str,
    item_description: str,
    item_prompt: str,
    *,
    report_errors: bool = True,
) -> bool:
    prompt = item_prompt or build_item_image_prompt(user, item_name)
    return await send_generated_image(
        message,
        llm,
        prompt,
        item_caption(item_description, item_name),
        report_errors=report_errors,
    )


async def append_inventory_item(db: GameDatabase, user_id: int, item_name: str) -> None:
    inventory = await db.get_inventory(user_id)
    inventory.append(item_name)
    await db.set_inventory(user_id, inventory)


async def ask_next_creation_question(message: Message, user: dict[str, object]) -> None:
    stage = get_creation_stage(user)

    if stage == "player_name":
        await message.answer(
            "Привет! Давай создадим твоего спутника и мир, в котором вы окажетесь.\n"
            "Для начала — как зовут ТЕБЯ?",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if stage == "companion_gender":
        await message.answer(
            "Какой пол у твоего спутника?",
            reply_markup=gender_keyboard(),
        )
        return
    if stage == "companion_name_mode":
        await message.answer(
            "Имя спутника ты хочешь придумать сам или чтобы я сделал это за тебя?",
            reply_markup=auto_manual_keyboard(),
        )
        return
    if stage == "companion_name":
        await message.answer(
            "Напиши имя или прозвище спутника.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if stage == "companion_personality_mode":
        await message.answer(
            "Характер и манеру поведения спутника пишешь сам или придумать мне?",
            reply_markup=auto_manual_keyboard(),
        )
        return
    if stage == "companion_personality":
        await message.answer(
            "Опиши по шаблону или как хочется: характер, манера речи, история, отношение к тебе.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if stage == "companion_appearance_mode":
        await message.answer(
            "Внешность спутника пишешь сам или придумать мне?",
            reply_markup=auto_manual_keyboard(),
        )
        return
    if stage == "companion_appearance":
        await message.answer(
            "Опиши по шаблону или как хочется: рост, одежда, приметы, общий образ.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if stage == "setting_mode":
        await message.answer(
            "Мир и сцену пишешь сам или придумать мне?",
            reply_markup=auto_manual_keyboard(),
        )
        return
    if stage == "setting":
        await message.answer(
            "Опиши мир и текущую ситуацию. Можно по шаблону: где вы, что происходит, что вокруг.\n"
            "Примеры:\n"
            "- 'Мы едем в поезде, я возвращаюсь домой, а спутник — случайный попутчик'\n"
            "- 'Мы охотники на драконов, сейчас в горах выслеживаем стаю'\n"
            "- 'Мы коллеги в офисе, сейчас обеденный перерыв'\n"
            "- 'Мы в постапокалипсисе, идём через пустошь в поисках воды'",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await message.answer(render_creation_summary(user), reply_markup=confirm_keyboard())


async def handle_creation_step(message: Message, db: GameDatabase, llm: LLMService, user: dict[str, object]) -> None:
    text = (message.text or "").strip()
    stage = get_creation_stage(user)

    if stage == "player_name":
        if not text:
            await ask_next_creation_question(message, user)
            return
        await db.update_user(message.from_user.id, player_name=text, state="creating")

    elif stage == "companion_gender":
        gender = normalize_gender(text)
        if not gender:
            await message.answer("Выбери пол кнопкой.", reply_markup=gender_keyboard())
            return
        await db.update_user(message.from_user.id, companion_gender=gender, state="creating")

    elif stage == "companion_name_mode":
        if is_manual_choice(text):
            await db.update_user(message.from_user.id, companion_name_mode="manual", state="creating")
            user = await db.get_user(message.from_user.id)
            await ask_next_creation_question(message, user)
            return
        if is_auto_choice(text):
            await db.update_user(message.from_user.id, companion_name_mode="auto", state="creating")
            user = await db.get_user(message.from_user.id)
            companion_name = await auto_generate_creation_value(llm, user, "companion_name")
            await db.update_user(message.from_user.id, companion_name=companion_name)
        else:
            await db.update_user(
                message.from_user.id,
                companion_name_mode="manual",
                companion_name=text,
                state="creating",
            )

    elif stage == "companion_name":
        if is_auto_choice(text):
            await db.update_user(message.from_user.id, companion_name_mode="auto", state="creating")
            user = await db.get_user(message.from_user.id)
            companion_name = await auto_generate_creation_value(llm, user, "companion_name")
            await db.update_user(message.from_user.id, companion_name=companion_name)
        else:
            await db.update_user(message.from_user.id, companion_name=text, state="creating")

    elif stage == "companion_personality_mode":
        if is_manual_choice(text):
            await db.update_user(message.from_user.id, companion_personality_mode="manual", state="creating")
            user = await db.get_user(message.from_user.id)
            await ask_next_creation_question(message, user)
            return
        if is_auto_choice(text):
            await db.update_user(message.from_user.id, companion_personality_mode="auto", state="creating")
            user = await db.get_user(message.from_user.id)
            companion_personality = await auto_generate_creation_value(llm, user, "companion_personality")
            await db.update_user(message.from_user.id, companion_personality=companion_personality)
        else:
            await db.update_user(
                message.from_user.id,
                companion_personality_mode="manual",
                companion_personality=text,
                state="creating",
            )

    elif stage == "companion_personality":
        if is_auto_choice(text):
            await db.update_user(message.from_user.id, companion_personality_mode="auto", state="creating")
            user = await db.get_user(message.from_user.id)
            companion_personality = await auto_generate_creation_value(llm, user, "companion_personality")
            await db.update_user(message.from_user.id, companion_personality=companion_personality)
        else:
            await db.update_user(message.from_user.id, companion_personality=text, state="creating")

    elif stage == "companion_appearance_mode":
        if is_manual_choice(text):
            await db.update_user(message.from_user.id, companion_appearance_mode="manual", state="creating")
            user = await db.get_user(message.from_user.id)
            await ask_next_creation_question(message, user)
            return
        if is_auto_choice(text):
            await db.update_user(message.from_user.id, companion_appearance_mode="auto", state="creating")
            user = await db.get_user(message.from_user.id)
            companion_appearance = await auto_generate_creation_value(llm, user, "companion_appearance")
            await db.update_user(message.from_user.id, companion_appearance=companion_appearance)
        else:
            await db.update_user(
                message.from_user.id,
                companion_appearance_mode="manual",
                companion_appearance=text,
                state="creating",
            )

    elif stage == "companion_appearance":
        if is_auto_choice(text):
            await db.update_user(message.from_user.id, companion_appearance_mode="auto", state="creating")
            user = await db.get_user(message.from_user.id)
            companion_appearance = await auto_generate_creation_value(llm, user, "companion_appearance")
            await db.update_user(message.from_user.id, companion_appearance=companion_appearance)
        else:
            await db.update_user(message.from_user.id, companion_appearance=text, state="creating")

    elif stage == "setting_mode":
        if is_manual_choice(text):
            await db.update_user(message.from_user.id, setting_mode="manual", state="creating")
            user = await db.get_user(message.from_user.id)
            await ask_next_creation_question(message, user)
            return
        if is_auto_choice(text):
            await db.update_user(message.from_user.id, setting_mode="auto", state="creating")
            user = await db.get_user(message.from_user.id)
            setting = await auto_generate_creation_value(llm, user, "setting")
            await db.update_user(message.from_user.id, setting=setting)
        else:
            await db.update_user(message.from_user.id, setting_mode="manual", setting=text, state="creating")

    elif stage == "setting":
        if is_auto_choice(text):
            await db.update_user(message.from_user.id, setting_mode="auto", state="creating")
            user = await db.get_user(message.from_user.id)
            setting = await auto_generate_creation_value(llm, user, "setting")
            await db.update_user(message.from_user.id, setting=setting)
        else:
            await db.update_user(message.from_user.id, setting=text, state="creating")

    user = await db.get_user(message.from_user.id)
    await ask_next_creation_question(message, user)


async def start_game(message: Message, db: GameDatabase, llm: LLMService, user: dict[str, object]) -> None:
    inventory = await db.get_inventory(message.from_user.id)
    system_prompt = build_system_prompt(user, inventory)
    await db.update_user(
        message.from_user.id,
        state="playing",
        system_prompt=system_prompt,
        current_location=user["setting"],
        last_scene_description="",
        bot_turns=0,
        game_over=0,
    )

    try:
        fresh_user = await db.get_user(message.from_user.id)
        inventory = await db.get_inventory(message.from_user.id)
        opening = await llm.generate_opening(fresh_user, [], inventory)
        rendered_plain = render_story_plain(opening.scene, opening.dialogue, fresh_user["companion_name"])
        rendered_html = render_story_html(opening.scene, opening.dialogue, fresh_user["companion_name"])
        await db.add_message(message.from_user.id, "assistant", rendered_plain)
        await db.update_user(
            message.from_user.id,
            last_scene_description=opening.scene,
            current_location=short_location(opening.new_location or fresh_user["setting"]),
        )
        await message.answer(rendered_html, reply_markup=main_keyboard(), parse_mode="HTML")

        scene_anchor = opening.new_location or opening.scene or fresh_user["setting"]
        if scene_anchor:
            await send_scene_art(
                message,
                llm,
                fresh_user,
                opening.new_location_description or opening.scene,
                scene_anchor,
                opening.scene_image_prompt,
                report_errors=False,
            )

        if opening.new_item:
            await append_inventory_item(db, message.from_user.id, opening.new_item)
            await send_item_art(
                message,
                llm,
                fresh_user,
                opening.new_item,
                opening.new_item_description,
                opening.item_image_prompt,
                report_errors=False,
            )

        await db.advance_story_turn(message.from_user.id)
    except Exception:
        logging.exception("Opening scene failed")
        await message.answer("Спутник задумался, повтори", reply_markup=main_keyboard())


async def handle_story(message: Message, db: GameDatabase, llm: LLMService, user: dict[str, object]) -> None:
    if int(user.get("game_over", 0)) or int(user.get("hunger", 0)) <= 0:
        await message.answer("Ты теряешь сознание от голода...", reply_markup=game_over_keyboard())
        return

    user_text = message.text or ""
    await db.add_message(message.from_user.id, "user", user_text)
    history = await db.get_recent_messages(message.from_user.id, settings.max_history_messages)

    try:
        fresh_user = await db.get_user(message.from_user.id)
        inventory = await db.get_inventory(message.from_user.id)
        reply = await llm.generate(fresh_user, history[:-1], user_text, inventory)
        rendered_plain = render_story_plain(reply.scene, reply.dialogue, fresh_user["companion_name"])
        rendered_html = render_story_html(reply.scene, reply.dialogue, fresh_user["companion_name"])
        await db.add_message(message.from_user.id, "assistant", rendered_plain)

        current_location = short_location(fresh_user.get("current_location") or fresh_user.get("setting") or "")
        next_location = short_location(reply.new_location) if reply.new_location else current_location
        await db.update_user(
            message.from_user.id,
            last_scene_description=reply.scene,
            current_location=next_location,
        )
        await message.answer(rendered_html, reply_markup=main_keyboard(), parse_mode="HTML")

        if reply.new_location and normalize(reply.new_location) != normalize(current_location):
            await send_scene_art(
                message,
                llm,
                fresh_user,
                reply.new_location_description or reply.scene,
                reply.new_location,
                reply.scene_image_prompt,
                report_errors=False,
            )

        if reply.new_item:
            await append_inventory_item(db, message.from_user.id, reply.new_item)
            await send_item_art(
                message,
                llm,
                fresh_user,
                reply.new_item,
                reply.new_item_description,
                reply.item_image_prompt,
                report_errors=False,
            )
    except LLMError:
        await message.answer("Спутник задумался, повтори", reply_markup=main_keyboard())
        return
    except Exception:
        logging.exception("Story generation failed")
        await message.answer("Спутник задумался, повтори", reply_markup=main_keyboard())
        return

    state = await db.advance_story_turn(message.from_user.id)
    if state["game_over"] or state["hunger"] <= 0:
        await message.answer("Ты теряешь сознание от голода...", reply_markup=game_over_keyboard())


async def handle_image(message: Message, db: GameDatabase, llm: LLMService) -> None:
    user = await db.get_user(message.from_user.id)
    scene_text = user.get("last_scene_description") or user.get("current_location") or user.get("setting")
    await send_scene_art(
        message,
        llm,
        user,
        scene_text or "",
        scene_text or "",
        "",
        report_errors=True,
    )


async def handle_inventory(message: Message, db: GameDatabase) -> None:
    inventory = await db.get_inventory(message.from_user.id)
    await message.answer(render_inventory(inventory), reply_markup=inventory_keyboard(inventory))


async def handle_eat(message: Message, db: GameDatabase) -> None:
    text = (message.text or "").strip()
    match = re.match(r"(?i)^\s*(?:🍞\s*)?есть\s+(.+)$", text)
    if not match:
        await handle_inventory(message, db)
        return

    item_name = match.group(1).strip()
    ok, result, hunger_restore = await db.consume_item(message.from_user.id, item_name)
    if not ok:
        await message.answer(result, reply_markup=main_keyboard())
        return

    updated = await db.get_user(message.from_user.id)
    await message.answer(
        f"Ты съел(а) {result} и восстановил(а) {hunger_restore} голода.\n"
        f"Сейчас голод: {updated['hunger']}/100",
        reply_markup=inventory_keyboard(await db.get_inventory(message.from_user.id)),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db: GameDatabase) -> None:
    user = await db.ensure_user(message.from_user.id)
    if int(user.get("game_over", 0)):
        await db.reset_user(message.from_user.id)
        user = await db.ensure_user(message.from_user.id)

    if user["state"] == "creating" or not user["player_name"]:
        await db.update_user(message.from_user.id, state="creating")
        await ask_next_creation_question(message, user)
        return

    if user["state"] == "playing":
        await message.answer(
            f"{user['player_name']} и {user['companion_name']} уже в мире.\n{user['setting']}",
            reply_markup=main_keyboard(),
        )
        return

    await db.update_user(message.from_user.id, state="creating")
    await ask_next_creation_question(message, user)


@router.message(F.text)
async def handle_text(message: Message, db: GameDatabase, llm: LLMService) -> None:
    text = message.text or ""
    user = await db.ensure_user(message.from_user.id)

    if is_restart(text):
        await db.reset_user(message.from_user.id)
        user = await db.ensure_user(message.from_user.id)
        await db.update_user(message.from_user.id, state="creating")
        await ask_next_creation_question(message, user)
        return

    if user["state"] == "creating":
        if get_creation_stage(user) == "confirm":
            if is_yes(text):
                await start_game(message, db, llm, user)
                return
            await message.answer(
                "Напиши 'да' чтобы начать, или 'заново' чтобы пересоздать.",
                reply_markup=confirm_keyboard(),
            )
            return

        await handle_creation_step(message, db, llm, user)
        return

    if user["state"] == "playing":
        if int(user.get("game_over", 0)) or int(user.get("hunger", 0)) <= 0:
            await message.answer("Ты теряешь сознание от голода...", reply_markup=game_over_keyboard())
            return

        pending_action = (user.get("pending_action") or "").strip()
        if pending_action == "rename_companion":
            new_name = text.strip()
            if not new_name:
                await message.answer("Напиши новое имя или прозвище.", reply_markup=ReplyKeyboardRemove())
                return
            await db.update_user(
                message.from_user.id,
                companion_name=new_name,
                pending_action="",
            )
            await message.answer(
                f"Хорошо, теперь спутника зовут {new_name}.",
                reply_markup=main_keyboard(),
            )
            return

        if is_rename_request(text):
            await db.update_user(message.from_user.id, pending_action="rename_companion")
            await message.answer("Как теперь зовут спутника?", reply_markup=ReplyKeyboardRemove())
            return

        if is_image_request(text):
            try:
                await handle_image(message, db, llm)
            except Exception:
                logging.exception("Image generation failed")
                await message.answer("Спутник задумался, повтори", reply_markup=main_keyboard())
            return

        if is_inventory(text):
            await handle_inventory(message, db)
            return

        if is_settings(text):
            await message.answer(render_settings(), reply_markup=main_keyboard())
            return

        if re.match(r"(?i)^\s*(?:🍞\s*)?есть\s+.+$", text):
            await handle_eat(message, db)
            return

        await handle_story(message, db, llm, user)
        return

    await message.answer("Напиши /start, чтобы начать новую историю.", reply_markup=main_keyboard())


async def run_polling(bot: Bot, dp: Dispatcher, llm: LLMService, db: GameDatabase) -> None:
    try:
        with suppress(Exception):
            await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        await llm.aclose()
        await db.close()
        await bot.session.close()


async def run_webhook(bot: Bot, dp: Dispatcher, llm: LLMService, db: GameDatabase) -> None:
    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret or None,
    ).register(app, path=settings.webhook_path)
    setup_application(app, dp, bot=bot)
    app.router.add_get("/", healthcheck)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.http_host, port=settings.http_port)
    await site.start()

    webhook_url = build_webhook_url()

    async def register_webhook_loop() -> None:
        delay = 5
        while True:
            try:
                await bot.set_webhook(
                    webhook_url,
                    secret_token=settings.webhook_secret or None,
                )
                logging.info("Webhook registered: %s", webhook_url)
                return
            except Exception:
                logging.exception("Webhook registration failed, retrying in %s seconds", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    webhook_task = asyncio.create_task(register_webhook_loop())

    try:
        await asyncio.Event().wait()
    finally:
        webhook_task.cancel()
        with suppress(asyncio.CancelledError):
            await webhook_task
        await runner.cleanup()
        await llm.aclose()
        await db.close()
        await bot.session.close()


async def main() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is missing")

    db = GameDatabase(settings.db_path)
    await db.init()

    llm = LLMService(settings)
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    dp["db"] = db
    dp["llm"] = llm

    if settings.run_mode == "webhook":
        await run_webhook(bot, dp, llm, db)
    else:
        await run_polling(bot, dp, llm, db)


if __name__ == "__main__":
    asyncio.run(main())
