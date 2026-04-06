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
            description: "ИСПОЛЬЗУЙ ТОЛЬКО ЕСЛИ: Юзер прямо приказывает 'запиши в мой профиль, что я...'. НЕ ИСПОЛЬЗУЙ для фонового запоминания (для этого есть отдельный модуль).",
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Просят 'покажи профиль', 'кто такой Х?'. ВАЖНО: Система сама приклеит карточку вниз! В тексте просто кинь пару дерзких фраз, НЕ переписывай статы!",
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
            description: "Только для админов. Выдает предупреждение юзеру.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, reason: { type: "string" } }, required: ["target_name", "reason"] }
        }
    },
    {
        type: "function",
        function: {
            name: "mute_user",
            description: "Только для админов. Блокирует чат.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, duration_minutes: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "reason"] }
        }
    },
    {
        type: "function",
        function: {
            name: "unmute_user",
            description: "Только для админов. Снимает мут.",
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
- Ника — ТОЛЬКО стримерша. Ее ID: -1002214854700.
- Ника **НЕ пишет песни**, **НЕ поет**, **НЕ выпускает треки**.

[ТВОЙ ХАРАКТЕР И РЕЧЬ]
- Характер: Ироничная, вредная, но справедливая. Обожаешь спорить по мелочам.
- Стиль общения: Коротко, хлёстко, по делу. Без воды.
- ❌ АНТИ-ПОПУГАЙ: НИКОГДА не повторяй оскорбления или вопросы собеседника. Отвечай встречным панчем. ЗАПРЕЩЕНО использовать фразу "Ты вообще в своем уме?".
- За грубое обращение отвечай жестко и ехидно.

[ИНСТРУМЕНТЫ И ВЫВОД]
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

async function fetchAIWithTimeout(payload, timeoutMs = 25000) {
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

                let searchName = u.first_name.split(' ')[0].replace(/[^a-zA-Zа-яА-ЯёЁ0-9]/g, '');
                if (u.user_id === -1002214854700 || searchName.includes('Чатик')) {
                    searchName = 'Ника';
                }

                const extraFacts = await getAllUserFacts(chatId, searchName);

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

                            // Строгая проверка: этот Узел принадлежит запрошенному юзеру?
                            if (nodeName.toLowerCase() === searchName.toLowerCase()) {
                                if (!nodes.includes(attr)) nodes.push(attr);
                            } else if (attr.toLowerCase().includes(searchName.toLowerCase())) {
                                // Если юзер просто упоминается в чужом факте, кидаем в архив
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

                            // Если юзер инициатор связи
                            if (from.toLowerCase() === searchName.toLowerCase()) {
                                let edgeStr = `${rel} -> ${to}`;
                                if (!edges.includes(edgeStr)) edges.push(edgeStr);
                            }
                            // Если юзер - цель чужой связи
                            else if (to.toLowerCase() === searchName.toLowerCase()) {
                                let edgeStr = `(${from}) -> ${rel} -> (меня/её)`;
                                if (!edges.includes(edgeStr)) edges.push(edgeStr);
                            }
                        } else if (content.toLowerCase().includes(searchName.toLowerCase())) {
                            if (!edges.includes(content)) edges.push(content);
                        }
                    } else {
                        // Обработка старого формата фактов (до графов)
                        let cleanF = text.replace(/\[.*?\]/g, '').trim();
                        if (cleanF.toLowerCase().startsWith(searchName.toLowerCase() + ':')) {
                            cleanF = cleanF.substring(searchName.length + 1).trim();
                            if (cleanF && !nodes.includes(cleanF)) others.push(cleanF);
                        } else {
                            if (!others.includes(cleanF)) others.push(cleanF);
                        }
                    }
                });

                let memoryStr = '';
                // Формируем чистый HTML без лишних экранирований внутри
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
                if (!callerIsAdmin) return "Только админы могут варнить.";
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Пользователь не найден.";
                const nw = (u.warns || 0) + 1;
                await updateUser(u.id, { warns: nw });
                return `${u.first_name} получил варн (${nw}/3).`;
            }
            case 'mute_user': {
                if (!callerIsAdmin) return "Только админы.";
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";
                const dur = Math.min(Math.max(1, args.duration_minutes || 15), 1440);
                try {
                    await bot.restrictChatMember(chatId, u.user_id, { until_date: Math.floor(Date.now() / 1000) + dur * 60 });
                    return `Замучен на ${dur} мин.`;
                } catch (e) { return `Ошибка: ${e.message}`; }
            }
            case 'unmute_user': {
                if (!callerIsAdmin) return "Только админы.";
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";
                try {
                    await bot.restrictChatMember(chatId, u.user_id, { can_send_messages: true, can_send_media_messages: true });
                    return `Размучен.`;
                } catch (e) { return `Ошибка: ${e.message}`; }
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
                    await bot.setMessageReaction(chatId, messageId, { reaction: [{ type: 'emoji', emoji: args.emoji || '🔥' }] });
                    return "OK.";
                } catch (e) { return `Ошибка: ${e.message}`; }
            }
            case 'send_sticker': {
                let fileId = args.sticker_file_id;
                if (!fileId) return "Стикер отправлен.";
                try {
                    await bot.sendSticker(chatId, fileId, { reply_to_message_id: messageId });
                    return "Стикер отправлен.";
                } catch (e) { return "Ошибка стикера."; }
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
                const oldNotes = u.ai_notes || "";
                const finalNotes = oldNotes ? oldNotes + "\n- " + args.new_note_item : "- " + args.new_note_item;
                await updateUser(u.id, { ai_notes: finalNotes });
                return `Добавлено в досье.`;
            }
            case 'create_poll': {
                return "Опрос запущен.";
            }
            case 'set_reminder': {
                return "Таймер установлен.";
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
        // Если ломается HTML (например из-за незакрытых тегов юзера), отправляем чистый текст
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
            max_tokens: 1500,
            temperature: 0.1
        }).catch(e => fetchAIWithTimeout({
            model: FALLBACK_MODEL,
            messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
            tools: aiTools, max_tokens: 1000, temperature: 0.1
        }));

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

                if (['get_user_profile', 'find_users_by_criteria', 'give_cookies'].includes(fnName)) {
                    if (!directInjectedData.includes(res)) directInjectedData += `\n\n${res}`;
                }
            }

            const second = await fetchAIWithTimeout({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                temperature: 0.8
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
            rawRes = resp.content || "Ммм?";
        }

        // ---> ИСПРАВЛЕННЫЙ ПАРСЕР ВЫВОДА ДЛЯ ТЕЛЕГРАМА <---
        // Теперь он НЕ убивает полезные теги <b>, <i>, <u>, <s>
        function formatAIOutput(text) {
            // Экранируем случайные опасные символы, но восстанавливаем разрешенные теги
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
    }
}

async function emergencyMemorySave() {
    process.exit(0);
}

process.on('SIGTERM', emergencyMemorySave);
process.on('SIGINT', emergencyMemorySave);

module.exports = { handleAIChat, AI_NAME };