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
    
    if (user) {
        // console.log(`[DB DEBUG] getUser FOUND: userId ${userId} -> dbId ${user.id}`);
    }

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
            photo_url: userInfo.photo_url || '',
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
    console.log(`[DB DEBUG] updateUser (DB_ID: ${id}) updates:`, updates);
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

async function claimDailyBonus(chatId, userId) {
    const user = await getUser(chatId, userId);
    if (!user) return { success: false, message: 'Пользователь не найден' };

    const now = new Date();
    // Проверяем кулдаун в 24 часа
    if (user.last_daily_claim) {
        const lastClaim = new Date(user.last_daily_claim);
        const diffHours = (now - lastClaim) / (1000 * 60 * 60);
        if (diffHours < 24) {
            const remaining = 24 - diffHours;
            const hours = Math.floor(remaining);
            const minutes = Math.floor((remaining - hours) * 60);
            return { success: false, timeRemaining: { hours, minutes }, message: `⏳ Бонус будет доступен через ${hours} ч. ${minutes} мин.` };
        }
    }

    // Вычисляем бонус: 50-150 XP, 10% шанс на +1 к репе.
    const bonusXp = Math.floor(Math.random() * 101) + 50; 
    const isRepGained = Math.random() < 0.10;
    
    // Подготовка обновлений. Проверяем повышение уровня в messageHandler, либо здесь.
    // Удобнее просто вернуть новый статус, а левелап проверять при следующем сообщении, или прямо тут.
    // Сделаем тут проверку:
    let newXp = (user.xp || 0) + bonusXp;
    let newLevel = user.level || 1;
    let nextXp = getNextLevelXp(newLevel);
    let levelUp = false;

    while (newXp >= nextXp) {
        newLevel++;
        nextXp = getNextLevelXp(newLevel);
        levelUp = true;
    }

    const updates = { 
        xp: newXp,
        level: newLevel,
        last_daily_claim: now.toISOString()
    };
    if (isRepGained) {
        updates.reputation = (user.reputation || 0) + 1;
    }

    await updateUser(user.id, updates);

    return { 
        success: true, 
        bonusXp, 
        isRepGained, 
        newXp, 
        newLevel, 
        levelUp,
        newReputation: updates.reputation || user.reputation
    };
}

function getNextLevelXp(level) {
    return 50 * level * level + 50 * level;
}

const { ANONYMOUS_ADMIN_ID } = require('./config');

module.exports = {
    getUser, updateUser, getBadWords, getNextLevelXp, claimDailyBonus,
    messageAuthors, reactionCooldowns, commandCooldowns, userCache,
    supabase, ANONYMOUS_ADMIN_ID, pendingVerifications
};
