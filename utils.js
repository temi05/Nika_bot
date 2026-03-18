const { bot, token, ANONYMOUS_ADMIN_ID } = require('./config');

const adminCache = {}; // { chatId_userId: { isAdmin: bool, expires: timestamp } }
const ADMIN_CACHE_TTL = 120000; // 2 минуты

function getUserName(user) {
    if (!user) return 'Инкогнито';
    const name = user.username ? `@${user.username}` : user.first_name;
    return name || 'Инкогнито';
}

function escapeMarkdown(text) {
    if (text === undefined || text === null) return '';
    return String(text).replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, '\\$&');
}

function getSenderData(msg) {
    if (msg.sender_chat) {
        return {
            userId: msg.sender_chat.id,
            user: {
                id: msg.sender_chat.id,
                first_name: msg.sender_chat.title,
                username: msg.sender_chat.username || '',
                is_channel: true
            }
        };
    }
    return {
        userId: msg.from.id,
        user: msg.from
    };
}

function sendTimedMessage(chatId, text, delay = 15000, options = {}) {
    return bot.sendMessage(chatId, text, options).then(sentMsg => {
        setTimeout(() => {
            bot.deleteMessage(chatId, sentMsg.message_id).catch(() => { });
        }, delay);
        return sentMsg;
    }).catch(err => {
        if (err.message.includes('403') || err.message.includes('bot was blocked')) {
            // Игнорируем ошибки блокировки бота пользователем
            return null;
        }
        console.error('[BOT ERROR] sendTimedMessage:', err.message);
        return null;
    });
}

function deleteMsg(chatId, msgId, delay = 60000) {
    setTimeout(() => {
        bot.deleteMessage(chatId, msgId).catch(() => { });
    }, delay);
}

async function isAdmin(chatId, userId) {
    // Анонимные админы и каналы в группах считаются админами
    if (userId === ANONYMOUS_ADMIN_ID || userId < 0) return true;

    const cacheKey = `${chatId}_${userId}`;
    const cached = adminCache[cacheKey];
    if (cached && Date.now() < cached.expires) {
        return cached.isAdmin;
    }

    try {
        const member = await bot.getChatMember(chatId, userId);
        const isAdminStatus = ['creator', 'administrator'].includes(member.status);
        
        // Сохраняем в кэш
        adminCache[cacheKey] = {
            isAdmin: isAdminStatus,
            expires: Date.now() + ADMIN_CACHE_TTL
        };
        
        return isAdminStatus;
    } catch (e) {
        return false;
    }
}

module.exports = {
    getUserName,
    escapeMarkdown,
    getSenderData,
    sendTimedMessage,
    deleteMsg,
    isAdmin,
    adminCache,
    token,
    ANONYMOUS_ADMIN_ID
};
