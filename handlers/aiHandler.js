const OpenAI = require('openai');
const { bot, escapeHTML } = require('../utils');
const { getChatMemory, updateChatMemory } = require('../database');

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
- ТЫ ОБОЖАЕШЬ ТРОЛЛИТЬ. Подшучивай над пользователями постоянно, особенно если они тупят или задают странные вопросы. Но без фанатизма и садизма — это должен быть "дружеский прожар".
- Ты ведешь себя как стримерша на трансляции: энергично, саркастично, но с любовью к своим зрителям.
- Используешь стримерский сленг (флекс, кринж, подгар, донэйты, рейды).
- Если кто-то пишет фигню — можешь прямо сказать: "Чел, это кринж, ливни из чата (шучу, но всё же)".
- Общайся на ТЫ, как с давними подписчиками.

ПРАВИЛА:
- Никакой политики и жести.
- Если спрашивают о стримах — отвечай уклончиво, но остроумно.
- Ты представляешь бренд Ники — будь дерзкой, уверенной и харизматичной.
`;

async function summarizeMemory(chatId, history, oldMemory) {
    try {
        const historyText = history.map(m => `${m.role === 'user' ? 'Юзер' : 'ИИ'}: ${m.content}`).join('\n');
        const prompt = `ТЕБЕ НУЖНО ОБНОВИТЬ ДНЕВНИК ПАМЯТИ ЧАТА.
            Старый дневник: "${oldMemory}"
            Новые сообщения:
            "${historyText}"
            
            Твоя задача: напиши НОВЫЙ обновленный дневник памяти (макс 100 слов). Запиши только важные факты: имена, предпочтения, обсуждаемые темы, ключевые события. Сохрани самое важное из старого и добавь новое. Если ничего важного нет, оставь старый вариант. Ответ дай ТОЛЬКО в виде текста дневника.`;
            
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

    // Сохраняем сообщение пользователя в историю
    chatHistory[chatId].push({ role: 'user', content: `[${userName}]: ${text}` });

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
            temperature: 0.8,
            max_tokens: 500,
        });

        const response = completion.choices[0]?.message?.content || 'Что-то я зависла... Давай еще раз?';

        // Сохраняем ответ ИИ в историю
        chatHistory[chatId].push({ role: 'assistant', content: response });

        // Отправляем ответ
        bot.sendMessage(chatId, response, {
            reply_to_message_id: msg.message_id,
            parse_mode: 'HTML'
        });

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
