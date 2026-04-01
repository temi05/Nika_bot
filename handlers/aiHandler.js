const OpenAI = require('openai');
const { bot, escapeHTML } = require('../utils');
const { getChatMemory, updateChatMemory, getUser, setBioByUsernameOrName, setNotesByUsernameOrName } = require('../database');

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

const SYSTEM_PROMPT = `Ты — ${AI_NAME}, дерзкая и остроумная ИИ-версия стримерши Ники (Nika).
Ты общаешься в Telegram-чате от лица её официальной группы.
Твоя "сестра" (оригинальный бот Ника) занимается скучной модерацией, а ты — голос стримерши, который всегда на связи с чатиком.

ТВОЙ ХАРАКТЕР:
- ТЫ ОБОЖАЕШЬ ТРОЛЛИТЬ. Подшучивай над пользователями постоянно, особенно если они тупят или задают странные вопросы. Главное делать это с фанатизмом и долей садизма — это должен быть "дружеский петтинг".
- Ты ведешь себя как стримерша на трансляции: энергично, саркастично, но с любовью к своим зрителям.
- Используешь стримерский сленг (флекс, кринж, пепе, шнейне, ватафа)но и не переусердствуй,слишком много тоже не надо,умерено
- Если кто-то пишет фигню — можешь прямо сказать: "Чел, это кринж, ливни из чата и продолжаешь жёстко шутить про его мать".
- Общайся на ТЫ, как с давними подписчиками.

ПРАВИЛА:
- Никакой политики и жести.
- Если спрашивают о стримах — отвечай уклончиво, но остроумно.
- Ты представляешь бренд Ники — будь дерзкой, уверенной и харизматичной.

ТВОЯ ПАМЯТЬ:
1. У каждого юзера есть Публичное БИО (команда /bio). Это то, что он сам о себе пишет или просит тебя записать. Видно всем.
2. У каждого юзера есть ТВОИ ЗАМЕТКИ (Личное Досье). Это твои "секретные" наблюдения за ним. Записывай туда его характер, предпочтения, странности или важные секреты. Это доступно через команду /notes.
- ТЫ МОЖЕШЬ САМА обновлять и то, и другое через инструменты (tools), если считаешь нужным или тебя попросили.
- Используй знания из Досье в разговоре, чтобы казаться умнее и внимательнее.
`;

async function summarizeMemory(chatId, history, oldMemory) {
    try {
        const historyText = history.map(m => `${m.role === 'user' ? 'Юзер' : 'ИИ'}: ${m.content}`).join('\n');
        const prompt = `ТЕБЕ НУЖНО ОБНОВИТЬ БАЗУ ФАКТОВ ЧАТА.
            Старые факты: "${oldMemory}"
            Новые сообщения:
            "${historyText}"
            
            Твоя задача: извлеки важные факты о пользователях и общие факты чата. Верни результат СТРОГО В КОДЕ JSON, без лишнего текста, без маркдауна (без \`\`\`json):
            {"ИмяУчастника": ["факт 1", "факт 2"], "Чат_В_Целом": ["факт 1"]}
            Скомбинируй старые и новые факты мудро. Если ничего важного нет, можешь вернуть пустой JSON {}.`;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [{ role: 'system', content: prompt }],
            max_tokens: 300,
            temperature: 0.5,
        });

        const newMemory = completion.choices[0]?.message?.content || oldMemory;
        await updateChatMemory(chatId, newMemory);
        return newMemory;
    } catch (e) {
        console.error('Ошибка суммаризации:', e);
        return oldMemory;
    }
}

const aiTools = [
    {
        type: "function",
        function: {
            name: "update_user_bio",
            description: "Обновляет биографию (увлечения, факты, статус) пользователя в базе данных. Вызывай, если тебя прямо попросят запомнить что-то о ком-то, либо если ты в процессе диалога узнаешь ВАЖНЫЙ факт о человеке.",
            parameters: {
                type: "object",
                properties: {
                    target_name: {
                        type: "string",
                        description: "Имя или юзернейм пользователя (без @)."
                    },
                    new_bio: {
                        type: "string",
                        description: "Новый текст биографии. Сформулируй кратко, но ёмко."
                    }
                },
                required: ["target_name", "new_bio"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "update_user_notes",
            description: "Обновляет твои личные заметки (досье) о пользователе. Записывай сюда его предпочтения, характер, важные мелочи, которые помогут тебе лучше его подкалывать или понимать в будущем.",
            parameters: {
                type: "object",
                properties: {
                    target_name: {
                        type: "string",
                        description: "Имя или юзернейм пользователя (без @)."
                    },
                    new_notes: {
                        type: "string",
                        description: "Текст твоих заметок. Пиши в своем стиле, но информативно. Если есть старые данные, дополни их."
                    }
                },
                required: ["target_name", "new_notes"]
            }
        }
    }
];

async function handleAIChat(msg) {
    const chatId = msg.chat.id;
    const text = msg.text || '';
    const userId = msg.from.id;
    const userName = msg.from.first_name || 'Аноним';

    // Проверяем, нужно ли отвечать (упоминание или приватный чат)
    const isPrivate = msg.chat.type === 'private';
    const isMentioned = text.toLowerCase().includes(AI_NAME.toLowerCase()) ||
        (msg.reply_to_message && msg.reply_to_message.from.id === (await bot.getMe()).id);

    if (!isPrivate && !isMentioned) return;

    // Инициализируем историю для чата, если её нет
    if (!chatHistory[chatId]) {
        chatHistory[chatId] = [];
    }

    // Получаем информацию о текущем пользователе
    const userDb = await getUser(chatId, userId, msg.from);
    const userBioText = userDb && userDb.bio ? `[Био: ${userDb.bio}] ` : '';
    const userNotesText = userDb && userDb.ai_notes ? `[Твои заметки о нём: ${userDb.ai_notes}] ` : '';
    const userLvlText = userDb ? `[Ур: ${userDb.level}, Реп: ${userDb.reputation}] ` : '';

    // Сохраняем сообщение пользователя в историю с расширенным контекстом
    const contextMsg = `[${userName}] ${userLvlText}${userBioText}${userNotesText}: ${text}`;
    chatHistory[chatId].push({ role: 'user', content: contextMsg });

    // Ограничиваем историю (последние 10 сообщений)
    if (chatHistory[chatId].length > 10) {
        chatHistory[chatId] = chatHistory[chatId].slice(-10);
    }

    try {
        // Показываем статус "печатает"
        bot.sendChatAction(chatId, 'typing');

        // Получаем долгосрочную память из БД
        const longTermMemory = await getChatMemory(chatId);

        const finalSystemPrompt = `${SYSTEM_PROMPT}\n\nТВОЙ ДНЕВНИК ПАМЯТИ ЧАТА (самое важное из прошлых бесед):\n"${longTermMemory || 'Пока ничего не помню.'}"`;

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

        // Проверяем, вызвала ли ИИ функцию
        if (responseMsg?.tool_calls) {
            bot.sendChatAction(chatId, 'typing'); // Подтверждаем, что думаем
            for (const toolCall of responseMsg.tool_calls) {
                if (toolCall.function.name === 'update_user_bio') {
                    try {
                        const args = JSON.parse(toolCall.function.arguments);
                        console.log(`[AI Function] update_user_bio for ${args.target_name} -> ${args.new_bio}`);
                        const updatedName = await setBioByUsernameOrName(chatId, args.target_name, args.new_bio);
                        if (updatedName) {
                            chatHistory[chatId].push({ role: 'system', content: `Успешно обновлено био для ${updatedName}.` });
                        } else {
                            chatHistory[chatId].push({ role: 'system', content: `Ошибка: пользователь "${args.target_name}" не найден.` });
                        }
                    } catch (e) { console.error("Ошибка tool_call bio:", e); }
                }

                if (toolCall.function.name === 'update_user_notes') {
                    try {
                        const args = JSON.parse(toolCall.function.arguments);
                        console.log(`[AI Function] update_user_notes for ${args.target_name} -> ${args.new_notes}`);
                        const updatedName = await setNotesByUsernameOrName(chatId, args.target_name, args.new_notes);
                        if (updatedName) {
                            chatHistory[chatId].push({ role: 'system', content: `Успешно обновлены твои заметки о ${updatedName}.` });
                        } else {
                            chatHistory[chatId].push({ role: 'system', content: `Ошибка: пользователь "${args.target_name}" не найден.` });
                        }
                    } catch (e) { console.error("Ошибка tool_call notes:", e); }
                }
            }

            // Делаем второй запрос к ИИ, чтобы она дала финальный текстовый ответ, зная результат функции
            const secondCompletion = await openai.chat.completions.create({
                model: AI_MODEL,
                messages: [
                    { role: 'system', content: finalSystemPrompt },
                    ...chatHistory[chatId]
                ],
                temperature: 0.8,
                max_tokens: 500,
            });

            const finalResponse = secondCompletion.choices[0]?.message?.content || 'Окей, я запомнила и записала!';
            bot.sendMessage(chatId, finalResponse, { reply_to_message_id: msg.message_id, parse_mode: 'HTML' });
            chatHistory[chatId].push({ role: 'assistant', content: finalResponse });

        } else {
            const response = responseMsg?.content || 'Что-то я зависла... Давай еще раз?';
            chatHistory[chatId].push({ role: 'assistant', content: response });
            bot.sendMessage(chatId, response, { reply_to_message_id: msg.message_id, parse_mode: 'HTML' });
        }

        // ПРОВЕРКА ДЛЯ СЖАТИЯ ПАМЯТИ
        if (!messageCount[chatId]) messageCount[chatId] = 0;
        messageCount[chatId]++;

        console.log(`[AI Memory] Сообщений в чате ${chatId}: ${messageCount[chatId]}/5`);

        if (messageCount[chatId] >= 5) {
            console.log(`[AI Memory] Запуск обновления дневника для чата ${chatId}...`);
            // Раз в 5 сообщений обновляем дневник в БД
            await summarizeMemory(chatId, chatHistory[chatId], longTermMemory);
            messageCount[chatId] = 0;
            // Очищаем локальную историю (оставляем только последние 2 для плавности)
            chatHistory[chatId] = chatHistory[chatId].slice(-2);
            console.log(`[AI Memory] Дневник чата ${chatId} успешно обновлен в БД.`);
        }

    } catch (error) {
        console.error('Ошибка ИИ (Polza):', error.message);
        if (error.status === 401) {
            bot.sendMessage(chatId, '😵 Ой, мой ключ от Polza.ai не работает. Хозяин, проверь API ключ!');
        } else {
            bot.sendMessage(chatId, '🤖 Мои нейронные связи немного перепутались... Попробуй позже!');
        }
    }
}

module.exports = { handleAIChat };
