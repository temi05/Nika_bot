const { bot, escapeHTML, getUserName, getSenderData, sendTimedMessage, isAdmin } = require('../utils');
const { getUser, updateUser, getBadWords, messageAuthors, reactionCooldowns, ANONYMOUS_ADMIN_ID, pendingVerifications, getChatSettings } = require('../database');

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

            if (foundBadWord || isPromoBlocked) {
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
        if (msg.text || isMedia) {
            const now = Date.now();
            if (now - dbUser.last_message_time >= 60000) {
                const xpGain = Math.floor(Math.random() * 11) + 15;
                const { getNextLevelXp } = require('../database');
                const nextXp = getNextLevelXp(dbUser.level);
                const newLevel = dbUser.xp + xpGain >= nextXp ? dbUser.level + 1 : dbUser.level;
                await updateUser(dbUser.id, { xp: dbUser.xp + xpGain, level: newLevel, last_message_time: now });
                if (newLevel > dbUser.level) {
                   const levelUpMsg = `🎉 Поздравляем!\n` + 
                                      `👤 <b>${escapeHTML(getUserName(user))}</b> достиг <b>${newLevel} уровня</b>! 🌟`;
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
                    const text = change > 0 
                        ? `🌟 <b>${escapeHTML(getUserName(user))}</b> передал печеньку <b>${escapeHTML(getUserName(rInfo))}</b>!\n└ Теперь у него <code>${receiver.reputation + change} 🍪</code>`
                        : `📉 <b>${escapeHTML(getUserName(user))}</b> отнял печеньку у <b>${escapeHTML(getUserName(rInfo))}</b>!\n└ Теперь у него <code>${receiver.reputation + change} 🍪</code>`;
                    sendTimedMessage(chatId, text, 60000, { parse_mode: 'HTML' });
                    reactionCooldowns[cooldownKey] = Date.now();
                }
            }
        }
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
