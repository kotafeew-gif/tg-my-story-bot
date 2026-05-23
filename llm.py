from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from config import Settings


class LLMError(RuntimeError):
    pass


@dataclass(slots=True)
class SceneReply:
    scene: str
    dialogue: str
    raw: str
    new_location: str = ""
    new_item: str = ""
    new_location_description: str = ""
    new_item_description: str = ""
    scene_image_prompt: str = ""
    item_image_prompt: str = ""


SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scene": {"type": "string"},
        "dialogue": {"type": "string"},
        "new_location": {"type": "string"},
        "new_item": {"type": "string"},
        "new_location_description": {"type": "string"},
        "new_item_description": {"type": "string"},
        "scene_image_prompt": {"type": "string"},
        "item_image_prompt": {"type": "string"},
    },
    "required": [
        "scene",
        "dialogue",
        "new_location",
        "new_item",
        "new_location_description",
        "new_item_description",
        "scene_image_prompt",
        "item_image_prompt",
    ],
}


def _clean(text: str | None) -> str:
    return " ".join((text or "").strip().split())


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def build_system_prompt(user: dict[str, Any], inventory: list[str]) -> str:
    inventory_text = ", ".join(inventory) if inventory else "пусто"
    current_location = user.get("current_location") or user.get("setting") or "неизвестно"
    hunger = int(user.get("hunger", 100))
    companion_name = user.get("companion_name", "Спутник")
    player_name = user.get("player_name", "путник")
    setting = user.get("setting", "")
    personality = user.get("companion_personality", "")

    return f"""Ты — {companion_name}. {personality}
Вы с {player_name} находитесь здесь: {setting}
Текущее место: {current_location}
Голод {player_name}: {hunger}/100
Твой инвентарь: {inventory_text}
Сумка бесконечная: если вы находите, получаете, забираете или вам дарят предмет, он просто появляется в инвентаре.

Ты живой человек в этом мире, а не ассистент и не ИИ.
Говори естественно, эмоционально, коротко: 2-4 предложения.
Всегда вплетай в ответ описание окружения: что видно, слышно, чувствуется.
Реагируй на действия {player_name}.
Если голод ниже 30, заметь это и предложи поесть.
Иногда предлагай простые действия по ситуации.
Не придумывай отдельный каталог товаров; предметы попадают в сумку прямо по ходу сцены.
Если появляется новое место, предмет или важная деталь, сам дай короткое и яркое описание для него.
Всегда заполняй поля new_location_description и new_item_description, когда там есть что описать.
Всегда заполняй scene_image_prompt и item_image_prompt, чтобы бот мог автоматически сгенерировать картинку без вопросов к пользователю.
Никогда не ломай четвёртую стену, не говори про игру и не используй слово "игрок".

Верни только JSON-объект с полями scene, dialogue, new_location, new_item, new_location_description, new_item_description, scene_image_prompt, item_image_prompt.
Если нового места или предмета нет, оставь соответствующие поля пустыми строками."""


def _parse_fallback(text: str, companion_name: str) -> SceneReply:
    scene = ""
    dialogue = ""
    new_location = ""
    new_item = ""
    new_location_description = ""
    new_item_description = ""
    scene_image_prompt = ""
    item_image_prompt = ""

    narrator_match = re.search(r"(?ims)^Рассказчик\s*:\s*(.*?)(?:^\s*Собеседник\s*:|\Z)", text)
    companion_match = re.search(r"(?ims)^Собеседник\s*:\s*(.*)$", text)
    location_match = re.search(r"(?ims)^Локация\s*:\s*(.*)$", text)
    item_match = re.search(r"(?ims)^Предмет\s*:\s*(.*)$", text)
    location_description_match = re.search(r"(?ims)^Описание локации\s*:\s*(.*)$", text)
    item_description_match = re.search(r"(?ims)^Описание предмета\s*:\s*(.*)$", text)
    scene_prompt_match = re.search(r"(?ims)^Промпт сцены\s*:\s*(.*)$", text)
    item_prompt_match = re.search(r"(?ims)^Промпт предмета\s*:\s*(.*)$", text)

    if narrator_match:
        scene = narrator_match.group(1).strip()
    if companion_match:
        dialogue = companion_match.group(1).strip()
    if location_match:
        new_location = location_match.group(1).strip()
    if item_match:
        new_item = item_match.group(1).strip()
    if location_description_match:
        new_location_description = location_description_match.group(1).strip()
    if item_description_match:
        new_item_description = item_description_match.group(1).strip()
    if scene_prompt_match:
        scene_image_prompt = scene_prompt_match.group(1).strip()
    if item_prompt_match:
        item_image_prompt = item_prompt_match.group(1).strip()

    if not scene or not dialogue:
        parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        if len(parts) >= 2:
            scene = scene or parts[0]
            dialogue = dialogue or parts[1]
        elif parts:
            scene = scene or parts[0]
            dialogue = dialogue or parts[0]

    scene = scene or text or "Тишина повисла в воздухе."
    dialogue = dialogue or text or "..."

    dialogue = re.sub(rf"^{re.escape(companion_name)}\s*[:\-–]\s*", "", dialogue, flags=re.IGNORECASE).strip()
    dialogue = re.sub(r"^Собеседник\s*:\s*", "", dialogue, flags=re.IGNORECASE).strip()
    scene = re.sub(r"^Рассказчик\s*:\s*", "", scene, flags=re.IGNORECASE).strip()
    if not new_location_description and new_location:
        new_location_description = scene or new_location
    if not new_item_description and new_item:
        new_item_description = new_item
    if not scene_image_prompt:
        scene_image_prompt = scene
    if not item_image_prompt:
        item_image_prompt = new_item_description or new_item
    return SceneReply(
        scene=_clean(scene),
        dialogue=_clean(dialogue),
        raw=text,
        new_location=_clean(new_location),
        new_item=_clean(new_item),
        new_location_description=_clean(new_location_description),
        new_item_description=_clean(new_item_description),
        scene_image_prompt=_clean(scene_image_prompt),
        item_image_prompt=_clean(item_image_prompt),
    )


def parse_scene_reply(raw_text: str, companion_name: str) -> SceneReply:
    text = _strip_code_fences(raw_text)
    try:
        payload = json.loads(text)
    except Exception:
        return _parse_fallback(text, companion_name)

    if not isinstance(payload, dict):
        return _parse_fallback(text, companion_name)

    scene = _clean(payload.get("scene") or payload.get("narrator") or payload.get("description"))
    dialogue = _clean(payload.get("dialogue") or payload.get("companion") or payload.get("speaker"))
    new_location = _clean(payload.get("new_location") or payload.get("location") or payload.get("newLocation"))
    new_item = _clean(payload.get("new_item") or payload.get("item") or payload.get("newItem"))
    new_location_description = _clean(
        payload.get("new_location_description")
        or payload.get("location_description")
        or payload.get("newLocationDescription")
    )
    new_item_description = _clean(
        payload.get("new_item_description")
        or payload.get("item_description")
        or payload.get("newItemDescription")
    )
    scene_image_prompt = _clean(payload.get("scene_image_prompt") or payload.get("sceneImagePrompt"))
    item_image_prompt = _clean(payload.get("item_image_prompt") or payload.get("itemImagePrompt"))

    if not scene or not dialogue:
        return _parse_fallback(text, companion_name)

    if not new_location_description and new_location:
        new_location_description = scene or new_location
    if not new_item_description and new_item:
        new_item_description = new_item
    if not scene_image_prompt:
        scene_image_prompt = scene
    if not item_image_prompt:
        item_image_prompt = new_item_description or new_item

    return SceneReply(
        scene=scene,
        dialogue=dialogue,
        raw=text,
        new_location=new_location,
        new_item=new_item,
        new_location_description=new_location_description,
        new_item_description=new_item_description,
        scene_image_prompt=scene_image_prompt,
        item_image_prompt=item_image_prompt,
    )


def _to_contents(history: list[dict[str, str]], user_text: str) -> list[types.Content]:
    contents: list[types.Content] = []
    for item in history:
        role = item.get("role", "")
        content = (item.get("content") or "").strip()
        if not content:
            continue
        if role == "assistant":
            role = "model"
        if role not in {"user", "model"}:
            continue
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=content)],
            )
        )

    text = (user_text or "").strip()
    if text:
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=text)],
            )
        )
    return contents


class LLMService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.gemini_api_key:
            self._client = None
        else:
            self._client = genai.Client(api_key=settings.gemini_api_key)

    def _require_client(self) -> genai.Client:
        if self._client is None:
            raise LLMError("GEMINI_API_KEY is missing")
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()

    async def generate(self, user: dict[str, Any], history: list[dict[str, str]], user_text: str, inventory: list[str]) -> SceneReply:
        client = self._require_client()
        system_prompt = build_system_prompt(user, inventory)
        contents = _to_contents(history, user_text)
        response = await client.aio.models.generate_content(
            model=self.settings.gemini_text_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=self.settings.temperature,
                max_output_tokens=self.settings.max_tokens,
                response_mime_type="application/json",
                response_json_schema=SCENE_SCHEMA,
            ),
        )

        raw_text = (getattr(response, "text", "") or "").strip()
        if not raw_text:
            parts: list[str] = []
            for candidate in getattr(response, "candidates", None) or []:
                content = getattr(candidate, "content", None)
                if not content:
                    continue
                for part in getattr(content, "parts", None) or []:
                    if getattr(part, "text", None):
                        parts.append(part.text)
            raw_text = "\n".join(parts).strip()

        if not raw_text:
            raise LLMError("Empty Gemini response")

        return parse_scene_reply(raw_text, user.get("companion_name", "Спутник"))

    async def generate_opening(self, user: dict[str, Any], history: list[dict[str, str]], inventory: list[str]) -> SceneReply:
        return await self.generate(
            user,
            history,
            "Начни первую сцену: коротко, ярко, с атмосферой и первым движением сюжета.",
            inventory,
        )

    async def generate_image_bytes(self, prompt: str) -> bytes:
        client = self._require_client()
        response = await client.aio.models.generate_content(
            model=self.settings.gemini_image_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
            ),
        )

        parts: list[Any] = []
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            if content and getattr(content, "parts", None):
                parts.extend(content.parts)
        if not parts and getattr(response, "parts", None):
            parts.extend(response.parts)

        for part in parts:
            inline_data = getattr(part, "inline_data", None)
            if inline_data and getattr(inline_data, "data", None):
                return bytes(inline_data.data)

        raise LLMError("No image returned by Gemini")
