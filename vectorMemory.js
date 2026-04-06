const OpenAI = require('openai');
const {
    insertKnowledge,
    searchKnowledge,
    checkFactExists,
    searchKnowledgeByText,
    getRecentKnowledge,
    transliterate
} = require('./database');

console.log('✅ [VECTOR MEMORY] Модуль графовой памяти (LightRAG v3.1) подключён');

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
// ИСПРАВЛЕНО: Используем реальный AI_MODEL из env, а не жёсткий gpt-4o-mini
const EXTRACTOR_MODEL = process.env.AI_MODEL || 'google/gemini-2.5-flash-lite';
// Эмбеддинги всегда через OpenAI-совместимый endpoint
const EMBEDDING_MODEL = 'text-embedding-3-small';

// Минимальная длина диалога для запуска экстракции (символов)
const MIN_HISTORY_LENGTH = 80;
// Макс. фактов за одну экстракцию
const MAX_FACTS_PER_BATCH = 10;
// Таймаут AI-вызовов
const AI_TIMEOUT_MS = 30000;

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: 'https://polza.ai/api/v1',
});

// ─────────────────────────────────────────────────────────────
// ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
// ─────────────────────────────────────────────────────────────

async function withTimeout(promise, ms = AI_TIMEOUT_MS) {
    const timeout = new Promise((_, reject) =>
        setTimeout(() => reject(new Error('AI_TIMEOUT')), ms)
    );
    return Promise.race([promise, timeout]);
}

async function createEmbedding(text) {
    if (!text || text.trim().length < 3) return null;
    try {
        const res = await withTimeout(
            openai.embeddings.create({ model: EMBEDDING_MODEL, input: text.slice(0, 512) })
        );
        return res.data[0].embedding;
    } catch (e) {
        if (e.message !== 'AI_TIMEOUT') {
            console.error('[VECTOR] Ошибка эмбеддинга:', e.message);
        }
        return null;
    }
}

// ─────────────────────────────────────────────────────────────
// ЭКСТРАКЦИЯ ФАКТОВ ИЗ ДИАЛОГА
// ─────────────────────────────────────────────────────────────

async function extractAndSaveFacts(chatId, historyText, participants = []) {
    // Защита от пустых и слишком коротких диалогов
    if (!historyText || historyText.trim().length < MIN_HISTORY_LENGTH) {
        console.log(`[MEMORY] Диалог слишком короткий (${historyText?.length || 0} симв.) — пропускаю`);
        return;
    }

    try {
        let cleanHistory = historyText.replace(/Чатик 🫐 Nika_grdt 👾/gi, 'Ника');

        const participantInfo = participants.length > 0
            ? 'Известные имена (для справки): ' + participants
                .map(p => p.replace(/Чатик 🫐 Nika_grdt 👾/gi, 'Ника'))
                .join(', ')
            : '';

        const prompt = `Ты — ядро долгосрочной памяти чат-бота (LightRAG).
ТВОЯ ЗАДАЧА: Находить ПОСТОЯННЫЕ факты о людях и их отношениях. Если фактов нет — вернуть пустой массив.

[АБСОЛЮТНЫЕ ЗАПРЕТЫ]:
1. ❌ Никогда не пиши "АТРИБУТ: участник диалога" — это мусор
2. ❌ Никогда не сохраняй временные действия: "играет", "смотрит", "пойдёт в зал", "устал", "сфоткал"
3. ❌ Никогда не сохраняй команды боту, обсуждение настроек, логов, профилей
4. ❌ Никогда не выдумывай факты. Сомневаешься — пропускай
5. ❌ Максимум ${MAX_FACTS_PER_BATCH} фактов за раз

[РАЗРЕШЁННЫЙ ФОРМАТ]:
✅ УЗЕЛ: [Имя] | АТРИБУТ: [Постоянный факт: профессия, возраст, хобби, болезнь, увлечение]
✅ СВЯЗЬ: [Имя1] -> [отношение] -> [Имя2]

[ВЫВОД JSON]:
{
  "reasoning": "краткое объяснение (1-2 предложения)",
  "facts": ["УЗЕЛ: ...", "СВЯЗЬ: ..."]
}

Примеры ХОРОШИХ фактов: "УЗЕЛ: алина | АТРИБУТ: занимается йогой"
Примеры ПЛОХИХ фактов: "УЗЕЛ: вася | АТРИБУТ: пойдёт в магазин", "СВЯЗЬ: вася -> спросил -> нику"`;

        const completion = await withTimeout(
            openai.chat.completions.create({
                model: EXTRACTOR_MODEL,
                messages: [
                    { role: 'system', content: prompt },
                    { role: 'user', content: `${participantInfo}\n\nДиалог:\n${cleanHistory.slice(0, 3000)}` }
                ],
                temperature: 0.0,
                max_tokens: 1000,
                response_format: { type: 'json_object' }
            })
        );

        const rawContent = completion.choices[0].message.content;

        let result;
        try {
            result = JSON.parse(rawContent);
        } catch (parseError) {
            // Аварийное спасение через регулярку
            const fallbackFacts = [];
            const regex = /(УЗЕЛ:|СВЯЗЬ:)[^"\\]+/gi;
            let match;
            while ((match = regex.exec(rawContent)) !== null) {
                const extracted = match[0].trim();
                if (extracted.length > 10 && !extracted.endsWith('УЗЕЛ:')) {
                    fallbackFacts.push(extracted);
                }
            }
            if (fallbackFacts.length > 0) {
                console.log(`[MEMORY] Аварийное спасение: ${fallbackFacts.length} фактов через regex`);
                result = { facts: fallbackFacts };
            } else {
                return;
            }
        }

        const facts = (result.facts || []).slice(0, MAX_FACTS_PER_BATCH);

        if (facts.length === 0) {
            if (result.reasoning) {
                console.log(`[MEMORY] Факты не найдены: ${result.reasoning.slice(0, 80)}`);
            }
            return;
        }

        console.log(`[MEMORY] Найдено ${facts.length} фактов. Сохраняю...`);
        let saved = 0;

        for (const fact of facts) {
            if (typeof fact !== 'string' || fact.trim() === '' || fact.includes('участник диалога')) continue;

            // Быстрая текстовая проверка перед дорогим эмбеддингом
            const exists = await checkFactExists(chatId, fact);
            if (exists) continue;

            const embedding = await createEmbedding(fact);
            if (!embedding) continue;

            // Проверка семантического дубликата (порог 0.88 — строже)
            const duplicates = await searchKnowledge(chatId, embedding, 1, 0.88);
            if (duplicates && duplicates.length > 0) {
                console.log(`[MEMORY] Пропущен дубликат: "${fact.slice(0, 50)}"`);
                continue;
            }

            await insertKnowledge(chatId, fact, embedding);
            saved++;
            console.log(`[MEMORY] ✅ Добавлено: ${fact.slice(0, 70)}`);
        }

        if (saved > 0) {
            console.log(`[MEMORY] Итого сохранено ${saved}/${facts.length} фактов`);
        }

    } catch (e) {
        if (e.message === 'AI_TIMEOUT') {
            console.warn('[MEMORY] Таймаут экстракции — пропускаю');
        } else {
            console.error('[MEMORY] Ошибка экстракции:', e.message);
        }
    }
}

// ─────────────────────────────────────────────────────────────
// ПОИСК РЕЛЕВАНТНЫХ ФАКТОВ ДЛЯ КОНТЕКСТА ИИ
// ─────────────────────────────────────────────────────────────

async function getRelevantFacts(chatId, userMessage, userName = '', activeParticipants = []) {
    try {
        if (!userMessage || userMessage.trim().length < 3) return '';

        let cleanMessage = userMessage.replace(/Чатик 🫐 Nika_grdt 👾/gi, 'Ника');
        let cleanUserName = userName.replace(/Чатик 🫐 Nika_grdt 👾/gi, 'Ника');

        const allFoundFacts = new Map(); // fact -> { source, relevance }

        const stopWords = new Set([
            'меня', 'тебя', 'чтобы', 'какой', 'такой', 'зачем', 'почему',
            'когда', 'будет', 'очень', 'просто', 'может', 'нужно', 'хочу', 'люблю',
            'тебе', 'мене', 'этот', 'этого', 'этой', 'этом', 'всего', 'тоже',
            'если', 'даже', 'вроде', 'опять', 'снова', 'уже', 'ещё', 'всё'
        ]);

        const getStemLocal = (word) => {
            if (!word || word.length < 3) return word;
            return word.toLowerCase()
                .replace(/[уаеяюиыо]$/i, '')
                .replace(/(ов|ев|ий|ый|ые|ие|ах|ях|ом|ем|ам|у|е|а|я)$/i, '')
                .replace(/(s|es|ed|ing)$/i, '');
        };

        const addFact = (fact, source, relevance = 0.5) => {
            if (!allFoundFacts.has(fact)) {
                allFoundFacts.set(fact, { source, relevance });
            }
        };

        // 1. Семантический поиск по смыслу сообщения (самый точный)
        const embeddingRaw = await createEmbedding(cleanMessage);
        if (embeddingRaw) {
            const vectorResults = await searchKnowledge(chatId, embeddingRaw, 8, 0.45);
            vectorResults.forEach(r => addFact(r.fact, 'semantic', r.similarity || 0.5));
        }

        // 2. Недавние факты о конкретном пользователе
        if (cleanUserName && cleanUserName.length >= 2) {
            const recentResults = await getRecentKnowledge(chatId, cleanUserName, 8);
            recentResults.forEach(r => addFact(r.fact, 'recent', 0.7));
        }

        // 3. Поиск по именам упомянутых людей и участников (ограничиваем стемы)
        const searchStems = new Set();
        const addTarget = (name) => {
            if (!name || name.length < 2) return;
            const clean = name.replace('@', '').toLowerCase();
            searchStems.add(clean);
            const stem = getStemLocal(clean);
            if (stem !== clean && stem.length >= 3) searchStems.add(stem);
            const trans = transliterate(name);
            if (trans && trans.toLowerCase() !== clean) searchStems.add(trans.toLowerCase());
        };

        addTarget(cleanUserName);

        // Только первые 3 участника чтобы не делать 20+ SQL запросов
        if (activeParticipants) {
            activeParticipants.slice(0, 3).forEach(p => {
                let pName = (p.firstName || '').replace(/Чатик 🫐 Nika_grdt 👾/gi, 'Ника');
                if (pName) addTarget(pName);
            });
        }

        // Имена из сообщения (с заглавной = вероятно имя)
        const potentialNames = cleanMessage.match(/([А-Я][а-яё]{2,}|@[a-zA-Z0-9_]+)/g) || [];
        potentialNames.slice(0, 5).forEach(n => addTarget(n.replace('@', '')));

        // Делаем не более 8 текстовых поисков
        for (const stem of Array.from(searchStems).slice(0, 8)) {
            if (stem.length < 3) continue;
            const byStem = await searchKnowledgeByText(chatId, stem, 5);
            byStem.forEach(r => addFact(r.fact, 'subject', 0.6));
        }

        // 4. Ключевые слова из сообщения (ограничиваем до 4 слов)
        const keywords = cleanMessage.split(/\s+/)
            .map(w => w.replace(/[.,!?;:()]/g, '').toLowerCase())
            .filter(w => w.length > 4 && !stopWords.has(w))
            .slice(0, 4);

        for (const word of keywords) {
            const stem = getStemLocal(word);
            const textResults = await searchKnowledgeByText(chatId, stem, 2);
            textResults.forEach(r => addFact(r.fact, 'keyword', 0.4));
        }

        if (allFoundFacts.size === 0) return '';

        // Сортировка: semantic > recent > subject > keyword
        const priorityOrder = { semantic: 0, recent: 1, subject: 2, keyword: 3 };
        const sorted = Array.from(allFoundFacts.entries())
            .sort(([, a], [, b]) => {
                const pDiff = priorityOrder[a.source] - priorityOrder[b.source];
                return pDiff !== 0 ? pDiff : b.relevance - a.relevance;
            });

        const factsText = sorted
            .slice(0, 12)
            .map(([fact]) => '- ' + fact)
            .join('\n');

        return factsText;

    } catch (e) {
        console.error('[MEMORY] Ошибка поиска фактов:', e.message);
        return '';
    }
}

// ─────────────────────────────────────────────────────────────
// УДАЛЕНИЕ ФАКТА
// ─────────────────────────────────────────────────────────────

async function forgetFact(chatId, query) {
    if (!query || query.trim() === '') return false;
    try {
        const embedding = await createEmbedding(query);
        if (!embedding) return false;
        const results = await searchKnowledge(chatId, embedding, 1, 0.72);
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