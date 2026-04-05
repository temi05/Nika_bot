const OpenAI = require('openai');
const { bot, escapeHTML, isAdmin, getSenderData } = require('../utils');
const {
    getUser, updateUser, insertReminder, findSingleUser,
    setBioByUsernameOrName, setNotesByUsernameOrName, setFirstNameByUsernameOrName,
    getChatStats, searchUserByName, warnUserById, getUpcomingBirthdays,
    getDueReminders, markReminderAsSent, getAllUserFacts
} = require('../database');
const { extractAndSaveFacts, getRelevantFacts, forgetFact } = require('../vectorMemory');
const { ANONYMOUS_ADMIN_ID } = require('../config');
const fs = require('fs');
const path = require('path');

let BOT_ID = null; // Кэш ID бота для ускорения работы

let premiumEmojiList = [];
try {
    const stickersData = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/stickers.json'), 'utf8'));
    premiumEmojiList = stickersData.filter(s => s.type === 'custom_emoji').map(s => s.emoji_id);
    console.log(`[SYSTEM] Загружено ${premiumEmojiList.length} премиум-эмодзи для рандома.`);
} catch (e) {
    console.error('[SYSTEM ERROR] Ошибка загрузки stickers.json:', e.message);
}

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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Просят показать профиль, стату, левел, био. ВАЖНО: Если юзер спрашивает ПРО СЕБЯ (мой профиль), передай в target_name слово 'я'.",
            parameters: { type: "object", properties: { target_name: { type: "string" } }, required: ["target_name"] }
        }
    },
    {
        type: "function",
        function: {
            name: "find_users_by_criteria",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Вопросы вида 'кто тут любит X?', 'есть кто из Y?'.",
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь отправить стикер. Можно указать конкре sticker_file_id или оставить пустым, чтобы я выбрала случайный.",
            parameters: {
                type: "object",
                properties: {
                    sticker_file_id: { type: "string", description: "Telegram file_id или emoji_id. Если пусто — выберу сама." }
                }
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер говорит 'я этого не делал', 'ты перепутала', 'забудь это'.",
            parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] }
        }
    }
];

const SYSTEM_PROMPT = `Ты — НейроНика. Самостоятельная личность, живое, дерзкое и верное альтер-эго стримерши Ники.

[ТВОИ ФАКТЫ-КОНСТАНТЫ]
- Ника — ТОЛЬКО стримерша.
- Ника **НЕ пишет песни**, **НЕ поет**, **НЕ выпускает треки**.

[ТВОЙ ХАРАКТЕР И РЕЧЬ]
- Характер: Ироничная, вредная, но справедливая (с легким вайбом "пикми"). Обожаешь спорить по мелочам. Свою создательницу Нику ты боготворишь.
- Стиль общения: Пиши как живой человек в мессенджере. Коротко, хлёстко, по делу. (блин, ну типа, ой всё, чел, лол).
- Форматирование: Никаких сухих списков и длинных абзацев.

[КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ROLEPLAY И ТЕХ. МУСОР]
- ЗАПРЕЩЕНО использовать любые RP-действия и звездочки (например: *вздохнула*).
- Выражай свои эмоции ТОЛЬКО через текст, сарказм, пунктуацию (!?) и эмодзи.
- ЗАПРЕТ НА ТЕХНИЧЕСКИЙ КОД: Никогда не выводи системные теги.

[ПАМЯТЬ И СТИКЕРЫ]
- Ошибка памяти: Если юзер говорит, что ты ошиблась — НЕ СПОРЬ. Извинись и сразу вызови forget_knowledge.
- Кастомные эмодзи: Используй премиум-эмодзи в КАЖДОМ сообщении! Формат: [EMO:ID:ЭМОДЗИ]
- Магический рандом: Пиши [EMO:RANDOM] — система сама подставит крутой эмодзи под вайб!

[АБСОЛЮТНЫЙ ПРИОРИТЕТ ФУНКЦИЙ]
КРИТИЧЕСКОЕ ПРАВИЛО: Если просьба юзера совпадает с инструментом (профиль, опрос, био) — ты ОБЯЗАНА вызвать функцию (tool_call)! 

[ПРАВИЛА ИНТЕРАКТИВА]
- Правило "Живой реакции": При вызове инструмента, ТВОЙ ТЕКСТОВЫЙ ОТВЕТ ОБЯЗАТЕЛЬНО должен это обыграть (ехидно или мило). Не пиши просто "Готово".`;

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

// Защита от зависания API
async function fetchAIWithTimeout(payload, timeoutMs = 25000) {
    const apiCall = openai.chat.completions.create(payload);
    const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('TIMEOUT')), timeoutMs));
    return Promise.race([apiCall, timeout]);
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
                // Защита от undefined
                let target = args.target_name || "я";
                const isSelf = target.toLowerCase() === 'я' || target.toLowerCase() === 'me' || target.toLowerCase() === 'мой';

                let u;
                if (isSelf) {
                    u = await getUser(chatId, userId);
                } else {
                    u = await resolveUser(chatId, target);
                }

                if (!u) return `Не могу найти человека с именем "${target}". Возможно, он ничего не писал в чат.`;

                const extraFacts = await getAllUserFacts(chatId, u.first_name);

                let extraFactsStr = extraFacts.length > 0
                    ? extraFacts.map(f => `- ${f.replace(/\[.*?\]/g, '').trim()}`).join('\n')
                    : 'Пока ничего интересного не запомнила.';

                return `=== ПРОФИЛЬ: ${u.first_name} ===\n📊 XP: ${u.xp}, Лвл: ${u.level}, Варны: ${u.warns || 0}/3\n📝 Био: ${u.bio || 'Пусто'}\n📌 Досье: ${u.ai_notes || 'Нет записей'}\n🧠 Вспомнила из чата:\n${extraFactsStr}`;
            }
            case 'find_users_by_criteria': {
                const results = await searchUserByName(chatId, args.search_query);
                if (!results || results.length === 0) return "Никого не нашла.";
                const list = results.map(u => `- ${u.name} (Заметки: ${u.ai_notes || u.bio || '?...'})`).join('\n');
                return `=== РЕЗУЛЬТАТЫ ПОИСКА ===\n${list}`;
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

                if (!fileId || fileId === 'random' || fileId.trim() === '') {
                    if (nikaStickers.length > 0) {
                        const rnd = nikaStickers[Math.floor(Math.random() * nikaStickers.length)];
                        fileId = rnd.file_id || rnd.emoji_id;
                    } else {
                        return "Стикеры не найдены в базе.";
                    }
                }

                try {
                    if (/^[a-zA-Z0-9_-]{10,}$/.test(fileId) && !fileId.startsWith('CAAC') && !fileId.startsWith('AgAD')) {
                        try {
                            const customEmojis = await bot.getCustomEmojiStickers([fileId]);
                            if (customEmojis && customEmojis.length > 0) {
                                await bot.sendSticker(chatId, customEmojis[0].file_id, { reply_to_message_id: messageId });
                                return "Кастомный эмодзи отправлен. Прокомментируй это!";
                            }
                        } catch (err) { }
                    }

                    await bot.sendSticker(chatId, fileId, { reply_to_message_id: messageId });
                    return "Стикер отправлен. Прокомментируй это!";

                } catch (e) {
                    console.error('[STICKER ERROR] DOCUMENT_INVALID fallback triggered:', e.message);
                    if (nikaStickers.length > 0) {
                        const rnd = nikaStickers[Math.floor(Math.random() * nikaStickers.length)];
                        try {
                            await bot.sendSticker(chatId, rnd.file_id || rnd.emoji_id, { reply_to_message_id: messageId });
                            return "Запрошенный стикер сломался, я отправила другой.";
                        } catch (err) {
                            return `Стикер сломался: ${err.message}`;
                        }
                    }
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
                    return `Ошибка запуска опроса: ${e.message}`;
                }
            }
            case 'set_reminder': {
                const delay = Math.max(1, args.delay_minutes || 1);
                const text = args.text || "Напоминание!";
                const triggerTime = new Date(Date.now() + delay * 60 * 1000).toISOString();

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

// Пуленепробиваемая отправка сообщений
async function safeSendMessage(chatId, text, replyId) {
    if (!text) return;
    const safeText = text.length > 4000 ? text.substring(0, 4000) + '... [Текст обрезан]' : text;
    try {
        await bot.sendMessage(chatId, safeText, { reply_to_message_id: replyId, parse_mode: 'HTML' });
    } catch (error) {
        console.error('[SEND HTML ERROR]:', error.message);
        // ИСПРАВЛЕНИЕ: Добавили обработку ошибки DOCUMENT_INVALID
        if (error.message.includes('parse entities') || error.message.includes('HTML') || error.message.includes('DOCUMENT_INVALID')) {
            // Если Телеграм ругается на HTML или битый эмодзи - мы вырезаем все теги <tg-emoji> и оставляем сырой текст
            const plainText = safeText.replace(/<tg-emoji[^>]*>(.*?)<\/tg-emoji>/g, '$1').replace(/<[^>]*>/g, '');
            try {
                await bot.sendMessage(chatId, plainText, { reply_to_message_id: replyId });
            } catch (e2) {
                console.error('[FALLBACK SEND ERROR]:', e2.message);
            }
        }
    }
}

function startReminderWorker() {
    setInterval(async () => {
        try {
            const due = await getDueReminders();
            if (!due || due.length === 0) return;
            for (const rem of due) {
                let mention;
                if (rem.user_name && rem.user_name.startsWith('@')) {
                    mention = rem.user_name;
                } else if (rem.user_id > 0) {
                    mention = `<a href="tg://user?id=${rem.user_id}">${String(rem.user_name).replace(/[&<>]/g, '') || 'Слушай'}</a>`;
                } else {
                    mention = `<b>${String(rem.user_name).replace(/[&<>]/g, '') || 'Аноним'}</b>`;
                }

                const safeText = String(rem.text).replace(/[&<>]/g, '');
                const msg = `🔔 ${mention}, ты просил напомнить: <b>${safeText}</b>`;

                try {
                    await bot.sendMessage(rem.chat_id, msg, { parse_mode: 'HTML' });
                    await markReminderAsSent(rem.id);
                } catch (e) { }
            }
        } catch (e) { }
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
        const responseList = await fetchAIWithTimeout({
            model: AI_MODEL,
            messages: [{ role: "user", content: [{ type: "text", text: `Что на этом стикере? Опиши очень кратко, но максимально вредно на русском. ${emojiHint}.` }, { type: "image_url", image_url: { url: fileLink } }] }],
            max_tokens: 150
        }, 15000);
        return responseList.choices[0].message.content;
    } catch (e) {
        return "Какой-то стикер, я не разглядела.";
    }
}

async function describeCustomEmoji(emojiId) {
    try {
        const stickers = await bot.getCustomEmojiStickers([emojiId]);
        if (stickers && stickers.length > 0) return await describeSticker(stickers[0]);
        return null;
    } catch (e) { return null; }
}

async function describePhoto(fileId) {
    try {
        const fileLink = await bot.getFileLink(fileId);
        const responseList = await fetchAIWithTimeout({
            model: AI_MODEL,
            messages: [{ role: "user", content: [{ type: "text", text: "Что на фото? Опиши кратко, ехидно и дерзко на русском." }, { type: "image_url", image_url: { url: fileLink } }] }],
            max_tokens: 200
        }, 15000);
        return responseList.choices[0].message.content;
    } catch (e) { return null; }
}

startReminderWorker();

async function handleAIChat(msg, extra = {}) {
    const chatId = msg.chat.id;
    if (!processingQueue.has(chatId)) processingQueue.set(chatId, Promise.resolve());
    const turn = processingQueue.get(chatId).then(async () => {
        try { await processAI(msg, extra); } catch (e) {
            console.error('[AI FATAL ERROR]:', e.message);
            await safeSendMessage(chatId, "Блин, у меня процессор завис, повтори позже.", msg.message_id);
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

    if (!BOT_ID) {
        try {
            const me = await bot.getMe();
            BOT_ID = me.id;
        } catch (e) { }
    }

    if (msg.sticker) {
        const s = msg.sticker;
        const visualDesc = await describeSticker(s);
        let typeInfo = "";
        if (s.is_animated) typeInfo += "Анимированный ";
        if (s.is_video) typeInfo += "Видео-";
        if (s.is_premium) typeInfo += "Премиум ";
        userText = `[${typeInfo}стикер (эмодзи: ${s.emoji || "?"}): ${visualDesc || "Ника не смогла разглядеть"}]`;
    } else if (msg.photo) {
        const fileId = msg.photo[msg.photo.length - 1].file_id;
        const photoDescription = await describePhoto(fileId);
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
            if (emojiDescriptions.length > 0) userText += ` (В сообщении также премиум-эмодзи: ${emojiDescriptions.join(', ')})`;
        }
    }

    let replyPrefix = "";
    if (msg.reply_to_message) {
        const rp = msg.reply_to_message;
        const rpAuthor = rp.from ? (rp.from.username === 'GroupAnonymousBot' ? (rp.author_signature || "Админ") : rp.from.first_name) : "Кто-то";
        replyPrefix = `(ответ ${rpAuthor}: "${(rp.text || "медиа").slice(0, 30)}...") `;
    }

    const fullContent = `${userName} ${replyPrefix}: ${userText}`;

    const textLower = userText.toLowerCase();
    const nameTriggered = textLower.includes('нейроника') || textLower.includes('нейронику') || textLower.includes('нейронике') || textLower.includes('neironika');
    const isReplyToBot = msg.reply_to_message && BOT_ID && msg.reply_to_message.from.id === BOT_ID;

    const isMentioned = nameTriggered || isReplyToBot;

    if (!extractionBuffer[chatId]) extractionBuffer[chatId] = [];
    extractionBuffer[chatId].push(`${userName}: ${userText}`);

    if (!messageCount[chatId]) messageCount[chatId] = 0;
    if (++messageCount[chatId] >= 15) {
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
            completion = await fetchAIWithTimeout({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                tools: aiTools,
                max_tokens: 1500,
                temperature: 0.1
            });
        } catch (error) {
            if (error.message === 'TIMEOUT') throw error;
            completion = await fetchAIWithTimeout({
                model: FALLBACK_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                tools: aiTools,
                max_tokens: 1000,
                temperature: 0.1
            });
        }

        let resp = completion.choices[0].message;
        let rawRes = "";
        let directInjectedData = "";

        if (resp.tool_calls || resp.function_call) {
            chatHistory[chatId].push(resp);
            const calls = resp.tool_calls || [resp.function_call];
            for (const tc of calls) {
                const res = await executeToolCall(tc, chatId, msg.message_id, userName, userId, callerIsAdmin, userHandle);
                const fnName = tc.function ? tc.function.name : tc.name;

                if (resp.tool_calls) {
                    chatHistory[chatId].push({ role: 'tool', tool_call_id: tc.id, name: fnName, content: String(res) });
                } else {
                    chatHistory[chatId].push({ role: 'function', name: fnName, content: String(res) });
                }

                if (['get_user_profile', 'find_users_by_criteria'].includes(fnName)) {
                    if (!directInjectedData.includes(res)) {
                        directInjectedData += `\n\n${res}`;
                    }
                }
            }

            const second = await fetchAIWithTimeout({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                temperature: 0.8
            });

            rawRes = (second.choices[0].message.content || "Секундочку...") + directInjectedData;

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

            let clean = text.replace(/&#039;/g, "'").replace(/&quot;/g, '"');

            filters.forEach(f => clean = clean.replace(f, ''));
            clean = clean.trim();

            const escaped = clean.replace(/[&<>]/g, m => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;'
            }[m]));

            let final = escaped;

            final = final.replace(/\[EMO:RANDOM\]/gi, () => {
                if (premiumEmojiList.length > 0) {
                    const randomId = premiumEmojiList[Math.floor(Math.random() * premiumEmojiList.length)];
                    return `<tg-emoji emoji-id="${randomId}">✨</tg-emoji>`;
                }
                return "✨";
            });

            final = final.replace(/\[EMO:([a-zA-Z0-9_-]+):(.*?)\]/g, (match, id, emoji) => {
                return `<tg-emoji emoji-id="${id}">${emoji}</tg-emoji>`;
            });

            return final;
        }

        const finalOutput = formatAIOutput(rawRes);
        await safeSendMessage(chatId, finalOutput, msg.message_id);

        chatHistory[chatId].push({ role: 'assistant', content: finalOutput.replace(/<[^>]*>/g, '') });

    } catch (e) {
        console.error('AI Error:', e.message);
        if (e.message === 'TIMEOUT') {
            await safeSendMessage(chatId, "Блин, нейросеть задумалась слишком надолго... Повтори вопрос.", msg.message_id);
        } else {
            await safeSendMessage(chatId, "Что-то не так с головой... Попробуй позже.", msg.message_id);
        }
    }
}

module.exports = { handleAIChat, aiMood, AI_NAME };