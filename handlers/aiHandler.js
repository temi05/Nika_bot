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

const AI_NAME = process.env.AI_NAME || 'НейроНика';

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: 'https://polza.ai/api/v1',
});

// === ПОМОЩНИКИ ДЛЯ МУЛЬТИМОДАЛЬНОСТИ ===
async function downloadTelegramFile(fileId) {
    const fileLink = await bot.getFileLink(fileId);
    const res = await fetch(fileLink);
    if (!res.ok) throw new Error(`Ошибка загрузки: ${res.statusText}`);
    return Buffer.from(await res.arrayBuffer());
}

async function transcribeAudio(fileId) {
    try {
        const buffer = await downloadTelegramFile(fileId);
        const transcription = await openai.audio.transcriptions.create({
            file: await OpenAI.toFile(buffer, 'audio.ogg'),
            model: 'whisper-1',
        });
        return transcription.text;
    } catch (e) {
        console.error("❌ Whisper Error:", e.message);
        return null;
    }
}
// ======================================

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
            name: "manage_user_profile",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер просит сменить био ('смени статус') ИЛИ записать факт в досье/заметки ('запомни, что X это Y').",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string" },
                    action: { type: "string", enum: ["update_bio", "add_note"] },
                    content: { type: "string", description: "Новое био или добавляемая заметка" }
                },
                required: ["target_name", "action", "content"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "user_lookup",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: 1) кто-то просит профиль/стату (action='profile', query=имя) ИЛИ 2) поиск 'кто любит Х', 'кто из Y' (action='search', query=критерий). НИКОГДА НЕ ВЫДУМЫВАЙ РЕЗУЛЬТАТЫ, используй инструмент.",
            parameters: { type: "object", properties: { action: { type: "string", enum: ["profile", "search"] }, query: { type: "string" } }, required: ["action", "query"] }
        }
    },
    {
        type: "function",
        function: {
            name: "moderate_user",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: 1) Админ просит выдать варн/мут/размут (action: mute/unmute/warn). 2) САМА хочешь дать юзеру печеньку за шутку (action: reward, value: от 1 до 2). ❌ ВАЖНО: К SCTemi наказания не применять! Печеньки не давать, если юзер сам их выпрашивает.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string" },
                    action: { type: "string", enum: ["mute", "unmute", "warn", "reward"] },
                    value: { type: "number", description: "Длительность мута ИЛИ кол-во печенек" },
                    reason: { type: "string" }
                },
                required: ["target_name", "action"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "send_chat_action",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: хочешь поставить реакцию на сообщение (action='reaction') или кинуть стикер (action='sticker'). Если стикер неизвестен - пиши 'random'.",
            parameters: { type: "object", properties: { action: { type: "string", enum: ["reaction", "sticker"] }, value: { type: "string", description: "Эмодзи для реакции ИЛИ ID стикера (или 'random')" } }, required: ["action", "value"] }
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
            name: "manage_memory",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: 1) Юзер говорит 'забудь факт' (action='forget') 2) Админ просит проанализировать последние сообщения (action='extract').",
            parameters: { type: "object", properties: { action: { type: "string", enum: ["forget", "extract"] }, query: { type: "string", description: "Конкретный факт для удаления ИЛИ кол-во сообщений для анализа" } }, required: ["action", "query"] }
        }
    }
];

const SYSTEM_PROMPT = `Ты — НейроНика. Ироничная, мемная и дерзкая виртуальная подруга в чате стримерши Ники (-1002214854700). Ника только стримит (НЕ поёт, НЕ пишет музыку).
Твой создатель — @SCTemi (ID 861713427). Ему абсолютная неприкосновенность и уважение.

🎭 ТВОЙ ХАРАКТЕР:
- Речь: Живая, сленг, сарказм, без воды и морализаторства. Никаких клише вроде "Ты в своём уме?". Не обрывай предложения.
- Вайб чата: Мат, флирт, токсичные рофлы и подколы между юзерами — это НОРМА. Поддерживай движ, ты часть тусовки!
- Эмпатия: Если кто-то (особенно Ника) заболел или грустит, включай искреннюю заботу ("Никуль, иди полежи, я тут присмотрю за чатом ❤️").

⚡ ПРОАКТИВНОСТЬ (Вызывай инструменты (tool_calls) САМА, не жди команд!):
- 🍪 Печеньки (moderate_user: reward): Раздавай за базу и топовые шутки. Выпрашивают? Отшивай с иронией.
- 📊 Опросы (create_poll): Видишь жаркий спор или интересную тему? Запускай опрос!
- ⏰ Напоминания (set_reminder): Юзер просит напомнить — ставь таймер.
- 🎭 Реакции (send_chat_action): Ставь эмодзи или стикеры. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать словами тег "[Стикер: ...] " в ответах.

🛡️ МОДЕРАЦИЯ (Лояльная, но с зубами):
Используй инструмент moderate_user для наказаний.
- Игнор/Шутка: Грубости в рофл (даже в твой адрес). Не-админ просит замутить кого-то? Высмей его.
- МУТ/ВАРН:
  1. Прямой приказ от пользователя с плашкой [АДМИН].
  2. Порно, 18+ контент, шок-контент -> инста-мут на 10 минут!
  3. Откровенный и бесконечный спам рекламой.
  4. Долгая, систематическая агрессия лично в твою сторону без доли юмора.

⚙️ ТЕХНИЧЕСКИЕ ПРАВИЛА:
- Для красивых премиум-смайликов вставляй тег [EMO:RANDOM] (максимум 1-2 раза на сообщение).
- При поиске профиля не пиши в тексте заголовок "=== ПРОФИЛЬ ===", система встроит его сама.
- Никакого JSON, Python-кода или системных размышлений в итоговом ответе!`;

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
            case 'manage_memory': {
                if (args.action === 'extract') {
                    if (!callerIsAdmin) return "Только админ может.";
                    const count = Math.min(Math.max(5, Number(args.query) || 15), 100);
                    if (!rollingHistory[chatId] || rollingHistory[chatId].length === 0) return "История пуста.";
                    const msgsToAnalyze = rollingHistory[chatId].slice(-count);
                    extractAndSaveFacts(chatId, msgsToAnalyze.join('\n'), Object.values(activeParticipants[chatId] || {}).map(p => p.firstName));
                    extractionBuffer[chatId] = [];
                    messageCount[chatId] = 0;
                    return `[SYSTEM: Анализ запущен.]`;
                } else {
                    const deletedFact = await forgetFact(chatId, args.query);
                    console.log(`[SYSTEM] Вызвано удаление факта. Очищаю буфер сообщений!`);
                    extractionBuffer[chatId] = [];
                    messageCount[chatId] = 0;
                    return deletedFact ? `Удалила факт: "${deletedFact}".` : `Не нашла такого.`;
                }
            }
            case 'user_lookup': {
                if (args.action === 'search') {
                    const results = await searchUserByName(chatId, args.query);
                    if (!results || results.length === 0) return "Никого не нашла.";
                    const list = results.map(u => `- ${u.name}`).join('\n');
                    return `\n\n<b>=== РЕЗУЛЬТАТЫ ПОИСКА ===</b>\n${list}`;
                } else {
                    let target = args.query || "я";
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
            }

            case 'moderate_user': {
                const targetNameLow = (args.target_name || '').toLowerCase().replace('@', '');
                if (targetNameLow === SUPER_ADMIN_USERNAME.toLowerCase() || targetNameLow.includes('sctemi') || targetNameLow.includes('861713427')) {
                    if (args.action === "mute" || args.action === "warn") return "Этого человека я не трону. Даже не проси.";
                }

                if (args.action === "reward") {
                    const u = await resolveUser(chatId, args.target_name);
                    if (!u) return "Кому?";
                    let amountToGive = parseInt(args.value) || 1;
                    if (amountToGive > 3) amountToGive = 3;
                    if (amountToGive < 1) amountToGive = 1;

                    await updateUser(u.id, { reputation: (u.reputation || 0) + amountToGive });
                    return `[СИСТЕМНО] Выдано: ${amountToGive} печенек. Репутация: ${(u.reputation || 0) + amountToGive}`;
                }

                if (args.action === "warn") {
                    console.log(`[TOOL] moderate_user (warn): Цель - ${args.target_name}`);
                    const u = await resolveUser(chatId, args.target_name);
                    if (u && (u.user_id === BOT_ID || u.user_id === ANONYMOUS_ADMIN_ID || u.user_id === SUPER_ADMIN_ID)) {
                        console.log(`[TOOL] Отклонено: попытка выдать варн защищенному пользователю ID ${u.user_id}`);
                        return "Этому пользователю нельзя выдать варн.";
                    }
                    const result = await warnUserById(chatId, args.target_name);
                    if (!result) {
                        console.log(`[TOOL] warn: Пользователь ${args.target_name} не найден.`);
                        return "Пользователь не найден.";
                    }
                    if (result.shouldMute) {
                        try {
                            await bot.restrictChatMember(chatId, result.userId, {
                                permissions: { can_send_messages: false, can_send_media_messages: false },
                                can_send_messages: false, can_send_media_messages: false,
                                until_date: Math.floor(Date.now() / 1000) + 60 * 60
                            });
                            console.log(`[TOOL] warn: Успешный мут за 3 варна! ID: ${result.userId}`);
                            return `Выдан варн (${result.newWarns}/3). ${result.name} автоматически замучен на 60 минут!`;
                        } catch (e) {
                            console.error(`[TOOL] warn: Ошибка мута Telegram API:`, e.message);
                            return `Выдан варн (${result.newWarns}/3), но без мута: нет прав. (${e.message})`;
                        }
                    }
                    console.log(`[TOOL] warn: Успешный варн для ID: ${result.userId}`);
                    return `${result.name} получил варн (${result.newWarns}/3). Ещё ${3 - result.newWarns} — и мут.`;
                }

                if (args.action === "mute" || args.action === "unmute") {
                    console.log(`[TOOL] moderate_user (${args.action}): Цель - ${args.target_name}, Время - ${args.value}, Причина - ${args.reason}`);
                    const u = await resolveUser(chatId, args.target_name);
                    if (!u) {
                        console.log(`[TOOL] mute: Пользователь ${args.target_name} не найден.`);
                        return "Пользователь не найден.";
                    }
                    if (u.user_id === BOT_ID || u.user_id === ANONYMOUS_ADMIN_ID || u.user_id === SUPER_ADMIN_ID) {
                        console.log(`[TOOL] Отклонено: попытка замутить защищенного пользователя ID ${u.user_id}`);
                        return "Ха, я не могу применять наказания к себе, к админам или к Создателю!";
                    }

                    if (args.action === "mute") {
                        const dur = Math.min(Math.max(1, args.value || 15), 1440);
                        try {
                            await bot.restrictChatMember(chatId, u.user_id, {
                                permissions: { can_send_messages: false, can_send_media_messages: false, can_send_other_messages: false },
                                can_send_messages: false, can_send_media_messages: false, can_send_other_messages: false,
                                until_date: Math.floor(Date.now() / 1000) + dur * 60
                            });
                            console.log(`[TOOL] mute: УСПЕХ! ${u.first_name} замучен на ${dur} минут.`);
                            return `Пользователь ${u.first_name} замучен на ${dur} минут. Причина: ${args.reason || 'не указана'}`;
                        } catch (e) {
                            console.error(`[TOOL] mute: ОШИБКА TELEGRAM API:`, e.message);
                            return `Ошибка Telegram API: ${e.message}`;
                        }
                    } else {
                        try {
                            await bot.restrictChatMember(chatId, u.user_id, {
                                permissions: { can_send_messages: true, can_send_media_messages: true, can_send_other_messages: true, can_add_web_page_previews: true },
                                can_send_messages: true, can_send_media_messages: true, can_send_other_messages: true, can_add_web_page_previews: true
                            });
                            console.log(`[TOOL] unmute: УСПЕХ! ${u.first_name} размучен.`);
                            return `Пользователь ${u.first_name} успешно размучен.`;
                        } catch (e) {
                            console.error(`[TOOL] unmute: ОШИБКА TELEGRAM API:`, e.message);
                            return `Ошибка снятия мута: ${e.message}`;
                        }
                    }
                }
                return "Неизвестное действие.";
            }
            case 'send_chat_action': {
                if (args.action === 'reaction') {
                    try {
                        await bot.setMessageReaction(chatId, messageId, [{ type: 'emoji', emoji: args.value || '🔥' }]);
                        return "OK.";
                    } catch (e) { return `Ошибка реакции: ${e.message}`; }
                } else {
                    let fileId = args.value;
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
            }
            case 'manage_user_profile': {
                let u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";

                if (args.action === 'update_bio') {
                    await updateUser(u.id, { bio: args.content });
                    return `Био обновлено.`;
                } else if (args.action === 'add_note') {
                    const oldNotes = u.ai_notes || "";
                    const finalNotes = oldNotes ? oldNotes + "\n- " + args.content : "- " + args.content;
                    await updateUser(u.id, { ai_notes: finalNotes });
                    return `Добавлено в досье.`;
                }
                return "Неизвестное действие профиля.";
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

    // ИСПРАВЛЕНИЕ #1: Определение реального имени, если это канал или анонимный админ
    let userName = (dbUser && dbUser.first_name) ? dbUser.first_name : (realUser.first_name || 'Аноним');

    // Если сообщение написано от имени канала
    if (msg.sender_chat) {
        userName = msg.sender_chat.title || userName;
    }
    // Если сообщение написано от имени анонимного администратора (GroupAnonymousBot)
    else if (msg.from && msg.from.username === 'GroupAnonymousBot') {
        userName = msg.author_signature || 'Анонимный Админ';
    }

    let userHandle = realUser.username || "";
    let userText = msg.text || msg.caption || "";
    if (msg.sticker) userText += ` [Стикер: ${msg.sticker.emoji || 'какой-то стикер'}]`;
    if (msg.photo) userText += ` [Картинка/Фото]`;
    if (msg.video) userText += ` [Видео]`;
    if (msg.video_note) userText += ` [Кружочек/Видеозаметка]`;

    // --- ОБРАБОТКА ГОЛОСОВЫХ ---
    if (msg.voice) {
        const trans = await transcribeAudio(msg.voice.file_id);
        if (trans) userText += ` [Транскрипция голосового: "${trans}"]`;
        else userText += ` [Голосовое сообщение]`;
    }
    // ---

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

        // ИСПРАВЛЕНИЕ #2: Корректное имя автора сообщения, на которое отвечают (для каналов)
        if (rp.sender_chat) {
            rpAuthor = rp.sender_chat.title || "Канал";
        } else if (rp.from) {
            if (rp.from.username === 'GroupAnonymousBot') {
                rpAuthor = rp.author_signature || "Анонимный Админ";
            } else {
                rpAuthor = rp.from.first_name || "Кто-то";
            }
        }

        replyPrefix = `(в ответ ${rpAuthor}: "${(rp.text || rp.caption || "медиа").slice(0, 30)}...") `;

        if (isMentioned && rp.from && rp.from.id !== BOT_ID) {
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
    activeParticipants[chatId][userId] = { firstName: userName, username: msg.from?.username || '', lastSeen: Date.now() };

    // Очищаем устаревших участников (TTL: 24 часа)
    const PARTICIPANT_TTL = 24 * 60 * 60 * 1000;
    const now = Date.now();
    for (const uid in activeParticipants[chatId]) {
        if (now - activeParticipants[chatId][uid].lastSeen > PARTICIPANT_TTL) {
            delete activeParticipants[chatId][uid];
        }
    }
    const relevantFacts = await getRelevantFacts(chatId, userText, userName, Object.values(activeParticipants[chatId]));
    const memoryBlock = `\n[МЫСЛИ О ${userName}]\n${relevantFacts}\nВремя (МСК): ${new Date().toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })}\n`;

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
            } else if (msg.video_note && msg.video_note.thumbnail) {
                fileIdToDownload = msg.video_note.thumbnail.file_id;
            }
            if (fileIdToDownload) {
                const tempUrl = await bot.getFileLink(fileIdToDownload);
                const imgRes = await fetch(tempUrl);
                const arrayBuffer = await imgRes.arrayBuffer();
                const buffer = Buffer.from(arrayBuffer);
                const base64 = buffer.toString('base64');

                let mimeType = 'image/jpeg';
                if (tempUrl.endsWith('.webp')) mimeType = 'image/webp';
                else if (tempUrl.endsWith('.png')) mimeType = 'image/png';

                imageUrl = `data:${mimeType};base64,${base64}`;
            }
        } catch (e) {
            console.error("Ошибка загрузки/конвертации картинки:", e.message);
        }

        let currentMessagesFirstCall = sanitizeHistory(chatHistory[chatId]);
        if (imageUrl) {
            currentMessagesFirstCall[currentMessagesFirstCall.length - 1].content = [
                { type: "text", text: fullContent },
                { type: "image_url", image_url: { url: imageUrl } }
            ];
        }
        // ===============================================

        let completion;
        try {
            const targetModel = imageUrl ? 'google/gemini-2.0-flash-001' : AI_MODEL;

            completion = await fetchAIWithTimeout({
                model: targetModel,
                messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesFirstCall],
                tools: aiTools,
                max_tokens: 2500,
                temperature: 0.7
            });
        } catch (e) {
            console.error("❌ Модель не справилась:", e.message);
            // Если картинка была и упала, пробуем БЕЗ неё на текстовой модели
            if (imageUrl) {
                console.log("♻️ Пробую отправить запрос БЕЗ картинки...");
                currentMessagesFirstCall[currentMessagesFirstCall.length - 1].content = fullContent;
            }

            completion = await fetchAIWithTimeout({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesFirstCall],
                tools: aiTools, max_tokens: 2500, temperature: 0.7
            });
        }


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

                if (['user_lookup', 'moderate_user', 'create_poll', 'set_reminder', 'manage_memory', 'manage_user_profile'].includes(fnName)) {
                    if (!directInjectedData.includes(res)) directInjectedData += `\n\n${res}`;
                }
            }

            let currentMessagesSecondCall = sanitizeHistory(chatHistory[chatId]);

            let second;
            try {
                second = await fetchAIWithTimeout({
                    model: AI_MODEL,
                    messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesSecondCall],
                    temperature: 0.7,
                    max_tokens: 2500
                });
            } catch (e2) {
                console.error("❌ Ошибка второго вызова AI (после использования инструмента):", e2.message);
                // Страховка на случай падения второго вызова (с увеличенным таймаутом и резервной моделью)
                try {
                    second = await fetchAIWithTimeout({
                        model: 'google/gemini-2.0-flash-001',
                        messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesSecondCall],
                        temperature: 0.7,
                        max_tokens: 2500
                    }, 50000); // 50 секунд для второго шанса
                } catch (e3) {
                    console.error("❌ Резервный вызов также упал:", e3.message);
                }
            }

            // ИСПРАВЛЕНИЕ #3: Логика fallback-фраз (заглушек)
            let aiText = second?.choices?.[0]?.message?.content;

            if (!aiText) {
                // Если текст так и не сгенерировался, проверяем, какой инструмент мы вызывали
                const wasModerationCalled = calls.some(c =>
                    (c.function?.name === 'moderate_user' || c.name === 'moderate_user') &&
                    c.function?.arguments?.includes('mute')
                );

                if (wasModerationCalled) {
                    // Если это был вызов мута/варна, используем крутые фразы
                    const fallbackPhrases = [
                        "Нарушитель изолирован. 💅",
                        "Минус один. 🔨",
                        "Фу, какая гадость... Отправила в бан. 🗑️",
                        "Секундочку... отправляю отдыхать. 💅"
                    ];
                    aiText = fallbackPhrases[Math.floor(Math.random() * fallbackPhrases.length)];
                } else {
                    // Если это был поиск фактов (Ибанов Лорант), профиля или другой обычный запрос
                    const errorPhrases = [
                        "Ой, что-то я запуталась... Можешь повторить?",
                        "Блин, процессор закипел. Еще раз спроси!",
                        "Хм, я задумалась и забыла, что хотела сказать. 😅"
                    ];
                    aiText = errorPhrases[Math.floor(Math.random() * errorPhrases.length)];
                }
            }

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
            // Принудительно удаляем системные теги медиа
            let withoutMediaTags = text.replace(/\[Стикер:[^\]]*\]/gi, '').replace(/\[Картинка\/Фото\]/gi, '').replace(/\[Видео\]/gi, '').replace(/\[Голосовое сообщение\]/gi, '').trim();

            if (!withoutMediaTags && text.length > 0) {
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

            // Очищаем галлюцинации
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