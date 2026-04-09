const { supabase } = require('./config');

const personaStateCache = {};
const PERSONA_STATE_CACHE_TTL = 300000;

function clamp01(value) {
    return Math.max(0, Math.min(1, Number(value || 0)));
}

function getCacheKey(chatId, userId) {
    return `${chatId}_${userId}`;
}

function normalizePersonaState(state = {}) {
    return {
        troll: clamp01(state.troll ?? 0.42),
        warmth: clamp01(state.warmth ?? 0.58),
        chaos: clamp01(state.chaos ?? 0.32),
        attachment: clamp01(state.attachment ?? 0.18),
        stage: String(state.stage || 'fresh'),
        exchanges: Math.max(0, Number(state.exchanges || 0)),
        lastUpdated: Number(state.lastUpdated || Date.now())
    };
}

async function getPersonaState(chatId, userId) {
    const cacheKey = getCacheKey(chatId, userId);
    if (personaStateCache[cacheKey] && Date.now() < personaStateCache[cacheKey].expires) {
        return personaStateCache[cacheKey].data;
    }

    const { data, error } = await supabase
        .from('bot_persona_state')
        .select('*')
        .eq('chat_id', chatId)
        .eq('user_id', userId)
        .maybeSingle();

    if (error) {
        console.warn('[DB WARN] getPersonaState:', error.message);
        return null;
    }

    const normalized = data ? normalizePersonaState({
        troll: data.troll,
        warmth: data.warmth,
        chaos: data.chaos,
        attachment: data.attachment,
        stage: data.stage,
        exchanges: data.exchanges,
        lastUpdated: data.updated_at ? new Date(data.updated_at).getTime() : Date.now()
    }) : null;

    personaStateCache[cacheKey] = { data: normalized, expires: Date.now() + PERSONA_STATE_CACHE_TTL };
    return normalized;
}

async function upsertPersonaState(chatId, userId, state = {}) {
    const normalized = normalizePersonaState(state);
    const payload = {
        chat_id: chatId,
        user_id: userId,
        troll: normalized.troll,
        warmth: normalized.warmth,
        chaos: normalized.chaos,
        attachment: normalized.attachment,
        stage: normalized.stage,
        exchanges: normalized.exchanges,
        updated_at: new Date().toISOString()
    };

    const { data, error } = await supabase
        .from('bot_persona_state')
        .upsert(payload, { onConflict: 'chat_id,user_id' })
        .select()
        .maybeSingle();

    if (error) {
        console.warn('[DB WARN] upsertPersonaState:', error.message);
        return null;
    }

    const cacheKey = getCacheKey(chatId, userId);
    personaStateCache[cacheKey] = { data: normalized, expires: Date.now() + PERSONA_STATE_CACHE_TTL };
    return data || payload;
}

module.exports = {
    getPersonaState,
    upsertPersonaState,
    normalizePersonaState
};
