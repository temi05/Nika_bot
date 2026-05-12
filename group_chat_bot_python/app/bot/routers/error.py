from __future__ import annotations

import logging
import traceback
from aiogram import Router
from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)

def build_error_router() -> Router:
    router = Router(name="error")

    @router.error()
    async def error_handler(event: ErrorEvent):
        # Логируем ошибку с полным стеком вызовов
        logger.error(f"Глобальная ошибка бота: {event.exception}", exc_info=event.exception)
        
        # Печатаем в консоль для удобства отладки
        print("\n--- [BOT ERROR] ---")
        traceback.print_exception(type(event.exception), event.exception, event.exception.__traceback__)
        print("-------------------\n")

        # Попробуем отправить сообщение пользователю, если это возможно
        if event.update.message:
            try:
                await event.update.message.answer(
                    "❌ <b>Произошла внутренняя ошибка.</b>\n"
                    "Разработчики уже уведомлены (наверное). Попробуйте позже.",
                    parse_mode="HTML"
                )
            except Exception:
                # Если не получилось ответить (например, бот заблокирован), просто игнорируем
                pass
        elif event.update.callback_query:
            try:
                await event.update.callback_query.answer(
                    "❌ Произошла ошибка при обработке нажатия.",
                    show_alert=True
                )
            except Exception:
                pass
                
    return router
