const { bot, escapeMarkdown, getUserName, getSenderData, sendTimedMessage, deleteMsg, isAdmin } = require('../utils');
const { getUser, updateUser, getBadWords, supabase, commandCooldowns, getNextLevelXp, ANONYMOUS_ADMIN_ID } = require('../database');

function registerCommands() {
    // /help
    bot.onText(/^\/help$/, async (msg) => {
        const chatId = msg.chat.id;
        const helpText = `🤖 <b>Что я умею:</b>\n\n` +
            `👤 <b>Для всех:</b>\n` +
            `/me — Моя статистика\n` +
            `/top — Топ активных участников\n` +
            `/shop — Магазин за печеньки 🍪\n` +
            `/give &lt;число&gt; — Передать печеньки\n` +
            `/kto &lt;вопрос&gt; — Случайный участник\n\n` +
            `👮‍♂️ <b>Для админов:</b>\n` +
            `/ban, /unban, /banword, /unbanword, /listwords\n\n` +
            `<i>Я защищаю чат и проверяю новичков!</i>`;
        bot.sendMessage(chatId, helpText, { parse_mode: 'HTML' });
    });

    // /me
    bot.onText(/^\/me(?:\s+(.+))?$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const requester = getSenderData(msg);
        let userId = requester.userId;
        let targetUser = requester.user;

        deleteMsg(chatId, msg.message_id);
        if (msg.reply_to_message) {
            const replyInfo = getSenderData(msg.reply_to_message);
            userId = replyInfo.userId; targetUser = replyInfo.user;
        } else if (match[1]) {
            const arg = match[1].trim();
            if (/^\d+$/.test(arg)) {
                userId = parseInt(arg); targetUser = { id: userId, first_name: `User ${userId}` };
            } else if (arg.startsWith('@')) {
                const { data } = await supabase.from('users').select('*').eq('chat_id', chatId).ilike('username', arg.substring(1)).maybeSingle();
                if (data) { userId = data.user_id; targetUser = data; }
                else return sendTimedMessage(chatId, `❌ Пользователь ${arg} не найден.`);
            }
        }

        const user = await getUser(chatId, userId, targetUser);
        if (!user) return;
        const nextXp = getNextLevelXp(user.level);
        
        const message = `📊 <b>Статистика:</b>` + (userId !== requester.userId ? ` (профиль ${escapeMarkdown(getUserName(targetUser))})` : '') + `\n\n` +
            `👤 Пользователь: ${escapeMarkdown(getUserName(user))}\n` +
            `⭐ Уровень: <b>${user.level}</b>\n` +
            `✨ Опыт: <b>${user.xp}</b>\n` +
            `🍪 Репутация: <b>${user.reputation}</b>\n` +
            `📈 До следующего уровня: <b>${nextXp - user.xp}</b> XP`;
        sendTimedMessage(chatId, message, 60000, { parse_mode: 'HTML' });
    });

    // /top
    bot.onText(/^\/top$/, async (msg) => {
        const chatId = msg.chat.id;
        const { userId } = getSenderData(msg);
        deleteMsg(chatId, msg.message_id);

        const { data: users } = await supabase.from('users').select('*').eq('chat_id', chatId).order('level', { ascending: false }).limit(10);
        if (!users || users.length === 0) return sendTimedMessage(chatId, 'Нет активности.');
        let text = '🏆 *Топ активных участников:*\n';
        users.forEach((u, i) => text += `${i + 1}\\. ${escapeMarkdown(getUserName(u))} — ${u.level} ур\\. \\(${u.reputation} 🍪\\)\n`);
        sendTimedMessage(chatId, text, 60000, { parse_mode: 'MarkdownV2' });
    });

    // /shop
    bot.onText(/^\/shop$/, async (msg) => {
        const chatId = msg.chat.id;
        const helpText = `🛒 *Магазин печенек:*
1. *Купить уровень* (+1 ур.) — 500 🍪
   Команда: /buy 1
2. *Снять варны* (в 0) — 200 🍪
   Команда: /buy 2`;
        bot.sendMessage(chatId, helpText, { parse_mode: 'Markdown' });
    });

    // /buy
    bot.onText(/^\/buy (\d+)$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId, user: senderInfo } = getSenderData(msg);
        const itemId = parseInt(match[1]);
        deleteMsg(chatId, msg.message_id);

        const user = await getUser(chatId, userId, senderInfo);
        if (!user) return;

        if (itemId === 1) { // Уровень
            const cost = 500;
            if (user.reputation < cost) return sendTimedMessage(chatId, `❌ Недостаточно печенек! (Нужно ${cost})`, 15000);
            await updateUser(user.id, { level: user.level + 1, xp: 0, reputation: user.reputation - cost });
            sendTimedMessage(chatId, `✨ ${getUserName(senderInfo)} купил уровень! Теперь ты ${user.level + 1} ур.`, 30000);
        } else if (itemId === 2) { // Варны
            const cost = 200;
            if (user.reputation < cost) return sendTimedMessage(chatId, `❌ Недостаточно печенек!`, 15000);
            await updateUser(user.id, { warns: 0, reputation: user.reputation - cost });
            sendTimedMessage(chatId, `✅ ${getUserName(senderInfo)}, варны сняты!`, 30000);
        }
    });

    // /give
    bot.onText(/^\/give (\d+)$/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId: senderId, user: senderInfo } = getSenderData(msg);
        const amount = parseInt(match[1]);
        deleteMsg(chatId, msg.message_id);

        if (amount <= 0 || !msg.reply_to_message) return sendTimedMessage(chatId, `❌ Неверная сумма или нет реплая!`, 15000);
        const { userId: receiverId, user: receiverInfo } = getSenderData(msg.reply_to_message);
        if (senderId === receiverId) return sendTimedMessage(chatId, `❌ Себе нельзя!`, 10000);

        const sender = await getUser(chatId, senderId, senderInfo);
        const receiver = await getUser(chatId, receiverId, receiverInfo);
        if (!sender || !receiver || sender.reputation < amount) return sendTimedMessage(chatId, `❌ Недостаточно 🍪`, 15000);

        await updateUser(sender.id, { reputation: sender.reputation - amount });
        await updateUser(receiver.id, { reputation: receiver.reputation + amount });
        sendTimedMessage(chatId, `🍪 ${getUserName(senderInfo)} передал ${amount} печенек ${getUserName(receiverInfo)}!`, 60000);
    });

    // /kto
    bot.onText(/\/kto (.+)/, async (msg, match) => {
        const chatId = msg.chat.id;
        deleteMsg(chatId, msg.message_id);
        const { data: users } = await supabase.from('users').select('*').eq('chat_id', chatId);
        if (!users || users.length === 0) return;
        const randomUser = users[Math.floor(Math.random() * users.length)];
        sendTimedMessage(chatId, `🤔 Я думаю, что ${match[1]} — это ${getUserName(randomUser)}!`, 60000);
    });

    // /ban, /unban
    bot.onText(/\/ban(?:\s+(.+))?/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId, user: sender } = getSenderData(msg);
        deleteMsg(chatId, msg.message_id);
        if (!(await isAdmin(chatId, userId))) return sendTimedMessage(chatId, '⛔ Нет прав!', 30000);

        let targetId, targetName;
        if (msg.reply_to_message) {
            targetId = msg.reply_to_message.from.id; targetName = getUserName(msg.reply_to_message.from);
        } else if (match[1]) {
            const arg = match[1].trim();
            if (/^\d+$/.test(arg)) { targetId = parseInt(arg); targetName = `ID ${targetId}`; }
            else if (arg.startsWith('@')) {
                const { data } = await supabase.from('users').select('user_id, username').eq('chat_id', chatId).ilike('username', arg.substring(1)).maybeSingle();
                if (data) { targetId = data.user_id; targetName = `@${data.username}`; }
            }
        }
        if (!targetId || targetId === ANONYMOUS_ADMIN_ID || (await isAdmin(chatId, targetId))) return sendTimedMessage(chatId, '❌ Нельзя забанить!', 30000);
        try { await bot.banChatMember(chatId, targetId); sendTimedMessage(chatId, `🚫 ${getUserName(sender)} забанил ${targetName}.`, 60000); }
        catch (e) { sendTimedMessage(chatId, '❌ Ошибка бана.', 30000); }
    });

    bot.onText(/\/unban(?:\s+(.+))?/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId, user: sender } = getSenderData(msg);
        deleteMsg(chatId, msg.message_id);
        if (!(await isAdmin(chatId, userId))) return;

        let targetId, targetName;
        if (match[1]) {
            const arg = match[1].trim();
            if (/^\d+$/.test(arg)) { targetId = parseInt(arg); targetName = `ID ${targetId}`; }
            else if (arg.startsWith('@')) {
                const { data } = await supabase.from('users').select('user_id, username').eq('chat_id', chatId).ilike('username', arg.substring(1)).maybeSingle();
                if (data) { targetId = data.user_id; targetName = `@${data.username}`; }
            }
        }
        if (!targetId) return;
        try { await bot.unbanChatMember(chatId, targetId, { only_if_banned: true }); sendTimedMessage(chatId, `✅ ${getUserName(sender)} разбанил ${targetName}.`, 60000); }
        catch (e) { sendTimedMessage(chatId, '❌ Ошибка разбана.', 30000); }
    });

    // /banword, /unbanword, /listwords
    bot.onText(/\/banword (.+)/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId } = getSenderData(msg);
        if (!(await isAdmin(chatId, userId))) return;
        const word = match[1].trim();
        await supabase.from('bad_words').insert([{ chat_id: chatId, word: word }]);
        sendTimedMessage(chatId, `✅ Слово "${word}" в бане.`, 60000);
    });

    bot.onText(/\/unbanword (.+)/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId } = getSenderData(msg);
        if (!(await isAdmin(chatId, userId))) return;
        const word = match[1].trim();
        await supabase.from('bad_words').delete().eq('chat_id', chatId).eq('word', word);
        sendTimedMessage(chatId, `✅ Слово "${word}" удалено.`, 60000);
    });

    bot.onText(/\/listwords/, async (msg) => {
        const chatId = msg.chat.id;
        const { userId } = getSenderData(msg);
        if (!(await isAdmin(chatId, userId))) return;
        const { data } = await supabase.from('bad_words').select('word').eq('chat_id', chatId);
        const words = data?.map(i => i.word) || [];
        sendTimedMessage(chatId, words.length ? `🚫 Запрещенные слова:\n${words.join(', ')}` : 'Список пуст.', 60000);
    });
}

module.exports = { registerCommands };
