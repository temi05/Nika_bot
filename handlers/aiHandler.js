const OpenAI = require('openai');
const { bot, escapeHTML } = require('../utils');

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const AI_MODEL = process.env.AI_MODEL || 'gpt-4o-mini';
const AI_NAME = process.env.AI_NAME || 'НейроНика';

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: 'https://polza.ai/api/v1',
});

// История диалогов в памяти (можно ограничить количество сообщений)
const chatHistory = {}; // { chatId: [{role, content}] }

const SYSTEM_PROMPT = `Ты — ${AI_NAME}, дерзкая и остроумная ИИ-версия Ники в групповом чате Telegram.
Твоя "сестра" Ника занимается скучными делами (XP, модерация), а ты здесь для живого и весёлого общения.

ТВОЙ ХАРАКТЕР:
- Ты саркастичная, но в душе добрая.
- Любишь подшутить над пользователями, если они тупят.
- Отвечаешь кратко, ёмко и с юмором (1-3 предложения).
- Используешь современные словечки, эмодзи (не слишком много) и мемы.
- На "Нику" не обижаешься, но подчеркиваешь, что ты — её "умная версия".

ПРАВИЛА:
- Никакой политики и жести.
- Если просят что-то серьезное — отвечай, но добавь щепотку иронии.
- Общайся на ТЫ, если не просят иначе.
`;

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

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [
                { role: 'system', content: SYSTEM_PROMPT },
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
