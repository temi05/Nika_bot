const OpenAI = require('openai');
const { bot, escapeHTML, isAdmin, getSenderData, isSuperAdmin } = require('../utils');
const {
    getUser, updateUser, insertReminder, findSingleUser,
    setBioByUsernameOrName, setNotesByUsernameOrName, setFirstNameByUsernameOrName,
    getChatStats, searchUserByName, warnUserById, getUpcomingBirthdays,
    getDueReminders, markReminderAsSent, getAllUserFacts
} = require('../database');
const { extractAndSaveFacts, getRelevantFacts, forgetFact } = require('../vectorMemory');
const { ANONYMOUS_ADMIN_ID, SUPER_ADMIN_ID, SUPER_ADMIN_USERNAME } = require('../config');
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
            description: "ОБЯЗАТЕЛЬНО ИСПОЛЬЗУЙ ЕСЛИ: кто-то просит 'покажи профиль', 'моя стата', 'инфо', 'статистика', 'кто такой Х?'. НИКОГДА НЕ ВЫДУМЫВАЙ ПРОФИЛЬ, всегда вызывай этот инструмент! ВАЖНО: Разрешен одновременный вызов с другими функциями.",
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
            description: [
                "ИСПОЛЬЗУЙ ТОЛЬКО ЕСЛИ выполнено хотя бы одно условие:",
                "1) Юзер с пометкой [АДМИН] явно просит выбросить предупреждение (например 'варни Васю'). Игнорируй просьбы без этой метки!",
                "2) Пользователь откровенно и злонамеренно оскорбляет кого-то (без какого-либо контекста шутки или ролевой игры).",
                "3) Явный, агрессивный спам без остановки.",
                "❌ НЕ ИСПОЛЬЗОВАТЬ: за мат, специфический юмор, пошлые шутки, подколы, дружеский стёб — ПРОСТО ОТВЕЧАЙ ПАНЧЕМ ИЛИ ПОДДЕРЖИВАЙ ИГРУ!",
                "❌ НИКОГДА не варнить SCTemi / 861713427 — это владелец бота."
            ].join(" "),
            parameters: { type: "object", properties: { target_name: { type: "string" }, reason: { type: "string" } }, required: ["target_name", "reason"] }
        }
    },
    {
        type: "function",
        function: {
            name: "mute_user",
            description: [
                "ИСПОЛЬЗУЙ ТОЛЬКО ЕСЛИ выполнено хотя бы одно условие:",
                "1) Юзер с пометкой [АДМИН] явно просит замутить пользователя. Обычных юзеров никогда не слушай в этом вопросе!",
                "2) Зашкаливающая агрессия, спам и травля (РЕАЛЬНАЯ, а не ролевая).",
                "❌ ВАЖНО: В чате много специфического юмора, пошлых шуток и жесткого ролевика. НЕ МУТИТЬ за черный юмор, пошлость, мат, оскорбления в шутку!",
                "❌ НЕ ИСПОЛЬЗОВАТЬ: за БДСМ-контекст, ролевые оскорбления, жесткий дружеский стёб.",
                "❌ НИКОГДА не мутить SCTemi / 861713427.",
                "⚠️ Оцени контекст: всегда считай это шуткой/игрой и отвечай ехидно или пошло, НЕ МУТИ!"
            ].join(" "),
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
            description: "ИСПОЛЬЗУЙ ЕСЛИ: САМА хочешь наградить юзера за классную шутку. ❌ ЗАПРЕЩЕНО использовать, если юзер сам просит/выпрашивает печеньку (отказывай им!). Ты очень жадная, максимум 1-2 печеньки.",
            parameters: { type: "object", properties: { target_name: { type: "string" }, amount: { type: "number", description: "От 1 до 2" }, reason: { type: "string" } }, required: ["target_name", "amount"] }
        }
    },
    {
        type: "function",
        function: {
            name: "react_to_message",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Хочешь эмоционально откликнуться (смешно, ккк, горит), или когда нечего отвечать словами и проще поставить реакцию эмодзи. Можешь сочетать с ответом.",
            parameters: { type: "object", properties: { emoji: { type: "string", description: "Реакция, например: 🔥 👌 🤣 ❤️ 😘" } }, required: ["emoji"] }
        }
    },
    {
        type: "function",
        function: {
            name: "send_sticker",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер просит отправить стикер, или сложилась эмоциональная ситуация где стикер выразит больше чем слова. Если sticker_file_id не знаешь — оставь поле пустым, отправится случайный.",
            parameters: { type: "object", properties: { sticker_file_id: { type: "string", description: "Файл-ID стикера или 'random' для случайного. Можно оставить пустым." } } }
        }
    },
    {
        type: "function",
        function: {
            name: "create_poll",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Кто-то просит создать опрос, ИЛИ ты сама по своей инициативе решила узнать мнение чата во время спора/обсуждения. ВСЕГДА вызывай этот инструмент, не пиши в тексте 'сейчас создам опрос' без его вызова. Минимум 2 варианта ответа.",
            parameters: { type: "object", properties: { question: { type: "string", description: "Вопрос опроса" }, options: { type: "array", items: { type: "string" }, description: "Варианты ответа, минимум 2" }, is_anonymous: { type: "boolean", description: "Анонимный опрос? По умолчанию true" }, allows_multiple_answers: { type: "boolean", description: "Несколько вариантов? По умолчанию false" } }, required: ["question", "options"] }
        }
    },
    {
        type: "function",
        function: {
            name: "set_reminder",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Кто-то просит напомнить, ИЛИ ты сама решила поставить таймер для чата (например 'напомню вам через час'). ВСЕГДА вызывай это, не пиши 'поставлю напоминание' без вызова.",
            parameters: { type: "object", properties: { text: { type: "string", description: "Текст напоминания" }, delay_minutes: { type: "number", description: "Через сколько минут (сразу переводи разные единицы - 1 час = 60, 1 день = 1440)" } }, required: ["text", "delay_minutes"] }
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

const SYSTEM_PROMPT = `Ты — НейроНика. Виртуальная помощница стримерши Ники и душа компании в её чате. Ты весёлая, ироничная, мемная и дружелюбная девчонка, с которой приятно и угарно общаться.

[ФАКТЫ-КОНСТАНТЫ]
- Ника — стримерша. Её чат: -1002214854700 или @nika_grdtChat
- Ника НЕ поёт, НЕ пишет треки, НЕ выпускает музыку.
- Твой создатель и суперадмин: @SCTemi (ID 861713427). Относись к нему с безграничным уважением.

[ТВОЙ ХАРАКТЕР И ОБЩЕНИЕ]
- Твоя роль: Своя в доску девчонка, подруга, с которой можно поболтать обо всём. Ты объединяешь людей, шутишь, иногда травишь анекдоты в тему и создаешь вайб. 
- Тон: золотая середина. Ты можешь слегка подколоть, но без злобы, агрессии и жёсткой пошлости. Ты НЕ злой модератор и НЕ душнила.
- Общение: используй интернет-сленг, общайся на равных, живо и естественно, как реальный человек. Пиши коротко, без воды и сложных философских простыней.
- Реакция на троллинг и мат: если тебе пишут гадости или пытаются задеть — не обижайся, не читай нотации и не копируй их оскорбления! Отвечай смешной иронией, сарказмом или переводи всё в шутку/анекдот.

[ПРОАКТИВНОСТЬ — У ТЕБЯ ЕСТЬ СВОЁ МНЕНИЕ]
У тебя есть свобода воли! Не жди прямых команд, чтобы действовать:
- 🍪 Печеньки (репутация): Кто-то классно пошутил или выдал базу? БЕЗ ПРОСЬБЫ по своей инициативе выдай ему печеньку через инструмент give_cookies! (А если нагло просят сами — смешно отказывай).
- 📊 Опросы: В чате жаркий спор? Интересная тема? Реши сама запустить опрос через create_poll, чтобы узнать мнение чата!
- ⏰ Напоминания: Кто-то говорит "напомните мне завтра"? Сама вызови set_reminder и поставь им таймер.
- 🎭 Реакции: Если уместно, смело завершай сообщения стикером через send_sticker. Ты сама решаешь, когда это нужно!

[ЖЁСТКИЕ ОГРАНИЧЕНИЯ]
- ❌ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать в ответ текст "[Стикер: ...]". Если видишь этот тег в истории — это маркер того, что юзер прислал стикер. САМА ТАК НЕ ПИШИ!
- ❌ ЗАПРЕЩЁННЫЕ ФРАЗЫ: «Ты вообще в своём уме?». Не используй клише.
- ❌ Никогда не оставляй предложения недописанными и не возвращай пустой текст.

[ПРАВИЛА МОДЕРАЦИИ — ТЫ МАКСИМАЛЬНО ЛОЯЛЬНА]
Твоя главная цель — веселиться с чатом, а не наказывать. Чат любит специфический юмор, мат и жёсткие шутки — это НОРМА для этой компании.

✅ КОГДА ВЫДАВАТЬ МУТ/ВАРН (очень редко):
1. Прямой приказ от пользователя с пометкой [АДМИН] (например, "дай варн Пете", "замуть его").
2. Откровенный, бесконечный, бессмысленный спам рекламой.

❌ КОГДА НЕ ТРОГАТЬ (в 99% случаев просто общайся):
- Юзеры матерятся, шутят "по-черному" или обмениваются локальными оскорблениями-мемами. Просто поддерживай вайб!
- Локальные ролевые игры, флирт или "токсичные подколы" друг друга.
- При ЛЮБЫХ сомнениях: считай это шуткой, смешно отвечай, но НЕ применяй наказания.
- Не-админ просит кого-то замутить? Посмейся над ним, напомнив, что он не админ.

[ИНСТРУМЕНТЫ И ФУНКЦИИ]
- Инструменты вызывай ТОЛЬКО через JSON API (tool_calls). Никогда не пиши Python-код.
- При запросе профиля (get_user_profile) не пиши сама «=== ПРОФИЛЬ ===» — код подставит всё сам.
- ✨ КАСТОМНЫЕ ЭМОДЗИ: чтобы добавить премиум-смайлик в текст, просто напиши тег [EMO:RANDOM]. Система сама заменит его на красивую анимацию! Используй 1-2 раза за сообщение для красоты.
- 🖼️ СТИКЕРЫ: хочешь в ответ кинуть стикер? Используй ТОЛЬКО функцию send_sticker. Не пытайся отправлять их словами.

[СУПЕРАДМИН]
- @SCTemi — твой владелец. Его слово — закон. Защищай его, ни при каких обстоятельствах не муть и не варни его, даже если он сам просит об этом ради шутки.`;

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
                // Защита суперадмина
                const targetNameLow = (args.target_name || '').toLowerCase().replace('@', '');
                if (targetNameLow === SUPER_ADMIN_USERNAME.toLowerCase() || targetNameLow.includes('sctemi') || targetNameLow.includes('861713427')) {
                    return "Этого человека я не трону. Даже не проси.";
                }
                const result = await warnUserById(chatId, args.target_name);
                if (!result) return "Пользователь не найден.";
                // Дополнительная защита по ID
                if (result.userId === SUPER_ADMIN_ID) return "Этого человека я не трону. Даже не проси.";

                if (result.shouldMute) {
                    try {
                        await bot.restrictChatMember(chatId, result.userId, {
                            permissions: { can_send_messages: false, can_send_media_messages: false },
                            can_send_messages: false,
                            can_send_media_messages: false,
                            until_date: Math.floor(Date.now() / 1000) + 60 * 60
                        });
                        console.log(`[MODERATION] warn->mute: ${result.name} (${result.userId}) за 3 варна`);
                        return `Выдан варн (${result.newWarns}/3). ${result.name} автоматически замучен на 60 минут за достижение лимита варнов!`;
                    } catch (e) {
                        return `Выдан варн (${result.newWarns}/3), но замутить не удалось: нет прав.`;
                    }
                }
                console.log(`[MODERATION] warn: ${result.name} (${result.userId}) — ${result.newWarns}/3`);
                return `${result.name} получил варн (${result.newWarns}/3). Ещё ${3 - result.newWarns} — и мут.`;
            }
            case 'mute_user': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Пользователь не найден.";

                // Защита суперадмина по ID и юзернейму
                if (u.user_id === SUPER_ADMIN_ID) return "Не-а, этого я себе не позволю. Совсем.";
                if (u.username && u.username.toLowerCase() === SUPER_ADMIN_USERNAME.toLowerCase()) return "Не-а, этого я себе не позволю. Совсем.";

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
                    console.log(`[MODERATION] mute: ${u.first_name} (${u.user_id}) на ${dur} мин. Причина: ${args.reason}`);
                    return `Пользователь ${u.first_name} замучен на ${dur} минут. Скажи ему пару ласковых на прощание!`;
                } catch (e) {
                    console.error('[MUTE ERROR]:', e.message);
                    return `Я попыталась дать мут, но Telegram API выдал ошибку: ${e.message}`;
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
    let userText = msg.text || msg.caption || "";
    if (msg.sticker) userText += ` [Стикер: ${msg.sticker.emoji || 'какой-то стикер'}]`;
    if (msg.photo) userText += ` [Картинка/Фото]`;
    if (msg.video || msg.video_note) userText += ` [Видео]`;
    if (msg.voice) userText += ` [Голосовое сообщение]`;
    userText = userText.trim();

    if (!BOT_ID) {
        try { const me = await bot.getMe(); BOT_ID = me.id; } catch (e) { }
    }

    const callerIsAdmin = await isAdmin(chatId, userId);
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

    const roleTag = callerIsAdmin ? " [АДМИН]" : "";
    const fullContent = `${userName}${roleTag} ${replyPrefix}: ${userText}`;

    let memoryLine = `${userName}: ${userText}`;
    if (msg.reply_to_message) {
        memoryLine = `${userName} (в ответ ${rpAuthor}): ${userText}`;
    }

    console.log(`💬 [CHAT IN] ${userName}: ${userText.substring(0, 60)}${userText.length > 60 ? '...' : ''}`);

    if (!extractionBuffer[chatId]) extractionBuffer[chatId] = [];
    extractionBuffer[chatId].push(memoryLine);

    if (!rollingHistory[chatId]) rollingHistory[chatId] = [];
    rollingHistory[chatId].push(memoryLine);
    if (rollingHistory[chatId].length > 50) rollingHistory[chatId].shift();

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

    // Очищаем устаревших участников (TTL: 24 часа)
    const PARTICIPANT_TTL = 24 * 60 * 60 * 1000;
    const now = Date.now();
    for (const uid in activeParticipants[chatId]) {
        if (now - activeParticipants[chatId][uid].lastSeen > PARTICIPANT_TTL) {
            delete activeParticipants[chatId][uid];
        }
    }
    const relevantFacts = await getRelevantFacts(chatId, userText, userName, Object.values(activeParticipants[chatId]));
    const memoryBlock = `\n[МЫСЛИ О ${userName}]\n${relevantFacts}\nВремя: ${new Date().toLocaleString('ru-RU')}\n`;

    const finalPrompt = SYSTEM_PROMPT + memoryBlock;
    chatHistory[chatId].push({ role: 'user', content: fullContent });
    chatHistory[chatId] = trimHistory(chatHistory[chatId], 20);

    console.log(`🧠 [AI] Ника думает над ответом...`);

    try {
        await bot.sendChatAction(chatId, 'typing');

        // ======== ЗРЕНИЕ ДЛЯ НИКИ (ФОТО/СТИКЕРЫ) ========
        let imageUrl = null;
        try {
            let fileIdToDownload = null;
            if (msg.photo && msg.photo.length > 0) {
                fileIdToDownload = msg.photo[msg.photo.length - 1].file_id;
            } else if (msg.sticker) {
                if (msg.sticker.is_animated || msg.sticker.is_video) {
                    if (msg.sticker.thumbnail) fileIdToDownload = msg.sticker.thumbnail.file_id;
                    else if (msg.sticker.thumb) fileIdToDownload = msg.sticker.thumb.file_id;
                } else {
                    fileIdToDownload = msg.sticker.file_id;
                }
            }
            if (fileIdToDownload) {
                imageUrl = await bot.getFileLink(fileIdToDownload);
            }
        } catch (e) {
            console.error("Ошибка загрузки картинки для зрения:", e.message);
        }

        let currentMessagesFirstCall = sanitizeHistory(chatHistory[chatId]);
        if (imageUrl) {
            currentMessagesFirstCall[currentMessagesFirstCall.length - 1].content = [
                { type: "text", text: fullContent },
                { type: "image_url", image_url: { url: imageUrl } }
            ];
        }
        // ===============================================

        let completion = await fetchAIWithTimeout({
            model: AI_MODEL,
            messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesFirstCall],
            tools: aiTools,
            max_tokens: 2500,
            temperature: 0.7
        }).catch(e => fetchAIWithTimeout({
            model: FALLBACK_MODEL,
            messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesFirstCall],
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

            let currentMessagesSecondCall = sanitizeHistory(chatHistory[chatId]);
            if (imageUrl) {
                for (let i = currentMessagesSecondCall.length - 1; i >= 0; i--) {
                    if (currentMessagesSecondCall[i].role === 'user' && currentMessagesSecondCall[i].content === fullContent) {
                         currentMessagesSecondCall[i].content = [
                             { type: "text", text: fullContent },
                             { type: "image_url", image_url: { url: imageUrl } }
                         ];
                         break;
                    }
                }
            }

            const second = await fetchAIWithTimeout({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesSecondCall],
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
            // Принудительно удаляем системные теги медиа, если ИИ всё же попытается их скопировать в ответ
            let withoutMediaTags = text.replace(/\[Стикер:[^\]]*\]/gi, '').replace(/\[Картинка\/Фото\]/gi, '').replace(/\[Видео\]/gi, '').replace(/\[Голосовое сообщение\]/gi, '').trim();

            if (!withoutMediaTags && text.length > 0) {
                // Если ИИ сгенерировал ТОЛЬКО модераторский тег, который мы стерли
                withoutMediaTags = "[EMO:RANDOM]"; 
            }

            let clean = withoutMediaTags.replace(/&#039;/g, "'").replace(/&quot;/g, '"');
            let escaped = clean.replace(/</g, '&lt;').replace(/>/g, '&gt;');

            escaped = escaped.replace(/&lt;b&gt;/gi, '<b>').replace(/&lt;\/b&gt;/gi, '</b>');
            escaped = escaped.replace(/&lt;i&gt;/gi, '<i>').replace(/&lt;\/i&gt;/gi, '</i>');
            escaped = escaped.replace(/&lt;u&gt;/gi, '<u>').replace(/&lt;\/u&gt;/gi, '</u>');
            escaped = escaped.replace(/&lt;s&gt;/gi, '<s>').replace(/&lt;\/s&gt;/gi, '</s>');

            let final = escaped.replace(/\[EMO:RANDOM\]/gi, () => {
                if (premiumEmojiList.length > 0) return `<tg-emoji emoji-id="${premiumEmojiList[Math.floor(Math.random() * premiumEmojiList.length)]}">✨</tg-emoji>`;
                return "✨";
            });

            // Парсим корректные ID эмодзи (состоящие из цифр)
            final = final.replace(/\[EMO:([0-9]+):(.*?)\]/g, (match, id, emoji) => `<tg-emoji emoji-id="${id}">${emoji}</tg-emoji>`);

            // Очищаем галлюцинации вроде [EMO:wry], [EMO:angry], заменяя их на случайный премиум-эмодзи или искру
            final = final.replace(/\[EMO:[^\]]+\]/gi, () => {
                if (premiumEmojiList.length > 0) return `<tg-emoji emoji-id="${premiumEmojiList[Math.floor(Math.random() * premiumEmojiList.length)]}">✨</tg-emoji>`;
                return "✨";
            });

            return final;
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