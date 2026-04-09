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

// Функция транслитерации для поиска (Ника -> Nika)
function transliterate(text) {
    const map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'zh',
        'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
        'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts',
        'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
    };
    return text.toLowerCase().split('').map(char => map[char] || char).join('');
}

const WARN_DECAY_DAYS = Math.max(1, Number(process.env.WARN_DECAY_DAYS || 7));

function areWarnsExpired(user) {
    if (!user || !user.warns || !user.last_warn_at) return false;
    const lastWarnAt = new Date(user.last_warn_at);
    if (Number.isNaN(lastWarnAt.getTime())) return false;
    const decayMs = WARN_DECAY_DAYS * 24 * 60 * 60 * 1000;
    return Date.now() - lastWarnAt.getTime() >= decayMs;
}

function isMissingLastWarnColumnError(error) {
    return Boolean(error && /last_warn_at/i.test(String(error.message || '')));
}

async function resetExpiredWarns(user) {
    if (!areWarnsExpired(user)) return user;

    const updates = { warns: 0, last_warn_at: null };
    const { data, error } = await supabase
        .from('users')
        .update(updates)
        .eq('id', user.id)
        .select()
        .maybeSingle();

    if (error) {
        console.warn('[DB WARN] resetExpiredWarns:', error.message);
        return { ...user, ...updates };
    }

    return data || { ...user, ...updates };
}

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
            birthday: null, bio: '', ai_notes: '', last_ai_time: 0
        };
        const { data, error: createError } = await supabase
            .from('users').insert([newUser]).select().single();
        if (createError) return null;
        user = data;
    }

    user = await resetExpiredWarns(user);

    userCache[cacheKey] = { data: user, expires: Date.now() + USER_CACHE_TTL };
    return user;
}

async function updateUser(id, updates) {
    console.log(`[DB UPDATE] User ID ${id}: ${Object.keys(updates).join(', ')}`);
    let { data, error } = await supabase.from('users').update(updates).eq('id', id).select();
    if (error && Object.prototype.hasOwnProperty.call(updates, 'last_warn_at') && isMissingLastWarnColumnError(error)) {
        const fallbackUpdates = { ...updates };
        delete fallbackUpdates.last_warn_at;
        ({ data, error } = await supabase.from('users').update(fallbackUpdates).eq('id', id).select());
    }
    if (error) {
        console.error('[DB ERROR] updateUser:', error.message);
        return;
    }
    const cacheKey = Object.keys(userCache).find(key => userCache[key].data.id === id);
    if (cacheKey) {
        userCache[cacheKey].data = { ...userCache[cacheKey].data, ...data[0] };
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
        return { link_filter_enabled: true };
    }

    if (!data) {
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
    await getChatSettings(chatId);

    const { error } = await supabase
        .from('chats')
        .update({ ...updates, updated_at: new Date().toISOString() })
        .eq('chat_id', chatId);

    if (error) {
        console.error('[DB ERROR] updateChatSettings:', error.message);
        return false;
    }

    delete chatSettingsCache[chatId];
    return true;
}

function cleanupStores() {
    const now = Date.now();
    const oneDay = 24 * 60 * 60 * 1000;

    for (const chatId in messageAuthors) {
        const keys = Object.keys(messageAuthors[chatId]);
        if (keys.length > 1000) {
            keys.slice(0, keys.length - 1000).forEach(k => delete messageAuthors[chatId][k]);
        }
    }

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

    const bonusXp = Math.floor(Math.random() * 101) + 50;
    const isRepGained = Math.random() < 0.10;

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

async function setBioByUsernameOrName(chatId, queryName, bio) {
    const data = await findBestUserMatch(chatId, queryName, {
        select: 'id, first_name',
        limit: 20,
        minScore: 100,
        excludeProfileFields: true
    });

    if (!data) return null;

    await updateUser(data.id, { bio });
    return data.first_name;
}

async function setNotesByUsernameOrName(chatId, queryName, notes) {
    const data = await findBestUserMatch(chatId, queryName, {
        select: 'id, first_name',
        limit: 20,
        minScore: 100
    });

    if (!data) return null;

    await updateUser(data.id, { ai_notes: notes });
    return data.first_name;
}

async function setFirstNameByUsernameOrName(chatId, queryName, newName) {
    if (!newName) return null;
    const data = await findBestUserMatch(chatId, queryName, {
        select: 'id, first_name',
        limit: 20,
        minScore: 100,
        excludeProfileFields: true
    });

    if (!data) return null;

    await updateUser(data.id, { first_name: newName });
    return data.first_name;
}

async function getChatStats(chatId) {
    const { data: topUsers, error: topError } = await supabase
        .from('users')
        .select('first_name, username, level, xp, reputation')
        .eq('chat_id', chatId)
        .order('level', { ascending: false })
        .order('xp', { ascending: false })
        .limit(5);

    const { count, error: countError } = await supabase
        .from('users')
        .select('*', { count: 'exact', head: true })
        .eq('chat_id', chatId);

    if (topError || countError) return null;

    return {
        totalUsers: count || 0,
        topUsers: (topUsers || []).map((u, i) => ({
            place: i + 1,
            name: u.username ? `@${u.username}` : u.first_name,
            level: u.level,
            xp: u.xp,
            cookies: u.reputation
        }))
    };
}

function getStem(word) {
    if (!word || word.length < 3) return word;
    return word.toLowerCase()
        .replace(/[уаеяюиыо]$/i, '')
        .replace(/(ов|ев|ий|ый|ые|ие|ах|ях|ом|ем)$/i, '')
        .replace(/(s|es|ed|ing)$/i, '');
}

function normalizeUserSearchText(value) {
    return String(value || '')
        .toLowerCase()
        .replace(/@/g, '')
        .replace(/[^\p{L}\p{N}\s_-]/gu, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function buildUserSearchVariants(query) {
    const clean = normalizeUserSearchText(query);
    const variants = new Set();
    if (!clean) return [];

    variants.add(clean);
    const stem = getStem(clean);
    if (stem) variants.add(stem);

    const translitClean = transliterate(clean);
    if (translitClean) variants.add(translitClean);

    const translitStem = transliterate(stem);
    if (translitStem) variants.add(translitStem);

    clean.split(/\s+/).forEach(part => {
        if (!part) return;
        variants.add(part);
        const partStem = getStem(part);
        if (partStem) variants.add(partStem);
        const partTranslit = transliterate(part);
        if (partTranslit) variants.add(partTranslit);
    });

    return Array.from(variants).filter(Boolean);
}

function scoreUserCandidate(user, variants) {
    const fields = [
        normalizeUserSearchText(user.first_name || ''),
        normalizeUserSearchText(user.username || ''),
        normalizeUserSearchText(user.bio || ''),
        normalizeUserSearchText(user.ai_notes || '')
    ];

    let score = 0;
    for (const variant of variants) {
        if (!variant) continue;
        const variantTokens = variant.split(/\s+/).filter(Boolean);

        for (const field of fields) {
            if (!field) continue;
            if (field === variant) score = Math.max(score, 150);
            if (field.startsWith(variant)) score = Math.max(score, 130);
            if (field.includes(` ${variant}`) || field.includes(`${variant} `)) score = Math.max(score, 120);
            if (field.includes(variant)) score = Math.max(score, 100);

            for (const token of field.split(/\s+/)) {
                if (token === variant) score = Math.max(score, 140);
                else if (token.startsWith(variant)) score = Math.max(score, 125);
                else if (variant.startsWith(token) && token.length >= 3) score = Math.max(score, 110);
            }

            if (variantTokens.length > 1) {
                const hits = variantTokens.filter(token => field.includes(token)).length;
                if (hits === variantTokens.length) score = Math.max(score, 135);
                else if (hits > 0) score = Math.max(score, 105 + hits);
            }
        }
    }

    return score;
}

async function findBestUserMatch(chatId, query, options = {}) {
    if (!query) return null;
    const rawQuery = String(query).trim();

    if (/^-?\d+$/.test(rawQuery)) {
        const numericUserId = Number(rawQuery);
        const { data: exactById, error: exactByIdError } = await supabase
            .from('users')
            .select(options.select || '*')
            .eq('chat_id', chatId)
            .eq('user_id', numericUserId)
            .limit(1)
            .maybeSingle();

        if (!exactByIdError && exactById) {
            return options.returnMany ? [{ user: exactById, score: 1000 }] : exactById;
        }
    }

    const variants = buildUserSearchVariants(query);
    if (variants.length === 0) return null;

    const broadNeedles = Array.from(new Set(
        variants
            .map(v => v.slice(0, Math.max(3, Math.min(v.length, 8))))
            .filter(v => v.length >= 3)
    )).slice(0, 6);

    const orParts = [];
    for (const needle of broadNeedles) {
        orParts.push(`username.ilike.%${needle}%`);
        orParts.push(`first_name.ilike.%${needle}%`);
        if (!options.excludeProfileFields) {
            orParts.push(`bio.ilike.%${needle}%`);
            orParts.push(`ai_notes.ilike.%${needle}%`);
        }
    }

    if (orParts.length === 0) return null;

    const { data, error } = await supabase
        .from('users')
        .select(options.select || '*')
        .eq('chat_id', chatId)
        .or(orParts.join(','))
        .limit(options.limit || 40);

    if (error || !data || data.length === 0) return null;

    const ranked = data
        .map(user => ({ user, score: scoreUserCandidate(user, variants) }))
        .filter(item => item.score >= (options.minScore || 100))
        .sort((a, b) => b.score - a.score);

    if (ranked.length === 0) return null;
    return options.returnMany ? ranked : ranked[0].user;
}

async function searchUserByName(chatId, query) {
    const matches = await findBestUserMatch(chatId, query, {
        select: 'user_id, first_name, username, level, xp, reputation, bio, ai_notes, warns, birthday',
        returnMany: true,
        limit: 25,
        minScore: 95
    });

    if (!matches || matches.length === 0) return null;
    return matches.slice(0, 5).map(({ user: u }) => ({
        user_id: u.user_id,
        name: u.username ? `@${u.username}` : u.first_name,
        level: u.level,
        xp: u.xp,
        cookies: u.reputation,
        bio: u.bio || 'Нет био',
        warns: u.warns,
        birthday: u.birthday || 'Не указан'
    }));
}

async function warnUserById(chatId, targetName) {
    const data = await findBestUserMatch(chatId, targetName, {
        select: 'id, first_name, username, warns, user_id',
        limit: 20,
        minScore: 100,
        excludeProfileFields: true
    });

    if (!data) return null;

    let lastWarnAt = null;
    const { data: warnMeta, error: warnMetaError } = await supabase
        .from('users')
        .select('last_warn_at')
        .eq('id', data.id)
        .maybeSingle();

    if (!warnMetaError && warnMeta) {
        lastWarnAt = warnMeta.last_warn_at;
    }

    const previousWarns = areWarnsExpired({ ...data, last_warn_at: lastWarnAt }) ? 0 : Number(data.warns || 0);
    const newWarns = Math.min(previousWarns + 1, 3);
    await updateUser(data.id, {
        warns: newWarns,
        last_warn_at: new Date().toISOString()
    });

    return {
        id: data.id,
        name: data.username ? `@${data.username}` : data.first_name,
        userId: data.user_id,
        newWarns: newWarns,
        shouldMute: previousWarns < 3 && newWarns >= 3
    };
}

async function getUpcomingBirthdays(chatId) {
    const { data, error } = await supabase
        .from('users')
        .select('first_name, username, birthday')
        .eq('chat_id', chatId)
        .not('birthday', 'is', null)
        .neq('birthday', '');

    if (error || !data) return [];

    const today = new Date();
    const upcoming = [];

    for (const u of data) {
        if (!u.birthday) continue;
        const parts = u.birthday.split('.');
        if (parts.length < 2) continue;

        const day = parseInt(parts[0]);
        const month = parseInt(parts[1]) - 1;

        const bdayThisYear = new Date(today.getFullYear(), month, day);
        if (bdayThisYear < today) bdayThisYear.setFullYear(today.getFullYear() + 1);

        const diffDays = Math.ceil((bdayThisYear - today) / (1000 * 60 * 60 * 24));

        if (diffDays <= 7) {
            upcoming.push({
                name: u.username ? `@${u.username}` : u.first_name,
                birthday: u.birthday,
                daysUntil: diffDays === 0 ? 'Сегодня!' : `через ${diffDays} дн.`
            });
        }
    }

    return upcoming.sort((a, b) => {
        const dA = a.daysUntil === 'Сегодня!' ? 0 : parseInt(a.daysUntil);
        const dB = b.daysUntil === 'Сегодня!' ? 0 : parseInt(b.daysUntil);
        return dA - dB;
    });
}

async function findSingleUser(chatId, query) {
    const user = await findBestUserMatch(chatId, query, {
        select: '*',
        limit: 25,
        minScore: 100,
        excludeProfileFields: true
    });
    return user || null;
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

function normalizeMemoryText(value) {
    return String(value || '')
        .toLowerCase()
        .replace(/\s+/g, ' ')
        .trim();
}

function normalizeKnowledgePayload(payload) {
    const base = (payload && typeof payload === 'object') ? { ...payload } : { fact: payload };

    const factType = base.factType
        || (base.kind === 'relation' ? 'relation' : base.kind === 'attribute' ? 'attribute' : undefined)
        || (base.relation && base.object ? 'relation' : undefined)
        || (base.attribute && base.value ? 'attribute' : undefined)
        || 'fact';

    return {
        ...base,
        factType,
        subjectName: base.subjectName || base.subject || null,
        relationType: base.relationType || base.relation || null,
        objectName: base.objectName || base.object || null,
        attribute: base.attribute || null,
        value: base.value || null,
    };
}

function buildKnowledgeFactText(payload) {
    const normalized = normalizeKnowledgePayload(payload);
    if (normalized.fact) return String(normalized.fact).trim();

    if (normalized.factType === 'relation' && normalized.subjectName && normalized.relationType && normalized.objectName) {
        return `СВЯЗЬ: ${normalized.subjectName} -> ${normalized.relationType} -> ${normalized.objectName}`;
    }

    if (normalized.subjectName && normalized.attribute && normalized.value) {
        return `УЗЕЛ: ${normalized.subjectName} | ${normalized.attribute}: ${normalized.value}`;
    }

    return '';
}

function buildKnowledgeFingerprint(payload) {
    const normalized = normalizeKnowledgePayload(payload);
    if (normalized.fingerprint) return normalizeMemoryText(normalized.fingerprint);

    if (normalized.factType === 'relation') {
        return `relation|${normalizeMemoryText(normalized.subjectName)}|${normalizeMemoryText(normalized.relationType)}|${normalizeMemoryText(normalized.objectName)}`;
    }

    if (normalized.subjectName && normalized.attribute && normalized.value) {
        return `attribute|${normalizeMemoryText(normalized.subjectName)}|${normalizeMemoryText(normalized.attribute)}|${normalizeMemoryText(normalized.value)}`;
    }

    return `fact|${normalizeMemoryText(buildKnowledgeFactText(normalized))}`;
}

function sanitizeKnowledgeEmbedding(embedding) {
    return Array.isArray(embedding) && embedding.length === 1536 ? embedding : null;
}

function shouldPromoteKnowledge(status, confidence, sourceCount, timesSeen) {
    if (status === 'confirmed') return true;
    return (sourceCount >= 2 && confidence >= 0.72) || timesSeen >= 3;
}

function getKnowledgeMemoryKind(row) {
    const kind = String(row?.meta?.memory_kind || '').trim().toLowerCase();
    if (kind === 'pattern' || kind === 'risk') return kind;
    return 'fact';
}

function applyKnowledgeFilters(rows, options = {}) {
    let filtered = Array.isArray(rows) ? [...rows] : [];
    const includeKinds = Array.isArray(options.includeMemoryKinds) && options.includeMemoryKinds.length
        ? new Set(options.includeMemoryKinds.map(v => String(v).toLowerCase()))
        : null;
    const excludeKinds = Array.isArray(options.excludeMemoryKinds) && options.excludeMemoryKinds.length
        ? new Set(options.excludeMemoryKinds.map(v => String(v).toLowerCase()))
        : null;

    if (includeKinds) {
        filtered = filtered.filter(row => includeKinds.has(getKnowledgeMemoryKind(row)));
    }

    if (excludeKinds) {
        filtered = filtered.filter(row => !excludeKinds.has(getKnowledgeMemoryKind(row)));
    }

    filtered.sort((left, right) => {
        const leftKindRank = getKnowledgeMemoryKind(left) === 'fact' ? 0 : getKnowledgeMemoryKind(left) === 'pattern' ? 1 : 2;
        const rightKindRank = getKnowledgeMemoryKind(right) === 'fact' ? 0 : getKnowledgeMemoryKind(right) === 'pattern' ? 1 : 2;
        if (leftKindRank !== rightKindRank) return leftKindRank - rightKindRank;

        const confidenceDiff = Number(right.confidence || 0) - Number(left.confidence || 0);
        if (confidenceDiff !== 0) return confidenceDiff;

        const timesSeenDiff = Number(right.times_seen || 0) - Number(left.times_seen || 0);
        if (timesSeenDiff !== 0) return timesSeenDiff;

        return new Date(right.last_seen_at || 0).getTime() - new Date(left.last_seen_at || 0).getTime();
    });

    if (options.limitAfterFilter) {
        filtered = filtered.slice(0, options.limitAfterFilter);
    }

    return filtered;
}

async function insertKnowledge(chatId, factText, embedding) {
    const payload = normalizeKnowledgePayload(factText);

    const fact = buildKnowledgeFactText(payload);
    if (!fact) return null;

    const fingerprint = buildKnowledgeFingerprint({ ...payload, fact });
    const confidence = Math.max(0.3, Math.min(0.98, Number(payload.confidence || 0.55)));
    const nowIso = new Date().toISOString();
    const safeEmbedding = sanitizeKnowledgeEmbedding(embedding);

    try {
        const { data: existing, error: existingError } = await supabase
            .from('bot_knowledge')
            .select('*')
            .eq('chat_id', chatId)
            .eq('fingerprint', fingerprint)
            .limit(1)
            .maybeSingle();

        if (existingError && !String(existingError.message || '').includes('fingerprint')) {
            console.error('[DB ERROR] insertKnowledge lookup:', existingError.message);
        }

        if (existing) {
            const mergedMeta = {
                ...(existing.meta || {}),
                ...(payload.meta || {}),
            };
            const memoryKind = getKnowledgeMemoryKind({ meta: mergedMeta });
            const nextTimesSeen = (existing.times_seen || 1) + 1;
            const nextSourceCount = (existing.source_count || 1) + 1;
            const nextConfidence = Math.min(
                0.98,
                Math.max(existing.confidence || 0.55, confidence) + (memoryKind === 'pattern' ? 0.04 : 0.08)
            );
            const nextStatus = shouldPromoteKnowledge(existing.status, nextConfidence, nextSourceCount, nextTimesSeen)
                ? 'confirmed'
                : 'candidate';

            const { data, error } = await supabase
                .from('bot_knowledge')
                .update({
                    fact,
                    embedding: safeEmbedding || existing.embedding,
                    fact_type: payload.factType || existing.fact_type || 'fact',
                    subject_name: payload.subjectName || existing.subject_name || null,
                    relation_type: payload.relationType || existing.relation_type || null,
                    object_name: payload.objectName || existing.object_name || null,
                    confidence: nextConfidence,
                    status: nextStatus,
                    source_count: nextSourceCount,
                    times_seen: nextTimesSeen,
                    fingerprint,
                    meta: mergedMeta,
                    last_seen_at: nowIso
                })
                .eq('id', existing.id)
                .select()
                .maybeSingle();

            if (error) throw error;
            if (data) data._memoryAction = 'updated';
            return data;
        }

        const status = shouldPromoteKnowledge(payload.status || 'candidate', confidence, 1, 1)
            ? 'confirmed'
            : (payload.status || 'candidate');

        const row = {
            chat_id: chatId,
            fact,
            embedding: safeEmbedding,
            fact_type: payload.factType || 'fact',
            subject_name: payload.subjectName || null,
            relation_type: payload.relationType || null,
            object_name: payload.objectName || null,
            confidence,
            status,
            source_count: 1,
            times_seen: 1,
            fingerprint,
            meta: payload.meta || {},
            last_seen_at: nowIso
        };

        const { data, error } = await supabase
            .from('bot_knowledge')
            .insert([row])
            .select()
            .maybeSingle();

        if (error) throw error;
        if (data) data._memoryAction = 'created';
        return data;
    } catch (error) {
        const fallback = await supabase
            .from('bot_knowledge')
            .insert([{ chat_id: chatId, fact, embedding: null }])
            .select()
            .maybeSingle();

        if (fallback.error) {
            console.error('[DB ERROR] insertKnowledge:', {
                primary: error?.message || String(error),
                fallback: fallback.error.message || String(fallback.error),
                fact,
                chatId
            });
            return null;
        }
        if (fallback.data) fallback.data._memoryAction = 'fallback_created';
        return fallback.data;
    }
}

async function searchKnowledge(chatId, queryEmbedding, limit = 3, threshold = 0.3, options = {}) {
    const statuses = options.statuses || ['confirmed'];
    const minConfidence = options.minConfidence ?? 0.55;

    const advanced = await supabase.rpc('match_knowledge_v2', {
        query_embedding: queryEmbedding,
        match_threshold: threshold,
        match_count: limit,
        p_chat_id: chatId,
        p_statuses: statuses,
        p_min_confidence: minConfidence
    });

    if (!advanced.error) {
        return applyKnowledgeFilters(advanced.data || [], {
            ...options,
            limitAfterFilter: limit
        });
    }

    const legacy = await supabase.rpc('match_knowledge', {
        query_embedding: queryEmbedding,
        match_threshold: threshold,
        match_count: limit,
        p_chat_id: chatId
    });

    if (legacy.error) {
        console.error('[DB ERROR] searchKnowledge:', advanced.error.message || legacy.error.message);
        return [];
    }

    return applyKnowledgeFilters((legacy.data || []).map(item => ({
        ...item,
        confidence: 0.7,
        status: 'confirmed',
        times_seen: 1,
        source_count: 1
    })), {
        ...options,
        limitAfterFilter: limit
    });
}

async function searchKnowledgeByText(chatId, query, limit = 5, options = {}) {
    const statuses = options.statuses || ['confirmed', 'candidate'];
    const minConfidence = options.minConfidence ?? 0;

    let advancedQuery = supabase
        .from('bot_knowledge')
        .select('*')
        .eq('chat_id', chatId)
        .ilike('fact', `%${query}%`)
        .gte('confidence', minConfidence)
        .in('status', statuses)
        .order('confidence', { ascending: false })
        .order('last_seen_at', { ascending: false })
        .limit(limit);

    const advanced = await advancedQuery;
    if (!advanced.error) {
        return applyKnowledgeFilters(advanced.data || [], {
            ...options,
            limitAfterFilter: limit
        });
    }

    const legacy = await supabase
        .from('bot_knowledge')
        .select('*')
        .eq('chat_id', chatId)
        .ilike('fact', `%${query}%`)
        .order('id', { ascending: false })
        .limit(limit);

    if (legacy.error) {
        console.error('[DB ERROR] searchKnowledgeByText:', advanced.error.message || legacy.error.message);
        return [];
    }

    return applyKnowledgeFilters(legacy.data || [], {
        ...options,
        limitAfterFilter: limit
    });
}

async function getRecentKnowledge(chatId, userName = "", limit = 10, options = {}) {
    const statuses = options.statuses || ['confirmed', 'candidate'];
    const minConfidence = options.minConfidence ?? 0;

    let advancedQuery = supabase
        .from('bot_knowledge')
        .select('*')
        .eq('chat_id', chatId)
        .gte('confidence', minConfidence)
        .in('status', statuses);

    if (userName) {
        advancedQuery = advancedQuery.or(
            `subject_name.ilike.%${userName}%,fact.ilike.%${userName}%`
        );
    }

    const advanced = await advancedQuery
        .order('last_seen_at', { ascending: false })
        .limit(limit);

    if (!advanced.error) {
        return applyKnowledgeFilters(advanced.data || [], {
            ...options,
            limitAfterFilter: limit
        });
    }

    let legacyQuery = supabase
        .from('bot_knowledge')
        .select('*')
        .eq('chat_id', chatId);

    if (userName) {
        legacyQuery = legacyQuery.ilike('fact', `%${userName}%`);
    }

    const legacy = await legacyQuery
        .order('id', { ascending: false })
        .limit(limit);

    if (legacy.error) {
        console.error('[DB ERROR] getRecentKnowledge:', advanced.error.message || legacy.error.message);
        return [];
    }
    return applyKnowledgeFilters(legacy.data || [], {
        ...options,
        limitAfterFilter: limit
    });
}

async function checkFactExists(chatId, factText) {
    const payload = normalizeKnowledgePayload(factText);

    const fact = buildKnowledgeFactText(payload);
    const fingerprint = buildKnowledgeFingerprint({ ...payload, fact });

    const advanced = await supabase
        .from('bot_knowledge')
        .select('id')
        .eq('chat_id', chatId)
        .eq('fingerprint', fingerprint)
        .limit(1)
        .maybeSingle();

    if (!advanced.error) return !!advanced.data;

    const legacy = await supabase
        .from('bot_knowledge')
        .select('id')
        .eq('chat_id', chatId)
        .eq('fact', fact)
        .limit(1)
        .maybeSingle();

    if (legacy.error) return false;
    return !!legacy.data;
}

async function deleteKnowledge(chatId, knowledgeId) {
    const { error } = await supabase
        .from('bot_knowledge')
        .delete()
        .eq('chat_id', chatId)
        .eq('id', knowledgeId);

    if (error) {
        console.error('[DB ERROR] deleteKnowledge:', error.message);
        return false;
    }
    return true;
}

async function upsertMemorySummary(chatId, periodKey, summary, sourceInc = 1) {
    if (!chatId || !periodKey || !summary) return false;

    const rpcResult = await supabase.rpc('touch_bot_memory_summary', {
        p_chat_id: chatId,
        p_period_key: periodKey,
        p_summary: summary,
        p_source_inc: sourceInc
    });

    if (!rpcResult.error) return true;

    const fallback = await supabase
        .from('bot_memory_summaries')
        .upsert([{
            chat_id: chatId,
            period_key: periodKey,
            summary,
            source_count: Math.max(sourceInc, 1),
            updated_at: new Date().toISOString()
        }], { onConflict: 'chat_id,period_key' });

    if (fallback.error) {
        if (!String(fallback.error.message || '').includes('bot_memory_summaries')) {
            console.error('[DB ERROR] upsertMemorySummary:', fallback.error.message);
        }
        return false;
    }
    return true;
}

async function getRecentMemorySummaries(chatId, limit = 3) {
    const { data, error } = await supabase
        .from('bot_memory_summaries')
        .select('*')
        .eq('chat_id', chatId)
        .order('updated_at', { ascending: false })
        .limit(limit);

    if (error) {
        if (!String(error.message || '').includes('bot_memory_summaries')) {
            console.error('[DB ERROR] getRecentMemorySummaries:', error.message);
        }
        return [];
    }
    return data || [];
}

async function weakenStaleKnowledge(chatId, options = {}) {
    const staleBeforeIso = options.staleBeforeIso;
    if (!chatId || !staleBeforeIso) return 0;

    const { data, error } = await supabase
        .from('bot_knowledge')
        .select('id, confidence, status, times_seen, last_seen_at, meta')
        .eq('chat_id', chatId)
        .in('status', ['candidate', 'confirmed'])
        .lt('last_seen_at', staleBeforeIso)
        .lt('times_seen', options.maxTimesSeen || 3)
        .lt('confidence', options.maxConfidence || 0.75)
        .limit(options.limit || 25);

    if (error) {
        if (!String(error.message || '').includes('last_seen_at')) {
            console.error('[DB ERROR] weakenStaleKnowledge:', error.message);
        }
        return 0;
    }

    if (!data || data.length === 0) return 0;

    let updated = 0;
    for (const row of data) {
        const memoryKind = getKnowledgeMemoryKind(row);
        const decayStep = memoryKind === 'pattern' ? 0.14 : memoryKind === 'risk' ? 0.1 : 0.08;
        const nextConfidence = Math.max(0.18, Number(row.confidence || 0.55) - decayStep);
        const nextStatus = nextConfidence < 0.45 ? 'candidate' : row.status;

        const { error: updateError } = await supabase
            .from('bot_knowledge')
            .update({
                confidence: nextConfidence,
                status: nextStatus
            })
            .eq('id', row.id);

        if (!updateError) updated++;
    }

    return updated;
}

async function insertReminder(chatId, userId, userName, text, triggerTime) {
    const { data, error } = await supabase.from('reminders').insert([{
        chat_id: chatId,
        user_id: userId,
        user_name: userName,
        text: text,
        trigger_time: triggerTime,
        is_sent: false
    }]).select().single();

    if (error) {
        console.error('[DB ERROR] insertReminder:', error.message);
        return null;
    }
    return data;
}

async function getDueReminders() {
    const now = new Date().toISOString();
    const { data, error } = await supabase
        .from('reminders')
        .select('*')
        .eq('is_sent', false)
        .lte('trigger_time', now);

    if (error) {
        console.error('[DB ERROR] getDueReminders:', error.message);
        return [];
    }
    return data;
}

async function markReminderAsSent(id) {
    const { error } = await supabase
        .from('reminders')
        .update({ is_sent: true })
        .eq('id', id);
    if (error) console.error('[DB ERROR] markReminderAsSent:', error.message);
}

// НОВАЯ ФУНКЦИЯ: Достает ВСЕ факты о человеке напрямую из векторной базы
async function getAllUserFacts(chatId, userName) {
    if (!userName) return [];
    const { data, error } = await supabase
        .from('bot_knowledge')
        .select('fact')
        .eq('chat_id', chatId)
        .ilike('fact', `%${userName}%`)
        .order('id', { ascending: false })
        .limit(15);

    if (error) {
        console.error('[DB ERROR] getAllUserFacts:', error.message);
        return [];
    }
    return data.map(d => d.fact);
}

module.exports = {

    getUser, updateUser, getBadWords, getNextLevelXp, claimDailyBonus,
    getChatSettings, updateChatSettings,
    setBirthday, setBio, getBirthdaysToday, setBioByUsernameOrName, setNotesByUsernameOrName, setFirstNameByUsernameOrName,
    getChatMemory, updateChatMemory, insertKnowledge, searchKnowledge, searchKnowledgeByText, getRecentKnowledge,
    checkFactExists, deleteKnowledge, upsertMemorySummary, getRecentMemorySummaries, weakenStaleKnowledge, transliterate,
    insertReminder, getDueReminders, markReminderAsSent,
    getChatStats, searchUserByName, warnUserById, getUpcomingBirthdays,
    findSingleUser, getAllUserFacts, // <-- Восстановленная функция здесь!
    messageAuthors, reactionCooldowns, commandCooldowns, userCache,
    supabase, ANONYMOUS_ADMIN_ID, pendingVerifications
};
