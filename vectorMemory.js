const OpenAI = require('openai');
const { insertKnowledge, searchKnowledge, checkFactExists } = require('./database');

const POLZA_API_KEY = process.env.POLZA_API_KEY || 'pza_Ut5ahRtIFZSzj_jKezwdRvQMMebqZ1BI';
const AI_MODEL = process.env.AI_MODEL || 'gpt-4o-mini';

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
async function extractAndSaveFacts(chatId, historyText) {
    try {
        const prompt = `Ты — эксперт по верификации данных. Твоя задача — извлечь из диалога ТОЛЬКО ДОЛГОСРОЧНЫЕ И ВЕРИФИЦИРОВАННЫЕ факты о юзерах.

[СТРОГИЕ ПРАВИЛА]
1. ПЕРВОЕ ЛИЦО: Записывай факт ТОЛЬКО если юзер прямо говорит О СЕБЕ (например: "Я работаю...", "Меня зовут...", "У меня есть собака")или если кто-то попросит добавить данные о другом человеке в профиль другого человека о котором говорит. 
2. НЕТ ПРЕДПОЛОЖЕНИЯМ: Если юзер просто обсуждает аниме или фильм, это НЕ значит, что он его любит. НЕ ЗАПИСЫВАЙ "любит аниме", если он прямо не сказал: "Я люблю аниме".
3. НЕТ ГАЛЛЮЦИНАЦИЯМ: Лучше вернуть пустой список, чем записать один сомнительный факт. Если диалог — просто флуд, верни {"facts": []}.
4. НЕТ МНЕНИЯМ И СЛУХАМ: Не записывай то, что юзеры говорят друг о друге (кто кому подлизывается, кто кого считает дураком). Это мусор.
5. ИГНОРИРУЙ СУБЪЕКТИВНОСТЬ: "Я сегодня злой", "Мне кажется..." — не записывай.

Формат ответа: JSON объект {"facts": ["Пользователь [ИМЯ]: [ФАКТ]"]}
Если фактов нет: {"facts": []}

Диалог для анализа:
${historyText}`;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [{ role: 'system', content: prompt }],
            temperature: 0.1,
            response_format: { type: 'json_object' }
        });

        const rawContent = completion.choices[0].message.content;
        console.log(`[MEMORY EXTRACTOR] Ответ ИИ: ${rawContent}`);
        
        const result = JSON.parse(rawContent);
        const facts = result.facts || [];

        for (const fact of facts) {
            const exists = await checkFactExists(chatId, fact);
            if (exists) {
                // Если факт уже есть в базе, не делаем эмбеддинг и пропускаем
                continue;
            }
            
            const embedding = await createEmbedding(fact);
            if (embedding) {
                // Семантическая дедупликация (если мысль совпадает на 90%+, пропускаем)
                const duplicates = await searchKnowledge(chatId, embedding, 1, 0.90);
                if (duplicates && duplicates.length > 0) {
                    console.log(`[MEMORY] Семантический дубликат пропущен. Фраза "${fact}" -> похожа на: "${duplicates[0].fact}"`);
                    continue;
                }

                await insertKnowledge(chatId, fact, embedding);
                console.log(`[MEMORY] Успешно запомнен факт: ${fact}`);
            }
        }
    } catch (e) {
        console.error('[MEMORY ERROR] Ошибка экстракции фактов:', e.message);
    }
}

// Поиск фактов для ответа
async function getRelevantFacts(chatId, userMessage) {
    if (!userMessage || userMessage.trim() === '') return "";

    const embedding = await createEmbedding(userMessage);
    if (!embedding) return "";

    // Ищем топ 3 факта с совпадением (similarity) больше 45% (чтобы отсечь мусор)
    const results = await searchKnowledge(chatId, embedding, 3, 0.45);
    if (results.length === 0) return "";

    const factsText = results.map((r, i) => `${i + 1}. ${r.fact}`).join('\n');
    console.log(`[MEMORY] Вытащены факты для текущего ответа:\n${factsText}`);
    return factsText;
}

module.exports = {
    extractAndSaveFacts,
    getRelevantFacts,
    createEmbedding
};
