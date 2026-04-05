# Пакет оптимизации №2 (Память и Инструменты)

Этот файл содержит исходный код критических узлов бота для передачи в **ChatGPT**, **Claude** или другую мощную модель. 

---

### ЗАПРОС №1: Оптимизация экстрактора памяти (vectorMemory.js)
**Цель:** Сделать извлечение фактов более точным, чтобы бот не записывал мусор и не галлюцинировал.

**Что отправить в чат с ИИ:**
> "Ниже промт для Node.js функции, которая извлекает LONG-TERM факты из диалога в группе. Оптимизируй его: сделай более строгим, добавь правила классификации (важное vs бытовое) и убедись, что формат ответа всегда идеальный JSON. Бот — НейроНика, дерзкая стримерша."

**Код для анализа:**
```javascript
const prompt = "Ты — эксперт по верификации данных. Твоя задача — извлечь из диалога ДОЛГОСРОЧНЫЕ факты об участниках чата.\n\n" +
    "[КРИТИЧЕСКИЕ ПРАВИЛА ИЗВЛЕЧЕНИЯ (Memory v4.2)]\n" +
    "1. СУБЪЕКТЫ: Каждый факт ДОЛЖЕН начинаться с Имени (в точности как в чате). НИКАКИХ 'Он', 'Она', 'Бот'.\n" +
    "2. ФОТО VS ПРЕДПОЧТЕНИЯ (ВАЖНО): Если в тексте есть пометка '(Ника видит на фото: ...)', записывай это ТОЛЬКО как внешнее наблюдение ('Имя: на фото был(а) с синими волосами').\n" +
    "3. ЗАПРЕТ ГАЛЛЮЦИНАЦИЙ: КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО интерпретировать визуальные детали как 'любит' или 'предпочитает'. Если на фото девушка в кожанке, НЕ ПИШИ 'Пользователь любит кожанки'. Пиши только то, что видишь.\n" +
    "4. ПРЕДПОЧТЕНИЯ: Записывай 'любит/хочет/интересуется' ТОЛЬКО если пользователь ЯВНО сказал об этом текстом (например: 'Я обожаю аниме').\n" +
    "5. ФОРМАТ: 'Имя: [Факт]'.\n\n" + participantInfo + "\n\nДиалог для анализа:\n" + historyText;
```

---

### ЗАПРОС №2: Оптимизация JSON-описаний инструментов (aiTools)
**Цель:** Сократить длину описаний (экономия токенов) и сделать их "понятнее" для модели Gemini 2.0 Flash Lite.

**Что отправить в чат с ИИ:**
> "У меня есть массив JSON-схем функций для ИИ-бота. Модель Gemini иногда путается, когда вызывать ту или иную функцию. Оптимизируй поле 'description' для каждой функции: оно должно быть коротким, но содержать ключевые триггеры (когда именно вызывать)."

**Код для анализа:**
```javascript
const aiTools = [
    { type: "function", function: { name: "update_user_bio", description: "Сменить био юзера.", parameters: { type: "object", properties: { target_name: { type: "string" }, new_bio: { type: "string" } }, required: ["target_name", "new_bio"] } } },
    {
        type: "function",
        function: {
            name: "update_user_notes",
            description: "ЗАПИСЬ В ДОСЬЕ (Имя, ДР, город, важные просьбы). Бытовуху игнорируй.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string" },
                    new_note_item: { type: "string" },
                    replace_all: { type: "boolean" }
                },
                required: ["target_name", "new_note_item"]
            }
        }
    },
    { type: "function", function: { name: "get_user_profile", description: "Профиль юзера (XP, лвл, варны).", parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } } },
    { type: "function", function: { name: "find_users_by_criteria", description: "Поиск людей по интересам или фактам.", parameters: { type: "object", properties: { search_query: { type: "string" } }, required: ["search_query"] } } },
    { type: "function", function: { name: "warn_user", description: "Варн за нарушения (только админы).", parameters: { type: "object", properties: { target_name: { type: "string" }, reason: { type: "string" } }, required: ["target_name", "reason"] } } },
    { type: "function", function: { name: "mute_user", description: "Мут (15м-24ч) (только админы).", parameters: { type: "object", properties: { target_name: { type: "string" }, duration_minutes: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "reason"] } } },
    { type: "function", function: { name: "give_cookies", description: "Дать печеньки (будь жадной!).", parameters: { type: "object", properties: { target_name: { type: "string" }, amount: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "amount"] } } },
    { type: "function", function: { name: "react_to_message", description: "Поставить реакцию.", parameters: { type: "object", properties: { emoji: { type: "string" } }, required: ["emoji"] } } },
    { type: "function", function: { name: "create_poll", description: "Создать опрос (варианты только массивом!).", parameters: { type: "object", properties: { question: { type: "string" }, options: { type: "array", items: { type: "string" } }, is_anonymous: { type: "boolean" }, allows_multiple_answers: { type: "boolean" } }, required: ["question", "options"] } } },
    { type: "function", function: { name: "set_reminder", description: "Напоминалка.", parameters: { type: "object", properties: { text: { type: "string" }, delay_minutes: { type: "number" } }, required: ["text", "delay_minutes"] } } },
    { type: "function", function: { name: "forget_knowledge", description: "Стереть ошибку в факте.", parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] } } }
];
```
