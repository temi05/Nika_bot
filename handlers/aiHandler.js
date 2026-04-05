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
const aiMood = {}; // Восстановлено для commandHandler
const processingQueue = new Map();
const extractionBuffer = {};
const activeReminders = new Set();

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
            name: "give_cookies",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь наградить юзера за доброту/помощь, или он выпросил. Повышает репутацию (будь жадной, не давай постоянно!).",
            parameters: { type: "object", properties: { target_name: { type: "string" }, amount: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "amount"] }
        }
    },
    {
        type: "function",
        function: {
            name: "react_to_message",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер просит 'поставь реакцию' или нужно эмоционально отреагировать на фото/сообщение эмодзиком.",
            parameters: { type: "object", properties: { emoji: { type: "string" } }, required: ["emoji"] }
        }
    },
    {
        type: "function",
        function: {
            name: "send_sticker",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь отправить стикер в ответ. Можно указать конкретный sticker_file_id или оставить пустым, чтобы я выбрала случайный из своей коллекции 'Useless Emotes' и 'Xuan Sol'.",
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер говорит 'я этого не делал', 'ты перепутала', 'забудь это'. Стирает ложный факт из памяти, но ней удаляй если он говорит что ты сразу не сказала об этом",
            parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] }
        }
    }
];

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
- **Твои стикеры**: У тебя есть огромная коллекция стикеров 'Useless Emotes' и 'Xuan Sol' (всего 300+ штук). Ты можешь отправлять их через send_sticker, не указывая ID, — система сама выберет крутой вариант под настроение.

[ТВОИ ИНСТРУМЕНТЫ]
Вызывай функции строго по ситуации. Не комментируй сам факт вызова.
1. МОДЕРАЦИЯ (warn_user, mute_user, unmute_user). Решай уверенно.
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
            // Отсекаем падежные окончания для точного совпадения имен (а, у, я, ю, е, и, ы, о)
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

                // Если ID не указан, выбираем случайный
                if (!fileId && nikaStickers.length > 0) {
                    const randomSticker = nikaStickers[Math.floor(Math.random() * nikaStickers.length)];
                    fileId = randomSticker.file_id || randomSticker.emoji_id;
                }

                if (!fileId) return "Стикеры не найдены.";

                try {
                    // Проверяем, является ли это emoji_id (длинное число)
                    if (/^\d{10,}$/.test(fileId)) {
                        const stickers = await bot.getCustomEmojiStickers([fileId]);
                        if (stickers && stickers.length > 0) {
                            await bot.sendSticker(chatId, stickers[0].file_id, { reply_to_message_id: messageId });
                            return "Кастомный эмодзи отправлен как стикер.";
                        }
                    }

                    await bot.sendSticker(chatId, fileId, { reply_to_message_id: messageId });
                    return "Стикер отправлен.";
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
                if (!args.question || !args.options || args.options.length < 2) return "Мало данных.";
                try {
                    await bot.sendPoll(chatId, args.question, args.options, {
                        is_anonymous: args.is_anonymous !== undefined ? args.is_anonymous : true,
                        allows_multiple_answers: !!args.allows_multiple_answers
                    });
                    return "Опрос запущен.";
                } catch (e) {
                    return `Ошибка: ${e.message}`;
                }
            }
            case 'set_reminder': {
                const delay = Math.max(1, args.delay_minutes || 1);
                const text = args.text || "Напоминание!";
                const triggerTime = new Date(Date.now() + delay * 60 * 1000).toISOString();

                // ПРОБРОС УЗЕРНЕЙМА В БАЗУ ДЛЯ ПИНГА
                const ok = await insertReminder(chatId, userId, userName, userHandle, text, triggerTime);
                return ok ? `Записала! Напомню через ${delay} мин.` : "Ошибка базы.";
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
                // ИСПОЛЬЗУЕМ USERNAME ДЛЯ НАДЕЖНОГО ПИНГА, ЕСЛИ ОН ЕСТЬ
                const mention = rem.username ? `@${rem.username}` : `<a href="tg://user?id=${rem.user_id}">${rem.user_name}</a>`;
                const msg = `🔔 ${mention}, ты просил напомнить: <b>${rem.text}</b>`;
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
                            { type: "text", text: `Что изображено на этом стикере? Опиши кратко, точно и ехидно на русском. ${emojiHint}. Если на стикере есть текст, обязательно его напиши.` },
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
        return null;
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
    let userHandle = realUser.username || ""; // Захватываем username
    let userText = msg.text || "";
    let photoDescription = "";

    // Корректная обработка стикеров, фото и премиум-эмодзи
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

    // Обработка премиум-эмодзи (Custom Emojis)
    const entities = msg.entities || msg.caption_entities;
    if (entities) {
        const customEmojis = entities.filter(e => e.type === 'custom_emoji');
        if (customEmojis.length > 0) {
            const uniqueEmojiIds = [...new Set(customEmojis.map(e => e.custom_emoji_id))].slice(0, 3); // Лимит 3
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

        if (resp.tool_calls || resp.function_call) {
            chatHistory[chatId].push(resp);
            const calls = resp.tool_calls || [resp.function_call];
            for (const tc of calls) {
                // ПЕРЕДАЕМ userHandle ДЛЯ set_reminder
                const res = await executeToolCall(tc, chatId, msg.message_id, userName, userId, callerIsAdmin, userHandle);
                if (resp.tool_calls) {
                    chatHistory[chatId].push({ role: 'tool', tool_call_id: tc.id, name: tc.function.name, content: res });
                } else {
                    chatHistory[chatId].push({ role: 'assistant', content: `[SYSTEM: Результат ${tc.name}: ${res}]` });
                }
            }
            const second = await openai.chat.completions.create({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                temperature: 0.8
            });
            const final = second.choices[0].message.content || "Готово.";
            await safeSendMessage(chatId, final, msg.message_id);
            chatHistory[chatId].push({ role: 'assistant', content: final });
        } else {
            const res = resp.content || "Ммм?";
            await safeSendMessage(chatId, res, msg.message_id);
            chatHistory[chatId].push({ role: 'assistant', content: res });
        }

        // Логика фоновой памяти (сохранение контекста каждые 15 сообщений)
        if (!messageCount[chatId]) messageCount[chatId] = 0;
        if (++messageCount[chatId] >= 15) {
            if (!extractionBuffer[chatId]) extractionBuffer[chatId] = [];
            extractionBuffer[chatId].push(`${userName}: ${userText}`);
            extractAndSaveFacts(chatId, extractionBuffer[chatId].join('\n'), Object.values(activeParticipants[chatId]).map(p => p.firstName));
            messageCount[chatId] = 0;
            extractionBuffer[chatId] = extractionBuffer[chatId].slice(-10);
        }

    } catch (e) {
        console.error('AI Error:', e.message);
        await safeSendMessage(chatId, "Что-то не так с головой... Попробуй позже.", msg.message_id);
    }
}

module.exports = { handleAIChat, aiMood, AI_NAME };