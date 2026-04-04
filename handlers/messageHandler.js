const { bot, escapeHTML, getUserName, getSenderData, sendTimedMessage, isAdmin } = require('../utils');
const { getUser, updateUser, getBadWords, messageAuthors, reactionCooldowns, ANONYMOUS_ADMIN_ID, pendingVerifications, getChatSettings, getBirthdaysToday } = require('../database');
const { handleAIChat } = require('./aiHandler');

let lastBirthdayCheck = {}; // { chatId: dateString }

// Буфер последних сообщений для пассивного наблюдения ИИ (только RAM, не в БД)
const chatBuffer = {}; // { chatId: [{name, text, time}] }
const CHAT_BUFFER_SIZE = 25; // Увеличил размер буфера для лучшего контекста
let passiveMessageCount = {}; // { chatId: count }

function registerMessageHandlers() {
    bot.on('message', async (msg) => {
        
        const chatId = msg.chat.id;
        const { userId, user } = getSenderData(msg);

        if (pendingVerifications[userId]) return; // Пропускаем, пока не пройдет капчу

        // 1. Пропускаем другие обработчики (капча и т.д.) если нужно
        if (msg.text?.startsWith('/')) return;

        // Логируем для реакций
        if (!messageAuthors[chatId]) messageAuthors[chatId] = {};
        messageAuthors[chatId][msg.message_id] = userId;

        // Получаем пользователя (один раз!)
        const dbUser = await getUser(chatId, userId, user);
        if (!dbUser) return;

        // 2. ФИЛЬТР
        if (msg.text) {
            const text = msg.text.toLowerCase();
            const badWords = await getBadWords(chatId);
            const isPromo = text.includes('t.me/') || text.includes('telegram.me/');

            // Кешируем регулярку для чата if needed, но достаточно собрать за один раз
            let foundBadWord = false;
            if (badWords.length > 0) {
                const escapedWords = badWords.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
                const regex = new RegExp(`(^|\\s|[.,!?;:()"])(${escapedWords.join('|')})($|\\s|[.,!?;:()"])`, 'i');
                foundBadWord = regex.test(text);
            }

            // Для ссылок: проверяем настройку чата и права пользователя
            let isPromoBlocked = false;
            if (isPromo) {
                const chatSettings = await getChatSettings(chatId);
                if (chatSettings.link_filter_enabled) {
                    // Фильтр включён: проверяем, является ли отправитель админом
                    const senderIsAdmin = await isAdmin(chatId, userId);
                    if (!senderIsAdmin) {
                        isPromoBlocked = true; // Не админ — блокируем
                    }
                }
            }

            // ПРИОРИТЕТ ИИ: Если в сообщении есть "НейроНика" или это реплай на ИИ — пропускаем фильтр мата
            // СТРИМЕРША (Ника) при этом остается под защитой
            const isAiMention = text.includes('нейроника');
            const isReplyToAi = msg.reply_to_message && msg.reply_to_message.from.is_bot;
            
            if ((foundBadWord || isPromoBlocked) && !isAiMention && !isReplyToAi) {
                bot.deleteMessage(chatId, msg.message_id).catch(() => { });
                const newWarns = dbUser.warns + 1;
                await updateUser(dbUser.id, { warns: newWarns });
                
                if (newWarns >= 3) {
                    await updateUser(dbUser.id, { warns: 0 });
                    const untilDate = Math.floor(Date.now() / 1000) + 3600;
                    bot.restrictChatMember(chatId, userId, { until_date: untilDate, can_send_messages: false })
                        .then(() => bot.sendMessage(chatId, `⛔ <b>${escapeHTML(getUserName(user))}</b> получил мут на 1 час за повторные нарушения.`, { parse_mode: 'HTML' }))
                        .catch(() => sendTimedMessage(chatId, `⚠️ <b>${escapeHTML(getUserName(user))}</b> нарушает, но у меня нет прав выдать мут!`, 15000, { parse_mode: 'HTML' }));
                } else {
                    sendTimedMessage(chatId, `⚠️ <b>${escapeHTML(getUserName(user))}</b>, нарушение! Выдано предупреждение <code>${newWarns} / 3</code>.`, 15000, { parse_mode: 'HTML' });
                }
                return;
            }
        }

        // 3. XP SYSTEM
        const isMedia = msg.photo || msg.voice || msg.video_note || msg.sticker;

        // 3.1 Записываем ВСЕ сообщения в буфер (для контекста ИИ)
        if (msg.text || msg.caption) {
            if (!chatBuffer[chatId]) chatBuffer[chatId] = [];
            // Если это анонимный админ, берем его подпись (если есть), иначе форматируем имя
            let userTag = '';
            if (msg.from && msg.from.username === 'GroupAnonymousBot') {
                userTag = msg.author_signature ? msg.author_signature : 'Анонимный админ';
            } else {
                const name = user ? user.first_name : (msg.from?.first_name || 'Аноним');
                const username = user && user.username ? ` (@${user.username})` : (msg.from?.username ? ` (@${msg.from.username})` : '');
                userTag = name + username;
            }

            chatBuffer[chatId].push({
                name: userTag,
                text: msg.text || msg.caption || (msg.photo ? '[📷 фото]' : '[💬 медиа]'),
                time: Date.now()
            });
            // Обрезаем буфер до CHAT_BUFFER_SIZE
            if (chatBuffer[chatId].length > CHAT_BUFFER_SIZE) {
                chatBuffer[chatId] = chatBuffer[chatId].slice(-CHAT_BUFFER_SIZE);
            }

            // Фоновая прослушка чата: раз в 25 сообщений сканируем буфер
            if (!passiveMessageCount[chatId]) passiveMessageCount[chatId] = 0;
            if (++passiveMessageCount[chatId] >= CHAT_BUFFER_SIZE) {
                console.log(`[PASSIVE MEMORY] Собрано ${CHAT_BUFFER_SIZE} сообщений. Запускаю анализ...`);
                const bufferText = chatBuffer[chatId].map(m => `${m.name}: ${m.text}`).join('\n');
                console.log(`[PASSIVE MEMORY] Краткий обзор буфера: ${bufferText.substring(0, 100)}...`);
                const { extractAndSaveFacts } = require('../vectorMemory');
                extractAndSaveFacts(chatId, bufferText);
                passiveMessageCount[chatId] = 0;
            }
        }

        if (msg.text || isMedia) {
            const now = Date.now();
            if (now - dbUser.last_message_time >= 60000) {
                const xpGain = Math.floor(Math.random() * 11) + 15;
                const { getNextLevelXp } = require('../database');
                const nextXp = getNextLevelXp(dbUser.level);
                const newLevel = dbUser.xp + xpGain >= nextXp ? dbUser.level + 1 : dbUser.level;
                await updateUser(dbUser.id, { xp: dbUser.xp + xpGain, level: newLevel, last_message_time: now });
                
                if (newLevel > dbUser.level) {
                   // Повышение уровня!
                   const levelUpPhrases = [
                       `👤 <b>${escapeHTML(getUserName(user))}</b>, ты теперь <b>${newLevel} уровня</b>. Растешь, не по дням, а по часам! 📈`,
                       `О, <b>${escapeHTML(getUserName(user))}</b> дополз до <b>${newLevel} лвла</b>. Неплохо, для начала. 🌟`,
                       `Смотрите-ка, <b>${escapeHTML(getUserName(user))}</b> апнул <b>${newLevel} уровень</b>! Продолжай в том же духе. 🔥`,
                       `<b>${newLevel} уровень</b> у <b>${escapeHTML(getUserName(user))}</b>! Скоро меня догонишь (шучу, нет). 🚀`
                   ];
                   const levelUpMsg = levelUpPhrases[Math.floor(Math.random() * levelUpPhrases.length)];
                   sendTimedMessage(chatId, levelUpMsg, 30000, { parse_mode: 'HTML' });
                }
            }
        }

        // 4. РЕПУТАЦИЯ (ОТВЕТЫ)
        if (msg.reply_to_message && msg.text) {
            const text = msg.text.trim().toLowerCase();
            const positive = ['+', 'спасибо', '👍', 'спс'];
            const negative = ['-', '👎', 'фу'];
            let change = positive.includes(text) ? 1 : (negative.includes(text) ? -1 : 0);

            if (change !== 0) {
                const { userId: rId, user: rInfo } = getSenderData(msg.reply_to_message);
                if (userId === rId) return;
                const cooldownKey = `${userId}_${rId}`;
                if (Date.now() - (reactionCooldowns[cooldownKey] || 0) < 60000) return sendTimedMessage(chatId, '⏳ Подожди минуту перед следующей оценкой!', 10000);
                
                const receiver = await getUser(chatId, rId, rInfo);
                if (receiver) {
                    await updateUser(receiver.id, { reputation: receiver.reputation + change });
                    const cookiePhrases = [
                        `🌟 <b>${escapeHTML(getUserName(user))}</b> передал печеньку <b>${escapeHTML(getUserName(rInfo))}</b>!\n└ Теперь у него <code>${receiver.reputation + change} 🍪</code>`,
                        `Держи, <b>${escapeHTML(getUserName(rInfo))}</b>, тебе прилетела печенька от <b>${escapeHTML(getUserName(user))}</b>. \n└ В копилке теперь <code>${receiver.reputation + change} 🍪</code>`
                    ];
                    const lossPhrases = [
                        `📉 <b>${escapeHTML(getUserName(user))}</b> отнял печеньку у <b>${escapeHTML(getUserName(rInfo))}</b>. Грустно.\n└ Осталось <code>${receiver.reputation + change} 🍪</code>`,
                        `Минус печенька у <b>${escapeHTML(getUserName(rInfo))}</b>. Постарался <b>${escapeHTML(getUserName(user))}</b>.\n└ Теперь их всего <code>${receiver.reputation + change} 🍪</code>`
                    ];
                    const repMsg = change > 0 
                        ? cookiePhrases[Math.floor(Math.random() * cookiePhrases.length)]
                        : lossPhrases[Math.floor(Math.random() * lossPhrases.length)];
                    sendTimedMessage(chatId, repMsg, 60000, { parse_mode: 'HTML' });
                    reactionCooldowns[cooldownKey] = Date.now();
                }
            }
        }

        // 5. BIRTHDAY CHECK (раз в день на первое сообщение)
        const todayStr = new Date().toLocaleDateString();
        if (lastBirthdayCheck[chatId] !== todayStr) {
            const bdays = await getBirthdaysToday(chatId);
            if (bdays && bdays.length > 0) {
                let bdayMsg = `🎂 <b>СЕГОДНЯ ДЕНЬ РОЖДЕНИЯ!</b> 🎉\n━━━━━━━━━━━━━━━━━━\n`;
                bdays.forEach(u => {
                    let ageText = '';
                    if (u.birthday && u.birthday.length === 10) {
                        const birthYear = parseInt(u.birthday.split('.')[2]);
                        const currentYear = new Date().getFullYear();
                        const age = currentYear - birthYear;
                        ageText = ` (${age} лет)`;
                    }
                    bdayMsg += `🌟 Поздравляем <b>${escapeHTML(getUserName(u))}</b>${ageText}! С твоим днем! 🥳🎁\n`;
                });
                bdayMsg += `━━━━━━━━━━━━━━━━━━\n<i>Желаем море печенек и высокого уровня во всём!</i>`;
                bot.sendMessage(chatId, bdayMsg, { parse_mode: 'HTML' });
            }
            lastBirthdayCheck[chatId] = todayStr;
        }

        // 6. AI CHAT (НейроНика)
        // Вызываем ИИ только если это не команда и сообщение прошло фильтры
        // Передаём фото и буфер чата для полного контекста
        await handleAIChat(msg, {
            chatBuffer: chatBuffer[chatId] || [],
            photo: msg.photo ? msg.photo[msg.photo.length - 1] : null, // Берём фото максимального размера
            caption: msg.caption || ''
        });
    });
}

async function handleReaction(reaction) {
    const chatId = reaction.chat.id;
    const authorId = messageAuthors[chatId]?.[reaction.message_id];
    if (!authorId) return;

    let actorId = reaction.user?.id || reaction.actor_chat?.id;
    if (!actorId || actorId === authorId) return;

    const getScore = (r) => {
        const emoji = r?.[0]?.emoji;
        return ['👎', '💩', '🤮'].includes(emoji) ? -1 : (emoji ? 1 : 0);
    };

    const delta = getScore(reaction.new_reaction) - getScore(reaction.old_reaction);
    if (delta === 0) return;

    const author = await getUser(chatId, authorId);
    if (author) await updateUser(author.id, { reputation: author.reputation + delta });
}

module.exports = { registerMessageHandlers, handleReaction };
