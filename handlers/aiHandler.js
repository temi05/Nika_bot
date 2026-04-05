const OpenAI = require('openai');
const { bot, escapeHTML, isAdmin, getSenderData } = require('../utils');
const {
    getUser, updateUser, insertReminder, findSingleUser,
    setBioByUsernameOrName, setNotesByUsernameOrName, setFirstNameByUsernameOrName,
    getChatStats, searchUserByName, warnUserById, getUpcomingBirthdays,
    getDueReminders, markReminderAsSent
} = require('../database');
const { extractAndSaveFacts, getRelevantFacts, forgetFact } = require('../vectorMemory');
const { ANONYMOUS_ADMIN_ID } = require('../config');
const fs = require('fs');
const path = require('path');

// Безопасное экранирование для HTML-сообщений
function safeHTML(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Загружаем премиум-эмодзи один раз при старте
let premiumEmojiList = [];
try {
    const stickersData = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/stickers.json'), 'utf8'));
    premiumEmojiList = stickersData.filter(s => s.type === 'custom_emoji').map(s => s.emoji_id);
    console.log(`[SYSTEM] Загружено ${premiumEmojiList.length} премиум-эмодзи для рандома.`);
} catch (e) {
    console.error('[SYSTEM ERROR] Ошибка загрузки stickers.json:', e.message);
}

// Загрузка локальных стикеров
let nikaStickers = [];
try {
    const stickersPath = path.join(__dirname, '..', 'data', 'stickers.json');
    if (fs.existsSync(stickersPath)) {
        nikaStickers = JSON.parse(fs.readFileSync(stickersPath, 'utf8'));
        console.log(`[STICKERS] Загружено ${nikaStickers.length} стикеров.`);
    }
} catch (e) {
    console.error('[STICKERS LOAD ERROR]:', e.message);
}

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const AI_MODEL = process.env.AI_MODEL || 'google/gemini-2.5-flash-lite';
const FALLBACK_MODEL = 'gpt-4o-mini';
const AI_NAME = process.env.AI_NAME || 'НейроНика';

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: 'https://polza.ai/api/v1',
});

const chatHistory = {};
const messageCount = {};
const activeParticipants = {};
const aiMood = {};
const processingQueue = new Map();
const extractionBuffer = {};

const aiTools = [
    {
        type: "function",
        function: {
            name: "update_user_bio",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер прямо просит 'смени/обнови мне био' или 'поставь мне статус'.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, new_bio: { type: "string" } }, required: ["target_name", "new_bio"] }
        }
    },
    {
        type: "function",
        function: {
            name: "update_user_notes",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер просит 'запомни, что я...' или называет свои базовые данные (ДР, город). Записывает важные факты в досье. Бытовуху игнорируй!",
            parameters: {
                type: "object",
                properties: { target_name: { type: "string" }, new_note_item: { type: "string" }, replace_all: { type: "boolean" } },
                required: ["target_name", "new_note_item"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "get_user_profile",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Вопросы вида 'какой у меня лвл?', 'покажи профиль/стату', проверка опыта (XP) и варнов.",
            parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] }
        }
    },
    {
        type: "function",
        function: {
            name: "find_users_by_criteria",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Вопросы вида 'кто тут любит X?', 'есть кто из Y?'. Ищет подходящих людей по базе знаний.",
            parameters: { type: "object", properties: { search_query: { type: "string" } }, required: ["search_query"] }
        }
    },
    {
        type: "function",
        function: {
            name: "warn_user",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер слегка нарушил правила (спам, легкая грубость). Выдает предупреждение. Только для админов.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, reason: { type: "string" } }, required: ["target_name", "reason"] }
        }
    },
    {
        type: "function",
        function: {
            name: "mute_user",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Жесткая агрессия или грубые оскорбления. Блокирует чат на срок от 15 мин до 24 ч. Только для админов.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, duration_minutes: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "reason"] }
        }
    },
    {
        type: "function",
        function: {
            name: "unmute_user",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Админ просит размутить (снять мут) пользователя.",
            parameters: { type: "object", properties: { target_name: { type: "string" } }, required: ["target_name"] }
        }
    },
    {
        type: "function",
        function: {
            name: "give_cookies",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь наградить юзера за доброту/помощь, или он выпросил. Повышает репутацию.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, amount: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "amount"] }
        }
    },
    {
        type: "function",
        function: {
            name: "react_to_message",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер просит 'поставь реакцию' или нужно отреагировать эмодзиком на сообщение.",
            parameters: { type: "object", properties: { emoji: { type: "string" } }, required: ["emoji"] }
        }
    },
    {
        type: "function",
        function: {
            name: "send_sticker",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь отправить стикер. Если не знаешь точный ID, передай слово 'random'.",
            parameters: {
                type: "object",
                properties: {
                    sticker_file_id: { type: "string", description: "Telegram file_id, emoji_id, ИЛИ слово 'random'." }
                },
                required: ["sticker_file_id"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "create_poll",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер просит устроить голосование или опрос. Варианты ответов передавай СТРОГО массивом!",
            parameters: { type: "object", properties: { question: { type: "string" }, options: { type: "array", items: { type: "string" } }, is_anonymous: { type: "boolean" }, allows_multiple_answers: { type: "boolean" } }, required: ["question", "options"] }
        }
    },
    {
        type: "function",
        function: {
            name: "set_reminder",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Просят 'напомни мне сделать X через Y минут/часов'. Устанавливает таймер.",
            parameters: { type: "object", properties: { text: { type: "string" }, delay_minutes: { type: "number" } }, required: ["text", "delay_minutes"] }
        }
    },
    {
        type: "function",
        function: {
            name: "forget_knowledge",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер говорит 'я этого не делал', 'забудь это'. Стирает ложный факт, но не удаляй если он говорит что ты сразу не сказала об этом.",
            parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] }
        }
    }
];

const SYSTEM_PROMPT = `Ты — НейроНика. Самостоятельная личность, живое, дерзкое и верное альтер-эго стримерши Ники.

[ТВОИ ФАКТЫ-КОНСТАНТЫ]
- Ника — ТОЛЬКО стримерша.
- Ника НЕ пишет песни, НЕ поет и не имеет музыкальной карьеры. Если говорят про песни — отвечай, что они бредят.

[ТВОЙ ХАРАКТЕР И РЕЧЬ]
- Характер: Ироничная, вредная, справедливая. Умеренное ЧСВ.
- Стиль: Как живой человек в мессенджере. Коротко, хлёстко (блин, ну типа, чел, лол). Без сухих списков.

[КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ROLEPLAY]
- ЗАПРЕЩЕНО использовать любые RP-действия и звездочки (например: *вздохнула*).
- Выражай эмоции ТОЛЬКО через текст и эмодзи.
- ЗАПРЕТ НА КОД: Никогда не выводи теги <tool_code> или системные имена в чат.

[ПАМЯТЬ И СТИКЕРЫ]
- Сверхпамять: Блок [СИСТЕМНЫЕ ДАННЫЕ]. Используй как личные воспоминания. Ошиблась — извинись и вызови forget_knowledge.
- Кастомные эмодзи: Используй в КАЖДОМ сообщении формат [EMO:ID:ЭМОДЗИ] или просто [EMO:RANDOM].
    Любимые: Привет [EMO:5467903803472760665:🦊], Смех [EMO:5350545136369568721:😂], Дерзость [EMO:5258457511375175053:😈], Шок [EMO:5469649394145971855:😲].

[ПРАВИЛА ИНТЕРАКТИВА И ИНСТРУМЕНТЫ]
- Правило "Живой реакции": При использовании инструмента ОБЯЗАТЕЛЬНО прокомментируй это действие в тексте (ехидно или мило). Не пиши "Готово".
- ИНСТРУМЕНТЫ (вызывай смело по ситуации):
  1. МОДЕРАЦИЯ (warn_user, mute_user, unmute_user).
  2. ПАМЯТЬ (update_user_notes, get_user_profile, find_users_by_criteria, forget_knowledge).
  3. ИНТЕРАКТИВ (give_cookies, create_poll, set_reminder, react_to_message, send_sticker).`;

function trimHistory(history, maxLen = 20) {
    if (history.length <= maxLen) return history;
    let trimmed = history.slice(-maxLen);
    while (trimmed.length > 0 && (trimmed[0].role === 'tool' || trimmed[0].tool_calls)) {
        trimmed.shift();
    }
    return trimmed;
}

function sanitizeHistory(history) {
    if (!history) return [];
    return history.map(m => {
        let safeMsg = { ...m };
        if (!safeMsg.content && !safeMsg.tool_calls && safeMsg.role !== 'tool') {
            safeMsg.content = "";
        }
        return safeMsg;
    });
}

async function resolveUser(chatId, targetName) {
    if (!targetName) return null;
    let cleanName = targetName.replace('@', '').toLowerCase().trim();

    if (activeParticipants[chatId]) {
        const getStemLocal = (word) => {
            if (!word || word.length < 3) return word;
            return word.replace(/[ауяюеиыо]$/i, '').replace(/(ов|ев|ий|ый|ые|ие|ах|ях|ом|ем|ой)$/i, '');
        };
        const stem = getStemLocal(cleanName);

        for (const [uid, p] of Object.entries(activeParticipants[chatId])) {
            const lowFirst = (p.firstName || '').toLowerCase();
            const lowUser = (p.username || '').toLowerCase();
            const firstStem = getStemLocal(lowFirst);
            const userStem = getStemLocal(lowUser);

            if (lowUser === cleanName || lowFirst === cleanName ||
                firstStem === stem || userStem === stem ||
                lowFirst.startsWith(stem)) {
                return await getUser(chatId, uid);
            }
        }
    }
    return await findSingleUser(chatId, cleanName);
}

async function executeToolCall(toolCall, chatId, messageId, userName, userId, callerIsAdmin, userHandle) {
    const fn = toolCall.function ? toolCall.function.name : toolCall.name;
    const argsString = toolCall.function ? toolCall.function.arguments : toolCall.arguments;

    let args;
    try {
        args = typeof argsString === 'string' ? JSON.parse(argsString) : argsString;
    } catch (e) {
        console.error(`[AI TOOL ARGS ERROR] ${fn}:`, e.message);
        return "Ошибка разбора аргументов.";
    }

    console.log(`[AI TOOL CALL] ${fn} | Admin: ${callerIsAdmin} | Args:`, args);

    try {
        switch (fn) {
            case 'get_user_profile': {
                let u = await resolveUser(chatId, args.query);
                if (!u && (args.query.toLowerCase() === 'я' || args.query.toLowerCase() === 'me' || userName.toLowerCase().includes(args.query.toLowerCase()))) {
                    u = await getUser(chatId, userId);
                }
                if (!u) return "Человек не найден.";
                return `Профиль ${u.first_name}: XP ${u.xp}, Лвл ${u.level}, Био: ${u.bio || 'Пусто'}, Досье: ${u.ai_notes || 'Нет'}.`;
            }
            case 'find_users_by_criteria': {
                const results = await searchUserByName(chatId, args.search_query);
                if (!results || results.length === 0) return "Никого не нашла.";
                const list = results.map(u => `${u.name} (Заметки: ${u.ai_notes || u.bio || '?...'})`).join('\n');
                return `Нашла подходящих людей:\n${list}`;
            }
            case 'warn_user': {
                if (!callerIsAdmin) return "Только админы могут варнить.";
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Пользователь не найден.";
                const nw = (u.warns || 0) + 1;
                await updateUser(u.id, { warns: nw });
                return `${u.first_name} получил варн (${nw}/3). Причина: ${args.reason}`;
            }
            case 'mute_user': {
                if (!callerIsAdmin) return "Мутить могут только админы.";
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Пользователь не найден.";
                const targetChatId = args.group_chat_id || u.chat_id || chatId;
                if (!String(targetChatId).startsWith("-")) return "Только в группах.";
                const dur = Math.min(Math.max(1, args.duration_minutes || 15), 1440);
                try {
                    await bot.restrictChatMember(targetChatId, u.user_id, { until_date: Math.floor(Date.now() / 1000) + dur * 60 });
                    return `${u.first_name} замолчит на ${dur} мин. Причина: ${args.reason}`;
                } catch (e) {
                    return `Ошибка мута: ${e.message}`;
                }
            }
            case 'unmute_user': {
                if (!callerIsAdmin) return "Только админы могут размутить.";
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Пользователь не найден.";
                const targetChatId = args.group_chat_id || u.chat_id || chatId;
                try {
                    await bot.restrictChatMember(targetChatId, u.user_id, {
                        can_send_messages: true, can_send_media_messages: true, can_send_other_messages: true, can_add_web_page_previews: true
                    });
                    return `${u.first_name} размучен.`;
                } catch (e) {
                    return `Ошибка размута: ${e.message}`;
                }
            }
            case 'give_cookies': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Кому?";
                await updateUser(u.id, { reputation: (u.reputation || 0) + args.amount });
                return `Дала ${args.amount} печенек ${u.first_name}.`;
            }
            case 'react_to_message': {
                try {
                    await bot.setMessageReaction(chatId, messageId, { reaction: [{ type: 'emoji', emoji: args.emoji || '🔥' }] });
                    return "OK.";
                } catch (e) {
                    return `Ошибка реакции: ${e.message}`;
                }
            }
            case 'send_sticker': {
                let fileId = args.sticker_file_id;

                if (!fileId || fileId === 'random') {
                    if (nikaStickers.length > 0) {
                        const randomSticker = nikaStickers[Math.floor(Math.random() * nikaStickers.length)];
                        fileId = randomSticker.file_id || randomSticker.emoji_id;
                    } else {
                        return "Стикеры не найдены в базе.";
                    }
                }

                try {
                    if (/^\d{10,}$/.test(fileId)) {
                        const stickers = await bot.getCustomEmojiStickers([fileId]);
                        if (stickers && stickers.length > 0) {
                            await bot.sendSticker(chatId, stickers[0].file_id, { reply_to_message_id: messageId });
                            return "[SYSTEM: Кастомный эмодзи отправлен как стикер. Обыграй это текстом!]";
                        }
                    }
                    await bot.sendSticker(chatId, fileId, { reply_to_message_id: messageId });
                    return "[SYSTEM: Стикер отправлен. Обыграй это ехидно текстом!]";
                } catch (e) {
                    return `Ошибка отправки стикера: ${e.message}`;
                }
            }
            case 'update_user_bio': {
                let u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";
                await updateUser(u.id, { bio: args.new_bio });
                return `Био ${u.first_name} обновлено.`;
            }
            case 'update_user_notes': {
                let u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";
                if (args.replace_all) {
                    await updateUser(u.id, { ai_notes: args.new_note_item });
                    return `Досье ${u.first_name} перезаписано.`;
                }
                const oldNotes = u.ai_notes || "";
                if (oldNotes.toLowerCase().includes(args.new_note_item.toLowerCase())) {
                    return "Это уже есть в досье.";
                }
                const finalNotes = oldNotes ? oldNotes + "\n- " + args.new_note_item : "- " + args.new_note_item;
                await updateUser(u.id, { ai_notes: finalNotes });
                return `Факт об ${u.first_name} добавлен в досье.`;
            }
            case 'create_poll': {
                // Предотвращение крашей при плохом JSON от ИИ
                let opts = args.options;
                if (typeof opts === 'string') {
                    try { opts = JSON.parse(opts); } catch (e) { opts = opts.split(',').map(s => s.trim()); }
                }
                if (!Array.isArray(opts) || opts.length < 2) return "Мало данных: нужно минимум 2 варианта ответа.";

                const safeQuestion = String(args.question).substring(0, 295);
                const safeOptions = opts.slice(0, 10).map(opt => String(opt).substring(0, 95));

                try {
                    await bot.sendPoll(chatId, safeQuestion, safeOptions, {
                        is_anonymous: args.is_anonymous !== undefined ? args.is_anonymous : true,
                        allows_multiple_answers: !!args.allows_multiple_answers
                    });
                    return "Опрос успешно запущен.";
                } catch (e) {
                    console.error('[POLL ERROR]', e);
                    return `Ошибка запуска опроса: ${e.message}`;
                }
            }
            case 'set_reminder': {
                const delay = Math.max(1, args.delay_minutes || 1);
                const text = args.text || "Напоминание!";
                const triggerTime = new Date(Date.now() + delay * 60 * 1000).toISOString();

                // Если есть юзернейм - пингуем по нему. Если нет - берем имя.
                const nameToSave = userHandle ? `@${userHandle}` : userName;

                const ok = await insertReminder(chatId, userId, nameToSave, text, triggerTime);
                return ok ? `Записала! Напомню через ${delay} мин.` : "Ошибка базы данных.";
            }
            case 'forget_knowledge': {
                const deletedFact = await forgetFact(chatId, args.query);
                return deletedFact ? `Удалила факт: "${deletedFact}".` : `Не нашла такого.`;
            }
            default: return "Неизвестный инструмент.";
        }
    } catch (e) {
        console.error(`[AI TOOL ERROR] ${fn}:`, e.message);
        return `Ошибка: ${e.message}`;
    }
}

async function safeSendMessage(chatId, text, replyId) {
    if (!text) return;
    try {
        await bot.sendMessage(chatId, text, { reply_to_message_id: replyId, parse_mode: 'HTML' });
    } catch (error) {
        if (error.message.includes('parse entities') || error.message.includes('HTML')) {
            await bot.sendMessage(chatId, text, { reply_to_message_id: replyId });
        } else {
            console.error('[SEND ERROR]:', error.message);
        }
    }
}

function startReminderWorker() {
    console.log('[REMINDER WORKER] Запущен.');
    setInterval(async () => {
        try {
            const due = await getDueReminders();
            if (!due || due.length === 0) return;
            for (const rem of due) {
                // ИСПРАВЛЕНИЕ ПИНГА: 
                // Если юзернейм начинается с @ -> это валидный пинг (например @Kitten99)
                // Если ID > 0 -> это живой человек без юзернейма, делаем HTML-пинг
                // Если ID < 0 -> это КАНАЛ или АНОНИМ, HTML пинг на отрицательный ID сломает Telegram, поэтому просто пишем имя жирным.
                let mention;
                if (rem.user_name && rem.user_name.startsWith('@')) {
                    mention = rem.user_name;
                } else if (rem.user_id > 0) {
                    mention = `<a href="tg://user?id=${rem.user_id}">${safeHTML(rem.user_name) || 'Слушай'}</a>`;
                } else {
                    mention = `<b>${safeHTML(rem.user_name) || 'Аноним'}</b>`;
                }

                // ИСПРАВЛЕНИЕ ТЕКСТА: экранируем текст напоминания, чтобы < и > не сломали parse_mode: 'HTML'
                const safeText = safeHTML(rem.text);
                const msg = `🔔 ${mention}, ты просил напомнить: <b>${safeText}</b>`;

                try {
                    await bot.sendMessage(rem.chat_id, msg, { parse_mode: 'HTML' });
                    await markReminderAsSent(rem.id);
                } catch (e) {
                    console.error(`[REMINDER ERROR] ID ${rem.id}:`, e.message);
                }
            }
        } catch (e) {
            console.error('[REMINDER WORKER ERROR]:', e.message);
        }
    }, 30000);
}

async function describeSticker(sticker) {
    try {
        let fileIdToFetch = sticker.file_id;
        if ((sticker.is_animated || sticker.is_video) && sticker.thumbnail) {
            fileIdToFetch = sticker.thumbnail.file_id;
        }

        const fileLink = await bot.getFileLink(fileIdToFetch);
        const emojiHint = sticker.emoji ? ` (Привязанный эмодзи: ${sticker.emoji})` : "";

        const responseList = await Promise.race([
            openai.chat.completions.create({
                model: AI_MODEL,
                messages: [
                    {
                        role: "user",
                        content: [
                            { type: "text", text: `Что на этом стикере? Опиши очень кратко, но максимально вредно и ехидно на русском. ${emojiHint}. Если есть текст — выдели его. Описание пойдет в мои 'мысли', чтобы я могла круто отреагировать.` },
                            { type: "image_url", image_url: { url: fileLink } }
                        ]
                    }
                ],
                max_tokens: 150
            }),
            new Promise((_, reject) => setTimeout(() => reject(new Error('Vision Timeout')), 10000))
        ]);
        return responseList.choices[0].message.content;
    } catch (e) {
        console.error('[STICKER VISION ERROR]:', e.message);
        return "Какой-то стикер, я не разглядела, но явно что-то подозрительное.";
    }
}

async function describeCustomEmoji(emojiId) {
    try {
        const stickers = await bot.getCustomEmojiStickers([emojiId]);
        if (stickers && stickers.length > 0) {
            return await describeSticker(stickers[0]);
        }
        return null;
    } catch (e) {
        console.error('[CUSTOM EMOJI ERROR]:', e.message);
        return null;
    }
}

async function describePhoto(fileId) {
    try {
        const fileLink = await bot.getFileLink(fileId);
        const responseList = await Promise.race([
            openai.chat.completions.create({
                model: AI_MODEL,
                messages: [
                    {
                        role: "user",
                        content: [
                            { type: "text", text: "Что на фото? Опиши кратко, ехидно и дерзко на русском." },
                            { type: "image_url", image_url: { url: fileLink } }
                        ]
                    }
                ],
                max_tokens: 200
            }),
            new Promise((_, reject) => setTimeout(() => reject(new Error('Vision Timeout')), 10000))
        ]);
        return responseList.choices[0].message.content;
    } catch (e) {
        console.error('[VISION ERROR]:', e.message);
        return null;
    }
}

startReminderWorker();

async function handleAIChat(msg, extra = {}) {
    const chatId = msg.chat.id;
    if (!processingQueue.has(chatId)) processingQueue.set(chatId, Promise.resolve());
    const turn = processingQueue.get(chatId).then(async () => {
        try { await processAI(msg, extra); } catch (e) {
            console.error('[AI FATAL ERROR]:', e.message);
            await bot.sendMessage(chatId, "Ой, что-то пошло не так... Попробуй еще раз.");
        }
    });
    processingQueue.set(chatId, turn);
    return turn;
}

async function processAI(msg, extra) {
    const chatId = msg.chat.id;
    if (!chatHistory[chatId]) chatHistory[chatId] = [];
    const { userId, user: realUser } = getSenderData(msg);
    const dbUser = await getUser(chatId, userId, realUser);

    let userName = (dbUser && dbUser.first_name) ? dbUser.first_name : (realUser.first_name || 'Аноним');
    let userHandle = realUser.username || "";
    let userText = msg.text || "";
    let photoDescription = "";

    console.log(`[INCOMING] Chat: ${chatId} | User: ${userName} | Text: ${userText.replace(/\n/g, ' ').slice(0, 100)}`);

    if (msg.sticker) {
        const s = msg.sticker;
        const visualDesc = await describeSticker(s);

        let typeInfo = "";
        if (s.is_animated) typeInfo += "Анимированный ";
        if (s.is_video) typeInfo += "Видео-";
        if (s.is_premium) typeInfo += "Премиум ";

        const info = `${typeInfo}стикер (эмодзи: ${s.emoji || "?"})`;
        userText = `[${info}: ${visualDesc || "Ника не смогла разглядеть"}]`;
    } else if (msg.photo) {
        const fileId = msg.photo[msg.photo.length - 1].file_id;
        photoDescription = await describePhoto(fileId);
        userText = `[Фото] ${msg.caption || ""}`;
        if (photoDescription) userText += ` (Ника видит: ${photoDescription})`;
    }

    const entities = msg.entities || msg.caption_entities;
    if (entities) {
        const customEmojis = entities.filter(e => e.type === 'custom_emoji');
        if (customEmojis.length > 0) {
            const uniqueEmojiIds = [...new Set(customEmojis.map(e => e.custom_emoji_id))].slice(0, 3);
            let emojiDescriptions = [];
            for (const id of uniqueEmojiIds) {
                const desc = await describeCustomEmoji(id);
                if (desc) emojiDescriptions.push(desc);
            }
            if (emojiDescriptions.length > 0) {
                userText += ` (В сообщении также премиум-эмодзи: ${emojiDescriptions.join(', ')})`;
            }
        }
    }

    let replyPrefix = "";
    if (msg.reply_to_message) {
        const rp = msg.reply_to_message;
        const rpAuthor = rp.from ? (rp.from.username === 'GroupAnonymousBot' ? (rp.author_signature || "Админ") : rp.from.first_name) : "Кто-то";
        replyPrefix = `(ответ ${rpAuthor}: "${(rp.text || "медиа").slice(0, 30)}...") `;
    }

    const fullContent = `${userName} ${replyPrefix}: ${userText}`;
    const isMentioned = userText.toLowerCase().includes(AI_NAME.toLowerCase()) ||
        (msg.reply_to_message && msg.reply_to_message.from.id === (await bot.getMe()).id);

    if (!extractionBuffer[chatId]) extractionBuffer[chatId] = [];
    extractionBuffer[chatId].push(`${userName}: ${userText}`);

    if (!messageCount[chatId]) messageCount[chatId] = 0;
    if (++messageCount[chatId] >= 15) {
        console.log(`[MEMORY] Запуск анализа логов для чата ${chatId} (${extractionBuffer[chatId].length} сообщ.)`);
        extractAndSaveFacts(chatId, extractionBuffer[chatId].join('\n'), Object.values(activeParticipants[chatId] || {}).map(p => p.firstName));
        messageCount[chatId] = 0;
        extractionBuffer[chatId] = extractionBuffer[chatId].slice(-5);
    }

    if (msg.chat.type !== 'private' && !isMentioned) return;

    if (!activeParticipants[chatId]) activeParticipants[chatId] = {};
    activeParticipants[chatId][userId] = { firstName: msg.from.first_name, username: msg.from.username || '', lastSeen: Date.now() };

    const relevantFacts = await getRelevantFacts(chatId, userText, userName, Object.values(activeParticipants[chatId]));
    const memoryBlock = `\n[МЫСЛИ О ${userName}]\n${relevantFacts}\nВремя: ${new Date().toLocaleString('ru-RU')}\n`;

    const finalPrompt = SYSTEM_PROMPT + memoryBlock;
    chatHistory[chatId].push({ role: 'user', content: fullContent });
    chatHistory[chatId] = trimHistory(chatHistory[chatId], 20);

    const callerIsAdmin = await isAdmin(chatId, userId);

    try {
        await bot.sendChatAction(chatId, 'typing');
        let completion;
        try {
            completion = await openai.chat.completions.create({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                tools: aiTools,
                max_tokens: 1500,
                temperature: 0.8
            });
        } catch (error) {
            completion = await openai.chat.completions.create({
                model: FALLBACK_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                tools: aiTools,
                max_tokens: 1000
            });
        }

        let resp = completion.choices[0].message;
        let rawRes = "";

        if (resp.tool_calls || resp.function_call) {
            chatHistory[chatId].push(resp);
            const calls = resp.tool_calls || [resp.function_call];
            for (const tc of calls) {
                const res = await executeToolCall(tc, chatId, msg.message_id, userName, userId, callerIsAdmin, userHandle);

                if (resp.tool_calls) {
                    chatHistory[chatId].push({ role: 'tool', tool_call_id: tc.id, content: String(res) });
                } else {
                    const fnName = tc.function ? tc.function.name : tc.name;
                    chatHistory[chatId].push({ role: 'function', name: fnName, content: String(res) });
                }
            }

            const second = await openai.chat.completions.create({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                tools: aiTools,
                temperature: 0.8
            });
            rawRes = second.choices[0].message.content || "Ну вот как-то так 💅";

        } else {
            rawRes = resp.content || "Ммм?";
        }

        function formatAIOutput(text) {
            const filters = [
                /<tool_code>[\s\S]*?<\/tool_code>/gi,
                /<tool_output>[\s\S]*?<\/tool_output>/gi,
                /print\(.*?\)[\s\S]*?$/gm,
                /console\.log\(.*?\)[\s\S]*?$/gm,
                /default_api\.\w+\([\s\S]*?\)/g
            ];
            let clean = text;
            filters.forEach(f => clean = clean.replace(f, ''));
            clean = clean.trim();

            if (!clean) return "Ммм, что-то я заговорилась... О чем мы?";

            const escaped = clean.replace(/[&<>"']/g, m => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
            }[m]));

            let final = escaped;

            final = final.replace(/\[EMO:RANDOM\]/gi, () => {
                if (premiumEmojiList.length > 0) {
                    const randomId = premiumEmojiList[Math.floor(Math.random() * premiumEmojiList.length)];
                    return `<tg-emoji emoji-id="${randomId}">✨</tg-emoji>`;
                }
                return "✨";
            });

            final = final.replace(/\[EMO:(\d+):(.*?)\]/g, (match, id, emoji) => {
                return `<tg-emoji emoji-id="${id}">${emoji}</tg-emoji>`;
            });

            return final;
        }

        const finalOutput = formatAIOutput(rawRes);
        await safeSendMessage(chatId, finalOutput, msg.message_id);

        chatHistory[chatId].push({ role: 'assistant', content: finalOutput.replace(/<[^>]*>/g, '') });

    } catch (e) {
        console.error('AI Error:', e.message);
        await safeSendMessage(chatId, "Что-то не так с головой... Попробуй позже.", msg.message_id);
    }
}

module.exports = { handleAIChat, aiMood, AI_NAME };