const { supabase } = require('./config');

const userCache = {}; // { chatId_userId: { data: userObj, expires: timestamp } }
const USER_CACHE_TTL = 300000; // 5 минут
const badWordsCache = {}; // { chatId: { words: [], expires: timestamp } }
const BAD_WORDS_CACHE_TTL = 600000; // 10 минут

// Хранилища для очистки
const messageAuthors = {};
const reactionCooldowns = {};
const commandCooldowns = {};
const pendingVerifications = {};

async function getUser(chatId, userId, userInfo = {}) {
    const cacheKey = `${chatId}_${userId}`;
    if (userCache[cacheKey] && Date.now() < userCache[cacheKey].expires) {
        return userCache[cacheKey].data;
    }

    let { data: user, error } = await supabase
        .from('users')
        .select('*')
        .eq('chat_id', chatId)
        .eq('user_id', userId)
        .single();

    if (error && error.code !== 'PGRST116') {
        console.error('[DB ERROR] getUser:', error.message);
        return null;
    }

    if (!user) {
        let name = userInfo.first_name || userInfo.title || 'Инкогнито';
        if (userId < 0 && (name === 'Инкогнито' || name === '')) {
            name = `Канал ${Math.abs(userId)}`;
        }
        
        const newUser = {
            chat_id: chatId,
            user_id: userId,
            username: userInfo.username || '',
            first_name: name,
            xp: 0, level: 1, reputation: 0, warns: 0, last_message_time: 0
        };
        const { data, error: createError } = await supabase
            .from('users').insert([newUser]).select().single();
        if (createError) return null;
        user = data;
    }

    userCache[cacheKey] = { data: user, expires: Date.now() + USER_CACHE_TTL };
    return user;
}

async function updateUser(id, updates) {
    const { error } = await supabase.from('users').update(updates).eq('id', id);
    if (error) {
        console.error('[DB ERROR] updateUser:', error.message);
        return;
    }
    const cacheKey = Object.keys(userCache).find(key => userCache[key].data.id === id);
    if (cacheKey) {
        userCache[cacheKey].data = { ...userCache[cacheKey].data, ...updates };
        userCache[cacheKey].expires = Date.now() + USER_CACHE_TTL;
    }
}

async function getBadWords(chatId) {
    if (badWordsCache[chatId] && Date.now() < badWordsCache[chatId].expires) {
        return badWordsCache[chatId].words;
    }
    const { data, error } = await supabase.from('bad_words').select('word').eq('chat_id', chatId);
    if (error) return [];
    const words = data.map(item => item.word);
    badWordsCache[chatId] = { words, expires: Date.now() + BAD_WORDS_CACHE_TTL };
    return words;
}

function cleanupStores() {
    const now = Date.now();
    const oneDay = 24 * 60 * 60 * 1000;

    // Очистка авторов сообщений
    for (const chatId in messageAuthors) {
        const keys = Object.keys(messageAuthors[chatId]);
        if (keys.length > 1000) {
            keys.slice(0, keys.length - 1000).forEach(k => delete messageAuthors[chatId][k]);
        }
    }

    // Очистка кулдаунов и кэша пользователей
    [reactionCooldowns, commandCooldowns, userCache].forEach(store => {
        for (const key in store) {
            const time = store[key].expires || store[key];
            if (now - time > oneDay) delete store[key];
        }
    });
}
setInterval(cleanupStores, 3600000);

function getNextLevelXp(level) {
    return 50 * level * level + 50 * level;
}

const { ANONYMOUS_ADMIN_ID } = require('./config');

module.exports = {
    getUser, updateUser, getBadWords, getNextLevelXp,
    messageAuthors, reactionCooldowns, commandCooldowns, userCache,
    supabase, ANONYMOUS_ADMIN_ID, pendingVerifications
};
