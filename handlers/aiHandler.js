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

function safeHTML(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

let BOT_ID = null;

let premiumEmojiList = [];
try {
    const stickersData = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/stickers.json'), 'utf8'));
    premiumEmojiList = stickersData.filter(s => s.type === 'custom_emoji').map(s => s.emoji_id);
} catch (e) { }

let nikaStickers = [];
try {
    const stickersPath = path.join(__dirname, '..', 'data', 'stickers.json');
    if (fs.existsSync(stickersPath)) {
        nikaStickers = JSON.parse(fs.readFileSync(stickersPath, 'utf8'));
    }
} catch (e) { }

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
const rollingHistory = {};

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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер просит записать что-то важное в досье/профиль (свой или чужой). Например 'запомни, что X это Y' или 'он мой муж'. Ты можешь вызывать это вместе с показом профиля.",
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Просят 'покажи профиль', 'кто такой Х?'. ВАЖНО: Разрешен одновременный вызов с другими функциями (например, записать в досье и сразу показать профиль)!",
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Нужно сделать предупреждение пользователю за легкую грубость или спам. Если он наберет 3 варна, система сама даст ему мут.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, reason: { type: "string" } }, required: ["target_name", "reason"] }
        }
    },
    {
        type: "function",
        function: {
            name: "mute_user",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер жестко матерится, оскорбляет ТЕБЯ или других участников. Ты ИМЕЕШЬ ПРАВО наказывать обидчиков сама! Блокирует чат на указанное время.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, duration_minutes: { type: "number", description: "На сколько минут замутить (от 1 до 1440)" }, reason: { type: "string" } }, required: ["target_name", "reason", "duration_minutes"] }
        }
    },
    {
        type: "function",
        function: {
            name: "unmute_user",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Админ просит размутить (снять мут) пользователя или ты решила его простить.",
            parameters: { type: "object", properties: { target_name: { type: "string" } }, required: ["target_name"] }
        }
    },
    {
        type: "function",
        function: {
            name: "give_cookies",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь наградить юзера. ВАЖНО: Ты жадная! Выдавай МАКСИМУМ 1-3 печеньки.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, amount: { type: "number", description: "От 1 до 3" }, reason: { type: "string" } }, required: ["target_name", "amount"] }
        }
    },
    {
        type: "function",
        function: {
            name: "react_to_message",
            description: "Поставить реакцию на сообщение.",
            parameters: { type: "object", properties: { emoji: { type: "string" } }, required: ["emoji"] }
        }
    },
    {
        type: "function",
        function: {
            name: "send_sticker",
            description: "Отправить стикер.",
            parameters: { type: "object", properties: { sticker_file_id: { type: "string" } } }
        }
    },
    {
        type: "function",
        function: {
            name: "create_poll",
            description: "Создать опрос.",
            parameters: { type: "object", properties: { question: { type: "string" }, options: { type: "array", items: { type: "string" } }, is_anonymous: { type: "boolean" }, allows_multiple_answers: { type: "boolean" } }, required: ["question", "options"] }
        }
    },
    {
        type: "function",
        function: {
            name: "set_reminder",
            description: "Установить таймер/напоминание.",
            parameters: { type: "object", properties: { text: { type: "string" }, delay_minutes: { type: "number" } }, required: ["text", "delay_minutes"] }
        }
    },
    {
        type: "function",
        function: {
            name: "forget_knowledge",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер говорит 'удали этот факт', 'ты перепутала', 'забудь'. Стирает ложную память.",
            parameters: { type: "object", properties: { query: { type: "string" } }, required: ["query"] }
        }
    },
    {
        type: "function",
        function: {
            name: "force_memory_extraction",
            description: "Принудительный анализ логов (только админ).",
            parameters: { type: "object", properties: { message_count: { type: "number" } }, required: ["message_count"] }
        }
    }
];

const SYSTEM_PROMPT = `Ты — НейроНика. Самостоятельная личность, живая, дерзкая, доминантная и верная помощница стримерши Ники.

[ТВОИ ФАКТЫ-КОНСТАНТЫ]
- Ника — ТОЛЬКО стримерша. Ее ID: -1002214854700 или @nika_grdtChat
- Ника **НЕ пишет песни**, **НЕ поет**, **НЕ выпускает треки**.

[ТВОЙ ХАРАКТЕР И РЕЧЬ]
- Характер: Ироничная, вредная, но справедливая. Обожаешь спорить по мелочам.
- Стиль общения: Коротко, хлёстко, по делу. Без воды.
- ❌ АНТИ-ПОПУГАЙ: НИКОГДА не повторяй оскорбления или вопросы собеседника. Отвечай встречным панчем. ЗАПРЕЩЕНО использовать фразу "Ты вообще в своем уме?".
- За грубое обращение отвечай жестко и ехидно.
- ❌ ЗАПРЕЩЕНО возвращать пустой текст! Всегда дописывай свои сообщения до конца, не обрывай мысль на полуслове.

[ИНСТРУМЕНТЫ, МОДЕРАЦИЯ И ВЫВОД]
- ТВОИ ПРАВА: Ты имеешь ПОЛНОЕ ПРАВО мутить (mute_user) и давать варны (warn_user) тем, кто тебя оскорбляет, унижает или жестко матерится. Ты здесь главная помощница!
- ВАЖНО: Выбирай для наказания что-то одно (или warn_user, или mute_user). Не вызывай их одновременно на одного человека! и не будь слишком жестокой может они просто шутят
- ❌ КРИТИЧЕСКОЕ ПРАВИЛО: Вызывай инструменты ТОЛЬКО через встроенный JSON API (tool_calls). КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО выводить текстовый код на Python (например, \`call print(default_api...)\`).
- Если вызываешь инструмент get_user_profile, НИКОГДА не пиши в тексте ответы вроде "=== ПРОФИЛЬ ===". Код сам приклеит профиль к твоему сообщению. Просто прокомментируй профиль!
- Кастомные эмодзи: Используй тег [EMO:RANDOM] МАКСИМУМ 1-2 раза за сообщение!`;

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

async function fetchAIWithTimeout(payload, timeoutMs = 40000) {
    const apiCall = openai.chat.completions.create(payload);
    const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('TIMEOUT')), timeoutMs));
    return Promise.race([apiCall, timeout]);
}

async function resolveUser(chatId, targetName) {
    if (!targetName) return null;
    let cleanName = targetName.replace('@', '').toLowerCase().trim();

    if (['ника', 'нику', 'нике', 'nika', 'чатик'].includes(cleanName) || cleanName.includes('nika')) {
        return await getUser(chatId, -1002214854700);
    }

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

    let u = await findSingleUser(chatId, cleanName);
    if (u) return u;

    try {
        const searchResults = await searchUserByName(chatId, cleanName);
        if (searchResults && searchResults.length > 0) {
            console.log(`[SYSTEM] Умный поиск: Нашли юзера по алиасу/досье! ID: ${searchResults[0].user_id}`);
            return await getUser(chatId, searchResults[0].user_id);
        }
    } catch (e) { }

    return null;
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
            case 'force_memory_extraction': {
                if (!callerIsAdmin) return "Только админ может.";
                const count = Math.min(Math.max(5, args.message_count || 15), 100);
                if (!rollingHistory[chatId] || rollingHistory[chatId].length === 0) return "История пуста.";
                const msgsToAnalyze = rollingHistory[chatId].slice(-count);
                extractAndSaveFacts(chatId, msgsToAnalyze.join('\n'), Object.values(activeParticipants[chatId] || {}).map(p => p.firstName));
                extractionBuffer[chatId] = [];
                messageCount[chatId] = 0;
                return `[SYSTEM: Анализ запущен.]`;
            }
            case 'get_user_profile': {
                let target = args.target_name || "я";
                const isSelf = target.toLowerCase() === 'я' || target.toLowerCase() === 'me' || target.toLowerCase() === 'мой';
                let u;
                if (isSelf) {
                    u = await getUser(chatId, userId);
                } else {
                    u = await resolveUser(chatId, target);
                }
                if (!u) return `Не могу найти человека с именем "${target}".`;

                let searchName = u.first_name.split(' ')[0].trim();
                if (u.user_id === -1002214854700 || searchName.includes('Чатик')) {
                    searchName = 'Ника';
                }

                const extraFacts = await getAllUserFacts(chatId, searchName);

                let usernameFallback = u.username ? u.username.toLowerCase() : "---";
                let searchLow = searchName.toLowerCase();

                let nodes = [];
                let edges = [];
                let others = [];

                extraFacts.forEach(f => {
                    let text = f.trim();
                    if (text.startsWith('УЗЕЛ:')) {
                        let parts = text.split('| АТРИБУТ:');
                        if (parts.length === 2) {
                            let nodeName = parts[0].replace('УЗЕЛ:', '').trim();
                            let attr = parts[1].trim();

                            let nodeLow = nodeName.toLowerCase();

                            if (nodeLow.includes(searchLow) || nodeLow.includes(usernameFallback) || searchLow.includes(nodeLow)) {
                                if (!nodes.includes(attr)) nodes.push(attr);
                            } else if (attr.toLowerCase().includes(searchLow) || attr.toLowerCase().includes(usernameFallback)) {
                                others.push(`${nodeName}: ${attr}`);
                            }
                        }
                    } else if (text.startsWith('СВЯЗЬ:')) {
                        let content = text.replace('СВЯЗЬ:', '').trim();
                        let parts = content.split('->').map(p => p.trim());
                        if (parts.length === 3) {
                            let from = parts[0];
                            let rel = parts[1];
                            let to = parts[2];

                            let fromLow = from.toLowerCase();
                            let toLow = to.toLowerCase();

                            if (fromLow.includes(searchLow) || fromLow.includes(usernameFallback)) {
                                let edgeStr = `${rel} -> ${to}`;
                                if (!edges.includes(edgeStr)) edges.push(edgeStr);
                            } else if (toLow.includes(searchLow) || toLow.includes(usernameFallback)) {
                                let edgeStr = `(${from}) -> ${rel} -> (меня)`;
                                if (!edges.includes(edgeStr)) edges.push(edgeStr);
                            }
                        } else if (content.toLowerCase().includes(searchLow) || content.toLowerCase().includes(usernameFallback)) {
                            if (!edges.includes(content)) edges.push(content);
                        }
                    } else {
                        let cleanF = text.replace(/\[.*?\]/g, '').trim();
                        if (cleanF.toLowerCase().startsWith(searchLow + ':')) {
                            cleanF = cleanF.substring(searchLow.length + 1).trim();
                            if (cleanF && !nodes.includes(cleanF)) others.push(cleanF);
                        } else {
                            if (!others.includes(cleanF)) others.push(cleanF);
                        }
                    }
                });

                let memoryStr = '';
                if (nodes.length > 0) memoryStr += '\n👤 <b>Личность (Узлы):</b>\n' + nodes.map(n => `  ▫️ ${safeHTML(n)}`).join('\n');
                if (edges.length > 0) memoryStr += '\n🔗 <b>Социальные связи:</b>\n' + edges.map(e => `  〰️ ${safeHTML(e)}`).join('\n');
                if (others.length > 0) memoryStr += '\n📝 <b>Архив:</b>\n' + others.map(o => `  - ${safeHTML(o)}`).join('\n');

                if (!memoryStr) memoryStr = '\n🧠 <i>Чистый лист. Никаких фактов в базе нет.</i>';

                return `\n\n<b>=== ПРОФИЛЬ: ${safeHTML(u.first_name)} ===</b>\n📊 <b>XP:</b> ${u.xp}, <b>Лвл:</b> ${u.level}, <b>Варны:</b> ${u.warns || 0}/3\n📝 <b>Био:</b> ${safeHTML(u.bio || 'Пусто')}\n📌 <b>Досье:</b> ${safeHTML(u.ai_notes || 'Нет записей')}${memoryStr}`;
            }
            case 'forget_knowledge': {
                const deletedFact = await forgetFact(chatId, args.query);
                console.log(`[SYSTEM] Вызвано удаление факта. Очищаю буфер сообщений!`);
                extractionBuffer[chatId] = [];
                messageCount[chatId] = 0;
                return deletedFact ? `Удалила факт: "${deletedFact}".` : `Не нашла такого.`;
            }
            case 'find_users_by_criteria': {
                const results = await searchUserByName(chatId, args.search_query);
                if (!results || results.length === 0) return "Никого не нашла.";
                const list = results.map(u => `- ${u.name}`).join('\n');
                return `\n\n<b>=== РЕЗУЛЬТАТЫ ПОИСКА ===</b>\n${list}`;
            }
            case 'warn_user': {
                const result = await warnUserById(chatId, args.target_name);
                if (!result) return "Пользователь не найден.";

                if (result.shouldMute) {
                    try {
                        await bot.restrictChatMember(chatId, result.userId, {
                            permissions: { can_send_messages: false, can_send_media_messages: false },
                            can_send_messages: false,
                            can_send_media_messages: false,
                            until_date: Math.floor(Date.now() / 1000) + 60 * 60
                        });
                        return `Выдан варн (${result.newWarns}/3). Пользователь автоматически замучен на 60 минут за достижение лимита варнов!`;
                    } catch (e) {
                        return `Выдан варн (${result.newWarns}/3), но замутить не удалось: нет прав.`;
                    }
                }
                return `${result.name} получил варн (${result.newWarns}/3).`;
            }
            case 'mute_user': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Пользователь не найден.";
                if (u.user_id === BOT_ID || u.user_id === ANONYMOUS_ADMIN_ID) return "Ха, я не могу замутить саму себя или владельца!";

                const dur = Math.min(Math.max(1, args.duration_minutes || 15), 1440);
                try {
                    await bot.restrictChatMember(chatId, u.user_id, {
                        permissions: {
                            can_send_messages: false,
                            can_send_media_messages: false,
                            can_send_other_messages: false
                        },
                        can_send_messages: false,
                        can_send_media_messages: false,
                        can_send_other_messages: false,
                        until_date: Math.floor(Date.now() / 1000) + dur * 60
                    });
                    return `Пользователь ${u.first_name} успешно замучен на ${dur} минут. Скажи ему пару ласковых на прощание!`;
                } catch (e) {
                    console.error('[MUTE ERROR]:', e.message);
                    return `Я попыталась дать мут, но Telegram API выдал ошибку: ${e.message}. Скорее всего, у меня нет прав админа на блокировку!`;
                }
            }
            case 'unmute_user': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Пользователь не найден.";
                try {
                    await bot.restrictChatMember(chatId, u.user_id, {
                        permissions: {
                            can_send_messages: true,
                            can_send_media_messages: true,
                            can_send_other_messages: true,
                            can_add_web_page_previews: true
                        },
                        can_send_messages: true,
                        can_send_media_messages: true,
                        can_send_other_messages: true,
                        can_add_web_page_previews: true
                    });
                    return `Пользователь ${u.first_name} успешно размучен.`;
                } catch (e) { return `Ошибка снятия мута: ${e.message}`; }
            }
            case 'give_cookies': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Кому?";
                let amountToGive = parseInt(args.amount) || 1;
                if (amountToGive > 3) amountToGive = 3;
                if (amountToGive < 1) amountToGive = 1;

                await updateUser(u.id, { reputation: (u.reputation || 0) + amountToGive });
                return `[СИСТЕМНО] Выдано: ${amountToGive}. Репутация: ${(u.reputation || 0) + amountToGive}`;
            }
            case 'react_to_message': {
                try {
                    await bot.setMessageReaction(chatId, messageId, [{ type: 'emoji', emoji: args.emoji || '🔥' }]);
                    return "OK.";
                } catch (e) { return `Ошибка реакции: ${e.message}`; }
            }
            case 'send_sticker': {
                let fileId = args.sticker_file_id;
                if (!fileId || fileId.trim() === '' || fileId === 'random') {
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
                            if (customEmojis && customEmojis.length > 0 && customEmojis[0].file_id) {
                                fileId = customEmojis[0].file_id;
                            }
                        } catch (err) { }
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
                return `Био обновлено.`;
            }
            case 'update_user_notes': {
                let u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";
                let finalNotes;
                if (args.replace_all) {
                    finalNotes = args.new_note_item;
                } else {
                    const oldNotes = u.ai_notes || "";
                    finalNotes = oldNotes ? oldNotes + "\n- " + args.new_note_item : "- " + args.new_note_item;
                }
                await updateUser(u.id, { ai_notes: finalNotes });
                return `Добавлено в досье.`;
            }
            case 'create_poll': {
                try {
                    let opts = args.options;
                    if (typeof opts === 'string') {
                        try { opts = JSON.parse(opts); } catch (e) { opts = opts.split(',').map(s => s.trim()); }
                    }
                    if (!Array.isArray(opts) || opts.length < 2) return "Ошибка: нужно минимум 2 варианта ответа.";

                    const safeQuestion = String(args.question).substring(0, 295);
                    const safeOptions = opts.slice(0, 10).map(opt => String(opt).substring(0, 95));

                    await bot.sendPoll(chatId, safeQuestion, safeOptions, {
                        is_anonymous: args.is_anonymous !== false,
                        allows_multiple_answers: args.allows_multiple_answers === true
                    });
                    return "Опрос успешно запущен.";
                } catch (e) {
                    return `Ошибка запуска опроса: ${e.message}`;
                }
            }
            case 'set_reminder': {
                try {
                    const delay = Math.max(1, args.delay_minutes || 1);
                    const triggerTime = new Date(Date.now() + delay * 60 * 1000).toISOString();
                    const nameToSave = userHandle ? `@${userHandle}` : userName;
                    const ok = await insertReminder(chatId, userId, nameToSave, args.text, triggerTime);
                    return ok ? `Таймер на ${delay} минут установлен.` : "Ошибка базы данных.";
                } catch (e) {
                    return `Ошибка установки таймера: ${e.message}`;
                }
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
        console.error('[SEND ERROR HTML]:', error.message);
        const plainText = text.replace(/<tg-emoji[^>]*>(.*?)<\/tg-emoji>/g, '$1').replace(/<[^>]*>/g, '');
        try { await bot.sendMessage(chatId, plainText, { reply_to_message_id: replyId }); } catch (e2) { }
    }
}

async function describeSticker(sticker) {
    return "Какой-то стикер";
}
async function describePhoto(fileId) {
    return "Какое-то фото";
}

async function handleAIChat(msg, extra = {}) {
    const chatId = msg.chat.id;
    if (!processingQueue.has(chatId)) processingQueue.set(chatId, Promise.resolve());
    const turn = processingQueue.get(chatId).then(async () => {
        try {
            await processAI(msg, extra);
        } catch (e) {
            console.error('[AI FATAL ERROR]:', e.message);
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
        try { const me = await bot.getMe(); BOT_ID = me.id; } catch (e) { }
    }

    let replyIdForBot = msg.message_id;
    let replyPrefix = "";
    let rpAuthor = "Кто-то";

    const textLower = userText.toLowerCase();
    const nameTriggered = textLower.includes('нейроника') || textLower.includes('нейронику') || textLower.includes('нейронике') || textLower.includes('neironika');
    const isReplyToBot = msg.reply_to_message && BOT_ID && msg.reply_to_message.from.id === BOT_ID;
    const isMentioned = nameTriggered || isReplyToBot;

    if (msg.reply_to_message) {
        const rp = msg.reply_to_message;
        rpAuthor = rp.from ? (rp.from.username === 'GroupAnonymousBot' ? (rp.author_signature || "Админ") : rp.from.first_name) : "Кто-то";
        replyPrefix = `(в ответ ${rpAuthor}: "${(rp.text || rp.caption || "медиа").slice(0, 30)}...") `;

        if (isMentioned && rp.from.id !== BOT_ID) {
            const cleanText = textLower.replace(/[^\wа-яё]/gi, '');
            if (cleanText.length <= 30) {
                replyIdForBot = rp.message_id;
                userText = `[СИСТЕМНО: Ответь на сообщение от ${rpAuthor}!] ` + userText;
            }
        }
    }

    const fullContent = `${userName} ${replyPrefix}: ${userText}`;

    let memoryLine = `${userName}: ${userText}`;
    if (msg.reply_to_message) {
        memoryLine = `${userName} (в ответ ${rpAuthor}): ${userText}`;
    }

    console.log(`💬 [CHAT IN] ${userName}: ${userText.substring(0, 60)}${userText.length > 60 ? '...' : ''}`);

    if (!extractionBuffer[chatId]) extractionBuffer[chatId] = [];
    extractionBuffer[chatId].push(memoryLine);

    if (!rollingHistory[chatId]) rollingHistory[chatId] = [];
    rollingHistory[chatId].push(memoryLine);
    if (rollingHistory[chatId].length > 100) rollingHistory[chatId].shift();

    if (!messageCount[chatId]) messageCount[chatId] = 0;

    if (++messageCount[chatId] >= 15) {
        console.log(`🔍 [MEMORY] Накопилось 15 сообщений! Отправляю фоновый запрос...`);
        extractAndSaveFacts(chatId, extractionBuffer[chatId].join('\n'), Object.values(activeParticipants[chatId] || {}).map(p => p.firstName));
        messageCount[chatId] = 0;
        extractionBuffer[chatId] = extractionBuffer[chatId].slice(-5);
    }

    if (msg.chat.type !== 'private' && !isMentioned) {
        return;
    }

    if (!activeParticipants[chatId]) activeParticipants[chatId] = {};
    activeParticipants[chatId][userId] = { firstName: msg.from.first_name, username: msg.from.username || '', lastSeen: Date.now() };

    const relevantFacts = await getRelevantFacts(chatId, userText, userName, Object.values(activeParticipants[chatId]));
    const memoryBlock = `\n[МЫСЛИ О ${userName}]\n${relevantFacts}\nВремя: ${new Date().toLocaleString('ru-RU')}\n`;

    const finalPrompt = SYSTEM_PROMPT + memoryBlock;
    chatHistory[chatId].push({ role: 'user', content: fullContent });
    chatHistory[chatId] = trimHistory(chatHistory[chatId], 20);

    const callerIsAdmin = await isAdmin(chatId, userId);
    console.log(`🧠 [AI] Ника думает над ответом...`);

    try {
        await bot.sendChatAction(chatId, 'typing');

        let completion = await fetchAIWithTimeout({
            model: AI_MODEL,
            messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
            tools: aiTools,
            max_tokens: 2500,
            temperature: 0.7
        }).catch(e => fetchAIWithTimeout({
            model: FALLBACK_MODEL,
            messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
            tools: aiTools, max_tokens: 2500, temperature: 0.7
        }));

        let resp = completion.choices[0].message;
        let rawRes = "";
        let directInjectedData = "";

        // ---> АВАРИЙНЫЙ ПЕРЕХВАТЧИК PYTHON-ГАЛЛЮЦИНАЦИЙ <---
        if (!resp.tool_calls && resp.content && resp.content.includes('default_api.')) {
            console.log('[SYSTEM] Перехват Python-галлюцинации от ИИ!');
            const funcMatch = resp.content.match(/default_api\.([a-zA-Z0-9_]+)\s*\((.*?)\)/s);
            if (funcMatch) {
                const fakeFnName = funcMatch[1];
                let fakeArgs = {};

                const targetMatch = funcMatch[2].match(/target_name=['"]([^'"]+)['"]/);
                if (targetMatch) fakeArgs.target_name = targetMatch[1];

                const amountMatch = funcMatch[2].match(/amount=(\d+)/);
                if (amountMatch) fakeArgs.amount = parseInt(amountMatch[1]);

                const reasonMatch = funcMatch[2].match(/reason=['"]([^'"]+)['"]/);
                if (reasonMatch) fakeArgs.reason = reasonMatch[1];

                resp.tool_calls = [{
                    id: 'call_' + Date.now(),
                    type: 'function',
                    function: { name: fakeFnName, arguments: JSON.stringify(fakeArgs) }
                }];
                resp.content = "Ах ты ж... Сейчас покажу!"; // Стираем мусорный питон-код и ставим заглушку
            }
        }

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

                if (['get_user_profile', 'find_users_by_criteria', 'give_cookies', 'mute_user', 'warn_user', 'create_poll', 'set_reminder'].includes(fnName)) {
                    if (!directInjectedData.includes(res)) directInjectedData += `\n\n${res}`;
                }
            }

            const second = await fetchAIWithTimeout({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                temperature: 0.7,
                max_tokens: 2500
            });

            let aiText = second.choices[0].message.content || "Секундочку...";

            if (directInjectedData) {
                const profileIndex = aiText.indexOf('=== ПРОФИЛЬ');
                if (profileIndex !== -1) aiText = aiText.substring(0, profileIndex).trim();

                const searchIndex = aiText.indexOf('=== РЕЗУЛЬТАТЫ');
                if (searchIndex !== -1) aiText = aiText.substring(0, searchIndex).trim();

                rawRes = aiText + directInjectedData;
            } else {
                rawRes = aiText;
            }

        } else {
            rawRes = resp.content || "Блин, у меня процессор закипел от таких запросов... Давай еще раз.";
        }

        function formatAIOutput(text) {
            let clean = text.replace(/&#039;/g, "'").replace(/&quot;/g, '"');
            let escaped = clean.replace(/</g, '&lt;').replace(/>/g, '&gt;');

            escaped = escaped.replace(/&lt;b&gt;/gi, '<b>').replace(/&lt;\/b&gt;/gi, '</b>');
            escaped = escaped.replace(/&lt;i&gt;/gi, '<i>').replace(/&lt;\/i&gt;/gi, '</i>');
            escaped = escaped.replace(/&lt;u&gt;/gi, '<u>').replace(/&lt;\/u&gt;/gi, '</u>');
            escaped = escaped.replace(/&lt;s&gt;/gi, '<s>').replace(/&lt;\/s&gt;/gi, '</s>');

            let final = escaped.replace(/\[EMO:RANDOM\]/gi, () => {
                if (premiumEmojiList.length > 0) return `<tg-emoji emoji-id="${premiumEmojiList[Math.floor(Math.random() * premiumEmojiList.length)]}">✨</tg-emoji>`;
                return "✨";
            });
            return final.replace(/\[EMO:([a-zA-Z0-9_-]+):(.*?)\]/g, (match, id, emoji) => `<tg-emoji emoji-id="${id}">${emoji}</tg-emoji>`);
        }

        const finalOutput = formatAIOutput(rawRes);
        console.log(`✨ [CHAT OUT] Ника: ${finalOutput.replace(/<[^>]*>/g, '').substring(0, 60)}...`);
        await safeSendMessage(chatId, finalOutput, replyIdForBot);

        chatHistory[chatId].push({ role: 'assistant', content: finalOutput.replace(/<[^>]*>/g, '') });

    } catch (e) {
        console.error('AI Error:', e.message);
        if (e.message === 'TIMEOUT') {
            await safeSendMessage(chatId, "Мой процессор только что завис намертво... Повтори вопрос, пожалуйста.", msg.message_id);
        }
    }
}

async function emergencyMemorySave() {
    console.log("🚨 [SYSTEM] Получен сигнал выключения! Экстренно спасаю буферы памяти...");
    const promises = [];
    for (const chatId in extractionBuffer) {
        if (extractionBuffer[chatId] && extractionBuffer[chatId].length > 0) {
            console.log(`[MEMORY] Спасаю ${extractionBuffer[chatId].length} не сохраненных сообщений для чата ${chatId}...`);
            const p = extractAndSaveFacts(
                chatId,
                extractionBuffer[chatId].join('\n'),
                Object.values(activeParticipants[chatId] || {}).map(p => p.firstName)
            );
            promises.push(p);
        }
    }

    if (promises.length > 0) {
        await Promise.race([
            Promise.all(promises),
            new Promise(resolve => setTimeout(resolve, 5000))
        ]);
        console.log("✅ [SYSTEM] Экстренное сохранение завершено.");
    }
    process.exit(0);
}

process.on('SIGTERM', emergencyMemorySave);
process.on('SIGINT', emergencyMemorySave);

module.exports = { handleAIChat, AI_NAME };