# Group Chat Engagement Bot

Этот бот предназначен для оживления групповых чатов в Telegram.

## Функции

1.  **Система уровней (RPG)**:
    *   За каждое сообщение пользователи получают опыт (XP).
    *   При накоплении опыта повышается уровень.
    *   Бот поздравляет с повышением уровня.
    *   Команда `/me` показывает текущую статистику.
    *   Команда `/top` показывает топ-10 активных участников.

2.  **Интерактив "Кто из нас?"**:
    *   Команда `/kto <вопрос>` выбирает случайного участника чата (из тех, кто писал сообщения).
    *   Пример: `/kto самый красивый` -> "Я думаю, что самый красивый — это @username!"

## Установка и запуск

1.  **Получите токен бота**:
    *   Напишите @BotFather в Telegram.
    *   Используйте команду `/newbot`, чтобы создать нового бота.
    *   Скопируйте полученный токен.

2.  **Настройка**:
    *   Откройте файл `.env` в этой папке.
    *   Замените `YOUR_TOKEN_HERE` на ваш токен.

3.  **Запуск**:
    *   Откройте терминал в этой папке.
    *   Запустите команду: `node index.js`

## Примечания

*   Бот сохраняет данные о пользователях в файл `database.json`.
*   Бот должен быть администратором в группе, чтобы читать все сообщения (или отключите "Group Privacy" в настройках бота через @BotFather, но лучше просто добавить его).

## AI Профили (новое)

Бот поддерживает профили поведения и пресеты модели через `.env`.

Пример:

```env
AI_BEHAVIOR_PROFILE=legacy_chaos
MODERATION_PROFILE=legacy_chaos

# Можно явно задать модель:
AI_MODEL=mistralai/mistral-small-3.1-24b-instruct

# Или выбрать пресет:
# AI_MODEL_PRESET=balanced_budget
# AI_MODEL_PRESET=speed_budget
# AI_MODEL_PRESET=low_censor_budget
# AI_MODEL_PRESET=low_censor_pro
# AI_MODEL_PRESET=low_censor_alt

AI_FAILSAFE_MODEL=google/gemini-2.5-flash-lite
AI_TEMPERATURE_MAIN=0.72
AI_TEMPERATURE_TOOL=0.60
AI_TEMPERATURE_CONTINUATION=0.55
```
