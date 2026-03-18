const captchapng = require('captchapng');
const { bot, token, escapeMarkdown, sendTimedMessage, isAdmin } = require('../utils');
const { pendingVerifications } = require('../database');

function registerVerificationHandlers() {
    bot.on('new_chat_members', async (msg) => {
        const chatId = msg.chat.id;
        const botId = parseInt(token.split(':')[0]);
        const inviter = msg.from;

        for (const member of msg.new_chat_members) {
            if (member.is_bot) {
                if (member.id !== botId) {
                    await bot.banChatMember(chatId, member.id);
                    sendTimedMessage(chatId, `🚨 <b>ОБНАРУЖЕН БОТ!</b> Пригласил: ${inviter.first_name}`, 300000, { parse_mode: 'HTML' });
                }
                continue;
            }

            // Ограничиваем права
            try {
                await bot.restrictChatMember(chatId, member.id, { can_send_messages: true, can_send_other_messages: false });
            } catch (e) { continue; }

            // Капча
            const num = parseInt(Math.random() * 9000 + 1000);
            const p = new captchapng(150, 150, num);
            p.color(30, 30, 30, 255); p.color(255, 255, 255, 255);
            const img = Buffer.from(p.getBase64(), 'base64');

            bot.sendPhoto(chatId, img, {
                caption: `👋 Привет! Напиши цифры с картинки за 2 минуты.`,
                parse_mode: 'Markdown'
            }).then(sentMsg => {
                const timer = setTimeout(async () => {
                    await bot.banChatMember(chatId, member.id, { until_date: Math.floor(Date.now() / 1000) + 60 });
                    bot.deleteMessage(chatId, sentMsg.message_id).catch(() => {});
                    delete pendingVerifications[member.id];
                }, 120000);
                pendingVerifications[member.id] = { timer, code: num.toString(), msgId: sentMsg.message_id };
            });
        }
    });

    bot.on('message', async (msg) => {
        if (pendingVerifications[msg.from.id]) {
            const v = pendingVerifications[msg.from.id];
            if (msg.text?.trim() === v.code) {
                clearTimeout(v.timer);
                delete pendingVerifications[msg.from.id];
                await bot.restrictChatMember(msg.chat.id, msg.from.id, { can_send_messages: true, can_send_other_messages: true, can_send_media_messages: true });
                bot.deleteMessage(msg.chat.id, v.msgId).catch(() => {});
                bot.deleteMessage(msg.chat.id, msg.message_id).catch(() => {});
                sendTimedMessage(msg.chat.id, `✅ ${msg.from.first_name} прошел проверку!`, 10000);
            } else {
                bot.deleteMessage(msg.chat.id, msg.message_id).catch(() => {});
            }
        }
    });

    bot.on('callback_query', async (query) => {
        if (query.data.startsWith('ban_')) {
            const chatId = query.message.chat.id;
            const targetId = parseInt(query.data.split('_')[1]);
            
            // Проверка прав (учитываем анонимных админов)
            const actorId = query.from.id;
            const chatActorId = query.message.sender_chat?.id; // Если админ анонимный
            
            if (!(await isAdmin(chatId, actorId)) && !(chatActorId && await isAdmin(chatId, chatActorId))) {
                return bot.answerCallbackQuery(query.id, { text: '⛔ У вас нет прав!' });
            }

            const v = pendingVerifications[targetId];
            if (v) { clearTimeout(v.timer); delete pendingVerifications[targetId]; }
            
            try {
                await bot.banChatMember(chatId, targetId);
                bot.deleteMessage(chatId, query.message.message_id).catch(() => {});
                bot.answerCallbackQuery(query.id, { text: '✅ Пользователь забанен.' });
            } catch (e) {
                bot.answerCallbackQuery(query.id, { text: '❌ Ошибка при бане.' });
            }
        }
    });
}

module.exports = { registerVerificationHandlers };
