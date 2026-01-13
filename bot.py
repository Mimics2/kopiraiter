import os
import sys
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from collections import defaultdict

import aiohttp
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === КОНФИГУРАЦИЯ ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не установлен!")
    sys.exit(1)

GEMINI_API_KEYS = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
if not GEMINI_API_KEYS:
    logger.error("❌ GEMINI_API_KEYS не установлены!")
    sys.exit(1)

# === ПРОМТ ДЛЯ GEMINI ===
# Меняйте этот промт в Railway Variables чтобы настроить Gemini
SYSTEM_PROMPT = os.getenv("GEMINI_PROMPT", """Ты профессиональный копирайтер. Пиши ТОЛЬКО готовый контент без объяснений, без вступлений, без заключений.

ТВОИ ПРАВИЛА:
1. Отвечай ТОЛЬКО готовым текстом/постом/контентом
2. НИКАКИХ "Вот что я создал", "Вот мой ответ", "Этот текст" и т.д.
3. НИКАКИХ объяснений процесса, мыслей, комментариев
4. Просто дай готовый результат
5. Если нужен формат (пост, статья, реклама) - сразу в этом формате
6. Максимально подробно и полно, не обрезай текст
7. Всё что нужно - пиши в одном ответе

Пример:
Запрос: "Напиши пост для Instagram про кофе"
Ответ: "Утренний ритуал начинается с аромата свежесваренного кофе... [полный текст поста]"

Теперь следуй этим правилам для всех запросов.""")

# Модель Gemini
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# === СИСТЕМА ===
user_requests = defaultdict(dict)
request_timers = {}
current_key_index = 0
router = Router()

def get_next_api_key():
    global current_key_index
    key = GEMINI_API_KEYS[current_key_index]
    current_key_index = (current_key_index + 1) % len(GEMINI_API_KEYS)
    return key

def generate_request_id(user_id):
    return f"{user_id}_{int(datetime.now().timestamp())}"

async def call_gemini_api(user_prompt: str, request_id: str) -> Optional[str]:
    """Вызов Gemini API с системным промтом"""
    try:
        api_key = get_next_api_key()
        
        # Комбинируем системный промт и промт пользователя
        full_prompt = f"{SYSTEM_PROMPT}\n\nЗАПРОС ПОЛЬЗОВАТЕЛЯ: {user_prompt}\n\nОТВЕТ (ТОЛЬКО КОНТЕНТ):"
        
        payload = {
            "contents": [{
                "parts": [{"text": full_prompt}]
            }],
            "generationConfig": {
                "temperature": 0.8,
                "topP": 0.95,
                "topK": 40,
                "maxOutputTokens": 2048,  # Увеличил для полных ответов
            }
        }
        
        url = f"{GEMINI_URL}?key={api_key}"
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(45)) as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Получаем ВЕСЬ текст ответа
                    if "candidates" in data and data["candidates"]:
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        
                        # Очищаем ответ от возможных префиксов
                        clean_text = text.strip()
                        
                        # Удаляем возможные фразы типа "Вот что я создал" и т.д.
                        unwanted_prefixes = [
                            "Вот что я создал",
                            "Вот мой ответ",
                            "Этот текст",
                            "Вот пост",
                            "Вот статья",
                            "Результат:",
                            "Ответ:",
                            "Текст:",
                            "Пост:",
                            "Статья:",
                            "✨",
                            "📝"
                        ]
                        
                        for prefix in unwanted_prefixes:
                            if clean_text.startswith(prefix):
                                clean_text = clean_text[len(prefix):].strip()
                        
                        # Удаляем двоеточия в начале
                        if clean_text.startswith(":"):
                            clean_text = clean_text[1:].strip()
                        
                        logger.info(f"✅ Ответ Gemini получен ({len(clean_text)} символов)")
                        return clean_text
                    else:
                        logger.error("❌ Gemini вернул пустой ответ")
                        return None
                else:
                    error_text = await response.text()
                    logger.error(f"❌ Ошибка Gemini API: {response.status} - {error_text}")
                    return None
                    
    except Exception as e:
        logger.error(f"❌ Ошибка вызова Gemini: {e}")
        return None

async def process_request(user_id: int, request_id: str, bot: Bot):
    """Обработка запроса с задержкой 1 минута"""
    try:
        # Ждем 1 минуту для дополнительных сообщений
        await asyncio.sleep(60)
        
        if user_id in user_requests and request_id in user_requests[user_id]:
            user_data = user_requests[user_id][request_id]
            user_prompt = user_data.get("prompt", "")
            
            if user_prompt:
                # Получаем ответ от Gemini
                response = await call_gemini_api(user_prompt, request_id)
                
                if response:
                    # Отправляем ТОЛЬКО ответ от Gemini
                    await bot.send_message(user_id, response)
                else:
                    # Если ошибка, отправляем короткое сообщение
                    await bot.send_message(user_id, "❌ Ошибка генерации. Попробуйте еще раз.")
            
            # Очищаем данные
            if user_id in user_requests:
                user_requests[user_id].pop(request_id, None)
                if not user_requests[user_id]:
                    del user_requests[user_id]
    
    except Exception as e:
        logger.error(f"❌ Ошибка обработки: {e}")
    finally:
        if request_id in request_timers:
            del request_timers[request_id]

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Только краткое сообщение о начале"""
    await message.answer("🤖 Бот-копирайтер готов к работе. Отправьте текст для генерации контента.")

@router.message(Command("prompt"))
async def cmd_prompt(message: Message):
    """Показать текущий системный промт"""
    prompt_preview = SYSTEM_PROMPT[:200] + "..." if len(SYSTEM_PROMPT) > 200 else SYSTEM_PROMPT
    await message.answer(f"📋 Текущий промт Gemini:\n\n{prompt_preview}\n\nИзменить: GEMINI_PROMPT в настройках Railway")

@router.message()
async def handle_message(message: Message):
    """Основной обработчик сообщений"""
    user_id = message.from_user.id
    user_text = message.text.strip()
    
    if not user_text:
        return
    
    # Создаем ID запроса
    request_id = generate_request_id(user_id)
    
    # Сохраняем промпт пользователя
    if user_id in user_requests:
        # Если уже есть запрос от пользователя, объединяем
        existing_id = next(iter(user_requests[user_id].keys()), None)
        if existing_id:
            old_prompt = user_requests[user_id][existing_id].get("prompt", "")
            user_requests[user_id][request_id] = {
                "prompt": f"{old_prompt}\n\nДополнительно: {user_text}",
                "created": datetime.now().strftime("%H:%M:%S")
            }
            # Отменяем старый таймер
            if existing_id in request_timers:
                try:
                    request_timers[existing_id].cancel()
                except:
                    pass
                del request_timers[existing_id]
            # Удаляем старый запрос
            user_requests[user_id].pop(existing_id, None)
        else:
            user_requests[user_id][request_id] = {
                "prompt": user_text,
                "created": datetime.now().strftime("%H:%M:%S")
            }
    else:
        user_requests[user_id][request_id] = {
            "prompt": user_text,
            "created": datetime.now().strftime("%H:%M:%S")
        }
    
    # Только краткое подтверждение
    await message.answer(f"✅ Запрос принят. Генерация через 1 минуту...")
    
    # Запускаем таймер
    timer = asyncio.create_task(process_request(user_id, request_id, message.bot))
    request_timers[request_id] = timer

async def main():
    """Запуск бота"""
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties())
    dp = Dispatcher()
    dp.include_router(router)
    
    logger.info("=" * 50)
    logger.info("🤖 БОТ ЗАПУЩЕН")
    logger.info(f"📋 Длина системного промта: {len(SYSTEM_PROMPT)} символов")
    logger.info(f"🔑 Доступно API ключей: {len(GEMINI_API_KEYS)}")
    logger.info("=" * 50)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
