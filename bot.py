
import os
import sys
import json
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from collections import defaultdict

import aiohttp
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация - ОБЯЗАТЕЛЬНО УКАЖИТЕ В Railway Variables!
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN не установлен!")
    logger.info("Пожалуйста, установите TELEGRAM_BOT_TOKEN в переменных окружения Railway")
    sys.exit(1)

GEMINI_API_KEYS_STR = os.getenv("GEMINI_API_KEYS", "")
if GEMINI_API_KEYS_STR:
    GEMINI_API_KEYS = [key.strip() for key in GEMINI_API_KEYS_STR.split(",") if key.strip()]
else:
    # Если ключи не указаны в переменных окружения, используйте этот список
    # НО ЛУЧШЕ УКАЗЫВАТЬ В Railway Variables!
    GEMINI_API_KEYS = [
        "your_gemini_api_key_1_here",
        "your_gemini_api_key_2_here", 
        "your_gemini_api_key_3_here"
    ]

# Проверка API ключей
if not GEMINI_API_KEYS or all("your_gemini_api_key_" in key for key in GEMINI_API_KEYS):
    logger.warning("GEMINI_API_KEYS не установлены или используются значения по умолчанию!")
    logger.info("Пожалуйста, установите GEMINI_API_KEYS в переменных окружения Railway")

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
    logger.info(f"Используется API ключ #{current_key_index}")
    return key

def generate_request_id(user_id: int) -> str:
    """Генерация уникального ID запроса"""
    timestamp = int(datetime.now().timestamp())
    return f"{user_id}_{timestamp}"

async def call_gemini_api(prompt: str, request_id: str) -> Optional[str]:
    """Вызов Gemini API"""
    try:
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
        
        logger.info(f"Отправка запроса {request_id} в Gemini API")
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if "candidates" in data and data["candidates"]:
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        logger.info(f"Успешный ответ для запроса {request_id}")
                        return text
                    else:
                        logger.error(f"Неверный формат ответа для запроса {request_id}")
                        return "❌ Ошибка: неверный формат ответа от API"
                else:
                    error_text = await response.text()
                    logger.error(f"Ошибка API для запроса {request_id}: {response.status} - {error_text}")
                    return f"❌ Ошибка API: {response.status}"
                    
    except aiohttp.ClientError as e:
        logger.error(f"Ошибка сети для запроса {request_id}: {e}")
        return "❌ Ошибка сети при подключении к API"
    except Exception as e:
        logger.error(f"Неожиданная ошибка для запроса {request_id}: {e}")
        return "❌ Неожиданная ошибка при обработке запроса"

async def process_request_with_delay(user_id: int, request_id: str, bot: Bot):
    """Обработка запроса с задержкой в 1 минуту"""
    try:
        # Ждем 1 минуту
        await asyncio.sleep(60)
        
        # Проверяем, существует ли еще запрос
        if user_id in user_requests and request_id in user_requests[user_id]:
            request_data = user_requests[user_id][request_id]
            prompt = request_data.get("prompt", "")
            
            if prompt:
                # Отправляем уведомление о начале обработки
                try:
                    await bot.send_message(user_id, f"🔄 Обрабатываю запрос ID: {request_id}")
                except:
                    pass
                
                # Вызываем API
                response_text = await call_gemini_api(prompt, request_id)
                
                # Форматируем ответ со специальным символом
                formatted_response = f"✨【Ответ на запрос {request_id}】✨\n\n{response_text}\n\n📌 Конец ответа"
                
                # Отправляем ответ
                try:
                    await bot.send_message(
                        user_id, 
                        formatted_response,
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")
            
            # Удаляем обработанный запрос
            if user_id in user_requests:
                user_requests[user_id].pop(request_id, None)
                # Если у пользователя больше нет запросов, удаляем его запись
                if not user_requests[user_id]:
                    user_requests.pop(user_id, None)
    
    except asyncio.CancelledError:
        logger.info(f"Таймер для запроса {request_id} отменен")
    except Exception as e:
        logger.error(f"Ошибка в process_request_with_delay для {request_id}: {e}")
    finally:
        # Удаляем таймер
        if request_id in request_timers:
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

Доступные команды:
/start - начать работу
/help - помощь
/status - статус запросов
/cancel - отменить все запросы
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

Доступные команды:
/status - показать текущие запросы
/cancel - отменить все ожидающие запросы
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
            status_text += f"• ID: `{req_id}`\n"
            status_text += f"  Текст: {prompt_preview}\n"
            status_text += f"  Создан: {created_time}\n"
            status_text += f"  Статус: ⏳ Ожидает обработки\n\n"
        
        await message.answer(status_text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    """Отменить все ожидающие запросы"""
    user_id = message.from_user.id
    
    if user_id in user_requests and user_requests[user_id]:
        count = len(user_requests[user_id])
        
        # Отменяем все таймеры
        cancelled_count = 0
        for req_id in list(user_requests[user_id].keys()):
            if req_id in request_timers:
                try:
                    request_timers[req_id].cancel()
                    cancelled_count += 1
                except:
                    pass
                request_timers.pop(req_id, None)
        
        # Очищаем запросы пользователя
        user_requests[user_id].clear()
        user_requests.pop(user_id, None)
        
        await message.answer(f"✅ Отменено {count} запросов ({cancelled_count} таймеров).")
    else:
        await message.answer("❌ Нет запросов для отмены.")

@router.message()
async def handle_message(message: Message):
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
    }
    
    # Отправляем подтверждение
    confirmation_text = f"""
✅ Запрос получен!

📝 ID запроса: `{request_id}`
🕐 Обработка начнется через 1 минуту...
✏️ Вы можете отправить дополнительные уточнения в течение этого времени.

Используйте /status для проверки состояния.
Используйте /cancel для отмены всех запросов.
"""
    await message.answer(confirmation_text, parse_mode=ParseMode.MARKDOWN)
    
    # Если уже есть таймер для этого пользователя, отменяем его
    existing_timer_id = None
    for req_id, timer_task in list(request_timers.items()):
        if req_id.startswith(f"{user_id}_"):
            try:
                timer_task.cancel()
            except:
                pass
            existing_timer_id = req_id
            request_timers.pop(req_id, None)
            break
    
    # Если нашли старый таймер, объединяем промпты
    if existing_timer_id and existing_timer_id in user_requests[user_id]:
        old_prompt = user_requests[user_id][existing_timer_id].get("prompt", "")
        user_requests[user_id][request_id]["prompt"] = old_prompt + "\n\nДополнение:\n" + user_text
        user_requests[user_id].pop(existing_timer_id, None)
        logger.info(f"Объединен запрос {existing_timer_id} с {request_id}")
    
    # Запускаем новый таймер
    try:
        timer_task = asyncio.create_task(
            process_request_with_delay(user_id, request_id, message.bot)
        )
        request_timers[request_id] = timer_task
        logger.info(f"Создан новый запрос {request_id} для пользователя {user_id}")
    except Exception as e:
        logger.error(f"Ошибка создания таймера для {request_id}: {e}")
        await message.answer("❌ Ошибка при создании запроса. Попробуйте еще раз.")

async def main():
    """Основная функция запуска бота"""
    try:
        logger.info("Запуск бота...")
        logger.info(f"Токен бота: {'установлен' if BOT_TOKEN else 'НЕ УСТАНОВЛЕН!'}")
        logger.info(f"Доступно API ключей Gemini: {len(GEMINI_API_KEYS)}")
        
        # Новый синтаксис для aiogram 3.7.0+
        bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        
        dp = Dispatcher()
        dp.include_router(router)
        
        # Проверка соединения
        me = await bot.get_me()
        logger.info(f"Бот запущен как @{me.username} ({me.full_name})")
        
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
