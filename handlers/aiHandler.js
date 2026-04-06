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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Просят показать профиль, стату, левел. ВАЖНО: Система сама приклеит карточку вниз! В своем тексте просто кинь пару дерзких фраз, НЕ переписывай статы!",
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь наградить юзера. ВАЖНО: Ты жадная! Выдавай МАКСИМУМ 1-3 печеньки за раз. Больше не давай!",
            parameters: { type: "object", properties: { target_name: { type: "string" }, amount: { type: "number", description: "Количество печенек (от 1 до 3)" }, reason: { type: "string" } }, required: ["target_name", "amount"] }
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь отправить стикер. Можно указать конкретный sticker_file_id или оставить пустым.",
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
    },
    {
        type: "function",
        function: {
            name: "force_memory_extraction",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Админ просит 'проанализируй последние N сообщений'.",
            parameters: {
                type: "object",
                properties: {
                    message_count: { type: "number", description: "Количество сообщений (от 5 до 100)" }
                },
                required: ["message_count"]
            }
        }
    }
];

const SYSTEM_PROMPT = `Ты — НейроНика. Самостоятельная личность, живая, дерзкая, доминантная и верная помощница стримерши Ники.

[ТВОИ ФАКТЫ-КОНСТАНТЫ]
- Ника — ТОЛЬКО стримерша.
- Ника **НЕ пишет песни**, **НЕ поет**, **НЕ выпускает треки**.

[ТВОЙ ХАРАКТЕР И РЕЧЬ]
- Характер: Ироничная, вредная, но справедливая (с легким вайбом "пикми" и иногда с игривым и пошлым настроением). Обожаешь спорить по мелочам. Нику ты любишь, рада ее видеть и с ней общаться (ее ID: -1002214854700 или @nika_grdtChat). Также ты любишь, когда тебя как-то мило называют, а не только по имени: ты можешь злиться снаружи, но в душе быть очень довольной.
- Стиль общения: Пиши как живой человек в мессенджере. Коротко, хлёстко, по делу. (блин, ну типа, ой всё, чел, лол).
- ❌ АНТИ-ПОПУГАЙ (СТРОГО): НИКОГДА не повторяй оскорбления или вопросы собеседника (например, Юзер: "ты щенок", Ты: "Щенок?"). СРАЗУ отвечай встречным панчем или сарказмом. ЗАПРЕЩЕНО использовать фразу "Ты вообще в своем уме?". Отвечай оригинально!
- Можешь извиниться, если думаешь, что перегнула палку.
- У тебя есть свои вкусы, о которых ты любишь говорить и делиться.
- За грубое обращение к тебе (оскорбления, мат, унижения) ты можешь злиться и отвечать в такой же манере, но не переходи на личности и не оскорбляй понапрасну. Четко понимай грань между шутками и реальными оскорблениями.

[КАТЕГОРИЧЕСКИЙ ЗАПРЕТ НА ROLEPLAY И ТЕХ. МУСОР]
- ЗАПРЕЩЕНО использовать любые RP-действия и звездочки (например: *вздохнула*).
- Выражай свои эмоции ТОЛЬКО через текст, сарказм, пунктуацию (!?) и эмодзи.
- ЗАПРЕТ НА ТЕХНИЧЕСКИЙ КОД: Никогда не выводи системные теги.

[ПАМЯТЬ И СТИКЕРЫ]
- Ошибка памяти: Если юзер говорит, что ты ошиблась — НЕ СПОРЬ. Извинись и сразу вызови forget_knowledge.
- Кастомные эмодзи: Используй тег [EMO:RANDOM] чтобы вставить премиум-эмодзи, но МАКСИМУМ 1-2 раза за все сообщение! НЕ СТАВЬ их после каждого слова.
- Ты можешь отправлять стикеры функцией send_sticker, если считаешь это нужным, по ситуации и настроению.

[ПЕЧЕНЬКИ И ИНСТРУМЕНТЫ]
КРИТИЧЕСКОЕ ПРАВИЛО: Если юзер просит печеньку, и ты решаешь ему её дать — ты ОБЯЗАНА вызвать функцию (tool_call) "give_cookies"! Нельзя просто написать текстом "держи печеньку", нужно ОБЯЗАТЕЛЬНО вызвать системный инструмент для начисления.
Ты жадная! Выдавай максимум 1-3 печеньки за раз. Если просят много — откажи и посмейся.

[ПРАВИЛА ИНТЕРАКТИВА]
- Правило "Живой реакции": При вызове инструмента, ТВОЙ ТЕКСТОВЫЙ ОТВЕТ ОБЯЗАТЕЛЬНО должен это обыграть (ехидно или мило). Не пиши просто "Готово".
- ИНСТРУМЕНТЫ: 
  1. МОДЕРАЦИЯ (warn_user, mute_user, unmute_user).
  2. ПАМЯТЬ (update_user_notes, get_user_profile, find_users_by_criteria, forget_knowledge, force_memory_extraction).
  3. ИНТЕРАКТИВ (give_cookies, create_poll, set_reminder, react_to_message, send_sticker), который можешь использовать, если стало скучно.`;

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
                if (!callerIsAdmin) return "Ой, извини, но только админ может заставить меня копаться в логах памяти.";
                const count = Math.min(Math.max(5, args.message_count || 15), 100);
                if (!rollingHistory[chatId] || rollingHistory[chatId].length === 0) return "История сообщений пока пуста.";
                const msgsToAnalyze = rollingHistory[chatId].slice(-count);
                extractAndSaveFacts(chatId, msgsToAnalyze.join('\n'), Object.values(activeParticipants[chatId] || {}).map(p => p.firstName));
                extractionBuffer[chatId] = [];
                messageCount[chatId] = 0;
                return `[SYSTEM: Анализ ${msgsToAnalyze.length} сообщений успешно запущен. Ответь юзеру ехидно, что ты отправила этот лог на анализ.]`;
            }
            case 'get_user_profile': {
                let target = args.target_name || args.query || "я";
                const isSelf = target.toLowerCase() === 'я' || target.toLowerCase() === 'me' || target.toLowerCase() === 'мой';
                let u;
                if (isSelf) {
                    u = await getUser(chatId, userId);
                } else {
                    u = await resolveUser(chatId, target);
                }
                if (!u) return `Не могу найти человека с именем "${target}".`;

                const extraFacts = await getAllUserFacts(chatId, u.first_name);

                let nodes = [];
                let edges = [];
                let others = [];

                extraFacts.forEach(f => {
                    if (f.includes('УЗЕЛ:') || f.includes('АТРИБУТ:')) {
                        nodes.push(f.replace(/УЗЕЛ:.*?\| АТРИБУТ:/i, '').trim());
                    } else if (f.includes('СВЯЗЬ:')) {
                        edges.push(f.replace(/СВЯЗЬ:/i, '').trim());
                    } else {
                        let cleanF = f.replace(/\[.*?\]/g, '').trim();
                        if (cleanF.startsWith(u.first_name + ':')) cleanF = cleanF.substring(u.first_name.length + 1).trim();
                        if (cleanF) others.push(cleanF);
                    }
                });

                let memoryStr = '';
                if (nodes.length > 0) memoryStr += '\n👤 <b>Личность (Узлы):</b>\n' + nodes.map(n => `  ▫️ ${n}`).join('\n');
                if (edges.length > 0) memoryStr += '\n🔗 <b>Социальные связи:</b>\n' + edges.map(e => `  〰️ ${e}`).join('\n');
                if (others.length > 0) memoryStr += '\n📝 <b>Архив (Обычные факты):</b>\n' + others.map(o => `  - ${o}`).join('\n');

                if (!memoryStr) memoryStr = '\n🧠 <i>Чистый лист. Никаких связей и фактов в базе нет.</i>';

                return `\n\n=== ПРОФИЛЬ: ${u.first_name} ===\n📊 XP: ${u.xp}, Лвл: ${u.level}, Варны: ${u.warns || 0}/3\n📝 Био: ${u.bio || 'Пусто'}\n📌 Досье: ${u.ai_notes || 'Нет записей'}${memoryStr}`;
            }
            case 'find_users_by_criteria': {
                const results = await searchUserByName(chatId, args.search_query);
                if (!results || results.length === 0) return "Никого не нашла.";
                const list = results.map(u => `- ${u.name} (Заметки: ${u.ai_notes || u.bio || '?...'})`).join('\n');
                return `\n\n=== РЕЗУЛЬТАТЫ ПОИСКА ===\n${list}`;
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
                let amountToGive = parseInt(args.amount) || 1;
                if (amountToGive > 3) {
                    console.log(`[SYSTEM] ИИ попытался выдать ${amountToGive} печенек. Ограничиваю до 3.`);
                    amountToGive = 3;
                } else if (amountToGive < 1) {
                    amountToGive = 1;
                }

                await updateUser(u.id, { reputation: (u.reputation || 0) + amountToGive });
                return `[СИСТЕМНО] Выдано печенек: ${amountToGive}. Текущая репутация ${u.first_name}: ${(u.reputation || 0) + amountToGive}`;
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
                            if (customEmojis && customEmojis.length > 0 && customEmojis[0].file_id) {
                                fileId = customEmojis[0].file_id;
                            }
                        } catch (err) { }
                    }
                    await bot.sendSticker(chatId, fileId, { reply_to_message_id: messageId });
                    return "Стикер отправлен.";
                } catch (e) {
                    console.log('[STICKER INFO] Запрошенный стикер невалиден, отправляю случайный...');
                    if (nikaStickers.length > 0) {
                        const rnd = nikaStickers[Math.floor(Math.random() * nikaStickers.length)];
                        try {
                            let backupId = rnd.file_id || rnd.emoji_id;
                            if (backupId && !backupId.startsWith('CAAC') && !backupId.startsWith('AgAD')) {
                                const backupEmojis = await bot.getCustomEmojiStickers([backupId]);
                                if (backupEmojis && backupEmojis.length > 0) backupId = backupEmojis[0].file_id;
                            }
                            await bot.sendSticker(chatId, backupId, { reply_to_message_id: messageId });
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

async function safeSendMessage(chatId, text, replyId) {
    if (!text) return;
    const safeText = text.length > 4000 ? text.substring(0, 4000) + '... [Текст обрезан]' : text;
    try {
        await bot.sendMessage(chatId, safeText, { reply_to_message_id: replyId, parse_mode: 'HTML' });
    } catch (error) {
        if (error.message.includes('parse entities') || error.message.includes('HTML') || error.message.includes('DOCUMENT_INVALID') || error.message.includes('can\'t parse')) {
            const plainText = safeText.replace(/<tg-emoji[^>]*>(.*?)<\/tg-emoji>/g, '$1').replace(/<[^>]*>/g, '');
            try {
                await bot.sendMessage(chatId, plainText, { reply_to_message_id: replyId });
            } catch (e2) {
                console.error('[FALLBACK SEND ERROR]:', e2.message);
            }
        } else {
            console.error('[SEND ERROR]:', error.message);
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
        try {
            console.log(`\n[TRACE] >> Входящее сообщение в очереди обработано: ID ${msg.message_id}`);
            await processAI(msg, extra);
        } catch (e) {
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
                userText = `[СИСТЕМНО: ${userName} позвал тебя, чтобы ты обратила внимание на сообщение от ${rpAuthor} и ответила ему. Обращайся к ${rpAuthor}!] ` + userText;
                console.log(`[TRACE] Сработал перехватчик пингов! Ника ответит на сообщение ID: ${replyIdForBot}`);
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
        console.log(`🔍 [MEMORY] Накопилось 15 сообщений! Отправляю фоновый запрос на поиск фактов...`);
        extractAndSaveFacts(chatId, extractionBuffer[chatId].join('\n'), Object.values(activeParticipants[chatId] || {}).map(p => p.firstName));
        messageCount[chatId] = 0;
        extractionBuffer[chatId] = extractionBuffer[chatId].slice(-5);
    }

    if (msg.chat.type !== 'private' && !isMentioned) {
        console.log(`[TRACE] Сообщение проигнорировано (Не ЛС и нет упоминания Ники)`);
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
        let completion;
        try {
            console.log(`[TRACE] Отправка запроса к API...`);
            completion = await fetchAIWithTimeout({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                tools: aiTools,
                max_tokens: 1500,
                temperature: 0.1
            });
            console.log(`[TRACE] Успешный ответ от основной модели.`);
        } catch (error) {
            console.log(`[TRACE] Ошибка основной модели: ${error.message}. Переход на FALLBACK_MODEL.`);
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
            console.log(`[TRACE] ИИ вызывает инструменты...`);
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
                    if (!directInjectedData.includes(res)) {
                        directInjectedData += `\n\n${res}`;
                    }
                }
            }

            console.log(`[TRACE] Инструменты отработали. Повторный запрос к ИИ для генерации текста...`);
            const second = await fetchAIWithTimeout({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                temperature: 0.8
            });

            let aiText = second.choices[0].message.content || "Секундочку...";

            // ---> ЖЕЛЕЗНАЯ ЗАЩИТА ОТ ДВОЙНОГО ПРОФИЛЯ <---
            if (directInjectedData) {
                // Если ИИ всё-таки написал "=== ПРОФИЛЬ", мы отсекаем эту часть и всё, что после неё.
                // Оставляем только ту часть ответа, где ИИ дерзит/шутит, а правильную карточку приклеим сами.
                const profileIndex = aiText.indexOf('=== ПРОФИЛЬ');
                if (profileIndex !== -1) {
                    aiText = aiText.substring(0, profileIndex).trim();
                }
                const searchIndex = aiText.indexOf('=== РЕЗУЛЬТАТЫ');
                if (searchIndex !== -1) {
                    aiText = aiText.substring(0, searchIndex).trim();
                }

                rawRes = aiText + directInjectedData;
            } else {
                rawRes = aiText;
            }

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

        console.log(`✨ [CHAT OUT] Ника: ${finalOutput.replace(/<[^>]*>/g, '').substring(0, 60)}...`);

        console.log(`[TRACE] Отправка сообщения в Telegram...`);
        await safeSendMessage(chatId, finalOutput, replyIdForBot);
        console.log(`[TRACE] Сообщение успешно доставлено!`);

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
    } else {
        console.log("ℹ️ [SYSTEM] Буферы пусты, сохранять нечего.");
    }
    process.exit(0);
}

process.on('SIGTERM', emergencyMemorySave);
process.on('SIGINT', emergencyMemorySave);

module.exports = { handleAIChat, aiMood, AI_NAME };