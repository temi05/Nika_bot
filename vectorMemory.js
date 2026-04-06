const OpenAI = require('openai');
const {
    insertKnowledge,
    searchKnowledge,
    checkFactExists,
    searchKnowledgeByText,
    getRecentKnowledge,
    transliterate
} = require('./database');

console.log('✅ [SYSTEM] Модуль строгой графовой памяти успешно подключен!');

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const AI_MODEL = process.env.AI_MODEL || 'gpt-4o-mini';

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: 'https://polza.ai/api/v1',
});

async function createEmbedding(text) {
    try {
        const res = await openai.embeddings.create({
            model: 'text-embedding-3-small',
            input: text,
        });
        return res.data[0].embedding;
    } catch (e) {
        console.error('[VECTOR ERROR] Ошибка генерации эмбеддинга:', e.message);
        return null;
    }
}

async function extractAndSaveFacts(chatId, historyText, participants = []) {
    try {
        const prompt = `Ты — ядро извлечения знаний (LightRAG Graph Extractor). Твоя задача — строить граф связей из чата.

[КРИТИЧЕСКИЕ ПРАВИЛА — ИГНОРИРОВАНИЕ МУСОРА]
ИГНОРИРУЙ ЦЕЛИКОМ И ПОЛНОСТЬЮ любой диалог, если он содержит:
1. Управление памятью: "удали этот факт", "запомни это", "покажи профиль".
2. Оценку бота: "ты тупая", "ИИ полезное записал", "как она работает".
3. Бытовуху и еду: печеньки, сон, поход в магазин.
Если чат состоит из этого — ВЕРНИ ПУСТОЙ МАССИВ.

[ПРАВИЛА ПОСТРОЕНИЯ ГРАФА]
Ты сохраняешь ТОЛЬКО УЗЛЫ и СВЯЗИ.
- УЗЕЛ: [Имя] | АТРИБУТ: [ФАКТ] (профессия, возраст, кинки, постоянные хобби).
- СВЯЗЬ: [Имя1] -> [отношение] -> [Имя2] (ненавидит, любит, фанатеет).

Имя "Чатик 🫐 Nika_grdt 👾" сокращай просто до "Ника". Все остальные имена очищай от тегов.

[СТРОГИЙ ФОРМАТ JSON]
ТЕБЕ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО использовать ключи "name" и "fact"!
Ты должен вернуть объект с ключом "facts", где внутри лежит массив ОБЫЧНЫХ СТРОК.

ПРАВИЛЬНЫЙ ОТВЕТ:
{
  "facts": [
    "УЗЕЛ: Алекс | АТРИБУТ: работает программистом",
    "СВЯЗЬ: Чика -> ненавидит -> Любимый"
  ]
}

ПУСТОЙ ОТВЕТ (используй в 90% случаев):
{
  "facts": []
}

Диалог для анализа:
${historyText}`;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [
                { role: 'system', content: prompt }
            ],
            temperature: 0.0,
            max_tokens: 1500,
            response_format: { type: 'json_object' }
        });

        const rawContent = completion.choices[0].message.content;
        console.log("[MEMORY EXTRACTOR] Ответ ИИ: " + rawContent);

        let result;
        try {
            result = JSON.parse(rawContent);
        } catch (parseError) {
            console.log("[MEMORY EXTRACTOR] Ошибка парсинга JSON: " + parseError.message);
            return;
        }

        let facts = result.facts || [];

        for (const fact of facts) {
            if (typeof fact !== 'string' || fact.trim() === '') continue;

            const exists = await checkFactExists(chatId, fact);
            if (exists) continue;

            const embedding = await createEmbedding(fact);
            if (embedding) {
                const duplicates = await searchKnowledge(chatId, embedding, 1, 0.85);
                if (duplicates && duplicates.length > 0) continue;

                await insertKnowledge(chatId, fact, embedding);
                console.log("[MEMORY] Успешно добавлен узел/связь: " + fact);
            }
        }
    } catch (e) {
        console.error('[MEMORY ERROR] Ошибка экстракции:', e.message);
    }
}

async function getRelevantFacts(chatId, userMessage, userName = "", activeParticipants = []) {
    try {
        if (!userMessage || userMessage.trim() === '') return "";

        const allFoundFacts = new Set();
        const finalFacts = [];

        const stopWords = new Set(['меня', 'тебя', 'чтобы', 'какой', 'такой', 'зачем', 'почему', 'когда', 'будет', 'очень', 'просто', 'может', 'нужно', 'хочу', 'люблю']);

        const getStemLocal = (word) => {
            if (!word || word.length < 3) return word;
            return word.toLowerCase()
                .replace(/[уаеяюиыо]$/i, '')
                .replace(/(ов|ев|ий|ый|ые|ие|ах|ях|ом|ем|ам|ам|у|е|а|я)$/i, '')
                .replace(/(s|es|ed|ing)$/i, '');
        };

        const embeddingRaw = await createEmbedding(userMessage);
        if (embeddingRaw) {
            const vectorResults = await searchKnowledge(chatId, embeddingRaw, 10, 0.45);
            vectorResults.forEach(r => {
                if (!allFoundFacts.has(r.fact)) {
                    allFoundFacts.add(r.fact);
                    finalFacts.push({ source: 'semantic', text: r.fact, relevance: r.similarity || 0.5 });
                }
            });
        }

        const words = userMessage.split(/\s+/)
            .map(w => w.replace(/[.,!?;:()]/g, '').toLowerCase())
            .filter(w => w.length > 3 && !stopWords.has(w));

        if (words.length > 0) {
            for (const word of words.slice(0, 7)) {
                const stem = getStemLocal(word);
                const textResults = await searchKnowledgeByText(chatId, stem, 3);
                textResults.forEach(r => {
                    if (!allFoundFacts.has(r.fact)) {
                        allFoundFacts.add(r.fact);
                        finalFacts.push({ source: 'keyword', text: r.fact });
                    }
                });
            }
        }

        if (userName) {
            const recentResults = await getRecentKnowledge(chatId, userName, 10);
            recentResults.forEach(r => {
                if (!allFoundFacts.has(r.fact)) {
                    allFoundFacts.add(r.fact);
                    finalFacts.push({ source: 'recent', text: r.fact });
                }
            });
        }

        const searchStems = new Set();
        const addTargetWithStem = (name) => {
            if (!name || name.length < 3) return;
            const stem = getStemLocal(name);
            searchStems.add(stem);
            searchStems.add(name.toLowerCase());
            const trans = transliterate(name);
            if (trans !== name.toLowerCase()) searchStems.add(getStemLocal(trans));
        };

        addTargetWithStem(userName);
        if (activeParticipants) {
            activeParticipants.forEach(p => {
                if (p.firstName) addTargetWithStem(p.firstName);
                if (p.username) addTargetWithStem(p.username);
            });
        }

        const potentialNames = userMessage.match(/([А-Я][а-я]+|@[a-zA-Z0-9_]+)/g) || [];
        potentialNames.forEach(n => addTargetWithStem(n.replace('@', '')));

        for (const stem of searchStems) {
            const byStem = await searchKnowledgeByText(chatId, stem, 10);
            byStem.forEach(r => {
                if (!allFoundFacts.has(r.fact)) {
                    allFoundFacts.add(r.fact);
                    finalFacts.push({ source: 'subject', text: r.fact });
                }
            });
        }

        if (finalFacts.length === 0) return "";

        const sortedFacts = finalFacts.sort((a, b) => {
            const order = { 'subject': 0, 'recent': 1, 'semantic': 2, 'keyword': 3 };
            return order[a.source] - order[b.source];
        });

        const factsText = sortedFacts
            .slice(0, 15)
            .map((f, i) => "- " + f.text)
            .join('\n');

        return factsText;

    } catch (e) {
        console.error('[MEMORY FATAL ERROR] Ошибка при поиске релевантных фактов:', e.message);
        return "";
    }
}

async function forgetFact(chatId, query) {
    if (!query || query.trim() === '') return false;
    const embedding = await createEmbedding(query);
    const results = await searchKnowledge(chatId, embedding, 1, 0.75);
    if (results && results.length > 0) {
        const target = results[0];
        if (target.id) {
            const success = await require('./database').deleteKnowledge(chatId, target.id);
            return success ? target.fact : false;
        }
    }
    return false;
}

module.exports = {
    extractAndSaveFacts,
    getRelevantFacts,
    createEmbedding,
    forgetFact
};