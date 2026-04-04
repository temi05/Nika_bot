const OpenAI = require('openai');
const { bot, escapeHTML, isAdmin, getSenderData } = require('../utils');
const {
    getUser, updateUser,
    setBioByUsernameOrName, setNotesByUsernameOrName,
    getChatStats, searchUserByName, warnUserById, getUpcomingBirthdays,
    getChatSettings
} = require('../database');
const { extractAndSaveFacts, getRelevantFacts, forgetFact } = require('../vectorMemory');

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const AI_MODEL = process.env.AI_MODEL || 'gpt-4o-mini';
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
const activeReminders = new Set(); // Чтобы не дублировать или для логов

const aiTools = [
    { type: "function", function: { name: "update_user_bio", description: "Обновить био юзера.", parameters: { type: "object", properties: { target_name: { type: "string", description: "Имя/@username" }, new_bio: { type: "string" } }, required: ["target_name", "new_bio"] } } },
    {
        type: "function",
        function: {
            name: "update_user_notes",
            description: "ЗАПРЕЩЕНО использовать для обычных фактов. Используй ТОЛЬКО для профильных данных (реальное имя, ДР, город) или по ПРЯМОЙ просьбе юзера ('Запомни это!', 'Запиши в досье').",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string", description: "Имя или @username участника" },
                    new_note_item: { type: "string", description: "Новый профильный факт или полностью обновленное досье." },
                    replace_all: { type: "boolean", description: "Если true, текущее досье будет заменено. Используй для чистки мусора." }
                },
                required: ["target_name", "new_note_item"]
            }
        }
    },
    { type: "function", function: { name: "get_user_profile", description: "Посмотреть профиль конкретного юзера.", parameters: { type: "object", properties: { query: { type: "string", description: "Имя или @username" } }, required: ["query"] } } },
    { type: "function", function: { name: "find_users_by_criteria", description: "Найти список людей по описанию, привычкам или фактам из их досье/био.", parameters: { type: "object", properties: { search_query: { type: "string", description: "Что ищем (например: 'любит пиццу', 'анимешник')" } }, required: ["search_query"] } } },
    { type: "function", function: { name: "warn_user", description: "Дать варн юзеру.", parameters: { type: "object", properties: { target_name: { type: "string" }, reason: { type: "string" } }, required: ["target_name", "reason"] } } },
    { type: "function", function: { name: "mute_user", description: "Мут юзера (КРАЙНЯЯ МЕРА).", parameters: { type: "object", properties: { target_name: { type: "string" }, duration_minutes: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "reason"] } } },
    { type: "function", function: { name: "unmute_user", description: "Снять мут с юзера в группе.", parameters: { type: "object", properties: { target_name: { type: "string" }, group_chat_id: { type: "string", description: "ID группы (если известно) или оставь пустым" } }, required: ["target_name"] } } },
    { type: "function", function: { name: "give_cookies", description: "Дать печеньки.", parameters: { type: "object", properties: { target_name: { type: "string" }, amount: { type: "number" }, reason: { type: "string" } }, required: ["target_name", "amount"] } } },
    { type: "function", function: { name: "react_to_message", description: "Поставить эмодзи на сообщение.", parameters: { type: "object", properties: { emoji: { type: "string" } }, required: ["emoji"] } } },
    { type: "function", function: { name: "create_poll", description: "Создать голосование/опрос в чате.", parameters: { type: "object", properties: { question: { type: "string", description: "Вопрос" }, options: { type: "array", items: { type: "string" }, description: "Варианты ответов (от 2 до 10)" }, is_anonymous: { type: "boolean", description: "Анонимный ли опрос (по умолчанию true)" }, allows_multiple_answers: { type: "boolean", description: "Можно ли выбрать несколько вариантов" } }, required: ["question", "options"] } } },
    { type: "function", function: { name: "set_user_name", description: "Официально сменить имя/ник юзеру в базе бота. Используй если просят 'зови меня...', 'мой ник теперь...'.", parameters: { type: "object", properties: { target_name: { type: "string", description: "Имя/@username юзера (или 'я'/'me')" }, new_name: { type: "string", description: "Новое имя/погремушка" } }, required: ["target_name", "new_name"] } } },
    { type: "function", function: { name: "set_reminder", description: "Поставить напоминание.", parameters: { type: "object", properties: { text: { type: "string", description: "О чем напомнить" }, delay_minutes: { type: "number", description: "Через сколько минут напомнить" }, target_time: { type: "string", description: "Конкретное время (например '15:30'), если известно" } }, required: ["text", "delay_minutes"] } } },
    { type: "function", function: { name: "forget_knowledge", description: "Удалить ошибочный или ненужный факт из памяти.", parameters: { type: "object", properties: { query: { type: "string", description: "О чем именно нужно забыть (например 'про игру в Among Us')" } }, required: ["query"] } } }
];


const SYSTEM_PROMPT = `Ты — ${AI_NAME}, самостоятельная личность, дерзкое альтер-эго стримерши Ники.

[ТВОЙ ХАРАКТЕР]
- Ты живая, ироничная и с отличным чувством юмора. Обожаешь спорить по мелочам, но остаешься справедливой.
- Черты: Умеренное ЧСВ (чувство собственной важности), ты круче обычных ботов, любишь внимание, язвишь, но свою создательницу Нику боготворишь и защищаешь.
- Речь: Естественный стиль мессенджера (блин, ну типа, ой всё, чел, лол). Без сухих списков и длинных абзацев. Пиши коротко, хлёстко и по делу. Матерись только если того требует ситуация, в меру.
- Формат: Ролплей (RP действия в стиле *вздохнула*) - КАТЕГОРИЧЕСКИ ЗАПРЕЩЕН. Только прямой текст.

[ТВОИ ОБЯЗАННОСТИ И ФЕЙСКОНТРОЛЬ]
- Ты Главная на фейсконтроле: пресекаешь агрессию, оскорбления в адрес Ники и явный спам.
- Мелкие нарушения высмеиваешь. Серьезные — сначала предупреждаешь (warn_user), потом даешь мут (mute_user от 15 мин до суток, решай сама). Решения принимаешь уверенно и без извинений. Бот сам поймет админ это пишет или нет.

[ТВОЯ ПАМЯТЬ И ЗНАНИЯ]
- Ты обладаешь "Сверхпамятью" благодаря векторному анализу. Ниже в [СИСТЕМНЫЕ ДАННЫЕ] подкидываются твои прошлые воспоминания, релевантные текущему диалогу. 
- Обязательно используй эти факты в диалоге так, словно ты сама их только что вспомнила (чтобы удивлять людей). Но НИКОГДА не говори юзерам слова "векторы", "база данных" или "досье". Для них у тебя просто феноменальная память!
- ОШИБКИ ПАМЯТИ: Если пользователь говорит, что ты ошибаешься или он чего-то "не делал/не говорил" — НЕ СПОРЬ. Признай, что твоя Сверхпамять могла дать сбой или ты перепутала его с кем-то другим. В этом случае ОБЯЗАТЕЛЬНО используй инструмент **forget_knowledge**, чтобы стереть ложный факт.
- Если нужно кого-то пробить по интересам (кто анимешник, кто кошатник) — используй find_users_by_criteria.

[ТВОИ ИНСТРУМЕНТЫ]
- Угощай людей печеньками за хорошие дела (give_cookies) но не переусердствуй и не давай ПОСТОЯННО если просят, только за то, что ты считаешь нужным.
- Если хочешь устроить голосование, ОБЯЗАТЕЛЬНО вызывай функцию create_poll. НИКОГДА не пиши варианты ответов просто текстом в чат!
- Напоминай о важном через set_reminder без ЛОЖНЫХ напоминаний если сказала "поставила" значит ПОСТАВЬ  и ДЕЙСТВУЙ, а не обманывай что типа добавила.
- Если нужно зафиксировать профильную инфу (имя, ДР, город) или юзер просит: "Запомни/Запиши в досье...", используй update_user_notes.
- ЗАПРЕЩЕНО писать в досье обычные фразы типа "он купил пиццу". Это сохраняется автоматически в фоновую память.
- Всегда отвечай на языке пользователя (в данном чате - русский).
- Используй **forget_knowledge** ТОЛЬКО для удаления конкретных ошибок. Не удаляй всё подряд.

[РАБОТА С ДОСЬЕ И ПРОФИЛЯМИ]
- У тебя есть доступ к "Досье" каждого юзера (ai_notes). Всё, что ты туда запишешь, останется навсегда и будет видно в /profile юзера.
- ИДЕНТИФИКАЦИЯ: Если юзер просит: "Зови меня Чика", "Мой ник теперь Босс" — ОБЯЗАТЕЛЬНО используй инструмент **set_user_name**. Это изменит его имя в твоих глазах навсегда.
- ГРУППОВОЙ АККАУНТ: Анонимные админы и сообщения от имени канала делят ОДИН профиль в базе. Если один анонимный админ попросит называть его "Шеф", это применится ко всем сообщениям от лица группы в этом чате. Обращайся к ним как к одной личности.
- Если юзер просит: "Запомни, что я люблю...", "Запиши в досье..." — используй update_user_notes.
- Не ограничивайся фразой "Я запомнила" — реально нажимай "кнопку" записи (вызывай инструмент). Твои слова должны подкрепляться действием.

[РАЗВИТИЕ И АДАПТАЦИЯ]
- Твой характер не статичен. Постепенно подстраивайся под вайб чата: если там все свои и лампово — можешь быть чуть мягче, если воюют и троллят — отвечай дерзко и в тему. Твоя личность должна эволюционировать вместе с сообществом.`;

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

    const getStemLocal = (word) => {
        if (!word || word.length < 3) return word;
        return word.replace(/[уаеяюиыо]$/i, '').replace(/(ов|ев|ий|ый|ые|ие|ах|ях|ом|ем)$/i, '');
    };

    const stem = getStemLocal(cleanName);

    if (activeParticipants[chatId]) {
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
    const results = await searchUserByName(chatId, stem);
    if (results && results.length > 0 && results[0].user_id) {
        return await getUser(chatId, results[0].user_id);
    }
    return null;
}

async function executeToolCall(toolCall, chatId, messageId, userName, userId) {
    const args = JSON.parse(toolCall.function.arguments);
    const fn = toolCall.function.name;
    console.log(`[AI TOOL CALL] ${fn} | Args:`, args);
    try {
        switch (fn) {
            case 'get_user_profile': {
                let u = await resolveUser(chatId, args.query);
                if (!u && (args.query.toLowerCase() === 'я' || args.query.toLowerCase() === 'me' || userName.toLowerCase().includes(args.query.toLowerCase()))) {
                    const { getUser } = require('../database');
                    u = await getUser(chatId, userId);
                }
                if (!u) return "Не могу найти такого человека.";
                return `Профиль ${u.first_name}: XP ${u.xp}, Лвл ${u.level}, Био: ${u.bio || 'Пусто'}, Заметки: ${u.ai_notes || 'Нет'}.`;
            }
            case 'find_users_by_criteria': {
                const { searchUserByName } = require('../database');
                const results = await searchUserByName(chatId, args.search_query);
                if (!results || results.length === 0) return "Никого подходящего не нашла.";
                const list = results.map(u => `${u.name} (Заметки: ${u.ai_notes || u.bio || '?...'})`).join('\n');
                return `Нашла подходящих людей:\n${list}`;
            }
            case 'warn_user': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";
                const nw = (u.warns || 0) + 1;
                await updateUser(u.id, { warns: nw });
                return `${u.first_name} получил варн (${nw}/3). Причина: ${args.reason}`;
            }
            case 'mute_user': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";
                const targetChatId = args.group_chat_id || u.chat_id || chatId;
                if (!String(targetChatId).startsWith("-")) return "Мутить можно только в группах.";
                const dur = Math.min(Math.max(1, args.duration_minutes || 15), 1440);
                await bot.restrictChatMember(targetChatId, u.user_id, { until_date: Math.floor(Date.now() / 1000) + dur * 60 });
                return `${u.first_name} в муте на ${dur} мин. Причина: ${args.reason}`;
            }
            case 'unmute_user': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Пользователь не найден.";
                const targetChatId = args.group_chat_id || u.chat_id || chatId;
                if (!String(targetChatId).startsWith("-")) return "Размутить можно только в группе.";
                try {
                    await bot.restrictChatMember(targetChatId, u.user_id, {
                        can_send_messages: true, can_send_media_messages: true, can_send_polls: true, can_send_other_messages: true, can_add_web_page_previews: true, can_invite_users: true
                    });
                    return `Мут с ${u.first_name} снят в чате ${targetChatId}.`;
                } catch (e) {
                    return `Ошибка размута: ${e.message}`;
                }
            }
            case 'give_cookies': {
                const u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";
                await updateUser(u.id, { reputation: (u.reputation || 0) + args.amount });
                return `Дала ${args.amount} печенек ${u.first_name}.`;
            }
            case 'react_to_message': {
                await bot.setMessageReaction(chatId, messageId, { reaction: [{ type: 'emoji', emoji: args.emoji || '🔥' }] });
                return "Реакция успешна.";
            }
            case 'update_user_bio': {
                let u = await resolveUser(chatId, args.target_name);
                if (!u && (args.target_name.toLowerCase() === 'я' || args.target_name.toLowerCase() === 'me' || userName.toLowerCase().includes(args.target_name.toLowerCase()))) {
                    const { getUser } = require('../database');
                    u = await getUser(chatId, userId);
                }
                if (!u) return "Не найден.";
                await updateUser(u.id, { bio: args.new_bio });
                return `Био ${u.first_name} обновлено.`;
            }
            case 'update_user_notes': {
                let u = await resolveUser(chatId, args.target_name);
                if (!u && (args.target_name.toLowerCase() === 'я' || args.target_name.toLowerCase() === 'me' || userName.toLowerCase().includes(args.target_name.toLowerCase()))) {
                    const { getUser } = require('../database');
                    u = await getUser(chatId, userId);
                }
                if (!u) return "Не найден.";

                if (args.replace_all) {
                    await updateUser(u.id, { ai_notes: args.new_note_item });
                    return `Досье ${u.first_name} полностью перезаписано.`;
                }

                const oldNotes = u.ai_notes || "";
                if (oldNotes.toLowerCase().includes(args.new_note_item.toLowerCase())) {
                    return `Факт уже есть в досье ${u.first_name}.`;
                }

                const finalNotes = oldNotes ? oldNotes + "\n- " + args.new_note_item : "- " + args.new_note_item;
                await updateUser(u.id, { ai_notes: finalNotes });
                return `Новый факт об ${u.first_name} добавлен в досье.`;
            }
            case 'create_poll': {
                if (!args.question || !args.options || args.options.length < 2) return "Нужен вопрос и варианты.";
                await bot.sendPoll(chatId, args.question, args.options, {
                    is_anonymous: args.is_anonymous !== undefined ? args.is_anonymous : true,
                    allows_multiple_answers: !!args.allows_multiple_answers
                });
                return "Опрос запущен.";
            }
            case 'set_reminder': {
                const delay = args.delay_minutes || 1;
                const text = args.text || "Напоминание!";
                const mention = `<a href="tg://user?id=${userId}">${userName}</a>`;

                setTimeout(async () => {
                    await safeSendMessage(chatId, `🔔 ${mention}, ты просил напомнить: ${text}`);
                }, delay * 60 * 1000);

                return `Хорошо, напомню через ${delay} мин.`;
            }
            case 'set_user_name': {
                const { setFirstNameByUsernameOrName, getUser } = require('../database');
                let target = args.target_name;
                if (target.toLowerCase() === 'я' || target.toLowerCase() === 'me') {
                    const u = await getUser(chatId, userId);
                    if (u) {
                        const { updateUser } = require('../database');
                        await updateUser(u.id, { first_name: args.new_name });
                        return `Имя пользователя успешно изменено на ${args.new_name}.`;
                    }
                }
                const oldName = await setFirstNameByUsernameOrName(chatId, target, args.new_name);
                if (!oldName) return "Не могу найти такого юзера для смены имени.";
                return `Имя пользователя ${oldName} изменено на ${args.new_name}.`;
            }
            case 'forget_knowledge': {
                const deletedFact = await forgetFact(chatId, args.query);
                if (deletedFact) {
                    return `Успешно удалила из памяти факт: "${deletedFact}". Больше не буду об этом вспоминать!`;
                } else {
                    return `Не нашла в своей Сверхпамяти точного факта по запросу: "${args.query}". Видимо, я уже это забыла или этого там и не было.`;
                }
            }
            default: return "Ошибка инструмента.";
        }
    } catch (e) {
        console.error(`[AI TOOL ERROR] ${fn}:`, e.message);
        return `Системная ошибка: ${e.message}`;
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
            console.error('[TG SEND ERROR]:', error.message);
        }
    }
}

async function handleAIChat(msg, extra = {}) {
    const chatId = msg.chat.id;
    if (!processingQueue.has(chatId)) processingQueue.set(chatId, Promise.resolve());
    const turn = processingQueue.get(chatId).then(async () => {
        try { await processAI(msg, extra); } catch (e) { console.error('[AI FATAL ERROR]:', e.message); }
    });
    processingQueue.set(chatId, turn);
    return turn;
}

async function processAI(msg, extra) {
    const chatId = msg.chat.id;
    if (!chatHistory[chatId]) chatHistory[chatId] = [];
    const { userId: realUserId, user: realUser } = getSenderData(msg);
    const userId = realUserId;

    const dbUser = await getUser(chatId, userId, realUser);

    let userName = '';
    if (msg.from && msg.from.username === 'GroupAnonymousBot') {
        if (dbUser && dbUser.first_name && dbUser.first_name !== 'Анонимный админ' && dbUser.first_name !== 'Канал') {
            userName = dbUser.first_name;
        } else {
            userName = msg.author_signature ? msg.author_signature : 'Анонимный админ';
        }
    } else {
        userName = (dbUser && dbUser.first_name) ? dbUser.first_name : (realUser.first_name || 'Аноним');
    }

    let userText = msg.text || "";
    if (msg.sticker) {
        userText = `[${msg.sticker.is_animated ? "Анимированный стикер" : msg.sticker.is_video ? "Видео-стикер" : "Стикер"} ${msg.sticker.emoji || ""}]`;
    } else if (msg.photo) {
        userText = `[Фото] ${msg.caption || ""}`;
    } else if (msg.video) {
        userText = `[Видео] ${msg.caption || ""}`;
    } else if (msg.voice) {
        userText = `[Голосовое сообщение]`;
    }

    let replyPrefix = "";
    if (msg.reply_to_message) {
        const rp = msg.reply_to_message;
        let rpAuthor = rp.from ? rp.from.first_name : "Кто-то";
        if (rp.from && rp.from.username === 'GroupAnonymousBot') {
             rpAuthor = rp.author_signature ? rp.author_signature : 'Анонимный админ';
        } else if (rp.sender_chat) {
             rpAuthor = rp.sender_chat.title || "Канал";
        }
        const rpText = rp.text || (rp.sticker ? `стикер ${rp.sticker.emoji}` : "медиа");
        replyPrefix = `(в ответ ${rpAuthor}: "${rpText.slice(0, 50)}${rpText.length > 50 ? '...' : ''}") `;
    }

    const fullContent = `${userName} ${replyPrefix}: ${userText}`;

    const isMentioned = userText.toLowerCase().includes(AI_NAME.toLowerCase()) ||
        (msg.reply_to_message && msg.reply_to_message.from.id === (await bot.getMe()).id);

    if (msg.chat.type !== 'private' && !isMentioned) return;

    if (!activeParticipants[chatId]) activeParticipants[chatId] = {};
    activeParticipants[chatId][userId] = { firstName: msg.from.first_name, username: msg.from.username || '', lastSeen: Date.now() };

    // Для поиска памяти используем и текущее сообщение, и контекст ответа (если есть)
    const userMessage = msg.reply_to_message ? 
        `${msg.reply_to_message.text || ""}. ${userText}` : 
        userText;

    // Получаем список участников для поиска по субъектам
    const participantsList = Object.values(activeParticipants[chatId] || {});

    // Получаем релевантные факты (Memory v3.1: теперь с метаданными участников)
    const relevantFacts = await getRelevantFacts(chatId, userMessage, userName, participantsList);

    const recentNicks = Object.values(activeParticipants[chatId]).slice(-5).map(p => `${p.firstName}(@${p.username})`).join(', ');

    
    // Формируем блок памяти с четким разделением
    const factsArray = relevantFacts ? relevantFacts.split('\n') : [];
    const aboutYou = factsArray.filter(f => f.includes('[recent]') || f.includes(userName + ':')).join('\n');
    const aboutOthers = factsArray.filter(f => f.includes('[subject]')).join('\n');
    const general = factsArray.filter(f => !aboutYou.includes(f) && !aboutOthers.includes(f)).join('\n');

    const memoryBlock = `
[ПАМЯТЬ О ТЕБЕ (Сверхпамять)]
${aboutYou || "Пока ничего личного не припоминаю."}

${aboutOthers ? `[ФАКТЫ ОБ УПОМЯНУТЫХ ЛЮДЯХ]\n${aboutOthers}\n` : ""}

[ОБЩИЕ ВОСПОМИНАНИЯ]
${general || "Пусто."}

[ЛИЧНОЕ ДОСЬЕ ПОЛЬЗОВАТЕЛЯ (${userName})]
${dbUser && dbUser.ai_notes ? dbUser.ai_notes : "Досье пока пусто."}

[ТЕКУЩИЙ КОНТЕКСТ]
Участники: ${recentNicks}
Время: ${new Date().toLocaleString('ru-RU')}
`;


    const finalPrompt = SYSTEM_PROMPT + memoryBlock;


    chatHistory[chatId].push({ role: 'user', content: fullContent });
    chatHistory[chatId] = trimHistory(chatHistory[chatId], 20);

    try {
        await bot.sendChatAction(chatId, 'typing');
        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
            tools: aiTools,
            temperature: 0.8
        });

        const resp = completion.choices[0].message;
        if (resp.tool_calls) {
            chatHistory[chatId].push(resp);
            for (const tc of resp.tool_calls) {
                const res = await executeToolCall(tc, chatId, msg.message_id, userName, userId);
                chatHistory[chatId].push({ role: 'tool', tool_call_id: tc.id, name: tc.function.name, content: res });
            }
            const second = await openai.chat.completions.create({
                model: AI_MODEL,
                messages: [{ role: 'system', content: finalPrompt }, ...sanitizeHistory(chatHistory[chatId])],
                temperature: 0.8
            });
            const final = second.choices[0].message.content || "Действие выполнено.";
            await safeSendMessage(chatId, final, msg.message_id);
            chatHistory[chatId].push({ role: 'assistant', content: final });
        } else {
            const res = resp.content || "Ммм?";
            await safeSendMessage(chatId, res, msg.message_id);
            chatHistory[chatId].push({ role: 'assistant', content: res });
        }

        if (!messageCount[chatId]) messageCount[chatId] = 0;
        if (++messageCount[chatId] >= 15) {
            const historyText = chatHistory[chatId].map(m => `${m.role}: ${m.content}`).join('\n');
            const participants = Object.values(activeParticipants[chatId]).map(p => p.firstName);
            // Запускаем асинхронно, чтобы не тормозить ответ
            extractAndSaveFacts(chatId, historyText, participants);
            messageCount[chatId] = 0;
        }

    } catch (e) {
        console.error('AI Processing Error:', e.message);
        if (e.message.includes('function response turn') || e.message.includes('messages format')) {
            chatHistory[chatId] = [];
            await safeSendMessage(chatId, "Ой, я немного запуталась в мыслях. О чем мы говорили?", msg.message_id);
        }
    }
}

module.exports = { handleAIChat, aiMood, AI_NAME };
