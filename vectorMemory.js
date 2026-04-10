const OpenAI = require('openai');
const {
    insertKnowledge,
    searchKnowledge,
    checkFactExists,
    searchKnowledgeByText,
    getRecentKnowledge,
    upsertMemorySummary,
    getRecentMemorySummaries,
    weakenStaleKnowledge,
    transliterate
} = require('./database');

console.log('✅ [VECTOR MEMORY] Модуль улучшенной памяти подключён');

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const EXTRACTOR_MODEL = process.env.MEMORY_EXTRACTOR_MODEL || process.env.AI_MODEL || 'google/gemini-2.5-flash-lite';
const EMBEDDING_MODEL = process.env.EMBEDDING_MODEL || 'text-embedding-3-small';
const AI_BASE_URL = process.env.AI_BASE_URL || process.env.OPENAI_BASE_URL || 'https://polza.ai/api/v1';

const MIN_HISTORY_LENGTH = 140;
const MAX_FACTS_PER_BATCH = 5;
const MAX_FACTS_IN_CONTEXT = 6;
const MAX_KEYWORDS = 4;
const MAX_SUMMARIES_IN_CONTEXT = 2;
const AI_TIMEOUT_MS = 30000;
const MEMORY_EXTRACT_TIMEOUT_MS = Number(process.env.MEMORY_EXTRACT_TIMEOUT_MS || 22000);
const EMBEDDING_LOG_COOLDOWN_MS = 5 * 60 * 1000;
const MEMORY_EXTRACT_INPUT_LIMIT = Number(process.env.MEMORY_EXTRACT_INPUT_LIMIT || 2200);
const MEMORY_EXTRACT_MAX_TOKENS = Number(process.env.MEMORY_EXTRACT_MAX_TOKENS || 320);
const EXPECTED_EMBEDDING_DIMENSION = 1536;

let lastEmbeddingWarningAt = 0;

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: AI_BASE_URL,
});

async function withTimeout(promise, ms = AI_TIMEOUT_MS) {
    const timeout = new Promise((_, reject) => {
        setTimeout(() => reject(new Error('AI_TIMEOUT')), ms);
    });
    return Promise.race([promise, timeout]);
}

function logEmbeddingWarning(message, extra = '') {
    const now = Date.now();
    if (now - lastEmbeddingWarningAt < EMBEDDING_LOG_COOLDOWN_MS) return;
    lastEmbeddingWarningAt = now;
    console.error(`[VECTOR] ${message}${extra ? `: ${extra}` : ''}`);
}

function extractEmbeddingVector(response) {
    if (!response) return null;
    if (Array.isArray(response?.data) && Array.isArray(response.data[0]?.embedding)) return response.data[0].embedding;
    if (Array.isArray(response?.embedding)) return response.embedding;
    if (Array.isArray(response?.data?.embedding)) return response.data.embedding;
    if (Array.isArray(response?.embeddings?.[0]?.embedding)) return response.embeddings[0].embedding;
    if (Array.isArray(response?.result?.data?.[0]?.embedding)) return response.result.data[0].embedding;
    return null;
}

function sanitizeEmbeddingVector(embedding, sourceLabel = EMBEDDING_MODEL) {
    if (!Array.isArray(embedding)) return null;
    if (embedding.length !== EXPECTED_EMBEDDING_DIMENSION) {
        logEmbeddingWarning(
            'Пропускаю embedding',
            `ожидал ${EXPECTED_EMBEDDING_DIMENSION}, получил ${embedding.length} у модели ${sourceLabel}`
        );
        return null;
    }
    return embedding;
}

function normalizeName(value) {
    return String(value || '')
        .replace(/Чатик 🫐 Nika_grdt 👾/gi, 'Ника')
        .replace(/^channel$/i, 'Ника')
        .replace(/^канал$/i, 'Ника')
        .replace(/^ника \(канал\)$/i, 'Ника')
        .trim();
}

function normalizeText(value) {
    return String(value || '')
        .replace(/\s+/g, ' ')
        .trim();
}

function clamp(num, min, max) {
    return Math.max(min, Math.min(max, Number(num)));
}

function getSummaryPeriodKey(date = new Date()) {
    return date.toISOString().slice(0, 10);
}

function getStemLocal(word) {
    if (!word || word.length < 3) return word;
    return word.toLowerCase()
        .replace(/[уаеяяюииыо]$/i, '')
        .replace(/(ов|ев|ий|ый|ые|ие|ах|ях|ом|ем|ам|у|е|а|я)$/i, '')
        .replace(/(s|es|ed|ing)$/i, '');
}

function buildFactText(item) {
    if (item.kind === 'relation') {
        return `СВЯЗЬ: ${item.subject} -> ${item.relation} -> ${item.object}`;
    }
    return `УЗЕЛ: ${item.subject} | ${item.attribute}: ${item.value}`;
}

function isSexualMemoryContent(text) {
    const normalized = normalizeText(text).toLowerCase();
    if (!normalized) return false;

    const blockedPatterns = [
        'бдсм', 'bdsm', 'кинк', 'kink', 'секс', 'sex', 'эрот', 'фетиш', 'fetish',
        'roleplay 18', 'ролевые игры', '50 оттен', 'грубости', 'доминир', 'сабмис',
        'nsfw', 'порно', 'porn', 'хентай', 'nudes', 'нюдс', 'флиртует', 'испытывает симпатию'
    ];

    return blockedPatterns.some(pattern => normalized.includes(pattern));
}

function shouldKeepMemoryFact(fact) {
    if (!fact) return false;

    if (typeof fact.fact === 'string') {
        return !isSexualMemoryContent(fact.fact);
    }

    const mergedText = [
        fact.subject,
        fact.attribute,
        fact.value,
        fact.relation,
        fact.object,
        fact.meta?.evidence
    ].filter(Boolean).join(' ');

    return !isSexualMemoryContent(mergedText);
}

function isSpeculativeOrCreepyMemory(text) {
    const normalized = normalizeText(text).toLowerCase();
    if (!normalized) return false;

    const blockedPatterns = [
        'манипул', 'токсичн', 'сталкер', 'одержим', 'ревн', 'завиду', 'одинок',
        'любит внимание', 'цепляется за', 'скрытая группа', 'заговор', 'ведом',
        'опасный человек', 'плохой человек', 'по сути человек', 'на самом деле',
        'приходит только из за', 'ходит за', 'следит за', 'влюблен', 'влюблена',
        'пара', 'отношениях с', 'хочет внимания', 'обижен на', 'обижена на',
        'манипулятор', 'абьюз', 'абьюзер'
    ];

    return blockedPatterns.some(pattern => normalized.includes(pattern));
}

function isLowValueEphemeralMemory(text) {
    const normalized = normalizeText(text).toLowerCase();
    if (!normalized) return false;

    const stableSignal = /\b(часто|обычно|регулярно|всегда|постоянно|предпочита|любит|нравит|после смен|в выходн|кажд(ый|ое|ую) день|каждый раз)\b/i.test(normalized);
    if (stableSignal) return false;

    const blockedRegexes = [
        /\b(пошел|иду|щас|сейчас)\s+спать\b/i,
        /\b(пошел|пойду)\s+в\s+душ\b/i,
        /\b(ору|лол|ахах|хаха|ржу)\b/i,
        /\bсегодня\s+(хочу|буду)\b/i,
        /\bпотом\s+зайду\b/i,
        /^\s*(ща|щас|ладно)\s*$/i
    ];

    return blockedRegexes.some((re) => re.test(normalized));
}

function convertExtractedFact(rawFact) {
    if (!rawFact || typeof rawFact !== 'object') return null;

    const kind = rawFact.kind === 'relation' ? 'relation' : 'attribute';
    const subject = normalizeName(rawFact.subject);
    if (!subject) return null;

    if (kind === 'relation') {
        const relation = normalizeText(rawFact.relation);
        const object = normalizeName(rawFact.object);
        if (!relation || !object) return null;

        return {
            factType: 'relation',
            kind,
            subject,
            relation,
            object,
            confidence: clamp(rawFact.confidence || 0.62, 0.35, 0.95),
            status: 'candidate',
            meta: {
                evidence: normalizeText(rawFact.evidence || ''),
                extractor: 'memory_v2',
            }
        };
    }

    const attribute = normalizeText(rawFact.attribute || 'факт');
    const value = normalizeText(rawFact.value);
    if (!attribute || !value) return null;

    return {
        factType: 'attribute',
        kind,
        subject,
        attribute,
        value,
        confidence: clamp(rawFact.confidence || 0.6, 0.35, 0.95),
        status: 'candidate',
        meta: {
            evidence: normalizeText(rawFact.evidence || ''),
            extractor: 'memory_v2',
        }
    };
}

function normalizeStoredFactShape(fact) {
    if (!fact) return null;
    if (fact.fact) {
        const text = normalizeText(fact.fact);
        return text ? { ...fact, fact: text } : null;
    }

    if (fact.kind === 'relation') {
        const subject = normalizeName(fact.subject);
        const relation = normalizeText(fact.relation);
        const object = normalizeName(fact.object);
        if (!subject || !relation || !object) return null;
        return { ...fact, subject, relation, object };
    }

    const subject = normalizeName(fact.subject);
    const attribute = normalizeText(fact.attribute);
    const value = normalizeText(fact.value);
    if (!subject || !attribute || !value) return null;
    return { ...fact, subject, attribute, value };
}

function classifyMemoryRecord(fact) {
    const factText = fact?.fact || buildFactText(fact);
    const normalized = normalizeText(factText).toLowerCase();

    if (isSpeculativeOrCreepyMemory(normalized) || isLowValueEphemeralMemory(normalized)) {
        return 'discard';
    }

    if (/\b(часто|обычно|регулярно|редко)\b/i.test(normalized)) {
        return 'pattern';
    }

    return 'fact';
}

function finalizeMemoryRecord(fact) {
    const normalizedFact = normalizeStoredFactShape(fact);
    if (!normalizedFact) return null;

    const classification = classifyMemoryRecord(normalizedFact);
    if (classification === 'discard') return null;

    const nextConfidence = classification === 'pattern'
        ? Math.min(Number(normalizedFact.confidence || 0.58), 0.74)
        : Number(normalizedFact.confidence || 0.58);

    return {
        ...normalizedFact,
        confidence: nextConfidence,
        status: classification === 'pattern'
            ? 'candidate'
            : (normalizedFact.status || 'candidate'),
        meta: {
            ...(normalizedFact.meta || {}),
            memory_kind: classification
        }
    };
}

function fallbackFactsFromText(rawContent) {
    const fallbackFacts = [];
    const regex = /(УЗЕЛ:|СВЯЗЬ:)[^"\\]+/gi;
    let match;

    while ((match = regex.exec(rawContent || '')) !== null) {
        const extracted = normalizeText(match[0]);
        if (extracted.length > 10 && !extracted.endsWith('УЗЕЛ:')) {
            fallbackFacts.push({
                factType: extracted.startsWith('СВЯЗЬ:') ? 'relation' : 'fact',
                fact: extracted,
                confidence: 0.55,
                status: 'candidate',
                meta: { extractor: 'fallback_regex' }
            });
        }
    }

    return fallbackFacts;
}

async function summarizeDialogue(historyText, participants = []) {
    const cleanHistory = normalizeText(historyText).slice(0, 2600);
    if (!cleanHistory) return '';

    const participantLine = participants.length
        ? `Участники: ${participants.map(normalizeName).filter(Boolean).join(', ')}`
        : '';

    try {
        const completion = await withTimeout(
            openai.chat.completions.create({
                model: EXTRACTOR_MODEL,
                messages: [
                    {
                        role: 'system',
                        content: `Сделай очень короткую сводку фрагмента чата для памяти бота.
Нужно 2-4 короткие строки:
- кто обсуждался;
- какие устойчивые темы/отношения всплыли;
- без воды, без шуток, без выдумки.
Пиши по-русски обычным текстом.`
                    },
                    { role: 'user', content: `${participantLine}\n\n${cleanHistory}` }
                ],
                temperature: 0,
                max_tokens: 180
            }),
            MEMORY_EXTRACT_TIMEOUT_MS
        );

        return normalizeText(completion.choices[0]?.message?.content || '').slice(0, 500);
    } catch (e) {
        if (e.message !== 'AI_TIMEOUT') {
            console.error('[MEMORY] Ошибка summary:', e.message);
        }
        return '';
    }
}

async function createEmbedding(text) {
    if (!text || text.trim().length < 3) return null;
    try {
        const res = await withTimeout(
            openai.embeddings.create({ model: EMBEDDING_MODEL, input: text.slice(0, 512) })
        );
        const embedding = sanitizeEmbeddingVector(extractEmbeddingVector(res));
        if (embedding) return embedding;
        const responseKeys = Object.keys(res || {}).slice(0, 8).join(', ');
        logEmbeddingWarning('Ошибка эмбеддинга', `неожиданный формат ответа у модели ${EMBEDDING_MODEL}${responseKeys ? ` | keys: ${responseKeys}` : ''}`);
        return null;
    } catch (e) {
        if (e.message !== 'AI_TIMEOUT') {
            console.error('[VECTOR] Ошибка эмбеддинга:', e.message);
        }
        return null;
    }
}

async function extractAndSaveFacts(chatId, historyText, participants = []) {
    if (!historyText || historyText.trim().length < MIN_HISTORY_LENGTH) {
        console.log(`[MEMORY] Диалог слишком короткий (${historyText?.length || 0} симв.) — пропускаю`);
        return;
    }

    try {
        const cleanHistory = historyText
            .replace(/Чатик 🫐 Nika_grdt 👾/gi, 'Ника')
            .replace(/^(?:Channel|Канал):\s/gmi, 'Ника: ');

        const compactHistory = cleanHistory.slice(0, MEMORY_EXTRACT_INPUT_LIMIT);
        const participantInfo = participants.length > 0
            ? 'Известные имена: ' + participants.map(normalizeName).filter(Boolean).join(', ')
            : '';

        const prompt = `Извлеки только устойчивые факты для долговременной памяти чата.

Сохраняй только то, что может пригодиться позже:
- кто кому кем приходится;
- устойчивые предпочтения, роли, прозвища, связи;
- биографические факты, если они звучат уверенно.

Не сохраняй:
- разовые действия, шутки момента, эмоции, планы на вечер;
- мусор вроде "участник диалога";
- сомнительные и неявные факты.

Если в диалоге встречаются Channel, Канал или Ника (канал), это Ника.
Следи за направлением связи: "Саня -> фанат -> Ника", а не наоборот.

Верни JSON:
{
  "facts": [
    {
      "kind": "attribute",
      "subject": "имя",
      "attribute": "роль|интерес|факт|прозвище|привычка",
      "value": "значение",
      "confidence": 0.0,
      "evidence": "короткая цитата или пересказ"
    },
    {
      "kind": "relation",
      "subject": "кто",
      "relation": "отношение",
      "object": "к кому",
      "confidence": 0.0,
      "evidence": "короткая цитата или пересказ"
    }
  ]
}`;

        const hardenedPrompt = `${prompt}

Дополнительные правила:
- Не делай психологических диагнозов и ярлыков о человеке.
- Не утверждай романтические связи, ревность, одержимость, манипуляции и скрытые мотивы как факт.
- Если можно сформулировать как наблюдение или паттерн, выбирай наблюдение или паттерн.
- Разрешено сохранять легкие бытовые привычки и предпочтения (сон, кофе, еда, игры), если это выглядит как повторяющийся паттерн.
- Не сохраняй лишнюю социальную слежку, которая не помогает модерации, контексту или персонализации.
`;

        const completion = await withTimeout(
            openai.chat.completions.create({
                model: EXTRACTOR_MODEL,
                messages: [
                    { role: 'system', content: hardenedPrompt },
                    { role: 'user', content: `${participantInfo}\n\nДиалог:\n${cleanHistory.slice(0, 3200)}` }
                ],
                temperature: 0,
                max_tokens: MEMORY_EXTRACT_MAX_TOKENS,
                response_format: { type: 'json_object' }
            }),
            MEMORY_EXTRACT_TIMEOUT_MS
        );

        const rawContent = completion.choices[0]?.message?.content || '{}';
        let parsed;

        try {
            parsed = JSON.parse(rawContent);
        } catch {
            parsed = { facts: fallbackFactsFromText(rawContent) };
        }

        const rawFactsCount = Array.isArray(parsed.facts) ? parsed.facts.length : 0;
        const extractedFacts = Array.isArray(parsed.facts)
            ? parsed.facts
                .map(convertExtractedFact)
                .map(finalizeMemoryRecord)
                .filter(Boolean)
                .filter(shouldKeepMemoryFact)
                .slice(0, MAX_FACTS_PER_BATCH)
            : [];

        if (extractedFacts.length === 0) {
            const fallbackFacts = fallbackFactsFromText(rawContent)
                .map(finalizeMemoryRecord)
                .filter(Boolean)
                .filter(shouldKeepMemoryFact)
                .slice(0, MAX_FACTS_PER_BATCH);
            if (fallbackFacts.length === 0) {
                console.log(`[MEMORY] Нет пригодных фактов: raw=${rawFactsCount} parsed=0 fallback=0`);
            }
            for (const fact of fallbackFacts) {
                const exists = await checkFactExists(chatId, fact);
                if (exists) {
                    console.log(`[MEMORY] fallback skipped existing: ${normalizeText(fact.fact || buildFactText(fact)).slice(0, 120)}`);
                    continue;
                }
                const embedding = await createEmbedding(fact.fact);
                const result = await insertKnowledge(chatId, fact, embedding);
                if (result?._memoryAction) {
                    console.log(`[MEMORY] fallback ${result._memoryAction}: ${normalizeText(fact.fact || buildFactText(fact)).slice(0, 120)}`);
                } else {
                    console.warn(`[MEMORY] fallback failed: ${normalizeText(fact.fact || buildFactText(fact)).slice(0, 120)}`);
                }
            }
            return;
        }

        let created = 0;
        let updated = 0;
        let failed = 0;
        let saved = 0;
        let skippedExisting = 0;

        for (const fact of extractedFacts) {
            const factText = buildFactText(fact);
            if (!factText || factText.includes('участник диалога')) continue;

            const exists = await checkFactExists(chatId, fact);
            if (!exists) {
                const embedding = await createEmbedding(factText);
                const duplicates = embedding
                    ? await searchKnowledge(chatId, embedding, 1, 0.9, {
                        statuses: ['confirmed', 'candidate'],
                        minConfidence: 0.45
                    })
                    : [];

                if (duplicates.length > 0) {
                    const result = await insertKnowledge(chatId, fact, embedding);
                    if (result?._memoryAction === 'created' || result?._memoryAction === 'fallback_created') {
                        created++;
                        saved++;
                    }
                    else if (result?._memoryAction === 'updated') {
                        updated++;
                        saved++;
                    }
                    else {
                        failed++;
                        console.warn(`[MEMORY] failed to persist: ${normalizeText(factText).slice(0, 160)}`);
                    }
                    continue;
                }
            }
            else {
                skippedExisting++;
            }

            const embedding = await createEmbedding(factText);
            const result = await insertKnowledge(chatId, fact, embedding);
            if (result?._memoryAction === 'created' || result?._memoryAction === 'fallback_created') {
                created++;
                saved++;
            }
            else if (result?._memoryAction === 'updated') {
                updated++;
                saved++;
            }
            else {
                failed++;
                console.warn(`[MEMORY] failed to persist: ${normalizeText(factText).slice(0, 160)}`);
            }
        }

        if (created > 0 || updated > 0 || failed > 0) {
            console.log(`[MEMORY] Сохранено ${saved}/${extractedFacts.length} фактов`);
        }

        if (created > 0 || updated > 0 || failed > 0) {
            saved = created + updated;
            console.log(`[MEMORY DETAIL] created=${created} updated=${updated} failed=${failed} total=${extractedFacts.length}`);
        }
        else if (skippedExisting > 0) {
            console.log(`[MEMORY] Все факты уже известны: skipped_existing=${skippedExisting} total=${extractedFacts.length}`);
        }
        else {
            console.log(`[MEMORY] Факты извлечены, но не записаны: parsed=${extractedFacts.length} failed=${failed}`);
        }

        const summary = await summarizeDialogue(cleanHistory, participants);
        if (summary) {
            await upsertMemorySummary(chatId, getSummaryPeriodKey(), summary, 1);
        }

        const staleBefore = new Date(Date.now() - 1000 * 60 * 60 * 24 * 21).toISOString();
        await weakenStaleKnowledge(chatId, {
            staleBeforeIso: staleBefore,
            limit: 20,
            maxTimesSeen: 3,
            maxConfidence: 0.76
        });
    } catch (e) {
        if (e.message === 'AI_TIMEOUT') {
            console.warn('[MEMORY] Таймаут экстракции — пропускаю');
        } else {
            console.error('[MEMORY] Ошибка экстракции:', e.message);
        }
    }
}

async function getRelevantFacts(chatId, userMessage, userName = '', activeParticipants = []) {
    try {
        if (!userMessage || userMessage.trim().length < 3) return '';

        const cleanMessage = normalizeText(userMessage.replace(/Чатик 🫐 Nika_grdt 👾/gi, 'Ника'));
        const cleanUserName = normalizeName(userName);
        const allFoundFacts = new Map();

        const stopWords = new Set([
            'меня', 'тебя', 'чтобы', 'какой', 'такой', 'зачем', 'почему',
            'когда', 'будет', 'очень', 'просто', 'может', 'нужно', 'хочу', 'люблю',
            'тебе', 'этот', 'этого', 'этой', 'этом', 'всего', 'тоже',
            'если', 'даже', 'вроде', 'опять', 'снова', 'уже', 'ещё', 'всё'
        ]);

        const addFact = (fact, source, relevance = 0.5, extra = {}) => {
            if (!fact) return;
            if (isSpeculativeOrCreepyMemory(fact) || isLowValueEphemeralMemory(fact)) return;
            const memoryKind = extra?.meta?.memory_kind || 'fact';
            const existing = allFoundFacts.get(fact);
            const score = {
                source,
                relevance,
                confidence: extra.confidence ?? 0.6,
                timesSeen: extra.timesSeen ?? 1,
                status: extra.status || 'confirmed',
                memoryKind
            };

            if (!existing || existing.relevance < relevance) {
                allFoundFacts.set(fact, score);
            }
        };

        const embedding = await createEmbedding(cleanMessage);
        if (embedding) {
            const semanticResults = await searchKnowledge(chatId, embedding, 5, 0.56, {
                statuses: ['confirmed'],
                minConfidence: 0.62
            });
            semanticResults.forEach(item => {
                addFact(item.fact, 'semantic', item.similarity || 0.55, item);
            });
        }

        if (cleanUserName) {
            const recentResults = await getRecentKnowledge(chatId, cleanUserName, 4, {
                statuses: ['confirmed', 'candidate'],
                minConfidence: 0.58
            });
            recentResults.forEach(item => {
                addFact(item.fact, 'recent', 0.68, item);
            });
        }

        const searchTerms = new Set();
        const pushTerm = (value) => {
            const clean = normalizeName(value).replace('@', '').toLowerCase();
            if (!clean || clean.length < 2) return;
            searchTerms.add(clean);
            const stem = getStemLocal(clean);
            if (stem && stem.length >= 3) searchTerms.add(stem);
            const trans = transliterate(clean);
            if (trans && trans.length >= 3) searchTerms.add(trans.toLowerCase());
        };

        pushTerm(cleanUserName);
        activeParticipants.slice(0, 3).forEach(person => pushTerm(person.firstName || person.username || ''));

        const potentialNames = cleanMessage.match(/([А-Я][а-яё]{2,}|@[a-zA-Z0-9_]+)/g) || [];
        potentialNames.slice(0, 5).forEach(name => pushTerm(name.replace('@', '')));

        for (const term of Array.from(searchTerms).slice(0, 5)) {
            if (term.length < 3) continue;
            const rows = await searchKnowledgeByText(chatId, term, 3, {
                statuses: ['confirmed', 'candidate'],
                minConfidence: 0.58
            });
            rows.forEach(item => addFact(item.fact, 'subject', 0.62, item));
        }

        const keywords = cleanMessage.split(/\s+/)
            .map(word => word.replace(/[.,!?;:()]/g, '').toLowerCase())
            .filter(word => word.length > 4 && !stopWords.has(word))
            .slice(0, MAX_KEYWORDS);

        for (const keyword of keywords) {
            const rows = await searchKnowledgeByText(chatId, getStemLocal(keyword), 1, {
                statuses: ['confirmed'],
                minConfidence: 0.64
            });
            rows.forEach(item => addFact(item.fact, 'keyword', 0.45, item));
        }

        const summaries = await getRecentMemorySummaries(chatId, MAX_SUMMARIES_IN_CONTEXT);

        if (allFoundFacts.size === 0 && summaries.length === 0) return '';

        const priorityOrder = { semantic: 0, recent: 1, subject: 2, keyword: 3 };
        const ranked = Array.from(allFoundFacts.entries())
            .sort(([, left], [, right]) => {
                const kindDiff = (left.memoryKind === 'fact' ? 0 : 1) - (right.memoryKind === 'fact' ? 0 : 1);
                if (kindDiff !== 0) return kindDiff;

                const priorityDiff = priorityOrder[left.source] - priorityOrder[right.source];
                if (priorityDiff !== 0) return priorityDiff;

                const confidenceDiff = (right.confidence || 0) - (left.confidence || 0);
                if (confidenceDiff !== 0) return confidenceDiff;

                const timesSeenDiff = (right.timesSeen || 0) - (left.timesSeen || 0);
                if (timesSeenDiff !== 0) return timesSeenDiff;

                return (right.relevance || 0) - (left.relevance || 0);
            });

        const factLines = ranked
            .filter(([fact]) => fact.length <= 220)
            .slice(0, MAX_FACTS_IN_CONTEXT)
            .map(([fact, meta]) => {
                const marker = meta.memoryKind === 'pattern'
                    ? '≈ '
                    : (meta.status === 'candidate' ? '~- ' : '- ');
                return `${marker}${fact}`;
            })
            .join('\n');

        const summaryLines = summaries
            .map(item => normalizeText(item.summary))
            .filter(Boolean)
            .slice(0, MAX_SUMMARIES_IN_CONTEXT)
            .map(text => `[SUMMARY] ${text}`)
            .join('\n');

        return [factLines, summaryLines].filter(Boolean).join('\n');
    } catch (e) {
        console.error('[MEMORY] Ошибка поиска фактов:', e.message);
        return '';
    }
}

async function forgetFact(chatId, query) {
    if (!query || query.trim() === '') return false;
    try {
        const embedding = await createEmbedding(query);
        if (!embedding) return false;

        const results = await searchKnowledge(chatId, embedding, 1, 0.72, {
            statuses: ['confirmed', 'candidate'],
            minConfidence: 0.4
        });

        if (results && results.length > 0) {
            const target = results[0];
            if (target.id) {
                const success = await require('./database').deleteKnowledge(chatId, target.id);
                return success ? target.fact : false;
            }
        }
    } catch (e) {
        console.error('[MEMORY] Ошибка удаления факта:', e.message);
    }
    return false;
}

module.exports = {
    extractAndSaveFacts,
    getRelevantFacts,
    createEmbedding,
    forgetFact
};
