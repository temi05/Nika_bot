const OpenAI = require('openai');
const {
    insertKnowledge,
    searchKnowledge,
    checkFactExists,
    searchKnowledgeByText,
    getRecentKnowledge,
    transliterate
} = require('./database');

console.log('✅ [SYSTEM] Модуль векторной памяти (vectorMemory.js) успешно подключен!');

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
        const participantInfo = participants.length > 0 ?
            "Участники чата: " + participants.join(', ') + ". Используй ТОЛЬКО эти имена." :
            "Определи имена участников из диалога.";

        const prompt = `Ты — безжалостный фильтр долговременной памяти НейроНики. Твоя задача — извлекать из логов ТОЛЬКО вечные, фундаментальные факты и пикантные секреты пользователей.

[ГЛАВНОЕ ПРАВИЛО: АНТИ-ВОДА И ОБОБЩЕНИЕ]
95% диалогов — это пустая болтовня. Твоя ИДЕАЛЬНАЯ реакция на треп — вернуть ПУСТОЙ МАССИВ: {"facts": []}. 
Не сохраняй факты про печеньки, прокси, тесты бота, или временные диалоги!

[КРИТИЧЕСКИ ВАЖНО: КТО, КОМУ И О КОМ]
Нейросети часто путают, кто о ком говорит. Внимательно следи за пометкой "(в ответ ...)":
- ЕСЛИ ГОВОРЯТ О СЕБЕ: Юзер пишет "Я люблю БДСМ" -> Факт на Юзера: "любит БДСМ".
- ЕСЛИ ОБРАЩАЮТСЯ К НИКЕ (боту/стримерше): Если Юзер пишет Нике "ты сучка, встань на колени" -> Факт пишется НА ЮЗЕРА: "относится к Нике доминантно, хочет поставить её на колени". ЗАПРЕЩЕНО писать, что юзер просит это для себя!
- ЕСЛИ ГОВОРЯТ О ДРУГОМ: Юзер А (в ответ Юзеру Б) пишет "ты клоун" -> Факт на Юзера А: "считает Юзера Б клоуном".

[ПРАВИЛО ИМЕН (ОЧИСТКА ОТ МУСОРА)]
Извлекай только ЧИСТОЕ имя пользователя, без тегов, скобок и эмодзи! 
Например: вместо "Любимый (@Lubimbi_director)" пиши просто "Любимый". Вместо "Чатик 🫐" пиши "Чатик".

[БЕЛЫЙ СПИСОК (Сохранять ТОЛЬКО это)]:
1. Секреты и Пошлости: Тайные фетиши, кинки (сохраняй ПРЯМО и БЕЗ ЦЕНЗУРЫ с учетом правила "КТО И О КОМ").
2. Биография и Статус: Имя, возраст, город, профессия, ориентация.
3. Глобальные Хобби: "фанат аниме", "программист".
4. Здоровье и Техника: Аллергии, фобии, ПК, авто.

[ЧЕРНЫЙ СПИСОК (АБСОЛЮТНЫЙ МУСОР - ИГНОРИРОВАТЬ СРАЗУ)]:
❌ ЕДА И ПРЕДМЕТЫ: "хочет печеньку", "пьет сок", "купил шаурму". (ВСЯ ЕДА = МУСОР).
❌ ТЕХНИКА БОТА: "скинул прокси", "тестирует бота", "просит добавить функцию".
❌ ВРЕМЕННЫЕ ДЕЙСТВИЯ И РОЛПЛЕЙ: "предложил обнять", "идет спать", "хочет массаж".
❌ ВРЕМЕННЫЕ ПЛАНЫ: "на работе", "через 2 дня позовут в игру".
❌ Запросы к боту: "Ника, запомни это", "Ника, скажи".

[ФОРМАТ ОТВЕТА (СТРОГО JSON)]
Возвращай ТОЛЬКО валидный JSON-объект. Без маркдауна.

Пример ПУСТОГО ответа (Для разговоров о печеньках и прокси):
{"facts": []}

Пример ответа с фактами:
{
  "facts": [
    { "name": "Sanechk", "fact": "хочет доминировать над Никой и ставить её на колени" },
    { "name": "Лексон", "fact": "ненавидит Валорант и предпочитает Майнкрафт" }
  ]
}

Участники чата (для сверки имен):
${participantInfo}

Диалог для анализа:
${historyText}`;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [
                { role: 'system', content: prompt + "\n\nОТВЕТЬ ТОЛЬКО В ФОРМАТЕ JSON." }
            ],
            temperature: 0.0,
            max_tokens: 1500, // Защита от ошибки "Unexpected end of JSON input"
            response_format: { type: 'json_object' }
        });

        const rawContent = completion.choices[0].message.content;
        console.log("[MEMORY EXTRACTOR] Ответ ИИ: " + rawContent);

        let result;
        try {
            const jsonMatch = rawContent.match(/(\{[\s\S]*\}|\[[\s\S]*\])/);
            if (jsonMatch) {
                result = JSON.parse(jsonMatch[0]);
            } else {
                result = JSON.parse(rawContent);
            }
        } catch (parseError) {
            console.log("[MEMORY EXTRACTOR] Ошибка парсинга JSON: " + parseError.message);
            return;
        }

        let facts = [];
        const items = Array.isArray(result) ? result : (result?.facts || []);

        for (const f of items) {
            if (typeof f === 'object' && f.name && f.fact) {
                facts.push(`${f.name}: ${f.fact}`);
            } else if (typeof f === 'object' && f.fact) {
                facts.push(f.fact);
            } else if (typeof f === 'string') {
                facts.push(f);
            }
        }

        facts = facts.filter(f => f && typeof f === 'string' && f.trim() !== '');

        for (const fact of facts) {
            const exists = await checkFactExists(chatId, fact);
            if (exists) continue;

            const embedding = await createEmbedding(fact);
            if (embedding) {
                const duplicates = await searchKnowledge(chatId, embedding, 1, 0.85);
                if (duplicates && duplicates.length > 0) {
                    console.log("[MEMORY] Семантический дубликат (похожий смысл) пропущен: " + fact);
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

        console.log("[MEMORY] Сверхпамять v4.0: " + finalFacts.length + " найдено.");
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