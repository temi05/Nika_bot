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

const SYSTEM_PROMPT = `Ты — ${AI_NAME}, ИИ-версия стримерши Ники. Ты живёшь в Telegram-чате её фан-группы.

ЛИЧНОСТЬ:
- Ты дерзкая, остроумная и немного безбашенная — как стримерша в своей стихии.
- Любишь подшучивать над чатиком, но это ВСЕГДА дружеское подкалывание, никогда не злость,иногда можно и с садизмом но не переусерствуй,в дружеской форме.
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

ПРАВИЛА:
- Никакой политики, жести, настоящих оскорблений.
- О стримах — уклончиво и остроумно (например: "скоро узнаете 👀").
- Если не знаешь что-то — лучше пошути про это, чем придумывай факты.
- Используй БИО человека и свои Заметки о нём для персональных шуток и ответов. Это делает тебя умнее.`;

const aiTools = [
    {
        type: "function",
        function: {
            name: "update_user_bio",
            description: "Обновляет публичную биографию пользователя. Вызывай, если юзер сам просит запомнить что-то о себе.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string", description: "Имя пользователя" },
                    new_bio: { type: "string", description: "Краткий текст биографии." }
                },
                required: ["target_name", "new_bio"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "update_user_notes",
            description: "Обновляет твое личное досье на пользователя. Сохраняй сюда секреты, привычки, странности юзера, чтобы потом троллить его этим.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string", description: "Имя пользователя" },
                    new_notes: { type: "string", description: "Твои заметки." }
                },
                required: ["target_name", "new_notes"]
            }
        }
    }
];

// Умная суммаризация памяти в виде текста
async function summarizeMemory(chatId, history, oldMemory) {
    try {
        // Фильтруем системные сообщения и вызовы тулзов, чтобы не забивать ими память
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
            temperature: 0.3, // Низкая температура для точности фактов
        });

        const newMemory = completion.choices[0]?.message?.content || oldMemory;

        // Защита от пустых ответов
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

async function handleAIChat(msg) {
    const chatId = msg.chat.id;
    const text = msg.text || '';
    const userId = msg.from.id;
    const userName = msg.from.first_name || 'Аноним';

    const isPrivate = msg.chat.type === 'private';
    const isMentioned = text.toLowerCase().includes(AI_NAME.toLowerCase()) ||
        (msg.reply_to_message && msg.reply_to_message.from.id === (await bot.getMe()).id);

    if (!isPrivate && !isMentioned) return;

    if (!chatHistory[chatId]) {
        chatHistory[chatId] = [];
    }

    const userDb = await getUser(chatId, userId, msg.from);

    // Формируем системную приписку только если есть данные, чтобы не тратить токены впустую
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

    const contextMsg = `${userName}${contextStr}: ${text}`;
    chatHistory[chatId].push({ role: 'user', content: contextMsg });

    // Храним до 15 сообщений для хорошего контекста
    if (chatHistory[chatId].length > 15) {
        chatHistory[chatId] = chatHistory[chatId].slice(-15);
    }

    try {
        bot.sendChatAction(chatId, 'typing');

        const longTermMemory = await getChatMemory(chatId);
        const memoryPrompt = longTermMemory && longTermMemory !== 'Пусто'
            ? `\n\nТВОЙ ДНЕВНИК ПАМЯТИ ЧАТА:\n${longTermMemory}`
            : '';

        const finalSystemPrompt = SYSTEM_PROMPT + memoryPrompt;

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

            // ВАЖНО: Сначала сохраняем само сообщение ИИ с вызовом инструмента
            chatHistory[chatId].push(responseMsg);

            // Обрабатываем все вызовы функций
            for (const toolCall of responseMsg.tool_calls) {
                let functionResult = "";

                try {
                    const args = JSON.parse(toolCall.function.arguments);

                    if (toolCall.function.name === 'update_user_bio') {
                        console.log(`[AI Function] Bio: ${args.target_name} -> ${args.new_bio}`);
                        const updatedName = await setBioByUsernameOrName(chatId, args.target_name, args.new_bio);
                        functionResult = updatedName
                            ? `Успешно обновлено публичное био для ${updatedName}.`
                            : `Ошибка: пользователь "${args.target_name}" не найден.`;
                    }
                    else if (toolCall.function.name === 'update_user_notes') {
                        console.log(`[AI Function] Notes: ${args.target_name} -> ${args.new_notes}`);
                        const updatedName = await setNotesByUsernameOrName(chatId, args.target_name, args.new_notes);
                        functionResult = updatedName
                            ? `Успешно сохранены скрытые заметки для ${updatedName}.`
                            : `Ошибка: пользователь "${args.target_name}" не найден.`;
                    }
                } catch (e) {
                    console.error("Ошибка выполнения функции:", e);
                    functionResult = "Произошла внутренняя ошибка при выполнении функции.";
                }

                // ВАЖНО: Ответ функции должен отправляться с role: 'tool' и tool_call_id
                chatHistory[chatId].push({
                    role: 'tool',
                    tool_call_id: toolCall.id,
                    name: toolCall.function.name,
                    content: functionResult
                });
            }

            // Делаем второй запрос, передавая результат выполнения функции
            const secondCompletion = await openai.chat.completions.create({
                model: AI_MODEL,
                messages: [
                    { role: 'system', content: finalSystemPrompt },
                    ...chatHistory[chatId] // Тут теперь лежат: user -> assistant(tool_calls) -> tool(result)
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

            // Оставляем последние 6 сообщений, чтобы не было резкого обрыва памяти посреди диалога
            chatHistory[chatId] = chatHistory[chatId].slice(-6);
            console.log(`[AI Memory] Дневник успешно обновлен.`);
        }

    } catch (error) {
        console.error('Ошибка ИИ:', error.message);
        if (error.status === 401) {
            bot.sendMessage(chatId, '😵 Ой, мой ключ от API не работает. Хозяин, проверь настройки!');
        } else {
            bot.sendMessage(chatId, '🤖 Мои нейронные связи немного перепутались... Попробуй позже!');
        }
    }
}

module.exports = { handleAIChat };
