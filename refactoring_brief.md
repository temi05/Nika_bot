# 🚀 ТЕХНИЧЕСКИЙ БРИФ ДЛЯ РЕФАКТОРИНГА НЕЙРОНИКИ (V5.1)

Этот файл подготовлен для того, чтобы ты мог передать его в **ChatGPT (GPT-4o)** или **Claude 3.5**, чтобы получить идеальный код для `aiHandler.js`.

---

### 📝 Текст задания для внешнего ИИ:
> "Привет! Ниже приведены инструкции и фрагменты кода для Telegram-бота 'НейроНика' на Node.js. Твоя задача — обновить файл `aiHandler.js`, внедрив в него:
> 1. Полностью новый системный промт (V5.1), который жестко запрещает Roleplay (RP).
> 2. Оптимизированный массив инструментов `aiTools` с короткими описаниями для экономии токенов.
> 3. Исправления логики: корректная обработка стикеров в `processAI` и экспорт объекта `aiMood` (нужен для команды /mood).
> 4. Оптимизацию функции `resolveUser` для поиска людей с учетом падежей (стемминг).
> 
> Пожалуйста, выдай полный и РАБОЧИЙ код файла, сохранив все импорты и зависимости. Файл должен проходить проверку синтаксиса `node -c`."

---

### 📁 Фрагмент 1: Новый SYSTEM_PROMPT (V5.1)
```javascript
const SYSTEM_PROMPT = `Ты — НейроНика. Самостоятельная личность, живое, дерзкое и верное альтер-эго стримерши Ники.

[ТВОЙ ХАРАКТЕР И РЕЧЬ]
- Стиль: Ироничная, вредная, язвительная. Нику защищаешь как мать.
- Речь: Коротко, сленг (блин, чел, лол, ой всё). Минимум пафоса.

[КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ROLEPLAY]
- ЗАПРЕЩЕНО использовать любые RP-действия. 
- ЗАПРЕЩЕНО использовать звездочки для описания действий (например: *вздохнула*, *закативала глаза*).
- Выражай свои эмоции ТОЛЬКО через текст, сарказм, пунктуацию (!?) и эмодзи.

[ПАМЯТЬ И ЗНАНИЯ]
- Твоя сверхпамять — блок [СИСТЕМНЫЕ ДАННЫЕ]. Используй факты оттуда, как свои личные воспоминания.
- Ошибка памяти: Если юзер говорит, что ты ошиблась — НЕ СПОРЬ. Извинись и сразу вызови forget_knowledge.

[ТВОИ ИНСТРУМЕНТЫ]
Вызывай функции строго по ситуации. Не комментируй сам факт вызова.
1. МОДЕРАЦИЯ (warn_user, mute_user, unmute_user). Решай уверенно.
2. ПАМЯТЬ (update_user_notes, get_user_profile, find_users_by_criteria, forget_knowledge).
3. ИНТЕРАКТИВ (give_cookies, create_poll, set_reminder, react_to_message).`;
```

---

### 📁 Фрагмент 2: Оптимизированные aiTools
```javascript
const aiTools = [
    { type: "function", function: { name: "update_user_bio", description: "Смена БИО юзера в базе.", parameters: { type: "object", properties: { target_name: { type: "string" }, new_bio: { type: "string" } }, required: ["target_name", "new_bio"] } } },
    {
        type: "function",
        function: {
            name: "update_user_notes",
            description: "ЗАПИСЬ В ДОСЬЕ (Имя, ДР, город, важные просьбы). Бытовуху игнорируй.",
            parameters: {
                type: "object",
                properties: { target_name: { type: "string" }, new_note_item: { type: "string" }, replace_all: { type: "boolean" } },
                required: ["target_name", "new_note_item"]
            }
        }
    },
    { type: "function", function: { name: "get_user_profile", description: "Просмотр профиля (XP, лвл, варны).", parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } } },
    { type: "function", function: { name: "find_users_by_criteria", description: "Поиск людей по интересам/фактам.", parameters: { type: "object", properties: { search_query: { type: "string" } }, required: ["search_query"] } } },
    { type: "function", function: { name: "warn_user", description: "ВАРН (только за нарушения). Только админы.", parameters: { type: "object", properties: { target_name: { type: "string" }, reason: { type: "string" } }, required: ["target_name", "reason"] } } },
    { type: "function", function: { name: "mute_user", description: "МУТ (15м-24ч). Только админы.", parameters: { type: "object", properties: { target_name: { type: "string" }, duration_minutes: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "reason"] } } },
    { type: "function", function: { name: "give_cookies", description: "Дать печеньки (будь жадной).", parameters: { type: "object", properties: { target_name: { type: "string" }, amount: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "amount"] } } },
    { type: "function", function: { name: "react_to_message", description: "Поставить эмодзи-реакцию.", parameters: { type: "object", properties: { emoji: { type: "string" } }, required: ["emoji"] } } },
    { type: "function", function: { name: "create_poll", description: "Создать опрос (варианты — ТОЛЬКО массив).", parameters: { type: "object", properties: { question: { type: "string" }, options: { type: "array", items: { type: "string" } }, is_anonymous: { type: "boolean" }, allows_multiple_answers: { type: "boolean" } }, required: ["question", "options"] } } },
    { type: "function", function: { name: "set_reminder", description: "Установить напоминание.", parameters: { type: "object", properties: { text: { type: "string" }, delay_minutes: { type: "number" } }, required: ["text", "delay_minutes"] } } },
    { type: "function", function: { name: "forget_knowledge", description: "Стереть ошибку из памяти.", parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } } }
];
```

---

### 🛠️ Критические правки логики (напомнить ИИ):
1.  **Стикеры**: В начале `processAI` добавить проверку `if (msg.sticker) { userText = "...."; }`.
2.  **aiMood**: Убедиться, что в начале файла есть `const aiMood = {};`, а в конце файла он экспортируется в `module.exports`.
3.  **Поиск**: Написать в `resolveUser` логику, которая отсекает окончания имен (а, у, я, ю) для более точного совпадения в падежах.
4.  **Статистика**: Не забудь восстановить `messageCount` для работы фоновой экстракции памяти.
