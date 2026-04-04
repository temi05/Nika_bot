const OpenAI = require('openai');
const { insertKnowledge, searchKnowledge, checkFactExists } = require('./database');

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const AI_MODEL = process.env.AI_MODEL || 'gemini-2.0-flash-lite-preview-02-05';

const openai = new OpenAI({
    apiKey: POLZA_API_KEY,
    baseURL: 'https://polza.ai/api/v1',
});

// Создает вектор (embedding) из текста
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

// Фоновое извлечение фактов из куска диалога
async function extractAndSaveFacts(chatId, historyText, participants = []) {
    try {
        const participantInfo = participants.length > 0 ? 
            "Участники чата: " + participants.join(', ') + ". Используй ТОЛЬКО эти имена для идентификации." : 
            "Определи имена участников из диалога.";

        const prompt = "Ты — эксперт по верификации данных. Твоя задача — извлечь из диалога ДОЛГОСРОЧНЫЕ факты об участниках чата.\n\n[ПРАВИЛА ИЗВЛЕЧЕНИЯ СВЯЗЕЙ (Memory v3)]\n1. СУБЪЕКТЫ: Каждый факт ДОЛЖЕН начинаться с Имени (в точности как в чате). НИКАКИХ 'Он', 'Она', 'Бот'.\n2. ФОРМАТ: 'Имя: [Действие] [Объект] (Контекст)'.\n3. СВЯЗИ: Описывай отношения между людьми.\n4. ПРИОРИТЕТЫ: Крупные покупки, хобби, важные события.\n\n" + participantInfo + "\n\nДиалог для анализа:\n" + historyText;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [{ role: 'system', content: prompt }],
            temperature: 0.1,
            response_format: { type: 'json_object' }
        });

        const rawContent = completion.choices[0].message.content;
        console.log("[MEMORY EXTRACTOR] Ответ ИИ: " + rawContent);

        let result;
        try {
            // v4.0: Более надежное извлечение JSON через RegEx
            const jsonMatch = rawContent.match(/\{[\s\S]*\}/);
            if (jsonMatch) {
                result = JSON.parse(jsonMatch[0]);
            } else {
                result = JSON.parse(rawContent);
            }
        } catch (parseError) {
            console.log("[MEMORY EXTRACTOR] Ошибка парсинга JSON: " + parseError.message);
            return;
        }
        
        const facts = result.facts || [];

        for (const fact of facts) {
            const exists = await checkFactExists(chatId, fact);
            if (exists) continue;

            const embedding = await createEmbedding(fact);
            if (embedding) {
                const duplicates = await searchKnowledge(chatId, embedding, 1, 0.95);
                if (duplicates && duplicates.length > 0) {
                    console.log("[MEMORY] Семантический дубликат пропущен: " + fact);
                    continue;
                }

                await insertKnowledge(chatId, fact, embedding);
                console.log("[MEMORY] Успешно запомнен факт: " + fact);
            }
        }
    } catch (e) {
        console.error('[MEMORY ERROR] Ошибка экстракции фактов:', e.message);
    }
}

// Поиск фактов для ответа (ГИБРИДНАЯ МОДЕЛЬ v3.3.1: Векторы + Широкие Ключи + Стеммы имен)
async function getRelevantFacts(chatId, userMessage, userName = "", activeParticipants = []) {
    if (!userMessage || userMessage.trim() === '') return "";

    const { searchKnowledgeByText, getRecentKnowledge, transliterate } = require('./database');
    const allFoundFacts = new Set();
    const finalFacts = [];

    // Стоп-слова
    const stopWords = new Set(['меня', 'тебя', 'чтобы', 'какой', 'такой', 'зачем', 'почему', 'когда', 'будет', 'очень', 'просто', 'может', 'нужно', 'хочу', 'люблю']);

    const getStemLocal = (word) => {
        if (!word || word.length < 3) return word;
        return word.toLowerCase()
            .replace(/[уаеяюиыо]$/i, '')
            .replace(/(ов|ев|ий|ый|ые|ие|ах|ях|ом|ем|ам|ам|у|е|а|я)$/i, '')
            .replace(/(s|es|ed|ing)$/i, '');
    };

    // --- СТРИМ 1: СЕМАНТИКА (ВЕКТОРЫ) ---
    const embeddingRaw = await createEmbedding(userMessage);
    // v4.0: Повышаем порог точности до 0.45 для минимизации галлюцинаций
    const vectorResults = await searchKnowledge(chatId, embeddingRaw, 10, 0.45);
    vectorResults.forEach(r => {
        if (!allFoundFacts.has(r.fact)) {
            allFoundFacts.add(r.fact);
            finalFacts.push({ source: 'semantic', text: r.fact, relevance: r.similarity || 0.5 });
        }
    });

    // --- СТРИМ 2: ШИРОКИЕ КЛЮЧЕВЫЕ СЛОВА ---
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

    // --- СТРИМ 3: ИСТОРИЯ КОНКРЕТНОГО ЮЗЕРА ---
    if (userName) {
        const recentResults = await getRecentKnowledge(chatId, userName, 10);
        recentResults.forEach(r => {
            if (!allFoundFacts.has(r.fact)) {
                allFoundFacts.add(r.fact);
                finalFacts.push({ source: 'recent', text: r.fact });
            }
        });
    }

    // --- СТРИМ 4: АГРЕССИВНЫЙ ПОИСК ПО СУБЪЕКТАМ ---
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
        .slice(0, 25)
        .map((f, i) => (i + 1) + ". [" + f.source + "] " + f.text)
        .join('\n');
    
    console.log("[MEMORY] Сверхпамять v3.3.1: " + finalFacts.length + " найдено.");
    return factsText;
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
