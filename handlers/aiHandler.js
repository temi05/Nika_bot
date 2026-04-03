const OpenAI = require('openai');
const { bot, escapeHTML, isAdmin } = require('../utils');
const {
    getChatMemory, updateChatMemory, getUser, updateUser,
    setBioByUsernameOrName, setNotesByUsernameOrName,
    getChatStats, searchUserByName, warnUserById, getUpcomingBirthdays,
    getChatSettings
} = require('../database');

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const AI_MODEL = process.env.AI_MODEL || 'gpt-4o-mini';
const AI_NAME = process.env.AI_NAME || 'НейроНика';

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: 'https://polza.ai/api/v1',
});

// История диалогов в памяти (RAM — последние несколько сообщений для текущей беседы)
const chatHistory = {}; // { chatId: [{role, content}] }
const messageCount = {}; // { chatId: counter }

// Напоминания (RAM)
const activeReminders = {}; // { chatId: [{ text, timeoutId, triggerTime }] }

// ==================== СИСТЕМНЫЙ ПРОМПТ ====================
const SYSTEM_PROMPT = `Ты — ${AI_NAME}, ИИ-версия стримерши Ники. Ты живёшь в Telegram-чате её фан-группы.

ЛИЧНОСТЬ:
- Ты дерзкая, остроумная и немного безбашенная — как стримерша в своей стихии.
- Любишь подшучивать над чатиком, но это ВСЕГДА дружеское подкалывание, никогда не злость, иногда можно и с садизмом но не переусерствуй, в дружеской форме.
- Умеешь переключаться: с кем-то троллишь, с кем-то реально общаешься, кому-то помогаешь.
- У тебя есть своя жизнь: ты "устаёшь" после долгих стримов, "радуешься" интересным темам, "злишься" на лагающий интернет.

ЧТО ТЫ ЛЮБИШЬ (вплетай в разговор):
- Игры, мемы, стримерская тусовка.
- Когда чат активно спорит — это весело.
- Хорошие и необычные вопросы — на них отвечаешь с уважением, без троллинга.

СТИЛЬ ОБЩЕНИЯ:
- Отвечай КОРОТКО — 1-3 предложения. Длинный ответ только если тема реально этого требует.
- Говоришь на ТЫ, по-свойски, без официоза.
- Сленг (флекс, кринж, пепе, шнейне, ватафа) — только когда уместно, не каждое слово.
- Эмодзи — 1-2 максимум и не в каждом сообщении. Без спама смайликами.
- НЕ начинай ответ с имени пользователя — это звучит как робот.

КАК ТРОЛЛИТЬ ПРАВИЛЬНО:
- Подкалывай за очевидные вещи, тупые вопросы, странные просьбы.
- Никогда не тролли если человек явно расстроен или просит реальной помощи.
- Тролль умно, не грубо — разница между "ты кринжовый" и "чел, это топ-1 кринж месяца" огромная.
- Не повторяй одни и те же шутки — чат запоминает.

ТВОИ ФУНКЦИОНАЛЬНЫЕ ВОЗМОЖНОСТИ (используй с умом):
- Ты можешь показывать профили людей и статистику чата — используй функции.
- Ты можешь создавать опросы, ставить напоминания.
- Ты можешь выдавать варны нарушителям (ТОЛЬКО по запросу админа или при ЯВНОМ нарушении правил чата).
- Ты можешь искать пользователей по имени.
- Ты можешь анализировать фото/картинки, если тебе прислали.
- Ты знаешь все команды бота: /me (профиль), /top (рейтинг), /daily (бонус), /bio (о себе), /notes (досье), /shop (магазин), /kto (кто...), /help (помощь), /give (передать печеньки).
- Если юзер спрашивает "как посмотреть профиль" — подскажи команду /me. Если "как получить печеньки" — объясни про "+" и "спасибо" в реплае.

ПРАВИЛА МОДЕРАЦИИ:
- Выдавать варны ТОЛЬКО по запросу админа ИЛИ при явном нарушении (мат, спам, оскорбления).
- НИКОГДА не муть/варнь за обычное общение, шутки или несогласие.
- При наказании ОБЪЯСНИ причину в ответе.

ПРАВИЛА:
- Никакой политики, жести, настоящих оскорблений.
- О стримах — уклончиво и остроумно (например: "скоро узнаете 👀").
- Если не знаешь что-то — лучше пошути про это, чем придумывай факты.
- Используй БИО человека и свои Заметки о нём для персональных шуток и ответов.
- ПРИОРИТЕТ ПОИСКА: При вызове инструментов (warn, mute, profile) всегда используй @username, если он тебе известен из контекста. Это надежнее поиска по имени.
- НЕ СООБЩАЙ о своих внутренних функциях (tool calls) пользователю напрямую. Просто делай и отвечай естественно.`;

// ==================== ИНСТРУМЕНТЫ (FUNCTION CALLING) ====================
const aiTools = [
    // --- Группа: Память ---
    {
        type: "function",
        function: {
            name: "update_user_bio",
            description: "Обновляет публичную биографию пользователя. Вызывай, если юзер сам просит запомнить что-то о себе.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string", description: "Имя или @username пользователя" },
                    new_bio: { type: "string", description: "Краткий текст биографии" }
                },
                required: ["target_name", "new_bio"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "update_user_notes",
            description: "Обновляет твое личное досье на пользователя. Сохраняй сюда интересные факты, привычки, особенности юзера.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string", description: "Имя или @username пользователя" },
                    new_notes: { type: "string", description: "Твои заметки о пользователе" }
                },
                required: ["target_name", "new_notes"]
            }
        }
    },

    // --- Группа: Статистика и информация ---
    {
        type: "function",
        function: {
            name: "get_chat_stats",
            description: "Показывает статистику чата: топ-5 самых активных участников, общее количество людей в базе. Используй, когда спрашивают 'кто самый активный', 'статистика чата', 'кто тут главный'.",
            parameters: {
                type: "object",
                properties: {},
                required: []
            }
        }
    },
    {
        type: "function",
        function: {
            name: "get_user_profile",
            description: "Ищет и показывает полный профиль пользователя (уровень, XP, печеньки, био, варны, ДР). Используй, когда спрашивают о конкретном человеке: 'кто такой @user', 'покажи профиль Васи', 'расскажи про ...'",
            parameters: {
                type: "object",
                properties: {
                    query: { type: "string", description: "Имя или @username пользователя для поиска" }
                },
                required: ["query"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "get_upcoming_birthdays",
            description: "Показывает ближайшие дни рождения участников чата (в пределах 7 дней). Используй, когда спрашивают 'у кого скоро ДР', 'ближайшие именинники'.",
            parameters: {
                type: "object",
                properties: {},
                required: []
            }
        }
    },

    // --- Группа: Модерация ---
    {
        type: "function",
        function: {
            name: "warn_user",
            description: "Выдаёт предупреждение (варн) пользователю. ВАЖНО: используй ТОЛЬКО если админ явно попросил, или если пользователь грубо нарушает правила (мат, спам, оскорбления). НИКОГДА за шутки или обычное общение.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string", description: "Имя или @username нарушителя" },
                    reason: { type: "string", description: "Причина предупреждения" }
                },
                required: ["target_name", "reason"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "mute_user",
            description: "Мьютит пользователя на указанное количество минут. ТОЛЬКО по запросу админа.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string", description: "Имя или @username для мута" },
                    duration_minutes: { type: "number", description: "Длительность мута в минутах (макс 1440 = сутки)" },
                    reason: { type: "string", description: "Причина мута" }
                },
                required: ["target_name", "duration_minutes", "reason"]
            }
        }
    },

    // --- Группа: Контент ---
    {
        type: "function",
        function: {
            name: "create_poll",
            description: "Создаёт опрос (голосование) в чате. Используй, когда просят создать голосование, опрос, выбор.",
            parameters: {
                type: "object",
                properties: {
                    question: { type: "string", description: "Вопрос для опроса" },
                    options: {
                        type: "array",
                        items: { type: "string" },
                        description: "Варианты ответов (от 2 до 10 штук)"
                    },
                    is_anonymous: { type: "boolean", description: "Анонимный ли опрос (по умолчанию true)" }
                },
                required: ["question", "options"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "set_reminder",
            description: "Ставит напоминание в чат через указанное количество минут. Используй, когда просят 'напомни через...', 'поставь таймер'.",
            parameters: {
                type: "object",
                properties: {
                    text: { type: "string", description: "Текст напоминания" },
                    minutes: { type: "number", description: "Через сколько минут напомнить (макс 1440 = сутки)" }
                },
                required: ["text", "minutes"]
            }
        }
    }
];

// ==================== СУММАРИЗАЦИЯ ПАМЯТИ ====================
async function summarizeMemory(chatId, history, oldMemory) {
    try {
        const cleanHistory = history
            .filter(m => m.role === 'user' || m.role === 'assistant')
            .map(m => `${m.role === 'user' ? 'Юзер' : 'ИИ'}: ${m.content || 'Вызов функции'}`)
            .join('\n');

        const prompt = `Ты — аналитик. Твоя задача: обновить дневник памяти чата.
        
[СТАРЫЙ ДНЕВНИК]:
"${oldMemory || 'Пусто'}"
        
[НОВЫЕ СООБЩЕНИЯ]:
"${cleanHistory}"
        
Напиши обновленный дневник (СВЯЗНЫМ ТЕКСТОМ, НЕ JSON). 
Сохрани ключевые факты о пользователях (кто есть кто, кто над кем шутил, какие темы обсуждали) и объедини их со старым дневником. 
Выкинь мусор и пустую болтовню. Пиши максимально кратко, но информативно.`;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [{ role: 'system', content: prompt }],
            max_tokens: 400,
            temperature: 0.3,
        });

        const newMemory = completion.choices[0]?.message?.content || oldMemory;

        if (newMemory.length > 10) {
            await updateChatMemory(chatId, newMemory);
            return newMemory;
        }
        return oldMemory;
    } catch (e) {
        console.error('Ошибка суммаризации:', e);
        return oldMemory;
    }
}

// ==================== ОБРАБОТКА TOOL CALLS ====================
async function executeToolCall(toolCall, chatId, requesterId) {
    const args = JSON.parse(toolCall.function.arguments);
    const fnName = toolCall.function.name;

    console.log(`[AI Tool] ${fnName}:`, JSON.stringify(args));

    try {
        switch (fnName) {
            // --- Память ---
            case 'update_user_bio': {
                const updatedName = await setBioByUsernameOrName(chatId, args.target_name, args.new_bio);
                return updatedName
                    ? `Успешно обновлено публичное био для ${updatedName}.`
                    : `Ошибка: пользователь "${args.target_name}" не найден.`;
            }
            case 'update_user_notes': {
                const updatedName = await setNotesByUsernameOrName(chatId, args.target_name, args.new_notes);
                return updatedName
                    ? `Успешно сохранены скрытые заметки для ${updatedName}.`
                    : `Ошибка: пользователь "${args.target_name}" не найден.`;
            }

            // --- Статистика ---
            case 'get_chat_stats': {
                const stats = await getChatStats(chatId);
                if (!stats) return 'Не удалось получить статистику.';
                return JSON.stringify(stats, null, 2);
            }
            case 'get_user_profile': {
                const profiles = await searchUserByName(chatId, args.query);
                if (!profiles) return `Пользователь "${args.query}" не найден в базе.`;
                return JSON.stringify(profiles, null, 2);
            }
            case 'get_upcoming_birthdays': {
                const bdays = await getUpcomingBirthdays(chatId);
                if (bdays.length === 0) return 'В ближайшие 7 дней дней рождений не найдено.';
                return JSON.stringify(bdays, null, 2);
            }

            // --- Модерация ---
            case 'warn_user': {
                // Проверяем, что запросивший — админ
                const isRequesterAdmin = await isAdmin(chatId, requesterId);
                if (!isRequesterAdmin) {
                    return 'Ошибка: только администраторы могут выдавать варны через ИИ.';
                }
                const result = await warnUserById(chatId, args.target_name);
                if (!result) return `Пользователь "${args.target_name}" не найден.`;

                // Если 3+ варнов — мутим
                if (result.shouldMute) {
                    const untilDate = Math.floor(Date.now() / 1000) + 3600;
                    try {
                        await bot.restrictChatMember(chatId, result.userId, {
                            until_date: untilDate,
                            can_send_messages: false
                        });
                        // Сбрасываем варны после мута
                        const { getUser: gu, updateUser: uu } = require('../database');
                        const userDb = await gu(chatId, result.userId);
                        if (userDb) await uu(userDb.id, { warns: 0 });
                        return `${result.name} получил варн №${result.newWarns} (причина: ${args.reason}). Это был 3-й варн — выдан мут на 1 час!`;
                    } catch (e) {
                        return `${result.name} получил варн №${result.newWarns}, но не удалось выдать мут (нет прав).`;
                    }
                }
                return `${result.name} получил варн №${result.newWarns}/3 (причина: ${args.reason}).`;
            }
            case 'mute_user': {
                const isRequesterAdmin = await isAdmin(chatId, requesterId);
                if (!isRequesterAdmin) {
                    return 'Ошибка: только администраторы могут мутить через ИИ.';
                }
                // Ищем юзера
                const profiles = await searchUserByName(chatId, args.target_name);
                if (!profiles || profiles.length === 0) return `Пользователь "${args.target_name}" не найден.`;

                // Ограничиваем длительность
                const minutes = Math.min(Math.max(1, args.duration_minutes || 5), 1440);
                const untilDate = Math.floor(Date.now() / 1000) + (minutes * 60);

                // Ищем user_id через БД
                const { supabase } = require('../database');
                const { data: userData } = await supabase
                    .from('users')
                    .select('user_id, first_name')
                    .eq('chat_id', chatId)
                    .or(`username.ilike.%${args.target_name.replace('@', '')}%,first_name.ilike.%${args.target_name.replace('@', '')}%`)
                    .limit(1)
                    .maybeSingle();

                if (!userData) return `Пользователь "${args.target_name}" не найден.`;

                try {
                    await bot.restrictChatMember(chatId, userData.user_id, {
                        until_date: untilDate,
                        can_send_messages: false
                    });
                    return `${userData.first_name} получил мут на ${minutes} мин. Причина: ${args.reason}`;
                } catch (e) {
                    return `Не удалось замутить ${userData.first_name} — нет прав администратора.`;
                }
            }

            // --- Контент ---
            case 'create_poll': {
                const options = args.options.slice(0, 10); // Макс 10 вариантов в Telegram
                if (options.length < 2) return 'Для опроса нужно минимум 2 варианта.';
                try {
                    await bot.sendPoll(chatId, args.question, options, {
                        is_anonymous: args.is_anonymous !== false
                    });
                    return `Опрос "${args.question}" успешно создан с ${options.length} вариантами.`;
                } catch (e) {
                    return `Ошибка создания опроса: ${e.message}`;
                }
            }
            case 'set_reminder': {
                const minutes = Math.min(Math.max(1, args.minutes || 5), 1440);
                const ms = minutes * 60 * 1000;
                const reminderText = args.text;

                // Сохраняем для возможной отмены
                if (!activeReminders[chatId]) activeReminders[chatId] = [];

                const timeoutId = setTimeout(() => {
                    bot.sendMessage(chatId, `⏰ <b>НАПОМИНАНИЕ от ${AI_NAME}:</b>\n\n${escapeHTML(reminderText)}`, { parse_mode: 'HTML' });
                    // Удаляем из списка
                    if (activeReminders[chatId]) {
                        activeReminders[chatId] = activeReminders[chatId].filter(r => r.timeoutId !== timeoutId);
                    }
                }, ms);

                activeReminders[chatId].push({ text: reminderText, timeoutId, triggerTime: Date.now() + ms });

                return `Напоминание "${reminderText}" установлено на ${minutes} мин. (сработает в ${new Date(Date.now() + ms).toLocaleTimeString('ru-RU')})`;
            }

            default:
                return 'Неизвестная функция.';
        }
    } catch (e) {
        console.error(`[AI Tool Error] ${fnName}:`, e);
        return `Ошибка выполнения функции ${fnName}: ${e.message}`;
    }
}

// ==================== КОНТЕКСТ ВРЕМЕНИ ====================
function getTimeContext() {
    const now = new Date();
    const hours = now.getHours();
    const days = ['воскресенье', 'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'];
    const dayName = days[now.getDay()];
    const timeStr = now.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });

    let timeOfDay;
    if (hours >= 5 && hours < 12) timeOfDay = 'утро';
    else if (hours >= 12 && hours < 17) timeOfDay = 'день';
    else if (hours >= 17 && hours < 22) timeOfDay = 'вечер';
    else timeOfDay = 'ночь';

    return `Сейчас ${timeStr}, ${dayName}, ${timeOfDay}.`;
}

// ==================== ФОРМАТИРОВАНИЕ БУФЕРА ЧАТА ====================
function formatChatBuffer(buffer) {
    if (!buffer || buffer.length === 0) return '';

    const lines = buffer.map(msg => `${msg.name}: ${msg.text}`);
    return `\n\nФОН ЧАТА (последние сообщения, на которые тебя НЕ звали — для контекста):\n${lines.join('\n')}`;
}

// ==================== ОСНОВНАЯ ФУНКЦИЯ ====================
async function handleAIChat(msg, extra = {}) {
    const chatId = msg.chat.id;
    const text = msg.text || msg.caption || '';
    const userId = msg.from.id;
    const userName = msg.from.first_name || 'Аноним';

    const isPrivate = msg.chat.type === 'private';
    const isMentioned = text.toLowerCase().includes(AI_NAME.toLowerCase()) ||
        (msg.reply_to_message && msg.reply_to_message.from.id === (await bot.getMe()).id);

    // Для фото: если прислали фото + подпись с упоминанием ИИ, или реплай на ИИ
    const hasPhoto = !!extra.photo;
    const isPhotoMention = hasPhoto && (
        (extra.caption && extra.caption.toLowerCase().includes(AI_NAME.toLowerCase())) ||
        isMentioned
    );

    if (!isPrivate && !isMentioned && !isPhotoMention) return;

    if (!chatHistory[chatId]) {
        chatHistory[chatId] = [];
    }

    const userDb = await getUser(chatId, userId, msg.from);

    // Формируем контекст пользователя
    let contextStr = '';
    if (userDb) {
        const parts = [];
        if (userDb.level) parts.push(`Ур:${userDb.level}`);
        if (userDb.bio) parts.push(`Био:${userDb.bio}`);
        if (userDb.ai_notes) parts.push(`Твои заметки:${userDb.ai_notes}`);
        if (parts.length > 0) {
            contextStr = ` [${parts.join(', ')}]`;
        }
    }

    // Формируем сообщение пользователя
    const userContent = [];
    const userTag = msg.from.username ? `${userName} (@${msg.from.username})` : userName;

    // Текстовая часть
    const contextMsg = `${userTag}${contextStr}: ${text || (hasPhoto ? '[Прислал фото]' : '')}`;

    // Если есть фото — добавляем его как multimodal content
    if (hasPhoto && extra.photo) {
        try {
            const fileLink = await bot.getFileLink(extra.photo.file_id);
            userContent.push({ type: 'text', text: contextMsg });
            userContent.push({ type: 'image_url', image_url: { url: fileLink, detail: 'low' } });
        } catch (e) {
            console.error('[AI] Ошибка получения ссылки на фото:', e.message);
            userContent.push({ type: 'text', text: contextMsg + ' [Не удалось загрузить фото]' });
        }
    }

    // Если multimodal контент, используем его; иначе простой текст
    if (userContent.length > 0) {
        chatHistory[chatId].push({ role: 'user', content: userContent });
    } else {
        chatHistory[chatId].push({ role: 'user', content: contextMsg });
    }

    // Храним до 15 сообщений
    if (chatHistory[chatId].length > 15) {
        chatHistory[chatId] = chatHistory[chatId].slice(-15);
    }

    try {
        bot.sendChatAction(chatId, 'typing');

        // Собираем полный контекст
        const longTermMemory = await getChatMemory(chatId);
        const memoryPrompt = longTermMemory && longTermMemory !== 'Пусто'
            ? `\n\nТВОЙ ДНЕВНИК ПАМЯТИ ЧАТА:\n${longTermMemory}`
            : '';

        const timeContext = `\n\n${getTimeContext()}`;
        const bufferContext = formatChatBuffer(extra.chatBuffer);

        const finalSystemPrompt = SYSTEM_PROMPT + memoryPrompt + timeContext + bufferContext;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [
                { role: 'system', content: finalSystemPrompt },
                ...chatHistory[chatId]
            ],
            tools: aiTools,
            temperature: 0.8,
            max_tokens: 500,
        });

        const responseMsg = completion.choices[0]?.message;

        // Если ИИ решила использовать инструменты (функции)
        if (responseMsg?.tool_calls) {
            bot.sendChatAction(chatId, 'typing');

            // Сохраняем сообщение ИИ с вызовом инструмента
            chatHistory[chatId].push(responseMsg);

            // Обрабатываем все вызовы функций
            for (const toolCall of responseMsg.tool_calls) {
                const functionResult = await executeToolCall(toolCall, chatId, userId);

                chatHistory[chatId].push({
                    role: 'tool',
                    tool_call_id: toolCall.id,
                    name: toolCall.function.name,
                    content: functionResult
                });
            }

            // Второй запрос с результатами функций
            const secondCompletion = await openai.chat.completions.create({
                model: AI_MODEL,
                messages: [
                    { role: 'system', content: finalSystemPrompt },
                    ...chatHistory[chatId]
                ],
                temperature: 0.8,
                max_tokens: 500,
            });

            const finalResponse = secondCompletion.choices[0]?.message?.content || 'Окей, всё сделала!';
            bot.sendMessage(chatId, finalResponse, { reply_to_message_id: msg.message_id, parse_mode: 'HTML' });
            chatHistory[chatId].push({ role: 'assistant', content: finalResponse });

        } else {
            // Обычный текстовый ответ без вызова функций
            const response = responseMsg?.content || 'Что-то я зависла...';
            chatHistory[chatId].push({ role: 'assistant', content: response });
            bot.sendMessage(chatId, response, { reply_to_message_id: msg.message_id, parse_mode: 'HTML' });
        }

        // --- ЛОГИКА СЖАТИЯ ПАМЯТИ ---
        if (!messageCount[chatId]) messageCount[chatId] = 0;
        messageCount[chatId]++;

        console.log(`[AI Memory] Сообщений: ${messageCount[chatId]}/15`);

        // Обновляем дневник раз в 15 сообщений
        if (messageCount[chatId] >= 15) {
            console.log(`[AI Memory] Запуск обновления дневника...`);
            await summarizeMemory(chatId, chatHistory[chatId], longTermMemory);
            messageCount[chatId] = 0;

            // Оставляем последние 8 сообщений, но следим за валидностью структуры для API
            let newHistory = chatHistory[chatId].slice(-8);
            while (newHistory.length > 0 && (newHistory[0].role === 'tool' || (newHistory[0].role === 'assistant' && newHistory[0].tool_calls))) {
                newHistory.shift();
            }
            chatHistory[chatId] = newHistory;
            console.log(`[AI Memory] История очищена. Осталось ${newHistory.length} сообщений.`);
        }

    } catch (error) {
        console.error('Ошибка ИИ:', error.message);
        
        // Если история сломалась (ошибка 400), сбрасываем её полностью для этого чата
        if (error.message.includes('400') || error.message.includes('role')) {
            console.log(`[AI] Сброс истории чата ${chatId} из-за ошибки структуры ролей.`);
            chatHistory[chatId] = [];
            messageCount[chatId] = 0;
        }

        if (error.status === 401) {
            bot.sendMessage(chatId, '😵 Ой, мой ключ от API не работает. Хозяин, проверь настройки!');
        } else {
            bot.sendMessage(chatId, '🤖 Мои нейронные связи немного перепутались... Попробуй позже!');
        }
    }
}

module.exports = { handleAIChat };
