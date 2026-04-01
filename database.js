const { supabase } = require('./config');

const userCache = {}; // { chatId_userId: { data: userObj, expires: timestamp } }
const USER_CACHE_TTL = 300000; // 5 минут
const badWordsCache = {}; // { chatId: { words: [], expires: timestamp } }
const BAD_WORDS_CACHE_TTL = 600000; // 10 минут

const chatSettingsCache = {}; // { chatId: { settings: {}, expires: timestamp } }
const CHAT_SETTINGS_CACHE_TTL = 300000; // 5 минут

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
            xp: 0, level: 1, reputation: 0, warns: 0, last_message_time: 0,
            birthday: null, bio: '', last_ai_time: 0
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

async function getChatSettings(chatId) {
    if (chatSettingsCache[chatId] && Date.now() < chatSettingsCache[chatId].expires) {
        return chatSettingsCache[chatId].settings;
    }

    let { data, error } = await supabase
        .from('chats')
        .select('*')
        .eq('chat_id', chatId)
        .maybeSingle();

    if (error) {
        console.error('[DB ERROR] getChatSettings:', error.message);
        // Возвращаем настройки по умолчанию в случае ошибки
        return { link_filter_enabled: true };
    }

    if (!data) {
        // Если настроек нет — создаём запись с дефолтными значениями
        const { data: newData, error: insertError } = await supabase
            .from('chats')
            .insert([{ chat_id: chatId, link_filter_enabled: true }])
            .select()
            .single();
        if (insertError) return { link_filter_enabled: true };
        data = newData;
    }

    chatSettingsCache[chatId] = { settings: data, expires: Date.now() + CHAT_SETTINGS_CACHE_TTL };
    return data;
}

async function updateChatSettings(chatId, updates) {
    // Убеждаемся, что запись существует
    await getChatSettings(chatId);

    const { error } = await supabase
        .from('chats')
        .update({ ...updates, updated_at: new Date().toISOString() })
        .eq('chat_id', chatId);

    if (error) {
        console.error('[DB ERROR] updateChatSettings:', error.message);
        return false;
    }

    // Сброс кэша для этого чата
    delete chatSettingsCache[chatId];
    return true;
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
    [reactionCooldowns, commandCooldowns, userCache, chatSettingsCache].forEach(store => {
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

// Новые функции для профиля
async function setBirthday(chatId, userId, birthday) {
    const user = await getUser(chatId, userId);
    if (!user) return false;
    await updateUser(user.id, { birthday });
    return true;
}

async function setBio(chatId, userId, bio) {
    const user = await getUser(chatId, userId);
    if (!user) return false;
    await updateUser(user.id, { bio });
    return true;
}

async function getBirthdaysToday(chatId) {
    const today = new Date();
    const day = String(today.getDate()).padStart(2, '0');
    const month = String(today.getMonth() + 1).padStart(2, '0');
    const dateStr = `${day}.${month}`;

    const { data, error } = await supabase
        .from('users')
        .select('*')
        .eq('chat_id', chatId)
        .ilike('birthday', `${dateStr}%`);
    
    if (error) return [];
    return data;
}

const { ANONYMOUS_ADMIN_ID } = require('./config');

async function getChatMemory(chatId) {
    const { data } = await supabase
        .from('chats')
        .select('ai_memory')
        .eq('chat_id', chatId)
        .single();
    return data?.ai_memory || '';
}

async function updateChatMemory(chatId, memory) {
    await supabase
        .from('chats')
        .update({ ai_memory: memory })
        .eq('chat_id', chatId);
}

module.exports = {
    getUser, updateUser, getBadWords, getNextLevelXp, claimDailyBonus,
    getChatSettings, updateChatSettings,
    setBirthday, setBio, getBirthdaysToday,
    getChatMemory, updateChatMemory,
    messageAuthors, reactionCooldowns, commandCooldowns, userCache,
    supabase, ANONYMOUS_ADMIN_ID, pendingVerifications
};
