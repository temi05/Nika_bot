const OpenAI = require('openai');
const {
    insertKnowledge,
    searchKnowledge,
    checkFactExists,
    searchKnowledgeByText,
    getRecentKnowledge,
    transliterate
} = require('./database');

console.log('✅ [SYSTEM] Модуль СВЕРХ-УМНОЙ графовой памяти (LightRAG v2) успешно подключен!');

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
        // ---> ЖЕСТКАЯ ОЧИСТКА ИМЕН ДО ОТПРАВКИ К ИИ <---
        // Выжигаем системное имя канала и заменяем на "Ника", чтобы ИИ не путался
        let cleanHistory = historyText.replace(/Чатик 🫐 Nika_grdt 👾/gi, "Ника");

        const participantInfo = participants.length > 0 ?
            "Участники: " + participants.map(p => p.replace(/Чатик 🫐 Nika_grdt 👾/gi, "Ника")).join(', ') :
            "Определи имена участников из диалога.";

        const prompt = `Ты — сверх-интеллектуальное ядро графовой памяти (LightRAG) НейроНики. Твоя задача — строить граф связей из чата.

[КРИТИЧЕСКИЕ ПРАВИЛА — АБСОЛЮТНЫЙ ЗАПРЕТ МУСОРА]
Ты ДОЛЖЕН безжалостно отбрасывать 99% сообщений.
1. ❌ МЕТА-ИГНОР (ОЧЕНЬ ВАЖНО): Если в тексте есть "удали", "забудь", "покажи профиль", "ИИ записал", "ты тупая" — ИГНОРИРУЙ ВЕСЬ БЛОК. Пользователи обсуждают твою память, нельзя записывать это как факт! Иначе ты запишешь то, что они просят удалить.
2. ❌ ВРЕМЕННЫЕ ДЕЙСТВИЯ: ЗАПРЕЩЕНО сохранять "работает", "сфоткала утром", "забыла скинуть", "придет", "лазил по настройкам", "играет". Это бытовуха, а не атрибуты!
3. ❌ ГАЛЛЮЦИНАЦИИ: ЗАПРЕЩЕНО присваивать один и тот же признак всем пользователям подряд.

[ЧТО ТАКОЕ УЗЕЛ И СВЯЗЬ]
✅ УЗЕЛ: [Имя] | АТРИБУТ: [ФАКТ]. Только вечные вещи (реальная профессия, возраст, ориентация, фобии, фетиши, город).
✅ СВЯЗЬ: [Имя1] -> [Отношение] -> [Имя2]. Только устойчивые отношения (в браке с, ненавидит, фанатеет от, презирает).

Имена очищай от тегов. Никаких (@username) или скобок.

[ФОРМАТ ВЫВОДА (JSON)]
Сначала ОБЯЗАТЕЛЬНО подумай в поле "reasoning", объясняя, почему ты отбрасываешь мусор или почему сохраняешь факт. Затем заполни массив "facts" (строгий формат строк).

ПРИМЕР:
{
  "reasoning": "Юзеры просят удалить память и обсуждают настройки стрима. Это мусор, отбрасываю. Алекс упомянул, что он врач - это фундаментально, создаю Узел.",
  "facts": [
    "УЗЕЛ: Алекс | АТРИБУТ: работает врачом"
  ]
}`;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [
                { role: 'system', content: prompt },
                { role: 'user', content: `Участники: ${participantInfo}\n\nДиалог:\n${cleanHistory}` }
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

        // Очищаем запросы от системного имени перед поиском
        let cleanMessage = userMessage.replace(/Чатик 🫐 Nika_grdt 👾/gi, "Ника");
        let cleanUserName = userName.replace(/Чатик 🫐 Nika_grdt 👾/gi, "Ника");

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

        const embeddingRaw = await createEmbedding(cleanMessage);
        if (embeddingRaw) {
            const vectorResults = await searchKnowledge(chatId, embeddingRaw, 10, 0.45);
            vectorResults.forEach(r => {
                if (!allFoundFacts.has(r.fact)) {
                    allFoundFacts.add(r.fact);
                    finalFacts.push({ source: 'semantic', text: r.fact, relevance: r.similarity || 0.5 });
                }
            });
        }

        const words = cleanMessage.split(/\s+/)
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

        if (cleanUserName) {
            const recentResults = await getRecentKnowledge(chatId, cleanUserName, 10);
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

        addTargetWithStem(cleanUserName);
        if (activeParticipants) {
            activeParticipants.forEach(p => {
                let pName = (p.firstName || "").replace(/Чатик 🫐 Nika_grdt 👾/gi, "Ника");
                if (pName) addTargetWithStem(pName);
                if (p.username) addTargetWithStem(p.username);
            });
        }

        const potentialNames = cleanMessage.match(/([А-Я][а-я]+|@[a-zA-Z0-9_]+)/g) || [];
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