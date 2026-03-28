const { bot, escapeHTML, getUserName, getSenderData, sendTimedMessage, deleteMsg, isAdmin } = require('../utils');
const { supabase, ANONYMOUS_ADMIN_ID } = require('../database');

function registerAdminCommands() {
    // /ban
    bot.onText(/\/ban(?:\s+(.+))?/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId, user: sender } = getSenderData(msg);
        deleteMsg(chatId, msg.message_id);
        
        if (!(await isAdmin(chatId, userId))) return sendTimedMessage(chatId, '⛔ Нет прав!', 30000);

        let targetId, targetName;
        if (msg.reply_to_message) {
            targetId = msg.reply_to_message.from.id; 
            targetName = getUserName(msg.reply_to_message.from);
        } else if (match[1]) {
            const arg = match[1].trim();
            if (/^\d+$/.test(arg)) { 
                targetId = parseInt(arg); 
                targetName = `ID ${targetId}`; 
            } else if (arg.startsWith('@')) {
                const { data } = await supabase.from('users').select('user_id, username').eq('chat_id', chatId).ilike('username', arg.substring(1)).maybeSingle();
                if (data) { 
                    targetId = data.user_id; 
                    targetName = `@${data.username}`; 
                }
            }
        }
        
        if (!targetId || targetId === ANONYMOUS_ADMIN_ID || (await isAdmin(chatId, targetId))) {
            return sendTimedMessage(chatId, '❌ Нельзя забанить этого пользователя!', 30000);
        }
        
        try { 
            await bot.banChatMember(chatId, targetId); 
            sendTimedMessage(chatId, `🚫 <b>${escapeHTML(getUserName(sender))}</b> выдал бан пользователю <b>${escapeHTML(targetName)}</b>.`, 60000, { parse_mode: 'HTML' }); 
        } catch (e) { 
            sendTimedMessage(chatId, '❌ Ошибка: не удалось забанить.', 30000); 
        }
    });

    // /unban
    bot.onText(/\/unban(?:\s+(.+))?/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId, user: sender } = getSenderData(msg);
        deleteMsg(chatId, msg.message_id);
        
        if (!(await isAdmin(chatId, userId))) return;

        let targetId, targetName;
        if (match[1]) {
            const arg = match[1].trim();
            if (/^\d+$/.test(arg)) { 
                targetId = parseInt(arg); 
                targetName = `ID ${targetId}`; 
            } else if (arg.startsWith('@')) {
                const { data } = await supabase.from('users').select('user_id, username').eq('chat_id', chatId).ilike('username', arg.substring(1)).maybeSingle();
                if (data) { 
                    targetId = data.user_id; 
                    targetName = `@${data.username}`; 
                }
            }
        }
        
        if (!targetId) return;
        
        try { 
            await bot.unbanChatMember(chatId, targetId, { only_if_banned: true }); 
            sendTimedMessage(chatId, `✅ <b>${escapeHTML(getUserName(sender))}</b> разбанил пользователя <b>${escapeHTML(targetName)}</b>.`, 60000, { parse_mode: 'HTML' }); 
        } catch (e) { 
            sendTimedMessage(chatId, '❌ Ошибка: не удалось разбанить.', 30000); 
        }
    });

    // /banword
    bot.onText(/\/banword (.+)/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId } = getSenderData(msg);
        
        if (!(await isAdmin(chatId, userId))) return;
        
        const word = match[1].trim().toLowerCase();
        await supabase.from('bad_words').insert([{ chat_id: chatId, word: word }]);
        
        sendTimedMessage(chatId, `✅ Слово "<b>${escapeHTML(word)}</b>" заблокировано.`, 60000, { parse_mode: 'HTML' });
    });

    // /unbanword
    bot.onText(/\/unbanword (.+)/, async (msg, match) => {
        const chatId = msg.chat.id;
        const { userId } = getSenderData(msg);
        
        if (!(await isAdmin(chatId, userId))) return;
        
        const word = match[1].trim().toLowerCase();
        await supabase.from('bad_words').delete().eq('chat_id', chatId).eq('word', word);
        
        sendTimedMessage(chatId, `✅ Слово "<b>${escapeHTML(word)}</b>" удалено из фильтра.`, 60000, { parse_mode: 'HTML' });
    });

    // /listwords
    bot.onText(/\/listwords/, async (msg) => {
        const chatId = msg.chat.id;
        const { userId } = getSenderData(msg);
        
        if (!(await isAdmin(chatId, userId))) return;
        
        const { data } = await supabase.from('bad_words').select('word').eq('chat_id', chatId);
        const words = data?.map(i => escapeHTML(i.word)) || [];
        
        const text = words.length 
            ? `🚫 <b>Запрещенные слова в чате:</b>\n\n<code>${words.join(', ')}</code>` 
            : '📭 <i>Список запрещенных слов пуст.</i>';
            
        sendTimedMessage(chatId, text, 60000, { parse_mode: 'HTML' });
    });
}

module.exports = { registerAdminCommands };
