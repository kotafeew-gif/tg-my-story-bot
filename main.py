from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, Message, ReplyKeyboardRemove
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from config import settings
from database import GameDatabase
from keyboards import (
    confirm_keyboard,
    game_over_keyboard,
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
        "Если хочешь, я могу позже добавить переключатели."
    )


def render_creation_summary(user: dict[str, object]) -> str:
    return (
        "Отлично! Мир создан.\n"
        f"{user['player_name']} и {user['companion_name']} — {user['setting']}\n"
        f"{user['companion_name']}: '{user['companion_personality']}'\n"
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
    if not user.get("player_name"):
        await message.answer(
            "Привет! Давай создадим твоего спутника и мир, в котором вы окажетесь.\n"
            "Для начала — как зовут ТЕБЯ?",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if not user.get("companion_name"):
        await message.answer("Как зовут твоего спутника?", reply_markup=ReplyKeyboardRemove())
        return
    if not user.get("companion_personality"):
        await message.answer(
            "Кто он(а)? Опиши в 2-3 предложениях характер, внешность, историю.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if not user.get("setting"):
        await message.answer(
            "В каком мире вы находитесь и чем занимаетесь?\n"
            "Примеры:\n"
            "- 'Мы едем в поезде, я возвращаюсь домой, а спутник — случайный попутчик'\n"
            "- 'Мы охотники на драконов, сейчас в горах выслеживаем стаю'\n"
            "- 'Мы коллеги в офисе, сейчас обеденный перерыв'\n"
            "- 'Мы в постапокалипсисе, идём через пустошь в поисках воды'",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    await message.answer(render_creation_summary(user), reply_markup=confirm_keyboard())


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
        rendered = f"Рассказчик: {opening.scene}\n\n{fresh_user['companion_name']}: {opening.dialogue}"
        await db.add_message(message.from_user.id, "assistant", rendered)
        await db.update_user(
            message.from_user.id,
            last_scene_description=opening.scene,
            current_location=short_location(opening.new_location or fresh_user["setting"]),
        )
        await message.answer(rendered, reply_markup=main_keyboard())

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
        rendered = f"Рассказчик: {reply.scene}\n\n{fresh_user['companion_name']}: {reply.dialogue}"
        await db.add_message(message.from_user.id, "assistant", rendered)

        current_location = short_location(fresh_user.get("current_location") or fresh_user.get("setting") or "")
        next_location = short_location(reply.new_location) if reply.new_location else current_location
        await db.update_user(
            message.from_user.id,
            last_scene_description=reply.scene,
            current_location=next_location,
        )
        await message.answer(rendered, reply_markup=main_keyboard())

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
        if not user.get("player_name"):
            await db.update_user(message.from_user.id, player_name=text.strip(), state="creating")
            user = await db.get_user(message.from_user.id)
            await ask_next_creation_question(message, user)
            return
        if not user.get("companion_name"):
            await db.update_user(message.from_user.id, companion_name=text.strip(), state="creating")
            user = await db.get_user(message.from_user.id)
            await ask_next_creation_question(message, user)
            return
        if not user.get("companion_personality"):
            await db.update_user(message.from_user.id, companion_personality=text.strip(), state="creating")
            user = await db.get_user(message.from_user.id)
            await ask_next_creation_question(message, user)
            return
        if not user.get("setting"):
            await db.update_user(message.from_user.id, setting=text.strip(), state="creating")
            user = await db.get_user(message.from_user.id)
            await ask_next_creation_question(message, user)
            return

        if is_yes(text):
            await start_game(message, db, llm, user)
            return

        await message.answer("Напиши 'да' чтобы начать, или 'заново' чтобы пересоздать.", reply_markup=confirm_keyboard())
        return

    if user["state"] == "playing":
        if int(user.get("game_over", 0)) or int(user.get("hunger", 0)) <= 0:
            await message.answer("Ты теряешь сознание от голода...", reply_markup=game_over_keyboard())
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
