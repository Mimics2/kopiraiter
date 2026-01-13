import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

import aiohttp
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEYS = os.getenv("GEMINI_API_KEYS", "").split(",")

# Если API ключи не в переменных окружения, можно указать здесь
if not GEMINI_API_KEYS or GEMINI_API_KEYS[0] == "":
    # Добавьте свои API ключи здесь или в переменных окружения Railway
    GEMINI_API_KEYS = [
        "your_gemini_api_key_1",
        "your_gemini_api_key_2",
        "your_gemini_api_key_3",
    ]

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"

# Структуры данных для хранения состояния
user_requests: Dict[int, Dict] = defaultdict(dict)  # user_id -> {request_id: data}
request_timers: Dict[str, asyncio.Task] = {}
current_key_index = 0
router = Router()

def get_next_api_key() -> str:
    """Получить следующий API ключ (ротация)"""
    global current_key_index
    if not GEMINI_API_KEYS:
        raise ValueError("No Gemini API keys available")
    
    key = GEMINI_API_KEYS[current_key_index]
    current_key_index = (current_key_index + 1) % len(GEMINI_API_KEYS)
    return key

def generate_request_id(user_id: int) -> str:
    """Генерация уникального ID запроса"""
    timestamp = int(datetime.now().timestamp())
    return f"{user_id}_{timestamp}"

async def call_gemini_api(prompt: str, request_id: str) -> Optional[str]:
    """Вызов Gemini API"""
    api_key = get_next_api_key()
    
    headers = {
        "Content-Type": "application/json",
    }
    
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.7,
            "topK": 40,
            "topP": 0.95,
            "maxOutputTokens": 1024,
        }
    }
    
    url = f"{GEMINI_API_URL}?key={api_key}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if "candidates" in data and data["candidates"]:
                        return data["candidates"][0]["content"]["parts"][0]["text"]
                    else:
                        logger.error(f"Invalid response format for request {request_id}")
                        return None
                else:
                    logger.error(f"API error for request {request_id}: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"Error calling Gemini API for request {request_id}: {e}")
        return None

async def process_request_with_delay(user_id: int, request_id: str):
    """Обработка запроса с задержкой в 1 минуту"""
    await asyncio.sleep(60)  # Ждем 1 минуту
    
    if request_id in user_requests[user_id]:
        request_data = user_requests[user_id][request_id]
        prompt = request_data.get("prompt", "")
        
        if prompt:
            # Отправляем запрос в Gemini
            bot = request_data.get("bot")
            
            # Отправляем уведомление о начале обработки
            await bot.send_message(user_id, f"🔄 Обрабатываю запрос ID: {request_id}")
            
            # Вызываем API
            response_text = await call_gemini_api(prompt, request_id)
            
            if response_text:
                # Форматируем ответ со специальным символом
                formatted_response = f"✨【Ответ на запрос {request_id}】✨\n\n{response_text}\n\n📌 Конец ответа"
                
                # Отправляем ответ
                await bot.send_message(
                    user_id, 
                    formatted_response,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await bot.send_message(
                    user_id,
                    f"❌ Ошибка при обработке запроса {request_id}. Попробуйте еще раз."
                )
        
        # Удаляем обработанный запрос
        user_requests[user_id].pop(request_id, None)
    
    # Удаляем таймер
    request_timers.pop(request_id, None)

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    welcome_text = """
🤖 Привет! Я бот-копирайтер на основе Gemini AI.

📝 Просто отправь мне текст, и я:
1. Присвою твоему запросу уникальный ID
2. Подожду 1 минуту на случай дополнительных уточнений
3. Обработаю все накопленные запросы
4. Верну ответы с пометкой ID каждого запроса

💡 Отправь мне текст для начала работы!
"""
    await message.answer(welcome_text)

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help"""
    help_text = """
📚 Помощь по использованию бота:

• Просто отправьте текст - бот начнет обработку
• Каждому запросу присваивается уникальный ID
• Бот ждет 1 минуту перед отправкой в Gemini
• Ответы приходят с указанием ID запроса
• Разные запросы обрабатываются независимо

❓ Пример: отправьте "Напиши рекламный текст для кофейни"
"""
    await message.answer(help_text)

@router.message(Command("status"))
async def cmd_status(message: Message):
    """Показать статус текущих запросов"""
    user_id = message.from_user.id
    pending_requests = user_requests.get(user_id, {})
    
    if not pending_requests:
        await message.answer("✅ У вас нет ожидающих запросов.")
    else:
        status_text = "📋 Ваши текущие запросы:\n\n"
        for req_id, req_data in pending_requests.items():
            prompt_preview = req_data.get("prompt", "")[:50] + "..."
            created_time = req_data.get("created", "")
            status_text += f"• ID: {req_id}\n"
            status_text += f"  Текст: {prompt_preview}\n"
            status_text += f"  Создан: {created_time}\n"
            status_text += f"  Статус: ⏳ Ожидает обработки\n\n"
        
        await message.answer(status_text)

@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    """Отменить все ожидающие запросы"""
    user_id = message.from_user.id
    
    if user_id in user_requests and user_requests[user_id]:
        count = len(user_requests[user_id])
        
        # Отменяем все таймеры
        for req_id in list(user_requests[user_id].keys()):
            if req_id in request_timers:
                request_timers[req_id].cancel()
                request_timers.pop(req_id, None)
        
        # Очищаем запросы пользователя
        user_requests[user_id].clear()
        
        await message.answer(f"✅ Отменено {count} запросов.")
    else:
        await message.answer("❌ Нет запросов для отмены.")

@router.message()
async def handle_message(message: Message, bot: Bot):
    """Обработчик всех сообщений"""
    user_id = message.from_user.id
    user_text = message.text
    
    if not user_text.strip():
        await message.answer("❌ Пожалуйста, отправьте текст для обработки.")
        return
    
    # Генерируем ID запроса
    request_id = generate_request_id(user_id)
    
    # Сохраняем запрос
    user_requests[user_id][request_id] = {
        "prompt": user_text,
        "created": datetime.now().strftime("%H:%M:%S"),
        "bot": bot
    }
    
    # Отправляем подтверждение
    confirmation_text = f"""
✅ Запрос получен!

📝 ID запроса: {request_id}
🕐 Обработка начнется через 1 минуту...
✏️ Вы можете отправить дополнительные уточнения в течение этого времени.

Используйте /status для проверки состояния.
Используйте /cancel для отмены всех запросов.
"""
    await message.answer(confirmation_text)
    
    # Если уже есть таймер для этого пользователя, отменяем его
    existing_timer = None
    for req_id, timer_task in list(request_timers.items()):
        if req_id.startswith(f"{user_id}_"):
            timer_task.cancel()
            request_timers.pop(req_id, None)
            # Объединяем промпты
            if req_id in user_requests[user_id]:
                old_prompt = user_requests[user_id][req_id].get("prompt", "")
                user_requests[user_id][request_id]["prompt"] = old_prompt + "\n\n" + user_text
                user_requests[user_id].pop(req_id, None)
    
    # Запускаем новый таймер
    timer_task = asyncio.create_task(process_request_with_delay(user_id, request_id))
    request_timers[request_id] = timer_task

async def main():
    """Основная функция запуска бота"""
    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)
    
    logger.info("Бот запущен!")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

