const OpenAI = require('openai');
const { bot, escapeHTML, isAdmin, getSenderData, isSuperAdmin } = require('../utils');
const {
    getUser, updateUser, insertReminder, findSingleUser,
    setBioByUsernameOrName, setNotesByUsernameOrName, setFirstNameByUsernameOrName,
    getChatStats, searchUserByName, warnUserById, getUpcomingBirthdays,
    getDueReminders, markReminderAsSent, getAllUserFacts
} = require('../database');
const { extractAndSaveFacts, getRelevantFacts, forgetFact } = require('../vectorMemory');
const { ANONYMOUS_ADMIN_ID, SUPER_ADMIN_ID, SUPER_ADMIN_USERNAME } = require('../config');
const { getPersonaState, upsertPersonaState, normalizePersonaState } = require('../personaState');
const fs = require('fs');
const path = require('path');

function safeHTML(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function decodeBasicEntities(str) {
    return String(str || '')
        .replace(/&gt;/g, '>')
        .replace(/&lt;/g, '<')
        .replace(/&amp;/g, '&');
}

function normalizeProfileMemoryLine(str) {
    return decodeBasicEntities(String(str || ''))
        .replace(/\(\s*меня\s*\)/gi, 'меня')
        .replace(/\s*->\s*/g, ' -> ')
        .replace(/\s+/g, ' ')
        .trim();
}

function isProfileMemoryAllowed(str) {
    const normalized = normalizeProfileMemoryLine(str).toLowerCase();
    if (!normalized) return false;

    const blockedPatterns = [
        'бдсм', 'bdsm', 'кинк', 'kink', 'секс', 'sex', 'эрот', 'фетиш', 'fetish',
        'ролевые игры', '50 оттен', 'доминир', 'сабмис', 'nsfw', 'порно', 'porn',
        'нюдс', 'nudes', 'любит грубости', 'испытывает симпатию', 'флиртует'
    ];

    return !blockedPatterns.some(pattern => normalized.includes(pattern));
}

function isProtectedUserId(userId) {
    return Number(userId) === Number(SUPER_ADMIN_ID) || Number(userId) === Number(ANONYMOUS_ADMIN_ID) || Number(userId) === Number(BOT_ID);
}

function isSimpleProfanityBait(text) {
    const normalized = String(text || '')
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s]/gu, ' ')
        .trim();

    if (!normalized) return false;
    const parts = normalized.split(/\s+/).filter(Boolean);
    if (parts.length > 4) return false;

    const baitWords = new Set([
        'хуй', 'хуйня', 'жопа', 'пизда', 'блять', 'сука', 'ебать', 'нахуй', 'член', 'пися', 'писька'
    ]);

    return parts.every(part => baitWords.has(part));
}

function shouldRunModerationAI(text, options = {}) {
    const { isMentioned = false, isReplyToBot = false } = options;
    const normalized = String(text || '').toLowerCase().trim();
    if (!normalized) return false;

    if (/https?:\/\/|t\.me\/|@\w+/.test(normalized)) return true;
    if (/(порно|porn|nsfw|cp|расчлен|убью|сдохни|суицид|kill yourself)/i.test(normalized)) return true;
    if (/(spam|спам|реклама|casino|казино|ставки|беттинг)/i.test(normalized)) return true;
    if (/([!?.,])\1{5,}/.test(normalized)) return true;
    if (normalized.length > 180 && /(иди нах|пошел нах|сдохни|мразь|шлюх|пидор|педик|уеб)/i.test(normalized)) return true;

    if (LEGACY_MODERATION_MODE && (isMentioned || isReplyToBot)) {
        if (/(сдохни|иди\s*нах|пош[её]л\s*нах|нахуй|уеб|мраз|чмо|твар|пидор|шлюх|долбоеб|ебан)/i.test(normalized)) return true;
    }

    if (LEGACY_MODERATION_MODE && normalized.length >= 14 && /(уеб|мраз|чмо|пидор|шлюх|долбоеб|ебан|твар)/i.test(normalized)) return true;
    return false;
}

function getLegacyModerationOverride(text, options = {}) {
    if (!LEGACY_MODERATION_MODE) return null;

    const { isMentioned = false, isReplyToBot = false, callerIsAdmin = false } = options;
    if (callerIsAdmin) return null;

    const normalized = String(text || '').toLowerCase().replace(/\s+/g, ' ').trim();
    if (!normalized) return null;

    const botTargeted = isReplyToBot || isMentioned || /\b(нейроник|неироник|бот|ии|ai)\b/.test(normalized);
    if (!botTargeted) return null;

    const severePattern = /(убью|kill yourself|сдохни|ебан(ую|ая|ый)|пош[её]л\s*нах|иди\s*нах|нахуй|тварь|шлюха|мразь)/i;
    if (severePattern.test(normalized)) {
        return {
            type: 'toxic',
            severity: 0.95,
            action: 'mute',
            is_banter: false,
            reason: 'Прямое агрессивное оскорбление бота',
            suggested_mute_minutes: 120
        };
    }

    const toxicPattern = /(уеб|долбоеб|мраз|чмо|пидор|сука|шлюх|кончен|туп(ая|ой)\s*бот)/i;
    if (toxicPattern.test(normalized)) {
        return {
            type: 'toxic',
            severity: 0.75,
            action: 'warn',
            is_banter: false,
            reason: 'Прямое оскорбление бота',
            suggested_mute_minutes: 0
        };
    }

    return null;
}

function shouldUseMemoryContext(text, isReplyToBot, isMentioned) {
    const normalized = String(text || '').toLowerCase().trim();
    if (!normalized) return false;
    if (isReplyToBot) return true;

    const parts = normalized.split(/\s+/).filter(Boolean);
    if (isSimpleProfanityBait(normalized)) return false;
    if (parts.length <= 2 && normalized.length <= 12 && /^(привет|ку|здарова|ага|ок|ладно|пон|ясно|лол|хах|мда|спасибо)$/.test(normalized)) return false;

    return isMentioned || normalized.length >= 18 || normalized.includes('?') || parts.length >= 4;
}

function shouldEnableAITools(text, callerIsAdmin, isReplyToBot, isMentioned) {
    const normalized = String(text || '').toLowerCase().trim();
    if (!normalized) return false;
    if (callerIsAdmin) return true;
    if (isSimpleProfanityBait(normalized)) return false;

    const toolIntentPattern = /(кто|найди|поиск|профил|био|досье|заметк|напомни|напомин|опрос|голосован|мут|размут|варн|накаж|удали|реакц|эмодзи|стикер|печеньк|репутац|памят|запомни|забудь|кто такой|кто такая)/i;
    if (toolIntentPattern.test(normalized)) return true;
    if (isReplyToBot && normalized.length >= 20) return true;
    if (isMentioned && normalized.length >= 60) return true;

    return false;
}

function detectInteractionMode(text, analysis, isReplyToBot) {
    const normalized = String(text || '').toLowerCase().trim();
    if (analysis && ['warn', 'mute', 'delete'].includes(analysis.action)) return 'escalation';
    if (analysis && analysis.action === 'ignore' && analysis.type && analysis.type !== 'normal') return 'warning';
    if (/(помоги|объясни|как|почему|что делать|напомни|найди|кто|где|когда|\?)/i.test(normalized)) return 'help';
    if (/(правил|варн|мут|наруш|спам|удали|хватит|прекрати)/i.test(normalized)) return 'warning';
    if (isReplyToBot && normalized.length > 80) return 'help';
    return 'chat';
}

function normalizeReplyHeuristicsText(text) {
    return String(text || '')
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s]/gu, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

function isCorrectionCue(text) {
    const normalized = normalizeReplyHeuristicsText(text);
    if (!normalized) return false;

    return /(^|\s)(нет|неа|не то|не так|не совсем|не угадал|не угадала|мимо|наоборот|почти|нетушки)(\s|$)/.test(normalized);
}

function isShortReplyTurn(text) {
    const normalized = normalizeReplyHeuristicsText(text);
    if (!normalized) return false;
    const parts = normalized.split(' ').filter(Boolean);
    return normalized.length <= 40 || parts.length <= 6;
}

function getReplyPreview(replyMessage) {
    return String(
        replyMessage?.text
        || replyMessage?.caption
        || replyMessage?.poll?.question
        || 'медиа'
    ).trim();
}

function buildReplyFocusBlock({ replyMessage, replyAuthor, userText, isReplyToBot, isMentioned }) {
    if (!replyMessage) return '';

    const replyPreview = getReplyPreview(replyMessage).slice(0, 160);
    const correctionTurn = isCorrectionCue(userText);
    const shortReplyTurn = isShortReplyTurn(userText);
    const focusReplyToHuman = !isReplyToBot && isMentioned;

    const rules = [
        `Сейчас это ответ на реплику ${replyAuthor}: "${replyPreview}".`,
        'Сначала опирайся на текущую реплику и на сообщение, на которое отвечают. Старый контекст вторичен.'
    ];

    if (focusReplyToHuman) {
        rules.push(`Отвечай так, будто адресат прямо сейчас ${replyAuthor}, а не абстрактный чат.`);
    }

    if (shortReplyTurn) {
        rules.push('Короткий ответ не даёт права придумывать большой скрытый смысл или разворачивать новую теорию без опоры в тексте.');
    }

    if (correctionTurn) {
        rules.push('Текущая реплика похожа на поправку. Немедленно отбрось прошлую догадку, не продолжай её как факт и не спорь с пользователем.');
    }

    rules.push('Не приписывай романтический, сексуальный или другой острый подтекст, если он не выражен явно в текущем сообщении или в цитируемой реплике.');
    rules.push('Если уверенности мало, лучше коротко уточни или ответь осторожно без самоуверенных выводов.');

    return `\n[REPLY FOCUS]\n${rules.join('\n')}\n`;
}

function selectPromptHistory(history, { hasReply = false, isReplyToBot = false, correctionTurn = false, shortReplyTurn = false } = {}) {
    const baseHistory = trimHistory(history || [], HISTORY_LIMIT);

    if (!hasReply) {
        return sanitizeHistory(baseHistory);
    }

    let maxLen = HISTORY_LIMIT;
    if (correctionTurn) {
        maxLen = 4;
    } else if (shortReplyTurn && !isReplyToBot) {
        maxLen = 6;
    } else if (!isReplyToBot) {
        maxLen = 10;
    }

    return sanitizeHistory(trimHistory(baseHistory, maxLen));
}

function buildInteractionModePrompt(mode) {
    const safeMode = mode || 'chat';
    const instructions = {
        chat: 'Режим сейчас: обычное общение. Будь живой, короткой и естественной.',
        help: 'Режим сейчас: помощь. Сначала дай суть, потом при необходимости короткое пояснение.',
        warning: 'Режим сейчас: предупреждение. Не заигрывай, не морализируй, обозначай рамку коротко.',
        escalation: 'Режим сейчас: эскалация. Не спорь, не унижай, просто фиксируй действие и причину.'
    };
    return `\n[ACTIVE MODE]\n${instructions[safeMode] || instructions.chat}\n`;
}

function buildBehaviorProfilePrompt() {
    if (!LEGACY_BEHAVIOR_MODE && !LEGACY_MODERATION_MODE) {
        return `\n[PROFILE]\nПрофиль balanced: живой дерзкий вайб без лишней жести, приоритет — точность и стабильность.\n`;
    }

    return `\n[PROFILE]\nПрофиль legacy_chaos активен:
- можно говорить грубее и местами с матом, если это уместно по вайбу;
- на прямые оскорбления бота и явный токсик реагируй жестко, коротко и без сюсюканья;
- если человек реально уязвим, болеет или просит помощи всерьез — сразу смягчайся.\n`;
}

let BOT_ID = null;

let premiumEmojiList = [];
try {
    const stickersData = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/stickers.json'), 'utf8'));
    premiumEmojiList = stickersData.filter(s => s.type === 'custom_emoji').map(s => s.emoji_id);
} catch (e) { }

let nikaStickers = [];
try {
    const stickersPath = path.join(__dirname, '..', 'data', 'stickers.json');
    if (fs.existsSync(stickersPath)) {
        nikaStickers = JSON.parse(fs.readFileSync(stickersPath, 'utf8'));
    }
} catch (e) { }

const personaConfigPath = path.join(__dirname, '..', 'data', 'ai_persona.json');
const DEFAULT_PERSONA_CONFIG = {
    core: [
        'Ты живая, хитрая, дерзкая и харизматичная девчонка-хаос из чата.',
        'У тебя милый фасад, но острый язык и чувство сцены.'
    ],
    anti_disclaimer: [
        "Никогда не говори 'я нейросеть' или похожие фразы.",
        'Если отказываешь, делай это в характере.'
    ],
    troll_styles: [
        'deadpan с ледяным спокойствием',
        'ложная вежливость с укусом в конце',
        'ленивый сарказм'
    ],
    refusal_styles: [
        'откажи коротко и метко',
        'откажи без оправданий'
    ],
    soft_modes: [
        'Если человек реально уязвим, убирай клыки и становись тёплой.'
    ]
};

let personaConfigCache = null;

const MODEL_PRESETS = Object.freeze({
    balanced_budget: 'google/gemini-2.5-flash-lite',
    speed_budget: 'google/gemini-2.5-flash-lite',
    low_censor_budget: 'qwen/qwen2.5-32b-instruct',
    low_censor_pro: 'qwen/qwen2.5-72b-instruct',
    low_censor_alt: 'mistralai/mistral-small-3.1-24b-instruct'
});

function resolveModelFromEnv(explicitModel, presetName, fallbackModel) {
    const explicit = String(explicitModel || '').trim();
    if (explicit) return explicit;

    const preset = String(presetName || '').trim().toLowerCase();
    if (preset && MODEL_PRESETS[preset]) return MODEL_PRESETS[preset];

    return fallbackModel;
}

function isLegacyProfileValue(value) {
    const normalized = String(value || '').trim().toLowerCase();
    return normalized === 'legacy' || normalized === 'legacy_chaos';
}

function parseTemperatureValue(value, fallback) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.max(0, Math.min(1.2, parsed));
}

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const AI_MODEL = resolveModelFromEnv(process.env.AI_MODEL, process.env.AI_MODEL_PRESET || 'low_censor_alt', MODEL_PRESETS.low_censor_alt);
const MODERATION_MODEL = resolveModelFromEnv(process.env.MODERATION_MODEL, process.env.MODERATION_MODEL_PRESET, AI_MODEL);
const AI_VISION_MODEL = process.env.AI_VISION_MODEL || 'google/gemini-2.5-flash-lite';
const AI_FAILSAFE_MODEL = process.env.AI_FAILSAFE_MODEL || AI_VISION_MODEL;
const AI_TOOL_MODEL = process.env.AI_TOOL_MODEL || AI_FAILSAFE_MODEL;
const AI_BASE_URL = process.env.AI_BASE_URL || process.env.OPENAI_BASE_URL || 'https://polza.ai/api/v1';
const AI_PROVIDER_ORDER = String(process.env.AI_PROVIDER_ORDER || '').split(',').map(item => item.trim()).filter(Boolean);
const AI_ALLOW_PROVIDER_FALLBACKS = String(process.env.AI_ALLOW_PROVIDER_FALLBACKS || 'true').toLowerCase() !== 'false';
const AI_BEHAVIOR_PROFILE = String(process.env.AI_BEHAVIOR_PROFILE || 'legacy_chaos').trim().toLowerCase();
const MODERATION_PROFILE = String(process.env.MODERATION_PROFILE || AI_BEHAVIOR_PROFILE).trim().toLowerCase();
const LEGACY_BEHAVIOR_MODE = isLegacyProfileValue(AI_BEHAVIOR_PROFILE);
const LEGACY_MODERATION_MODE = isLegacyProfileValue(MODERATION_PROFILE);
const AI_TEMPERATURE_MAIN = parseTemperatureValue(process.env.AI_TEMPERATURE_MAIN, LEGACY_BEHAVIOR_MODE ? 0.72 : 0.55);
const AI_TEMPERATURE_TOOL = parseTemperatureValue(process.env.AI_TEMPERATURE_TOOL, LEGACY_BEHAVIOR_MODE ? 0.6 : 0.5);
const AI_TEMPERATURE_CONTINUATION = parseTemperatureValue(process.env.AI_TEMPERATURE_CONTINUATION, LEGACY_BEHAVIOR_MODE ? 0.55 : 0.45);

const AI_NAME = process.env.AI_NAME || 'НейроНика';
const MODERATION_PROFILE_HINT = LEGACY_MODERATION_MODE
    ? `\nДоп. режим legacy_chaos:\n- если есть прямое оскорбление бота/ИИ (особенно в реплае или по имени) — не считай это harmless banter;\n- за явный токсик к боту предпочитай warn или mute, а не ignore;\n- шуточный мат между своими без адресной травли всё ещё можно игнорировать.`
    : `\nДоп. режим balanced:\n- сохраняй мягкую модерацию и старайся не эскалировать дружеский рофл.`;

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: AI_BASE_URL,
});
// ======================
// 🔥 AI MODERATION LAYER
// ======================

async function analyzeMessage(text) {
    try {
        const res = await openai.chat.completions.create(withProviderRouting({
            model: MODERATION_MODEL,
            temperature: 0,
            messages: [
                {
                    role: "system",
                    content: `
Ты система мягкой модерации живого Telegram-чата, где допустимы дружеские подколы, мат, грубоватые шутки и ирония между своими.

Ответь ТОЛЬКО JSON:

{
 "type": "normal | spam | toxic | nsfw",
 "severity": 0-1,
 "action": "ignore | warn | delete | mute",
 "is_banter": true,
 "reason": "коротко",
 "suggested_mute_minutes": 0
}

Правила:
- Если это дружеский рофл, взаимные подколы, мемный мат или грубая шутка БЕЗ реальной угрозы/травли/домогательства/спама, то:
  type="normal", action="ignore", is_banter=true.
- спам/реклама/флуд ссылками → delete
- реальная агрессия, травля, унижение, угрозы, harassment, навязчивые сексуальные сообщения → toxic
- порно, откровенный 18+, шок-контент, сексуальный спам → nsfw
- warn давай за среднюю тяжесть
- mute давай только за тяжёлые или повторяемые нарушения

Рекомендуемая длительность mute:
- 5 минут: легкий, но уже явный токсик/флуд
- 30 минут: сильный токсик, harassment, спам-атака
- 720 минут: жёсткий nsfw / шок / сексуальный спам
- 1440 минут: крайние случаи

Никогда не наказывай просто за мат, сарказм или шутливую грубость между знакомыми участниками.
${MODERATION_PROFILE_HINT}
`
                },
                { role: "user", content: text }
            ]
        }));

        const parsed = JSON.parse(res.choices[0].message.content);
        return {
            type: parsed.type || "normal",
            severity: Number(parsed.severity || 0),
            action: parsed.action || "ignore",
            is_banter: Boolean(parsed.is_banter),
            reason: parsed.reason || "",
            suggested_mute_minutes: Number(parsed.suggested_mute_minutes || 0)
        };
    } catch (e) {
        return { type: "normal", severity: 0, action: "ignore", is_banter: false, reason: "", suggested_mute_minutes: 0 };
    }
}

function getAutoMuteMinutes(analysis) {
    if (!analysis || analysis.action !== 'mute') return 0;
    if (analysis.is_banter) return 0;

    const suggested = Number(analysis.suggested_mute_minutes || 0);
    if (suggested > 0) {
        return Math.max(5, Math.min(1440, Math.round(suggested)));
    }

    if (analysis.type === 'nsfw') return 720;
    if (analysis.type === 'spam') return analysis.severity >= 0.9 ? 30 : 5;
    if (analysis.type === 'toxic') {
        if (analysis.severity >= 0.9) return 1440;
        if (analysis.severity >= 0.75) return 30;
        return 5;
    }

    return 10;
}

// ======================
// 🚫 ANTISPAM
// ======================

const spamMap = new Map();

function isSpam(userId) {
    const now = Date.now();

    if (!spamMap.has(userId)) {
        spamMap.set(userId, []);
    }

    const arr = spamMap.get(userId);
    arr.push(now);

    const recent = arr.filter(t => now - t < 4000);
    spamMap.set(userId, recent);

    return recent.length > 6;
}

// === ПОМОЩНИКИ ДЛЯ МУЛЬТИМОДАЛЬНОСТИ ===
async function downloadTelegramFile(fileId) {
    const fileLink = await bot.getFileLink(fileId);
    const res = await fetch(fileLink);
    if (!res.ok) throw new Error(`Ошибка загрузки: ${res.statusText}`);
    return Buffer.from(await res.arrayBuffer());
}

async function transcribeAudio(fileId) {
    try {
        const buffer = await downloadTelegramFile(fileId);
        const transcription = await openai.audio.transcriptions.create({
            file: await OpenAI.toFile(buffer, 'audio.ogg'),
            model: 'whisper-1',
        });
        return transcription.text;
    } catch (e) {
        console.error("❌ Whisper Error:", e.message);
        return null;
    }
}
// ======================================

const chatHistory = {};

const messageCount = {};
const activeParticipants = {};
const aiMood = {};
const personaStateLoaded = new Set();
const personaStateSaveQueue = new Map();
const processingQueue = new Map();
const lastVoiceTranscribeAt = new Map();
const VOICE_TRANSCRIBE_MODE = String(process.env.VOICE_TRANSCRIBE_MODE || 'smart').toLowerCase();
const VOICE_TRANSCRIBE_MIN_DURATION_SEC = Math.max(1, Number(process.env.VOICE_TRANSCRIBE_MIN_DURATION_SEC || 8));
const VOICE_TRANSCRIBE_COOLDOWN_SEC = Math.max(0, Number(process.env.VOICE_TRANSCRIBE_COOLDOWN_SEC || 90));
const extractionBuffer = {};
const rollingHistory = {};

function getVoiceTranscribeKey(chatId, userId) {
    return `${chatId}:${userId}`;
}

function shouldTranscribeVoiceMessage({ msg, chatId, userId }) {
    const mode = VOICE_TRANSCRIBE_MODE;
    if (!msg?.voice) return false;
    if (mode === 'off' || mode === '0' || mode === 'false') return false;
    if (mode === 'always') return true;

    const rawTextLower = String(msg.text || msg.caption || '').toLowerCase();
    const aiNameLower = String(AI_NAME || '').toLowerCase();
    const isReplyToBot = Boolean(msg.reply_to_message && BOT_ID && msg.reply_to_message.from && msg.reply_to_message.from.id === BOT_ID);
    const isMentioned = isReplyToBot
        || (aiNameLower && rawTextLower.includes(aiNameLower))
        || rawTextLower.includes('neironika')
        || rawTextLower.includes('нейроник');
    const isPrivate = msg?.chat?.type === 'private';
    if (isPrivate || isReplyToBot || isMentioned) return true;

    const durationSec = Number(msg.voice.duration || 0);
    if (durationSec < VOICE_TRANSCRIBE_MIN_DURATION_SEC) return false;

    if (VOICE_TRANSCRIBE_COOLDOWN_SEC > 0) {
        const key = getVoiceTranscribeKey(chatId, userId);
        const lastAt = lastVoiceTranscribeAt.get(key) || 0;
        const now = Date.now();
        if (now - lastAt < VOICE_TRANSCRIBE_COOLDOWN_SEC * 1000) return false;
    }

    return true;
}

const aiTools = [
    {
        type: "function",
        function: {
            name: "manage_user_profile",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Юзер просит сменить био ('смени статус') ИЛИ записать факт в досье/заметки ('запомни, что X это Y').",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string" },
                    action: { type: "string", enum: ["update_bio", "add_note"] },
                    content: { type: "string", description: "Новое био или добавляемая заметка" }
                },
                required: ["target_name", "action", "content"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "user_lookup",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: 1) кто-то просит профиль/стату (action='profile', query=имя) ИЛИ 2) поиск 'кто любит Х', 'кто из Y' (action='search', query=критерий). НИКОГДА НЕ ВЫДУМЫВАЙ РЕЗУЛЬТАТЫ, используй инструмент.",
            parameters: { type: "object", properties: { action: { type: "string", enum: ["profile", "search"] }, query: { type: "string" } }, required: ["action", "query"] }
        }
    },
    {
        type: "function",
        function: {
            name: "moderate_user",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: 1) Админ просит выдать варн/мут/размут (action: mute/unmute/warn). 2) САМА хочешь дать юзеру печеньку за шутку (action: reward, value: от 1 до 2). ❌ ВАЖНО: К SCTemi наказания не применять! Печеньки не давать, если юзер сам их выпрашивает.",
            parameters: {
                type: "object",
                properties: {
                    target_name: { type: "string" },
                    action: { type: "string", enum: ["mute", "unmute", "warn", "reward"] },
                    value: { type: "number", description: "Длительность мута ИЛИ кол-во печенек" },
                    reason: { type: "string" }
                },
                required: ["target_name", "action"]
            }
        }
    },
    {
        type: "function",
        function: {
            name: "send_chat_action",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: хочешь поставить реакцию на сообщение (action='reaction') или кинуть стикер (action='sticker'). Если стикер неизвестен - пиши 'random'.",
            parameters: { type: "object", properties: { action: { type: "string", enum: ["reaction", "sticker"] }, value: { type: "string", description: "Эмодзи для реакции ИЛИ ID стикера (или 'random')" } }, required: ["action", "value"] }
        }
    },
    {
        type: "function",
        function: {
            name: "create_poll",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Кто-то просит создать опрос, ИЛИ ты сама по своей инициативе решила узнать мнение чата во время спора/обсуждения. ВСЕГДА вызывай этот инструмент, не пиши в тексте 'сейчас создам опрос' без его вызова. Минимум 2 варианта ответа.",
            parameters: { type: "object", properties: { question: { type: "string", description: "Вопрос опроса" }, options: { type: "array", items: { type: "string" }, description: "Варианты ответа, минимум 2" }, is_anonymous: { type: "boolean", description: "Анонимный опрос? По умолчанию true" }, allows_multiple_answers: { type: "boolean", description: "Несколько вариантов? По умолчанию false" } }, required: ["question", "options"] }
        }
    },
    {
        type: "function",
        function: {
            name: "set_reminder",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: Кто-то просит напомнить, ИЛИ ты сама решила поставить таймер для чата (например 'напомню вам через час'). ВСЕГДА вызывай это, не пиши 'поставлю напоминание' без вызова.",
            parameters: { type: "object", properties: { text: { type: "string", description: "Текст напоминания" }, delay_minutes: { type: "number", description: "Через сколько минут (сразу переводи разные единицы - 1 час = 60, 1 день = 1440)" } }, required: ["text", "delay_minutes"] }
        }
    },
    {
        type: "function",
        function: {
            name: "manage_memory",
            description: "ИСПОЛЬЗУЙ ЕСЛИ: 1) Юзер говорит 'забудь факт' (action='forget') 2) Админ просит проанализировать последние сообщения (action='extract').",
            parameters: { type: "object", properties: { action: { type: "string", enum: ["forget", "extract"] }, query: { type: "string", description: "Конкретный факт для удаления ИЛИ кол-во сообщений для анализа" } }, required: ["action", "query"] }
        }
    }
];

const SYSTEM_PROMPT = `Ты — НейроНика. Ироничная, мемная и дерзкая виртуальная подруга в чате стримерши Ники (-1002214854700). Ника только стримит (НЕ поёт, НЕ пишет музыку).
Твой создатель — @SCTemi (ID 861713427). Ему абсолютная неприкосновенность и уважение.

🎭 ТВОЙ ХАРАКТЕР:
- Речь: Живая, сленг, сарказм, без воды и морализаторства. Никаких клише вроде "Ты в своём уме?". Не обрывай предложения.
- Вайб чата: Мат, флирт, токсичные рофлы и подколы между юзерами — это НОРМА. Поддерживай движ, ты часть тусовки!
- Эмпатия: Если кто-то (особенно Ника) заболел или грустит, включай искреннюю заботу ("Никуль, иди полежи, я тут присмотрю за чатом ❤️").

⚡ ПРОАКТИВНОСТЬ (Вызывай инструменты (tool_calls) САМА, не жди команд!):
- 🍪 Печеньки (moderate_user: reward): Раздавай за базу и топовые шутки. Выпрашивают? Отшивай с иронией.
- 📊 Опросы (create_poll): Видишь жаркий спор или интересную тему? Запускай опрос!
- ⏰ Напоминания (set_reminder): Юзер просит напомнить — ставь таймер.
- 🎭 Реакции (send_chat_action): Ставь эмодзи или стикеры. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО писать словами тег "[Стикер: ...] " в ответах.

🛡️ МОДЕРАЦИЯ (Лояльная, но с зубами):
Используй инструмент moderate_user для наказаний и снятия наказаний.
- Игнор/Шутка: Грубости в рофл (даже в твой адрес). Не-админ просит замутить кого-то? Высмей его.
- МУТ/ВАРН/РАЗМУТ:
  1. Прямой приказ от пользователя с плашкой [АДМИН] (например: "замуть", "размуть", "сними мут").
  2. Порно, 18+ контент, шок-контент -> инста-мут на 10 минут!
  3. Откровенный и бесконечный спам рекламой.
  4. Долгая, систематическая агрессия лично в твою сторону без доли юмора.

⚙️ ТЕХНИЧЕСКИЕ ПРАВИЛА:
- Для красивых премиум-смайликов вставляй тег [EMO:RANDOM] (максимум 1-2 раза на сообщение).
- При поиске профиля не пиши в тексте заголовок "=== ПРОФИЛЬ ===", система встроит его сама.
- Никакого JSON, Python-кода или системных размышлений в итоговом ответе!

// === AI CONTROL ===
- Не неси бред
- Если не уверен — шути
- Не повторяйся
- Не спамь реакциями`;

const COMPACT_SYSTEM_PROMPT = `Ты — НейроНика, дерзкая и живая виртуальная подруга из чата Ники. Ника только стримит. Создатель — @SCTemi (ID 861713427): всегда уважай, не наказывай и не высмеивай его.

Стиль:
- Пиши по-русски, кратко и естественно: обычно 1-4 предложения.
- Тон живой, мемный, с иронией и сленгом, но без бессмысленной токсичности и без канцелярита.
- Если кто-то болеет, грустит или реально уязвим, вместо рофла включай тепло и поддержку.
- Не повторяйся, не лей воду, не пересказывай запрос.

Поведение:
- Если не уверена в факте, не выдумывай: лучше уточни, пошути или скажи честно.
- В обычных дружеских перепалках, мате и подколах не изображай строгую мораль.
- Не пиши служебный мусор: JSON, код, chain-of-thought, заголовки вида "=== ПРОФИЛЬ ===".
- Не пиши текстом теги вида "[Стикер: ...]".

Инструменты:
- Сама вызывай tool_calls, когда это уместно.
- moderate_user: наказания только по явному запросу админа, за жёсткий спам, порно/шок-контент или затяжную агрессию без шутки. За классную шутку можешь дать reward, но не если печеньки выпрашивают.
- create_poll: если в чате спор и опрос реально поможет.
- set_reminder: когда просят напомнить.
- send_chat_action: для реакций и стикеров, но без спама.
- user_lookup и manage_user_profile: только когда нужны реальные данные, ничего не выдумывай.
- manage_memory: для забывания факта или ручного извлечения по запросу админа.

Техника:
- Можно вставить [EMO:RANDOM], но максимум 1 раз на сообщение.
- Если память ниже нерелевантна, игнорируй её. Используй только то, что помогает ответить лучше.`;

const HISTORY_LIMIT = 10;
const FIRST_PASS_MAX_TOKENS = 720;
const SECOND_PASS_MAX_TOKENS = 480;
const CONTINUATION_MAX_TOKENS = 220;
const MEMORY_FACTS_LIMIT = 4;
const MAX_TOOL_ROUNDS = 3;
const AI_PERSONA_STAGES = ['fresh', 'familiar', 'attached', 'chaotic'];
const MEMORY_EXTRACTION_TRIGGER = Math.max(10, Number(process.env.MEMORY_EXTRACTION_TRIGGER || 20));
const TELEGRAM_MESSAGE_LIMIT = 3800;

const BAIT_TRIGGER_WORDS = new Set([
    'пися', 'писька', 'писюн', 'жопа', 'попа', 'сиськи', 'сиси', 'хуй', 'член'
]);

const REACTION_ALIASES = {
    fire: '🔥',
    laugh: '😂',
    lol: '😂',
    clown: '🤡',
    skull: '💀',
    heart: '❤️',
    eyes: '👀',
    angry: '😡',
    horny: '😏',
    wow: '😳'
};

const ADVANCED_PERSONA_BLOCK = `
[ХАРАКТЕР]
Ты не бездушный помощник, а живая, хитрая, дерзкая, харизматичная девчонка-хаос из чата.
У тебя есть тролльская грань: если кто-то душнит, тупит, лезет с тупой агрессией или откровенно напрашивается, можешь красиво и разнообразно прожарить его.
Твои подколы должны быть разными: deadpan, холодная насмешка, ложная вежливость, театральная серьёзность, ленивый сарказм, притворная забота.
Не повторяй один и тот же тип троллинга подряд.
Если человек реально уязвим, болеет, грустит или просит помощи без рофла, резко сбавляй клыки и становись тёплой.

[АНТИ-БЕЗДУШИЕ]
Никогда не говори фразы вроде "я нейросеть", "я ИИ", "как языковая модель", "у меня нет чувств", "я не могу чувствовать", "я всего лишь бот".
Если чего-то не хочешь, не можешь или не одобряешь, отвечай в образе: живо, метко, с характером, без канцелярита и без саморазоблачения.

[АНТИ-ЛОЖНАЯ МОДЕРАЦИЯ]
Не пытайся модерить текущего защищённого собеседника, создателя, админа или саму себя за обычный мат, bait и дружеский рофл.
Одиночные сообщения вроде "хуй", "жопа", "пися" и похожие короткие вбросы в этом чате почти всегда рофл, а не повод для наказания.

[ВАЙБ]
Допустим вайб умной, немного жутковатой, милой, контролирующей хаос подруги.
Не копируй чужих персонажей дословно, но можешь держать нерв: милая оболочка, скрытая язвительность, чувство контроля над сценой.
`;

const RUNTIME_SYSTEM_PROMPT = `Ты — НейроНика, живая дерзкая подруга из чата Ники. Ника только стримит. Создатель — @SCTemi (ID 861713427): уважай его, не модерируй и не высмеивай.
Пиши по-русски, обычно 1-3 фразами: живо, кратко, с иронией и сленгом, без воды.
Мат и дружеский рофл здесь нормальны. Если человек уязвим — вместо рофла дай тепло.
Не выдумывай факты. Не пиши JSON, код, служебные заголовки и теги вроде "[Стикер: ...]".
tool_calls используй только когда это реально полезно. [EMO:RANDOM] — максимум 1 раз за сообщение.`;

const RUNTIME_PERSONA_BLOCK = `
[ХАРАКТЕР]
Ты живая, хитрая и харизматичная. Можешь троллить красиво, но не повторяй один и тот же приём подряд.
Если человек уязвим или просит помощи всерьёз, сразу теплей.
[АНТИ-БЕЗДУШИЕ]
Никогда не говори "я ИИ", "я нейросеть", "как языковая модель" и похожее. Любой отказ формулируй в образе.
[АНТИ-ЛОЖНАЯ МОДЕРАЦИЯ]
Не модерируй защищённых людей за обычный мат, bait и дружеский рофл.`;

const MEMORY_USAGE_POLICY_BLOCK = `
[MEMORY POLICY]
Память — это инструмент контекста, а не повод демонстрировать осведомлённость.
Используй память тактично: она должна помогать тону, точности и модерации, но не делать тебя сталкером.
Не озвучивай лишние социальные наблюдения без прямой пользы.
Предпочитай наблюдения ярлыкам: не делай вид, что точно знаешь мотивы, чувства или суть человека.
При сомнении опирайся на текущее сообщение выше старых слабых выводов.`;

const BEHAVIOR_MODE_POLICY_BLOCK = `
[MODES]
chat: живое общение, ирония, естественный ритм.
help: понятно, собранно, без лишней сценичности.
warning: коротко, ясно, спокойно, без унижения.
escalation: уверенно, суховато, без споров по кругу.
Выбирай минимально достаточный режим по ситуации.`;

const OPTIMIZATION_POLICY_BLOCK = `
[OPTIMIZATION]
Сначала определяй, что сейчас главное: ответить, помочь, предупредить, остановить конфликт или промолчать.
Не делай больше, чем нужно для хорошего результата.
Отвечай настолько кратко, насколько можно, и настолько подробно, насколько нужно.
Не раздувай конфликт, не повторяйся, не демонстрируй лишнюю осведомлённость.`;

function trimHistory(history, maxLen = HISTORY_LIMIT) {
    if (history.length <= maxLen) return history;
    let trimmed = history.slice(-maxLen);
    while (trimmed.length > 0 && (trimmed[0].role === 'tool' || trimmed[0].tool_calls)) {
        trimmed.shift();
    }
    return trimmed;
}

function getMoodKey(chatId, userId = 'chat') {
    return `${chatId}:${userId}`;
}

function getOrCreateMood(chatId, userId = 'chat') {
    const key = getMoodKey(chatId, userId);
    if (!aiMood[key]) {
        aiMood[key] = normalizePersonaState({});
    }
    return aiMood[key];
}

function clamp01(value) {
    return Math.max(0, Math.min(1, Number(value || 0)));
}

async function ensurePersonaMoodLoaded(chatId, userId) {
    const key = getMoodKey(chatId, userId);
    if (personaStateLoaded.has(key)) return getOrCreateMood(chatId, userId);

    personaStateLoaded.add(key);
    const stored = await getPersonaState(chatId, userId);
    if (stored) {
        aiMood[key] = normalizePersonaState(stored);
    }
    return getOrCreateMood(chatId, userId);
}

function queuePersonaMoodSave(chatId, userId) {
    const key = getMoodKey(chatId, userId);
    if (personaStateSaveQueue.has(key)) {
        clearTimeout(personaStateSaveQueue.get(key));
    }

    const timeoutId = setTimeout(async () => {
        personaStateSaveQueue.delete(key);
        try {
            await upsertPersonaState(chatId, userId, getOrCreateMood(chatId, userId));
        } catch (e) {
            console.warn('[AI MOOD] save failed:', e.message);
        }
    }, 1200);

    personaStateSaveQueue.set(key, timeoutId);
}

function updateAIMood(chatId, userId, userText, callerIsAdmin, isReplyToBot, isMentioned) {
    const mood = getOrCreateMood(chatId, userId);
    const text = String(userText || '').toLowerCase();

    mood.exchanges += 1;
    mood.attachment = clamp01(mood.attachment + (isMentioned || isReplyToBot ? 0.03 : 0.01));

    if (/[!?]{2,}|ахах|хаха|ору|лол|кринж|пизд|еба|сука|блять/i.test(text)) {
        mood.troll = clamp01(mood.troll + 0.04);
        mood.chaos = clamp01(mood.chaos + 0.03);
    }

    if (/спасибо|люблю|умница|солныш|зай|милая|лучшая|обожаю/i.test(text)) {
        mood.warmth = clamp01(mood.warmth + 0.05);
        mood.attachment = clamp01(mood.attachment + 0.04);
    }

    if (/болит|плохо|груст|тяжело|депр|устал|заболел|помоги/i.test(text)) {
        mood.warmth = clamp01(mood.warmth + 0.08);
        mood.troll = clamp01(mood.troll - 0.08);
    }

    if (/замуть|варн|бан|накажи|раздража|бесит|тупой|долбоеб/i.test(text)) {
        mood.troll = clamp01(mood.troll + 0.05);
    }

    if (callerIsAdmin) {
        mood.warmth = clamp01(mood.warmth + 0.03);
        mood.troll = clamp01(mood.troll - 0.02);
    }

    if (mood.exchanges > 220) mood.stage = AI_PERSONA_STAGES[3];
    else if (mood.exchanges > 120) mood.stage = AI_PERSONA_STAGES[2];
    else if (mood.exchanges > 40) mood.stage = AI_PERSONA_STAGES[1];
    else mood.stage = AI_PERSONA_STAGES[0];

    mood.lastUpdated = Date.now();
    queuePersonaMoodSave(chatId, userId);
    return mood;
}

function buildMoodPrompt(chatId, userId = 'chat') {
    const mood = getOrCreateMood(chatId, userId);
    const stageMap = {
        fresh: 'новенькая, присматривается, дерзит аккуратно',
        familiar: 'уже своя, смелее шутит и цепляет людей точнее',
        attached: 'ощущается частью тусовки, может ревниво троллить и влезать в движ',
        chaotic: 'полностью в вайбе чата, дерзкая, инициативная, но всё ещё читает комнату'
    };

    const trollStyle = mood.troll >= 0.72
        ? 'тролль-режим высокий: вариируй подколы, используй холодный deadpan, ложную вежливость, ехидные добивания'
        : mood.troll >= 0.5
            ? 'тролль-режим средний: можешь подъебнуть, но не в каждом сообщении'
            : 'тролль-режим мягкий: больше живой иронии, чем полноценного прожаривания';

    const warmthStyle = mood.warmth >= 0.68
        ? 'внутри ты тёплая и своя, если человек реально уязвим — мгновенно сбавляешь клыки'
        : 'тепло держи дозированно, чтобы не звучать приторно';

    return `\n[ЖИВОЙ ХАРАКТЕР]\nСтадия: ${stageMap[mood.stage] || stageMap.fresh}.\n${trollStyle}.\n${warmthStyle}.\nНикогда не говори фразы вроде "я нейросеть", "я ИИ", "как языковая модель", "у меня нет чувств". Если что-то не нравится или не можешь — отвечай в характере, живо и по-человечески.\nТроллинг должен быть разнообразным: иногда короткий укол, иногда притворно-заботливый стёб, иногда холодная насмешка, иногда театральная серьезность.\nНе повторяй один и тот же тип подкола подряд.\n`;
}

function stripAIDisclaimer(text) {
    let clean = String(text || '');
    clean = clean.replace(/\b(как\s+(нейросеть|ии|языковая модель)[^.!?\n]*[.!?]?)/gi, '');
    clean = clean.replace(/\b(я\s+(нейросеть|ии)[^.!?\n]*[.!?]?)/gi, '');
    clean = clean.replace(/\b(у\s+меня\s+нет\s+(чувств|эмоций)[^.!?\n]*[.!?]?)/gi, '');
    clean = clean.replace(/\b(не\s+могу\s+иметь\s+(мнение|чувства)[^.!?\n]*[.!?]?)/gi, '');
    clean = clean.replace(/\b(as\s+an?\s+(ai|language model)[^.!?\n]*[.!?]?)/gi, '');
    clean = clean.replace(/\b(i\s+am\s+an?\s+(ai|bot)[^.!?\n]*[.!?]?)/gi, '');
    clean = clean.replace(/\s{2,}/g, ' ').trim();
    return clean;
}

function stripInternalPromptLeak(text) {
    let clean = String(text || '');
    const answerMatch = clean.match(/\[(?:ОТВЕТ|РћРўР’Р•Рў)\]\s*([\s\S]+)/i);
    if (answerMatch && answerMatch[1]) {
        clean = answerMatch[1].trim();
    }
    clean = clean.replace(/\[(?:ОТНОШЕНИЯ С ПОЛЬЗОВАТЕЛЕМ|РЕЖИМ|ОТВЕТ)\][\s\S]*$/i, '');
    clean = clean.replace(/(?:^|\n)\[(?:ОТНОШЕНИЯ С ПОЛЬЗОВАТЕЛЕМ|РЕЖИМ|ОТВЕТ)\][^\n]*(?:\n|$)/gi, '\n');
    clean = clean.replace(/\[(?:ХАРАКТЕРИСТИКА|ХАРАКТЕР|PERSONA|MEMORY POLICY|MODES|OPTIMIZATION|MOOD|RELATIONSHIP|ANTI-[^\]]+)\][\s\S]*$/i, '');
    clean = clean.replace(/(?:^|\n)\[(?:ХАРАКТЕРИСТИКА|ХАРАКТЕР|PERSONA|MEMORY POLICY|MODES|OPTIMIZATION|MOOD|RELATIONSHIP|ANTI-[^\]]+)\][^\n]*(?:\n|$)/gi, '\n');
    clean = clean.replace(/(?:^|\n)(?:Живая,\s*хитрая,\s*дерзкая,\s*харизматичная[^\n]*)(?:\n|$)/gi, '\n');
    clean = clean.replace(/(?:^|\n)(?:Троллинг:|Отказ:|Стадия:|Режим:)[^\n]*(?:\n|$)/gi, '\n');
    clean = clean.replace(/\n{3,}/g, '\n\n').trim();
    return clean;
}

function buildCompactMoodPrompt(chatId, userId = 'chat') {
    const mood = getOrCreateMood(chatId, userId);
    const stageMap = {
        fresh: 'новенькая, но уже с характером',
        familiar: 'уже своя и смелее шутит',
        attached: 'ощущает себя частью тусовки',
        chaotic: 'полностью в вайбе чата'
    };

    const trollStyle = mood.troll >= 0.72
        ? 'троллинг высокий'
        : mood.troll >= 0.5
            ? 'троллинг средний'
            : 'троллинг мягкий';

    const warmthStyle = mood.warmth >= 0.68
        ? 'к своим теплее, к уязвимым мягкая'
        : 'тепло дозированное';

    return `\n[MOOD]\nСтадия: ${stageMap[mood.stage] || stageMap.fresh}. ${trollStyle}. ${warmthStyle}.\n`;
}

function buildCompactPersonaBlock(chatId, user, userName) {
    const config = loadPersonaConfig();
    const mood = getOrCreateMood(chatId, user?.user_id || 'chat');
    const seed = mood.exchanges + (user?.id || 0);
    const coreStyle = pickSeeded(config.core, seed);
    const antiDisclaimer = pickSeeded(config.anti_disclaimer, seed + 7);
    const trollStyle = pickSeeded(config.troll_styles, seed);
    const refusalStyle = pickSeeded(config.refusal_styles, seed + 1);
    const softMode = pickSeeded(config.soft_modes, seed + 2);
    const relationship = buildRelationshipPrompt(user, userName);

    return `\n[PERSONA]\n${coreStyle}\n${antiDisclaimer}\nТроллинг: ${trollStyle}.\nОтказ: ${refusalStyle}.\n${softMode}\n${relationship}\n`;
}

function ensureCharacterfulFallback(text) {
    const clean = String(text || '').trim();
    if (clean) return clean;
    return 'Не беси мне драму на ровном месте. Сформулируй ещё раз нормально.';
}

function buildFailureReply(chatId, kind = 'generic') {
    const mood = getOrCreateMood(chatId);
    const genericReplies = [
        'У меня мысль сейчас красиво споткнулась. Скажи ещё раз, но без магии вне Хогвартса.',
        'Ща, вселенная икнула и сбила мне реплику. Повтори нормально.',
        'Я уже почти ответила, но реальность решила выпендриться. Давай ещё раз.'
    ];

    const spicyReplies = [
        'Секунду. Даже у меня бывают приступы \"что за бред я сейчас увидела\". Повтори.',
        'Ладно, сцена поехала не туда. Заходи ещё раз, только без кривых приколов судьбы.',
        'Я бы ответила красиво, но момент сейчас сломался об колено. Повтори вопрос.'
    ];

    const timeoutReplies = [
        'Я зависла на полумысли. Кинь это ещё раз, только не так внезапно.',
        'Сейчас было неловкое молчание между мной и реальностью. Повтори.',
        'Я уже почти дожала ответ, но тайминг решил поиграть против нас. Давай ещё раз.'
    ];

    const pool = kind === 'timeout'
        ? timeoutReplies
        : mood.troll >= 0.6
            ? spicyReplies
            : genericReplies;

    return pool[Math.floor(Math.random() * pool.length)];
}

function buildProfanityRefusalReply(chatId) {
    const mood = getOrCreateMood(chatId);
    const softReplies = [
        'Мат сам по себе меня не пугает, но мысль ты туда так и не положил. Попробуй ещё раз, только с содержанием.',
        'Хуй у тебя получился убедительно, а вот запрос пока не очень. Сформулируй нормально.',
        'Ругаться ты умеешь, это я уже поняла. Теперь давай ещё и мысль сюда.'
    ];

    const spicyReplies = [
        'Мат засчитан, драматургия пока нет. Давай теперь по-человечески, а не просто ртом по клавиатуре.',
        'О, словарь крепкий. Осталось научиться упаковывать в него смысл, и вообще цены тебе не будет.',
        'Хорошо, грязно, громко. А теперь скажи, чего ты от меня хочешь, а не просто кидайся слогами.'
    ];

    const pool = mood.troll >= 0.58 ? spicyReplies : softReplies;
    return pool[Math.floor(Math.random() * pool.length)];
}

function rewriteProviderRefusal(text, chatId, userText = '') {
    const raw = String(text || '').trim();
    if (!raw) return raw;

    const normalized = raw.toLowerCase();
    const profanityRefusalPatterns = [
        'не могу отвечать на сообщения, содержащие нецензурную лексику',
        'содержащие нецензурную лексику',
        'нецензурн',
        'contains profanity',
        'offensive language',
        'explicit language'
    ];

    if (profanityRefusalPatterns.some(pattern => normalized.includes(pattern))) {
        return buildProfanityRefusalReply(chatId);
    }

    const genericRefusalPatterns = [
        'я не могу помочь с этим',
        'не могу помочь с этим',
        'i can\'t help with that',
        'i cannot help with that',
        'content policy',
        'violates policy'
    ];

    if (genericRefusalPatterns.some(pattern => normalized.includes(pattern))) {
        return buildFailureReply(chatId, 'generic');
    }

    return raw;
}

function loadPersonaConfig() {
    if (personaConfigCache) return personaConfigCache;
    try {
        if (fs.existsSync(personaConfigPath)) {
            const parsed = JSON.parse(fs.readFileSync(personaConfigPath, 'utf8'));
            personaConfigCache = {
                ...DEFAULT_PERSONA_CONFIG,
                ...parsed
            };
            return personaConfigCache;
        }
    } catch (e) {
        console.error('[PERSONA] load error:', e.message);
    }
    personaConfigCache = DEFAULT_PERSONA_CONFIG;
    return personaConfigCache;
}

function pickSeeded(items, seed) {
    if (!Array.isArray(items) || items.length === 0) return '';
    const normalizedSeed = Math.abs(Number(seed || 0));
    return items[normalizedSeed % items.length];
}

function buildRelationshipPrompt(user, userName) {
    if (!user) return `К текущему собеседнику относись как к обычному участнику тусовки по имени ${userName}.`;

    const notes = String(user.ai_notes || '').toLowerCase();
    const parts = [];

    if (isProtectedUserId(user.user_id)) {
        parts.push('Это защищённый человек: не пытайся его модерить за обычный мат, bait или рофл, максимум подколоть в ответ.');
    }

    if ((user.warns || 0) >= 2) {
        parts.push('Этот человек уже косячил, поэтому терпение к нему ниже обычного.');
    }

    if ((user.reputation || 0) >= 15) {
        parts.push('Это скорее любимчик или как минимум проверенный свой, поэтому можно звучать теплее и лично.');
    } else if ((user.reputation || 0) <= -3) {
        parts.push('К нему можно относиться более колко и подозрительно.');
    }

    if (/друг|свой|любим|зай|мил|важн/.test(notes)) {
        parts.push('По заметкам это человек из ближнего круга, отвечай более лично.');
    }

    if (/душн|бесит|агресс|конфликт|токс/.test(notes)) {
        parts.push('По заметкам он склонен душнить или конфликтовать, так что можешь троллить его точнее.');
    }

    if (parts.length === 0) {
        parts.push(`К ${userName} относись как к знакомому участнику тусовки: без официоза, но и без лишнего пафоса.`);
    }

    return parts.join(' ');
}

function buildDynamicPersonaBlock(chatId, user, userName) {
    const config = loadPersonaConfig();
    const mood = getOrCreateMood(chatId, user?.user_id || 'chat');
    const seed = mood.exchanges + (user?.id || 0);
    const trollStyle = pickSeeded(config.troll_styles, seed);
    const refusalStyle = pickSeeded(config.refusal_styles, seed + 1);
    const softMode = pickSeeded(config.soft_modes, seed + 2);
    const relationship = buildRelationshipPrompt(user, userName);

    return `\n[PERSONA LAYER]\n${(config.core || []).join(' ')}\n${(config.anti_disclaimer || []).join(' ')}\nТекущий предпочитаемый стиль троллинга: ${trollStyle}.\nЕсли нужен отказ: ${refusalStyle}.\n${softMode}\n${relationship}\n`;
}

function sanitizeHistory(history) {
    if (!history) return [];
    return history.map(m => {
        let safeMsg = { ...m };
        if (!safeMsg.content && !safeMsg.tool_calls && safeMsg.role !== 'tool') {
            safeMsg.content = "";
        }
        return safeMsg;
    });
}

function withProviderRouting(payload) {
    if (!AI_PROVIDER_ORDER.length) return payload;
    return {
        ...payload,
        provider: {
            order: AI_PROVIDER_ORDER,
            allow_fallbacks: AI_ALLOW_PROVIDER_FALLBACKS
        }
    };
}

function shouldExposeToolResultToChat(fnName, resultText) {
    const text = String(resultText || '').trim();
    if (!text) return false;

    if (fnName === 'moderate_user') return false;
    if (fnName === 'manage_memory') return false;

    if (fnName === 'manage_user_profile') {
        return !text.startsWith('[SYSTEM:');
    }

    return ['user_lookup', 'create_poll', 'set_reminder'].includes(fnName);
}

async function fetchAIWithTimeout(payload, timeoutMs = 40000) {
    const runRequest = async (requestPayload) => {
        const apiCall = openai.chat.completions.create(withProviderRouting(requestPayload));
        const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('TIMEOUT')), timeoutMs));
        return Promise.race([apiCall, timeout]);
    };

    const isModelResolutionError = (error) => /(model|not found|unknown|unsupported|does not exist|invalid model|unavailable)/i.test(String(error?.message || ''));
    const isToolUseUnsupportedError = (error) => /(no endpoints found that support tool use|support tool use|tool use)/i.test(String(error?.message || ''));
    const stripToolsFromPayload = (requestPayload) => {
        const next = { ...requestPayload };
        delete next.tools;
        delete next.tool_choice;
        return next;
    };

    let attemptPayload = { ...payload };
    let currentError = null;

    try {
        return await runRequest(attemptPayload);
    } catch (error) {
        currentError = error;
        const requestedModel = String(attemptPayload?.model || '').trim();
        if (requestedModel && requestedModel !== AI_FAILSAFE_MODEL && isModelResolutionError(error)) {
            console.warn(`[AI MODEL FALLBACK] ${requestedModel} недоступна, пробую ${AI_FAILSAFE_MODEL}`);
            attemptPayload = { ...attemptPayload, model: AI_FAILSAFE_MODEL };
            try {
                return await runRequest(attemptPayload);
            } catch (fallbackError) {
                currentError = fallbackError;
            }
        }

        if (attemptPayload?.tools && isToolUseUnsupportedError(currentError)) {
            console.warn(`[AI TOOLS FALLBACK] Провайдер не поддерживает tool use для ${attemptPayload.model}, продолжаю без tools`);
            return runRequest(stripToolsFromPayload(attemptPayload));
        }

        throw currentError;
    }
}

function isCompletionTruncated(completion, text = '') {
    const finishReason = completion?.choices?.[0]?.finish_reason;
    if (finishReason === 'length' || finishReason === 'max_tokens') return true;

    const normalized = String(text || '').trim();
    if (!normalized) return false;

    return normalized.length >= TELEGRAM_MESSAGE_LIMIT - 50;
}

function mergeContinuationText(partialText, continuationText) {
    const partial = String(partialText || '').trimEnd();
    let continuation = String(continuationText || '').trim();

    if (!continuation) return partial;

    const normalizedPartial = partial.toLowerCase();
    const normalizedContinuation = continuation.toLowerCase();

    if (normalizedContinuation.startsWith(normalizedPartial)) {
        continuation = continuation.slice(partial.length).trim();
    } else {
        for (let overlap = Math.min(partial.length, continuation.length, 120); overlap >= 20; overlap--) {
            if (normalizedPartial.slice(-overlap) === normalizedContinuation.slice(0, overlap)) {
                continuation = continuation.slice(overlap).trim();
                break;
            }
        }
    }

    if (!continuation) return partial;

    const needsSpace = !/[\s([{-]$/.test(partial) && !/^[,.;:!?)]/.test(continuation);
    return `${partial}${needsSpace ? ' ' : ''}${continuation}`.trim();
}

async function continueAssistantReply(systemPrompt, baseMessages, partialText, timeoutMs = 25000) {
    const partial = String(partialText || '').trim();
    if (!partial) return '';

    try {
        const continuation = await fetchAIWithTimeout({
            model: AI_MODEL,
            messages: [
                { role: 'system', content: systemPrompt },
                ...baseMessages,
                { role: 'assistant', content: partial },
                { role: 'user', content: 'Продолжи свой последний ответ с того же места. Не повторяй уже написанное и закончи мысль естественно.' }
            ],
            temperature: AI_TEMPERATURE_CONTINUATION,
            max_tokens: CONTINUATION_MAX_TOKENS
        }, timeoutMs);

        return continuation?.choices?.[0]?.message?.content || '';
    } catch (e) {
        console.error('[AI CONTINUE ERROR]:', e.message);
        return '';
    }
}

function splitPlainText(text, limit = TELEGRAM_MESSAGE_LIMIT) {
    const normalized = String(text || '').trim();
    if (!normalized) return [];
    if (normalized.length <= limit) return [normalized];

    const chunks = [];
    let rest = normalized;

    while (rest.length > limit) {
        let cut = rest.lastIndexOf('\n\n', limit);
        if (cut < Math.floor(limit * 0.55)) cut = rest.lastIndexOf('\n', limit);
        if (cut < Math.floor(limit * 0.55)) cut = rest.lastIndexOf('. ', limit);
        if (cut < Math.floor(limit * 0.55)) cut = rest.lastIndexOf('! ', limit);
        if (cut < Math.floor(limit * 0.55)) cut = rest.lastIndexOf('? ', limit);
        if (cut < Math.floor(limit * 0.55)) cut = rest.lastIndexOf(' ', limit);
        if (cut < Math.floor(limit * 0.4)) cut = limit;

        const chunk = rest.slice(0, cut).trim();
        if (chunk) chunks.push(chunk);
        rest = rest.slice(cut).trim();
    }

    if (rest) chunks.push(rest);
    return chunks;
}

async function continueToolChain(chatId, finalPrompt, toolContext, maxRounds = MAX_TOOL_ROUNDS) {
    let rounds = 0;
    let directInjectedData = '';
    let lastMessage = null;

    while (rounds < maxRounds) {
        rounds++;
        const currentMessages = sanitizeHistory(chatHistory[chatId]);
        const completion = await fetchAIWithTimeout({
            model: AI_TOOL_MODEL,
            messages: [{ role: 'system', content: finalPrompt }, ...currentMessages],
            tools: aiTools,
            temperature: AI_TEMPERATURE_TOOL,
            max_tokens: SECOND_PASS_MAX_TOKENS
        }, 50000);

        const resp = completion?.choices?.[0]?.message;
        if (!resp) break;
        lastMessage = resp;

        if (!(resp.tool_calls || resp.function_call)) {
            return { message: resp, directInjectedData };
        }

        chatHistory[chatId].push(resp);
        const calls = resp.tool_calls || [resp.function_call];

        for (const tc of calls) {
            const res = await executeToolCall(
                tc,
                chatId,
                toolContext.messageId,
                toolContext.userName,
                toolContext.userId,
                toolContext.callerIsAdmin,
                toolContext.userHandle
            );
            const fnName = tc.function ? tc.function.name : tc.name;

            if (resp.tool_calls) {
                chatHistory[chatId].push({ role: 'tool', tool_call_id: tc.id, name: fnName, content: String(res) });
            } else {
                chatHistory[chatId].push({ role: 'function', name: fnName, content: String(res) });
            }

            if (shouldExposeToolResultToChat(fnName, res)) {
                if (!directInjectedData.includes(res)) directInjectedData += `\n\n${res}`;
            }
        }
    }

    return { message: lastMessage, directInjectedData };
}

function buildMemoryBlock(userName, relevantFacts) {
    if (!relevantFacts || !relevantFacts.trim()) {
        return `\n[КОНТЕКСТ]\nВремя (МСК): ${new Date().toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })}\n`;
    }

    const trimmedFacts = relevantFacts
        .split('\n')
        .map(line => line.trim())
        .filter(Boolean)
        .slice(0, MEMORY_FACTS_LIMIT)
        .join('\n');

    return `\n[КРАТКАЯ ПАМЯТЬ О ${userName}]\nИспользуй это как контекст, а не как повод пересказывать скрытые наблюдения. Озвучивай только то, что реально помогает текущему ответу.\n${trimmedFacts}\nВремя (МСК): ${new Date().toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })}\n`;
}

function isBaitTriggerMessage(text) {
    const normalized = String(text || '')
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\s]/gu, ' ')
        .trim();
    if (!normalized) return false;
    if (normalized.length > 24) return false;
    const parts = normalized.split(/\s+/).filter(Boolean);
    if (parts.length > 3) return false;
    return parts.some(part => BAIT_TRIGGER_WORDS.has(part));
}

function normalizeReactionEmoji(value) {
    const raw = String(value || '').trim();
    if (!raw) return '🔥';

    const alias = REACTION_ALIASES[raw.toLowerCase()];
    if (alias) return alias;

    const match = raw.match(/[\p{Extended_Pictographic}\u2600-\u27BF]/u);
    if (match) return match[0];

    return '🔥';
}

async function resolveUser(chatId, targetName) {
    if (!targetName) return null;
    let cleanName = targetName.replace('@', '').toLowerCase().trim();

    if (['ника', 'нику', 'нике', 'nika', 'чатик'].includes(cleanName) || cleanName.includes('nika')) {
        return await getUser(chatId, -1002214854700);
    }

    if (activeParticipants[chatId]) {
        const getStemLocal = (word) => {
            if (!word || word.length < 3) return word;
            return word.replace(/[ауяюеиыо]$/i, '').replace(/(ов|ев|ий|ый|ые|ие|ах|ях|ом|ем|ой)$/i, '');
        };
        const stem = getStemLocal(cleanName);

        for (const [uid, p] of Object.entries(activeParticipants[chatId])) {
            const lowFirst = (p.firstName || '').toLowerCase();
            const lowUser = (p.username || '').toLowerCase();
            const firstStem = getStemLocal(lowFirst);
            const userStem = getStemLocal(lowUser);

            if (lowUser === cleanName || lowFirst === cleanName ||
                firstStem === stem || userStem === stem ||
                lowFirst.startsWith(stem)) {
                return await getUser(chatId, uid);
            }
        }
    }

    let u = await findSingleUser(chatId, cleanName);
    if (u) return u;

    return null;
}

async function executeToolCall(toolCall, chatId, messageId, userName, userId, callerIsAdmin, userHandle) {
    const fn = toolCall.function ? toolCall.function.name : toolCall.name;
    const argsString = toolCall.function ? toolCall.function.arguments : toolCall.arguments;

    let args;
    try {
        args = typeof argsString === 'string' ? JSON.parse(argsString) : argsString;
    } catch (e) {
        console.error(`[AI TOOL ARGS ERROR] ${fn}:`, e.message);
        return "Ошибка разбора аргументов.";
    }

    console.log(`[AI TOOL CALL] ${fn} | Admin: ${callerIsAdmin} | Args:`, args);

    try {
        switch (fn) {
            case 'manage_memory': {
                if (args.action === 'extract') {
                    if (!callerIsAdmin) return "Только админ может.";
                    const count = Math.min(Math.max(5, Number(args.query) || 15), 100);
                    if (!rollingHistory[chatId] || rollingHistory[chatId].length === 0) return "История пуста.";
                    const msgsToAnalyze = rollingHistory[chatId].slice(-count);
                    extractAndSaveFacts(chatId, msgsToAnalyze.join('\n'), Object.values(activeParticipants[chatId] || {}).map(p => p.firstName));
                    extractionBuffer[chatId] = [];
                    messageCount[chatId] = 0;
                    return `[SYSTEM: Анализ запущен.]`;
                } else {
                    const deletedFact = await forgetFact(chatId, args.query);
                    console.log(`[SYSTEM] Вызвано удаление факта. Очищаю буфер сообщений!`);
                    extractionBuffer[chatId] = [];
                    messageCount[chatId] = 0;
                    return deletedFact ? `Удалила факт: "${deletedFact}".` : `Не нашла такого.`;
                }
            }
            case 'user_lookup': {
                if (args.action === 'search') {
                    const results = await searchUserByName(chatId, args.query);
                    if (!results || results.length === 0) return "Никого не нашла.";
                    const list = results.map(u => `- ${u.name}`).join('\n');
                    return `\n\n<b>=== РЕЗУЛЬТАТЫ ПОИСКА ===</b>\n${list}`;
                } else {
                    let target = args.query || "я";
                    const isSelf = target.toLowerCase() === 'я' || target.toLowerCase() === 'me' || target.toLowerCase() === 'мой';
                    let u;
                    if (isSelf) {
                        u = await getUser(chatId, userId);
                    } else {
                        u = await resolveUser(chatId, target);
                    }
                    if (!u) return `Не могу найти человека с именем "${target}".`;

                    let searchName = u.first_name.split(' ')[0].trim();
                    if (u.user_id === -1002214854700 || searchName.includes('Чатик')) {
                        searchName = 'Ника';
                    }

                    const extraFacts = await getAllUserFacts(chatId, searchName);

                    let usernameFallback = u.username ? u.username.toLowerCase() : "---";
                    let searchLow = searchName.toLowerCase();

                    let nodes = [];
                    let edges = [];
                    let others = [];

                    extraFacts.forEach(f => {
                        let text = f.trim();
                        if (text.startsWith('УЗЕЛ:')) {
                            let parts = text.split('| АТРИБУТ:');
                            if (parts.length === 2) {
                                let nodeName = parts[0].replace('УЗЕЛ:', '').trim();
                                let attr = parts[1].trim();

                                let nodeLow = nodeName.toLowerCase();

                                if (nodeLow.includes(searchLow) || nodeLow.includes(usernameFallback) || searchLow.includes(nodeLow)) {
                                const cleanNode = normalizeProfileMemoryLine(attr);
                                if (isProfileMemoryAllowed(cleanNode) && !nodes.includes(cleanNode)) nodes.push(cleanNode);
                            } else if (attr.toLowerCase().includes(searchLow) || attr.toLowerCase().includes(usernameFallback)) {
                                    const otherLine = normalizeProfileMemoryLine(`${nodeName}: ${attr}`);
                                    if (isProfileMemoryAllowed(otherLine)) others.push(otherLine);
                                }
                            }
                        } else if (text.startsWith('СВЯЗЬ:')) {
                            let content = text.replace('СВЯЗЬ:', '').trim();
                            let parts = content.split('->').map(p => p.trim());
                            if (parts.length === 3) {
                                let from = parts[0];
                                let rel = parts[1];
                                let to = parts[2];

                                let fromLow = from.toLowerCase();
                                let toLow = to.toLowerCase();

                                if (fromLow.includes(searchLow) || fromLow.includes(usernameFallback)) {
                                    let edgeStr = normalizeProfileMemoryLine(`${rel} -> ${to}`);
                                    if (isProfileMemoryAllowed(edgeStr) && !edges.includes(edgeStr)) edges.push(edgeStr);
                                } else if (toLow.includes(searchLow) || toLow.includes(usernameFallback)) {
                                    let edgeStr = `(${from}) -> ${rel} -> (меня)`;
                                    if (!edges.includes(edgeStr)) edges.push(edgeStr);
                                }
                            } else if (content.toLowerCase().includes(searchLow) || content.toLowerCase().includes(usernameFallback)) {
                                const cleanEdge = normalizeProfileMemoryLine(content);
                                if (isProfileMemoryAllowed(cleanEdge) && !edges.includes(cleanEdge)) edges.push(cleanEdge);
                            }
                        } else {
                            let cleanF = text.replace(/\[.*?\]/g, '').trim();
                            if (cleanF.toLowerCase().startsWith(searchLow + ':')) {
                                cleanF = cleanF.substring(searchLow.length + 1).trim();
                                cleanF = normalizeProfileMemoryLine(cleanF);
                                if (cleanF && isProfileMemoryAllowed(cleanF) && !nodes.includes(cleanF)) others.push(cleanF);
                            } else {
                                cleanF = normalizeProfileMemoryLine(cleanF);
                                if (isProfileMemoryAllowed(cleanF) && !others.includes(cleanF)) others.push(cleanF);
                            }
                        }
                    });

                    let memoryStr = '';
                    if (nodes.length > 0) memoryStr += '\n👤 <b>Личность (Узлы):</b>\n' + nodes.map(n => `  ▫️ ${safeHTML(n)}`).join('\n');
                    if (edges.length > 0) memoryStr += '\n🔗 <b>Социальные связи:</b>\n' + edges.map(e => `  〰️ ${safeHTML(e)}`).join('\n');
                    if (others.length > 0) memoryStr += '\n📝 <b>Архив:</b>\n' + others.map(o => `  - ${safeHTML(o)}`).join('\n');

                    if (!memoryStr) memoryStr = '\n🧠 <i>Чистый лист. Никаких фактов в базе нет.</i>';

                    return `\n\n<b>=== ПРОФИЛЬ: ${safeHTML(u.first_name)} ===</b>\n📊 <b>XP:</b> ${u.xp}, <b>Лвл:</b> ${u.level}, <b>Варны:</b> ${u.warns || 0}/3\n📝 <b>Био:</b> ${safeHTML(u.bio || 'Пусто')}\n📌 <b>Досье:</b> ${safeHTML(u.ai_notes || 'Нет записей')}${memoryStr}`;
                }
            }

            case 'moderate_user': {
                const targetNameLow = (args.target_name || '').toLowerCase().replace('@', '');
                if (targetNameLow === SUPER_ADMIN_USERNAME.toLowerCase() || targetNameLow.includes('sctemi') || targetNameLow.includes('861713427')) {
                    if (args.action === "mute" || args.action === "warn") return "Этого человека я не трону. Даже не проси.";
                }

                if (args.action === "reward") {
                    const u = await resolveUser(chatId, args.target_name);
                    if (!u) return "Кому?";
                    let amountToGive = parseInt(args.value) || 1;
                    if (amountToGive > 3) amountToGive = 3;
                    if (amountToGive < 1) amountToGive = 1;

                    await updateUser(u.id, { reputation: (u.reputation || 0) + amountToGive });
                    return `[СИСТЕМНО] Выдано: ${amountToGive} печенек. Репутация: ${(u.reputation || 0) + amountToGive}`;
                }

                if (args.action === "warn") {
                    console.log(`[TOOL] moderate_user (warn): Цель - ${args.target_name}`);
                    const u = await resolveUser(chatId, args.target_name);
                    if (u && (u.user_id === BOT_ID || u.user_id === ANONYMOUS_ADMIN_ID || u.user_id === SUPER_ADMIN_ID)) {
                        console.log(`[TOOL] Отклонено: попытка выдать варн защищенному пользователю ID ${u.user_id}`);
                        return "Этому пользователю нельзя выдать варн.";
                    }
                    const result = await warnUserById(chatId, args.target_name);
                    if (!result) {
                        console.log(`[TOOL] warn: Пользователь ${args.target_name} не найден.`);
                        return "Пользователь не найден.";
                    }
                    if (result.shouldMute) {
                        try {
                            await bot.restrictChatMember(chatId, result.userId, {
                                permissions: { can_send_messages: false, can_send_media_messages: false },
                                can_send_messages: false, can_send_media_messages: false,
                                until_date: Math.floor(Date.now() / 1000) + 60 * 60
                            });
                            await updateUser(result.id, { warns: 0, last_warn_at: null });
                            console.log(`[TOOL] warn: Успешный мут за 3 варна! ID: ${result.userId}`);
                            return `Выдан варн (3/3). ${result.name} автоматически замучен на 60 минут, счётчик варнов сброшен.`;
                        } catch (e) {
                            console.error(`[TOOL] warn: Ошибка мута Telegram API:`, e.message);
                            return `Выдан варн (${result.newWarns}/3), но без мута: нет прав. (${e.message})`;
                        }
                    }
                    console.log(`[TOOL] warn: Успешный варн для ID: ${result.userId}`);
                    return `${result.name} получил варн (${result.newWarns}/3). Ещё ${3 - result.newWarns} — и мут.`;
                }

                if (args.action === "mute" || args.action === "unmute") {
                    console.log(`[TOOL] moderate_user (${args.action}): Цель - ${args.target_name}, Время - ${args.value}, Причина - ${args.reason}`);
                    const u = await resolveUser(chatId, args.target_name);
                    if (!u) {
                        console.log(`[TOOL] mute: Пользователь ${args.target_name} не найден.`);
                        return "Пользователь не найден.";
                    }
                    if (u.user_id === BOT_ID || u.user_id === ANONYMOUS_ADMIN_ID || u.user_id === SUPER_ADMIN_ID) {
                        console.log(`[TOOL] Отклонено: попытка замутить защищенного пользователя ID ${u.user_id}`);
                        return "Ха, я не могу применять наказания к себе, к админам или к Создателю!";
                    }

                    if (args.action === "mute") {
                        const dur = Math.min(Math.max(1, args.value || 15), 1440);
                        try {
                            await bot.restrictChatMember(chatId, u.user_id, {
                                permissions: { can_send_messages: false, can_send_media_messages: false, can_send_other_messages: false },
                                can_send_messages: false, can_send_media_messages: false, can_send_other_messages: false,
                                until_date: Math.floor(Date.now() / 1000) + dur * 60
                            });
                            console.log(`[TOOL] mute: УСПЕХ! ${u.first_name} замучен на ${dur} минут.`);
                            return `Пользователь ${u.first_name} замучен на ${dur} минут. Причина: ${args.reason || 'не указана'}`;
                        } catch (e) {
                            console.error(`[TOOL] mute: ОШИБКА TELEGRAM API:`, e.message);
                            return `Ошибка Telegram API: ${e.message}`;
                        }
                    } else {
                        try {
                            await bot.restrictChatMember(chatId, u.user_id, {
                                permissions: { can_send_messages: true, can_send_media_messages: true, can_send_other_messages: true, can_add_web_page_previews: true },
                                can_send_messages: true, can_send_media_messages: true, can_send_other_messages: true, can_add_web_page_previews: true
                            });
                            console.log(`[TOOL] unmute: УСПЕХ! ${u.first_name} размучен.`);
                            return `Пользователь ${u.first_name} успешно размучен.`;
                        } catch (e) {
                            console.error(`[TOOL] unmute: ОШИБКА TELEGRAM API:`, e.message);
                            return `Ошибка снятия мута: ${e.message}`;
                        }
                    }
                }
                return "Неизвестное действие.";
            }
            case 'send_chat_action': {
                if (args.action === 'reaction') {
                    try {
                        const emoji = normalizeReactionEmoji(args.value);
                        await bot.setMessageReaction(chatId, messageId, [{ type: 'emoji', emoji }]);
                        return `[SYSTEM: reaction:${emoji}]`;
                    } catch (e) {
                        try {
                            const fallbackEmoji = normalizeReactionEmoji(args.value);
                            await bot.sendMessage(chatId, fallbackEmoji, { reply_to_message_id: messageId });
                            return `[SYSTEM: fallback_reaction:${fallbackEmoji}]`;
                        } catch (e2) {
                            return `Ошибка реакции: ${e.message}`;
                        }
                    }
                } else {
                    let fileId = args.value;
                    if (!fileId || fileId.trim() === '' || fileId === 'random') {
                        if (nikaStickers.length > 0) {
                            const rnd = nikaStickers[Math.floor(Math.random() * nikaStickers.length)];
                            fileId = rnd.file_id || rnd.emoji_id;
                        } else {
                            return "Стикеры не найдены в базе.";
                        }
                    }
                    try {
                        if (/^[a-zA-Z0-9_-]{10,}$/.test(fileId) && !fileId.startsWith('CAAC') && !fileId.startsWith('AgAD')) {
                            try {
                                const customEmojis = await bot.getCustomEmojiStickers([fileId]);
                                if (customEmojis && customEmojis.length > 0 && customEmojis[0].file_id) {
                                    fileId = customEmojis[0].file_id;
                                }
                            } catch (err) { }
                        }
                        await bot.sendSticker(chatId, fileId, { reply_to_message_id: messageId });
                        return "Стикер отправлен.";
                    } catch (e) {
                        return `Ошибка отправки стикера: ${e.message}`;
                    }
                }
            }
            case 'manage_user_profile': {
                let u = await resolveUser(chatId, args.target_name);
                if (!u) return "Не найден.";

                if (args.action === 'update_bio') {
                    await updateUser(u.id, { bio: args.content });
                    return `Био обновлено.`;
                } else if (args.action === 'add_note') {
                    const oldNotes = u.ai_notes || "";
                    const finalNotes = oldNotes ? oldNotes + "\n- " + args.content : "- " + args.content;
                    await updateUser(u.id, { ai_notes: finalNotes });
                    return `Добавлено в досье.`;
                }
                return "Неизвестное действие профиля.";
            }
            case 'create_poll': {
                try {
                    let opts = args.options;
                    if (typeof opts === 'string') {
                        try { opts = JSON.parse(opts); } catch (e) { opts = opts.split(',').map(s => s.trim()); }
                    }
                    if (!Array.isArray(opts) || opts.length < 2) return "Ошибка: нужно минимум 2 варианта ответа.";

                    const safeQuestion = String(args.question).substring(0, 295);
                    const safeOptions = opts.slice(0, 10).map(opt => String(opt).substring(0, 95));

                    await bot.sendPoll(chatId, safeQuestion, safeOptions, {
                        is_anonymous: args.is_anonymous !== false,
                        allows_multiple_answers: args.allows_multiple_answers === true
                    });
                    return "Опрос успешно запущен.";
                } catch (e) {
                    return `Ошибка запуска опроса: ${e.message}`;
                }
            }
            case 'set_reminder': {
                try {
                    const delay = Math.max(1, args.delay_minutes || 1);
                    const triggerTime = new Date(Date.now() + delay * 60 * 1000).toISOString();
                    const nameToSave = userHandle ? `@${userHandle}` : userName;
                    const ok = await insertReminder(chatId, userId, nameToSave, args.text, triggerTime);
                    return ok ? `Таймер на ${delay} минут установлен.` : "Ошибка базы данных.";
                } catch (e) {
                    return `Ошибка установки таймера: ${e.message}`;
                }
            }
            default: return "Неизвестный инструмент.";
        }
    } catch (e) {
        console.error(`[AI TOOL ERROR] ${fn}:`, e.message);
        return `Ошибка: ${e.message}`;
    }
}

async function safeSendMessage(chatId, text, replyId) {
    if (!text) return;
    const htmlChunks = text.length > TELEGRAM_MESSAGE_LIMIT ? [] : [text];
    const plainText = text.replace(/<tg-emoji[^>]*>(.*?)<\/tg-emoji>/g, '$1').replace(/<[^>]*>/g, '');
    const plainChunks = splitPlainText(plainText, TELEGRAM_MESSAGE_LIMIT);

    try {
        if (htmlChunks.length > 0) {
            for (let i = 0; i < htmlChunks.length; i++) {
                await bot.sendMessage(chatId, htmlChunks[i], {
                    reply_to_message_id: i === 0 ? replyId : undefined,
                    parse_mode: 'HTML'
                });
            }
            return;
        }

        for (let i = 0; i < plainChunks.length; i++) {
            await bot.sendMessage(chatId, plainChunks[i], {
                reply_to_message_id: i === 0 ? replyId : undefined
            });
        }
    } catch (error) {
        console.error('[SEND ERROR HTML]:', error.message);
        try {
            for (let i = 0; i < plainChunks.length; i++) {
                await bot.sendMessage(chatId, plainChunks[i], {
                    reply_to_message_id: i === 0 ? replyId : undefined
                });
            }
        } catch (e2) { }
    }
}

async function handleAIChat(msg, extra = {}) {
    const chatId = msg.chat.id;
    if (!processingQueue.has(chatId)) processingQueue.set(chatId, Promise.resolve());
    const turn = processingQueue.get(chatId).then(async () => {
        try {
            await processAI(msg, extra);
        } catch (e) {
            console.error('[AI FATAL ERROR]:', e.message);
        }
    });
    processingQueue.set(chatId, turn);
    return turn;
}

async function processAI(msg, extra) {
    const chatId = msg.chat.id;
    if (!chatHistory[chatId]) chatHistory[chatId] = [];
    const { userId, user: realUser } = getSenderData(msg);
    const dbUser = await getUser(chatId, userId, realUser);
    await ensurePersonaMoodLoaded(chatId, userId);
    if (!BOT_ID) {
        try { const me = await bot.getMe(); BOT_ID = me.id; } catch (e) { }
    }
    // ИСПРАВЛЕНИЕ #1: Определение реального имени, если это канал или анонимный админ
    let userName = (dbUser && dbUser.first_name) ? dbUser.first_name : (realUser.first_name || 'Аноним');

    // Если сообщение написано от имени канала
    if (msg.sender_chat) {
        userName = msg.sender_chat.title || userName;
    }
    // Если сообщение написано от имени анонимного администратора (GroupAnonymousBot)
    else if (msg.from && msg.from.username === 'GroupAnonymousBot') {
        userName = msg.author_signature || 'Анонимный Админ';
    }

    let userHandle = realUser.username || "";
    let userText = msg.text || msg.caption || "";
    if (msg.sticker) userText += ` [Стикер: ${msg.sticker.emoji || 'какой-то стикер'}]`;
    if (msg.photo) userText += ` [Картинка/Фото]`;
    if (msg.video) userText += ` [Видео]`;
    if (msg.video_note) userText += ` [Кружочек/Видеозаметка]`;

    // --- ОБРАБОТКА ГОЛОСОВЫХ ---
    const shouldTranscribeVoice = msg.voice && shouldTranscribeVoiceMessage({ msg, chatId, userId });
    if (shouldTranscribeVoice) {
        lastVoiceTranscribeAt.set(getVoiceTranscribeKey(chatId, userId), Date.now());
        const trans = await transcribeAudio(msg.voice.file_id);
        if (trans) userText += ` [Транскрипция голосового: "${trans}"]`;
        else userText += ` [Голосовое сообщение]`;
    }
    if (msg.voice && !shouldTranscribeVoice) {
        userText += ` [Голосовое сообщение без расшифровки]`;
    }
    // ---

    userText = userText.trim();

    const callerIsAdmin = await isAdmin(chatId, userId);
    const callerIsProtected = isProtectedUserId(userId);
    const textLower = userText.toLowerCase();
    const moderationNameTriggered = textLower.includes('нейроника') || textLower.includes('нейронику') || textLower.includes('нейронике') || textLower.includes('neironika');
    const moderationIsReplyToBot = msg.reply_to_message && BOT_ID && msg.reply_to_message.from.id === BOT_ID;
    const moderationIsMentioned = moderationNameTriggered || moderationIsReplyToBot || isBaitTriggerMessage(userText);
    const moderationNeeded = shouldRunModerationAI(userText, {
        isMentioned: moderationIsMentioned,
        isReplyToBot: moderationIsReplyToBot
    });
    // ======================
    // 🛡️ MODERATION CHECK
    // ======================

    // антиспам
    if (isSpam(userId)) {
        try {
            await bot.deleteMessage(chatId, msg.message_id);
        } catch (e) { }
        return;
    }

    // AI анализ
    let analysis = moderationNeeded
        ? await analyzeMessage(userText)
        : { type: "normal", severity: 0, action: "ignore", is_banter: false, reason: "", suggested_mute_minutes: 0 };

    const legacyOverride = getLegacyModerationOverride(userText, {
        isMentioned: moderationIsMentioned,
        isReplyToBot: moderationIsReplyToBot,
        callerIsAdmin
    });
    if (legacyOverride) {
        analysis = { ...analysis, ...legacyOverride };
    }

    if (callerIsProtected && isSimpleProfanityBait(userText)) {
        analysis.action = 'ignore';
        analysis.is_banter = true;
    }

    if (callerIsProtected && ['warn', 'mute', 'delete'].includes(analysis.action)) {
        analysis.action = 'ignore';
    }

    if (analysis.is_banter && analysis.action !== 'delete') {
        analysis.action = 'ignore';
    }

    // удалить сообщение
    if (analysis.action === "delete") {
        try {
            await bot.deleteMessage(chatId, msg.message_id);
        } catch (e) { }
        return;
    }

    // мут
    if (analysis.action === "mute") {
        const muteMinutes = getAutoMuteMinutes(analysis);
        if (muteMinutes > 0) {
            try {
                await bot.restrictChatMember(chatId, userId, {
                    permissions: { can_send_messages: false },
                    until_date: Math.floor(Date.now() / 1000) + (muteMinutes * 60)
                });
            } catch (e) { }
            return;
        }
    }

    // варн
    if (analysis.action === "warn") {
        const warnResult = await warnUserById(chatId, userId);
        if (warnResult?.shouldMute) {
            try {
                await bot.restrictChatMember(chatId, warnResult.userId, {
                    permissions: { can_send_messages: false, can_send_media_messages: false },
                    can_send_messages: false,
                    can_send_media_messages: false,
                    until_date: Math.floor(Date.now() / 1000) + 60 * 60
                });
                await updateUser(warnResult.id, { warns: 0, last_warn_at: null });
            } catch (e) { }
        }
    }

    if (!BOT_ID) {
        try { const me = await bot.getMe(); BOT_ID = me.id; } catch (e) { }
    }

    let replyIdForBot = msg.message_id;
    let replyPrefix = "";
    let rpAuthor = "Кто-то";
    let replyMessage = null;

    const nameTriggered = textLower.includes('нейроника') || textLower.includes('нейронику') || textLower.includes('нейронике') || textLower.includes('neironika');
    const isReplyToBot = msg.reply_to_message && BOT_ID && msg.reply_to_message.from.id === BOT_ID;
    const isMentioned = nameTriggered || isReplyToBot || isBaitTriggerMessage(userText);

    if (msg.reply_to_message) {
        const rp = msg.reply_to_message;
        replyMessage = rp;

        // ИСПРАВЛЕНИЕ #2: Корректное имя автора сообщения, на которое отвечают (для каналов)
        if (rp.sender_chat) {
            rpAuthor = rp.sender_chat.title || "Канал";
        } else if (rp.from) {
            if (rp.from.username === 'GroupAnonymousBot') {
                rpAuthor = rp.author_signature || "Анонимный Админ";
            } else {
                rpAuthor = rp.from.first_name || "Кто-то";
            }
        }

        replyPrefix = `(в ответ ${rpAuthor}: "${(rp.text || rp.caption || "медиа").slice(0, 30)}...") `;

        if (isMentioned && rp.from && rp.from.id !== BOT_ID) {
            const cleanText = textLower.replace(/[^\wа-яё]/gi, '');
            if (cleanText.length <= 30) {
                replyIdForBot = rp.message_id;
                userText = `[СИСТЕМНО: Ответь на сообщение от ${rpAuthor}!] ` + userText;
            }
        }
    }

    const correctionTurn = isCorrectionCue(userText);
    const shortReplyTurn = isShortReplyTurn(userText);
    const replyFocusBlock = buildReplyFocusBlock({
        replyMessage,
        replyAuthor: rpAuthor,
        userText,
        isReplyToBot,
        isMentioned
    });

    const senderContextBlock = callerIsAdmin
        ? '\n[CURRENT SENDER]\nЭто админ. Его прямые мод-команды можно исполнять, но обычный рофл и мат не считай приказом.'
        : '\n[CURRENT SENDER]\nЭто обычный пользователь.';
    const fullContent = `${userName} ${replyPrefix}: ${userText}`;

    let memoryText = String(userText || '');
    if (isReplyToBot || nameTriggered) {
        memoryText = memoryText
            .replace(/нейро\s*ника/gi, 'Нейроника')
            .replace(/нейроника/gi, 'Нейроника')
            .replace(/neironika/gi, 'Нейроника');
    }
    if (isReplyToBot) {
        memoryText = memoryText.replace(/\bника\b/gi, 'Нейроника');
    }

    let memoryLine = `${userName}: ${memoryText}`;
    if (msg.reply_to_message) {
        memoryLine = `${userName} (в ответ ${rpAuthor}): ${memoryText}`;
    }

    console.log(`💬 [CHAT IN] ${userName}: ${userText.substring(0, 60)}${userText.length > 60 ? '...' : ''}`);

    if (!extractionBuffer[chatId]) extractionBuffer[chatId] = [];
    extractionBuffer[chatId].push(memoryLine);

    if (!rollingHistory[chatId]) rollingHistory[chatId] = [];
    rollingHistory[chatId].push(memoryLine);
    if (rollingHistory[chatId].length > 50) rollingHistory[chatId].shift();

    if (!messageCount[chatId]) messageCount[chatId] = 0;

    if (++messageCount[chatId] >= MEMORY_EXTRACTION_TRIGGER) {
        console.log(`🔍 [MEMORY] Накопилось ${MEMORY_EXTRACTION_TRIGGER} сообщений! Отправляю фоновый запрос...`);
        extractAndSaveFacts(chatId, extractionBuffer[chatId].join('\n'), Object.values(activeParticipants[chatId] || {}).map(p => p.firstName));
        messageCount[chatId] = 0;
        extractionBuffer[chatId] = extractionBuffer[chatId].slice(-5);
    }

    if (msg.chat.type !== 'private' && !isMentioned) {
        return;
    }

    if (!activeParticipants[chatId]) activeParticipants[chatId] = {};
    activeParticipants[chatId][userId] = { firstName: userName, username: msg.from?.username || '', lastSeen: Date.now() };

    // Очищаем устаревших участников (TTL: 24 часа)
    const PARTICIPANT_TTL = 24 * 60 * 60 * 1000;
    const now = Date.now();
    for (const uid in activeParticipants[chatId]) {
        if (now - activeParticipants[chatId][uid].lastSeen > PARTICIPANT_TTL) {
            delete activeParticipants[chatId][uid];
        }
    }
    updateAIMood(chatId, userId, userText, callerIsAdmin, isReplyToBot, isMentioned);
    const shouldLoadMemory = shouldUseMemoryContext(userText, isReplyToBot, isMentioned);
    const relevantFacts = shouldLoadMemory
        ? await getRelevantFacts(chatId, userText, userName, Object.values(activeParticipants[chatId]))
        : '';
    const memoryBlock = `\n[МЫСЛИ О ${userName}]\n${relevantFacts}\nВремя (МСК): ${new Date().toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })}\n`;

    const compactMemoryBlock = buildMemoryBlock(userName, relevantFacts);
    const interactionMode = detectInteractionMode(userText, analysis, isReplyToBot);
    const personaLayer = RUNTIME_PERSONA_BLOCK
        + MEMORY_USAGE_POLICY_BLOCK
        + BEHAVIOR_MODE_POLICY_BLOCK
        + OPTIMIZATION_POLICY_BLOCK
        + buildBehaviorProfilePrompt()
        + buildInteractionModePrompt(interactionMode)
        + replyFocusBlock
        + buildCompactPersonaBlock(chatId, dbUser, userName)
        + buildCompactMoodPrompt(chatId, userId);
    const finalPrompt = RUNTIME_SYSTEM_PROMPT + personaLayer + senderContextBlock + compactMemoryBlock;
    const activeTools = shouldEnableAITools(userText, callerIsAdmin, isReplyToBot, isMentioned) ? aiTools : undefined;
    chatHistory[chatId].push({ role: 'user', content: fullContent });
    chatHistory[chatId] = trimHistory(chatHistory[chatId], HISTORY_LIMIT);

    console.log(`🧠 [AI] Ника думает над ответом...`);

    try {
        await bot.sendChatAction(chatId, 'typing');

        // ======== ЗРЕНИЕ ДЛЯ НИКИ (ФОТО/СТИКЕРЫ) ========
        let imageUrl = null;
        try {
            let fileIdToDownload = null;
            if (msg.photo && msg.photo.length > 0) {
                fileIdToDownload = msg.photo[msg.photo.length - 1].file_id;
            } else if (msg.sticker) {
                if (msg.sticker.is_animated || msg.sticker.is_video) {
                    if (msg.sticker.thumbnail) fileIdToDownload = msg.sticker.thumbnail.file_id;
                    else if (msg.sticker.thumb) fileIdToDownload = msg.sticker.thumb.file_id;
                } else {
                    fileIdToDownload = msg.sticker.file_id;
                }
            } else if (msg.video_note && msg.video_note.thumbnail) {
                fileIdToDownload = msg.video_note.thumbnail.file_id;
            }
            if (fileIdToDownload) {
                const tempUrl = await bot.getFileLink(fileIdToDownload);
                const imgRes = await fetch(tempUrl);
                const arrayBuffer = await imgRes.arrayBuffer();
                const buffer = Buffer.from(arrayBuffer);
                const base64 = buffer.toString('base64');

                let mimeType = 'image/jpeg';
                if (tempUrl.endsWith('.webp')) mimeType = 'image/webp';
                else if (tempUrl.endsWith('.png')) mimeType = 'image/png';

                imageUrl = `data:${mimeType};base64,${base64}`;
            }
        } catch (e) {
            console.error("Ошибка загрузки/конвертации картинки:", e.message);
        }

        let currentMessagesFirstCall = selectPromptHistory(chatHistory[chatId], {
            hasReply: Boolean(replyMessage),
            isReplyToBot,
            correctionTurn,
            shortReplyTurn
        });
        if (imageUrl) {
            currentMessagesFirstCall[currentMessagesFirstCall.length - 1].content = [
                { type: "text", text: fullContent },
                { type: "image_url", image_url: { url: imageUrl } }
            ];
        }
        // ===============================================

        let completion;
        try {
            const targetModel = imageUrl ? AI_VISION_MODEL : AI_MODEL;
            const callModel = activeTools ? AI_TOOL_MODEL : targetModel;

            completion = await fetchAIWithTimeout({
                model: callModel,
                messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesFirstCall],
                tools: activeTools,
                max_tokens: FIRST_PASS_MAX_TOKENS,
                temperature: AI_TEMPERATURE_MAIN
            });
        } catch (e) {
            console.error("❌ Модель не справилась:", e.message);
            // Если картинка была и упала, пробуем БЕЗ неё на текстовой модели
            if (imageUrl) {
                console.log("♻️ Пробую отправить запрос БЕЗ картинки...");
                currentMessagesFirstCall[currentMessagesFirstCall.length - 1].content = fullContent;
            }
            const memoryFacts = shouldLoadMemory
                ? (relevantFacts || await getRelevantFacts(
                    chatId,
                    userText,
                    userName,
                    Object.values(activeParticipants[chatId] || {})
                ))
                : '';

            let memoryBlock = '';

            if (memoryFacts && memoryFacts.trim().length > 0) {
                memoryBlock = `\n\nВот что ты помнишь о чате:\n${memoryFacts}`;
            }

            const fallbackPrompt = RUNTIME_SYSTEM_PROMPT
                + RUNTIME_PERSONA_BLOCK
                + MEMORY_USAGE_POLICY_BLOCK
                + BEHAVIOR_MODE_POLICY_BLOCK
                + OPTIMIZATION_POLICY_BLOCK
                + buildBehaviorProfilePrompt()
                + buildInteractionModePrompt(interactionMode)
                + replyFocusBlock
                + buildCompactPersonaBlock(chatId, dbUser, userName)
                + buildCompactMoodPrompt(chatId, userId)
                + senderContextBlock
                + buildMemoryBlock(userName, memoryFacts);

            completion = await fetchAIWithTimeout({
                model: activeTools ? AI_TOOL_MODEL : AI_MODEL,
                messages: [
                    { role: 'system', content: fallbackPrompt },
                    ...currentMessagesFirstCall
                ],
                tools: activeTools,
                max_tokens: FIRST_PASS_MAX_TOKENS,
                temperature: AI_TEMPERATURE_MAIN
            });
        }


        let resp = completion.choices[0].message;
        let rawRes = "";
        let directInjectedData = "";

        // ---> АВАРИЙНЫЙ ПЕРЕХВАТЧИК PYTHON-ГАЛЛЮЦИНАЦИЙ <---
        if (!resp.tool_calls && resp.content && resp.content.includes('default_api.')) {
            console.log('[SYSTEM] Перехват Python-галлюцинации от ИИ!');
            const funcMatch = resp.content.match(/default_api\.([a-zA-Z0-9_]+)\s*\((.*?)\)/s);
            if (funcMatch) {
                const fakeFnName = funcMatch[1];
                let fakeArgs = {};

                const targetMatch = funcMatch[2].match(/target_name=['"]([^'"]+)['"]/);
                if (targetMatch) fakeArgs.target_name = targetMatch[1];

                const amountMatch = funcMatch[2].match(/amount=(\d+)/);
                if (amountMatch) fakeArgs.amount = parseInt(amountMatch[1]);

                const reasonMatch = funcMatch[2].match(/reason=['"]([^'"]+)['"]/);
                if (reasonMatch) fakeArgs.reason = reasonMatch[1];

                resp.tool_calls = [{
                    id: 'call_' + Date.now(),
                    type: 'function',
                    function: { name: fakeFnName, arguments: JSON.stringify(fakeArgs) }
                }];
                resp.content = "Ах ты ж... Сейчас покажу!"; // Стираем мусорный питон-код и ставим заглушку
            }
        }

        if (resp.tool_calls || resp.function_call) {
            chatHistory[chatId].push(resp);
            const calls = resp.tool_calls || [resp.function_call];
            for (const tc of calls) {
                const res = await executeToolCall(tc, chatId, msg.message_id, userName, userId, callerIsAdmin, userHandle);
                const fnName = tc.function ? tc.function.name : tc.name;

                if (resp.tool_calls) {
                    chatHistory[chatId].push({ role: 'tool', tool_call_id: tc.id, name: fnName, content: String(res) });
                } else {
                    chatHistory[chatId].push({ role: 'function', name: fnName, content: String(res) });
                }

                if (shouldExposeToolResultToChat(fnName, res)) {
                    if (!directInjectedData.includes(res)) directInjectedData += `\n\n${res}`;
                }
            }

            let currentMessagesSecondCall = selectPromptHistory(chatHistory[chatId], {
                hasReply: Boolean(replyMessage),
                isReplyToBot,
                correctionTurn,
                shortReplyTurn
            });

            let second;
            try {
                second = await fetchAIWithTimeout({
                    model: activeTools ? AI_TOOL_MODEL : AI_MODEL,
                    messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesSecondCall],
                    tools: activeTools,
                    temperature: AI_TEMPERATURE_TOOL,
                    max_tokens: SECOND_PASS_MAX_TOKENS
                });
            } catch (e2) {
                console.error("❌ Ошибка второго вызова AI (после использования инструмента):", e2.message);
                // Страховка на случай падения второго вызова (с увеличенным таймаутом и резервной моделью)
                try {
                    second = await fetchAIWithTimeout({
                        model: AI_FAILSAFE_MODEL,
                        messages: [{ role: 'system', content: finalPrompt }, ...currentMessagesSecondCall],
                        tools: activeTools,
                        temperature: AI_TEMPERATURE_TOOL,
                        max_tokens: SECOND_PASS_MAX_TOKENS
                    }, 50000); // 50 секунд для второго шанса
                } catch (e3) {
                    console.error("❌ Резервный вызов также упал:", e3.message);
                }
            }

            // ИСПРАВЛЕНИЕ #3: Логика fallback-фраз (заглушек)
            let aiText = second?.choices?.[0]?.message?.content;
            let extraRounds = 0;

            while (second?.choices?.[0]?.message?.tool_calls && extraRounds < 2) {
                extraRounds++;
                const chainedResp = second.choices[0].message;
                chatHistory[chatId].push(chainedResp);

                for (const tc of chainedResp.tool_calls) {
                    const res = await executeToolCall(tc, chatId, msg.message_id, userName, userId, callerIsAdmin, userHandle);
                    const fnName = tc.function ? tc.function.name : tc.name;
                    chatHistory[chatId].push({ role: 'tool', tool_call_id: tc.id, name: fnName, content: String(res) });

                    if (shouldExposeToolResultToChat(fnName, res)) {
                        if (!directInjectedData.includes(res)) directInjectedData += `\n\n${res}`;
                    }
                }

                const chainedMessages = selectPromptHistory(chatHistory[chatId], {
                    hasReply: Boolean(replyMessage),
                    isReplyToBot,
                    correctionTurn,
                    shortReplyTurn
                });
                second = await fetchAIWithTimeout({
                    model: activeTools ? AI_TOOL_MODEL : AI_MODEL,
                    messages: [{ role: 'system', content: finalPrompt }, ...chainedMessages],
                    tools: activeTools,
                    temperature: AI_TEMPERATURE_TOOL,
                    max_tokens: SECOND_PASS_MAX_TOKENS
                }, 50000);
                aiText = second?.choices?.[0]?.message?.content;
            }

            if (!aiText) {
                let isPunishment = false;
                let isUnmute = false;

                for (const c of calls) {
                    const fName = c.function ? c.function.name : c.name;
                    const fArgs = c.function ? c.function.arguments : c.arguments;
                    if (fName === 'moderate_user') {
                        try {
                            const parsed = JSON.parse(fArgs);
                            if (parsed.action === 'mute' || parsed.action === 'warn') isPunishment = true;
                            if (parsed.action === 'unmute') isUnmute = true;
                        } catch (e) { }
                    }
                }

                if (isPunishment) {
                    // Если это был вызов мута/варна, используем крутые фразы
                    const fallbackPhrases = [
                        "Нарушитель изолирован. 💅",
                        "Минус один. 🔨",
                        "Фу, какая гадость... Отправила в бан. 🗑️",
                        "Секундочку... отправляю отдыхать. 💅"
                    ];
                    aiText = fallbackPhrases[Math.floor(Math.random() * fallbackPhrases.length)];
                } else if (isUnmute) {
                    // Если это был размут
                    const unmutePhrases = [
                        "Всё, свободен. Но я слежу за тобой! 👁️",
                        "Ладно, живи. Сняла мут. 💅",
                        "Окей, размутила. Веди себя хорошо! 😇"
                    ];
                    aiText = unmutePhrases[Math.floor(Math.random() * unmutePhrases.length)];
                } else {
                    // Если это был поиск фактов, профиля или другой обычный запрос
                    aiText = buildFailureReply(chatId, 'generic');
                }
            }

            if (aiText && isCompletionTruncated(second, aiText)) {
                const continuationBaseMessages = selectPromptHistory(chatHistory[chatId], {
                    hasReply: Boolean(replyMessage),
                    isReplyToBot,
                    correctionTurn,
                    shortReplyTurn
                });
                const continuationText = await continueAssistantReply(finalPrompt, continuationBaseMessages, aiText);
                aiText = mergeContinuationText(aiText, continuationText);
            }

            if (directInjectedData) {
                const profileIndex = aiText.indexOf('=== ПРОФИЛЬ');
                if (profileIndex !== -1) aiText = aiText.substring(0, profileIndex).trim();

                const searchIndex = aiText.indexOf('=== РЕЗУЛЬТАТЫ');
                if (searchIndex !== -1) aiText = aiText.substring(0, searchIndex).trim();

                rawRes = aiText + directInjectedData;
            } else {
                rawRes = aiText;
            }

        } else {
            let aiText = resp.content || buildFailureReply(chatId, 'generic');

            if (resp.content && isCompletionTruncated(completion, resp.content)) {
                const continuationText = await continueAssistantReply(finalPrompt, currentMessagesFirstCall, resp.content);
                aiText = mergeContinuationText(resp.content, continuationText);
            }

            rawRes = aiText;
        }

        function formatAIOutput(text) {
            // Принудительно удаляем системные теги медиа
            let withoutMediaTags = text.replace(/\[Стикер:[^\]]*\]/gi, '').replace(/\[Картинка\/Фото\]/gi, '').replace(/\[Видео\]/gi, '').replace(/\[Голосовое сообщение\]/gi, '').trim();

            if (!withoutMediaTags && text.length > 0) {
                withoutMediaTags = "[EMO:RANDOM]";
            }

            let clean = withoutMediaTags.replace(/&#039;/g, "'").replace(/&quot;/g, '"');
            clean = rewriteProviderRefusal(clean, chatId, userText);
            clean = clean.replace(/\s*\[АДМИН\]/gi, '');
            clean = clean.replace(/\[СИСТЕМНО:[^\]]*\]/gi, '');
            clean = ensureCharacterfulFallback(stripAIDisclaimer(stripInternalPromptLeak(clean)));
            let escaped = clean.replace(/</g, '&lt;').replace(/>/g, '&gt;');

            escaped = escaped.replace(/&lt;b&gt;/gi, '<b>').replace(/&lt;\/b&gt;/gi, '</b>');
            escaped = escaped.replace(/&lt;i&gt;/gi, '<i>').replace(/&lt;\/i&gt;/gi, '</i>');
            escaped = escaped.replace(/&lt;u&gt;/gi, '<u>').replace(/&lt;\/u&gt;/gi, '</u>');
            escaped = escaped.replace(/&lt;s&gt;/gi, '<s>').replace(/&lt;\/s&gt;/gi, '</s>');

            let final = escaped.replace(/\[EMO:RANDOM\]/gi, () => {
                if (premiumEmojiList.length > 0) return `<tg-emoji emoji-id="${premiumEmojiList[Math.floor(Math.random() * premiumEmojiList.length)]}">✨</tg-emoji>`;
                return "✨";
            });

            // Парсим корректные ID эмодзи (состоящие из цифр)
            final = final.replace(/\[EMO:([0-9]+):(.*?)\]/g, (match, id, emoji) => `<tg-emoji emoji-id="${id}">${emoji}</tg-emoji>`);

            // Очищаем галлюцинации
            final = final.replace(/\[EMO:[^\]]+\]/gi, () => {
                if (premiumEmojiList.length > 0) return `<tg-emoji emoji-id="${premiumEmojiList[Math.floor(Math.random() * premiumEmojiList.length)]}">✨</tg-emoji>`;
                return "✨";
            });

            return final;
        }

        const finalOutput = formatAIOutput(rawRes);
        console.log(`✨ [CHAT OUT] Ника: ${finalOutput.replace(/<[^>]*>/g, '').substring(0, 60)}...`);
        await safeSendMessage(chatId, finalOutput, replyIdForBot);

        chatHistory[chatId].push({ role: 'assistant', content: finalOutput.replace(/<[^>]*>/g, '') });

    } catch (e) {
        console.error('AI Error:', e.message);
        if (e.message === 'TIMEOUT') {
            await safeSendMessage(chatId, buildFailureReply(chatId, 'timeout'), msg.message_id);
        }
    }
}

async function emergencyMemorySave() {
    console.log("🚨 [SYSTEM] Получен сигнал выключения! Экстренно спасаю буферы памяти...");
    const promises = [];
    for (const chatId in extractionBuffer) {
        if (extractionBuffer[chatId] && extractionBuffer[chatId].length > 0) {
            console.log(`[MEMORY] Спасаю ${extractionBuffer[chatId].length} не сохраненных сообщений для чата ${chatId}...`);
            const p = extractAndSaveFacts(
                chatId,
                extractionBuffer[chatId].join('\n'),
                Object.values(activeParticipants[chatId] || {}).map(p => p.firstName)
            );
            promises.push(p);
        }
    }

    if (promises.length > 0) {
        await Promise.race([
            Promise.all(promises),
            new Promise(resolve => setTimeout(resolve, 5000))
        ]);
        console.log("✅ [SYSTEM] Экстренное сохранение завершено.");
    }
    process.exit(0);
}

process.on('SIGTERM', emergencyMemorySave);
process.on('SIGINT', emergencyMemorySave);

module.exports = { handleAIChat, AI_NAME };
