---
title: Telegram RPG Bot
emoji: 🎭
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
---

# Telegram RPG Bot

Текстовая RPG для Telegram на `aiogram 3.x` с AI-спутником, рассказчиком, бесконечной сумкой и автокартинками.

## Модели
- Текст: `gemini-2.5-flash-lite`
- Картинки: `gemini-2.0-flash-preview-image-generation`

## Локальный запуск
1. Создай `.env`
2. Укажи `BOT_TOKEN` и `GEMINI_API_KEY`
3. Установи зависимости: `pip install -r requirements.txt`
4. Запусти: `python main.py`

## Hugging Face Spaces
1. Создай Space с SDK `Docker`
2. Добавь secrets:
   - `BOT_TOKEN`
   - `GEMINI_API_KEY`
3. Залей файлы проекта
4. Space сам поднимет webhook

## Команды
- `Сумка`
- `Картинка`
- `Настройки`
- `Есть [предмет]`
- `заново`
