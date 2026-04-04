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
        const prompt = `Ты — эксперт по верификации данных. Твоя задача — извлечь из диалога ДОЛГОСРОЧНЫЕ факты об участниках чата.

[ПРАВИЛА ИЗВЛЕЧЕНИЯ СВЯЗЕЙ (GraphRAG-lite)]
1. СУБЪЕКТЫ: Всегда выделяй, КТО совершил действие или О КОМ идет речь (Имя или @username).
2. СВЯЗИ: Описывай отношения между людьми ("друг", "враг", "вместе играют") и предметами ("купил", "хочет", "владеет").
3. ПРИОРИТЕТЫ: Крупные покупки, хобби, место работы, важные события, отношения между участниками чата.
4. ФОРМАТ ТРОЙКИ: Используй структуру [Субъект] [Действие/Связь] [Объект]. Например: "Чика купил iPhone 15", "Марго и Чика дружат", "Темирлан живет в Казахстане".
5. КОНТЕКСТ: Если факт привязан к событию (вечерний стрим, игра в доту), кратко укажи это.

Формат ответа: JSON объект {"facts": ["Субъект: Связь -> Объект (Контекст)"]}
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

        let result;
        try {
            // 1. Предварительная очистка от Markdown
            let cleanContent = rawContent.replace(/```json/g, '').replace(/```/g, '').trim();
            
            // 2. Если JSON не валиден (возможно обрезан), пробуем восстановить скобки и кавычки
            try {
                result = JSON.parse(cleanContent);
            } catch (initialError) {
                // Пытаемся закрыть кавычки, если строка обрезана в середине
                if (cleanContent.startsWith('{')) {
                    // Если нечетное количество кавычек, добавляем одну в конце
                    const quotes = (cleanContent.match(/"/g) || []).length;
                    if (quotes % 2 !== 0) cleanContent += '"';
                    
                    let balance_braces = (cleanContent.match(/{/g) || []).length - (cleanContent.match(/}/g) || []).length;
                    let balance_brackets = (cleanContent.match(/\[/g) || []).length - (cleanContent.match(/]/g) || []).length;
                    
                    while (balance_brackets > 0) { cleanContent += ']'; balance_brackets--; }
                    while (balance_braces > 0) { cleanContent += '}'; balance_braces--; }
                    
                    result = JSON.parse(cleanContent);
                } else {
                    throw initialError;
                }
            }
        } catch (parseError) {
            console.log(`[MEMORY EXTRACTOR] ⚠️ Не удалось распарсить JSON. Возможно, ответ слишком короткий или поврежден. Ошибка: ${parseError.message}`);
            return;
        }
        
        const facts = result.facts || [];

        const savedNewFacts = [];

        for (const fact of facts) {
            const exists = await checkFactExists(chatId, fact);
            if (exists) {
                // Если факт уже есть в базе, не делаем эмбеддинг и пропускаем
                continue;
            }

            const embedding = await createEmbedding(fact);
            if (embedding) {
                // Семантическая дедупликация (теперь более деликатная: 95%+)
                const duplicates = await searchKnowledge(chatId, embedding, 1, 0.95);
                if (duplicates && duplicates.length > 0) {
                    console.log(`[MEMORY] Семантический дубликат пропущен. Фраза "${fact}" почти идентична уже известной.`);
                    continue;
                }


                await insertKnowledge(chatId, fact, embedding);
                console.log(`[MEMORY] Успешно запомнен факт: ${fact}`);
                savedNewFacts.push(fact);
            }
        }
    } catch (e) {
        console.error('[MEMORY ERROR] Ошибка экстракции фактов:', e.message);
    }
}

// Поиск фактов для ответа (ГИБРИДНАЯ МОДЕЛЬ: Векторы + Ключевые слова + История)
async function getRelevantFacts(chatId, userMessage, userName = "") {
    if (!userMessage || userMessage.trim() === '') return "";

    const { searchKnowledgeByText, getRecentKnowledge } = require('./database');
    const allFoundFacts = new Set();
    const finalFacts = [];

    // --- СТРИМ 1: СЕМАНТИКА (ВЕКТОРЫ) ---
    const embeddingRaw = await createEmbedding(userMessage);
    const vectorResults = await searchKnowledge(chatId, embeddingRaw, 5, 0.40);
    vectorResults.forEach(r => {
        if (!allFoundFacts.has(r.fact)) {
            allFoundFacts.add(r.fact);
            finalFacts.push({ source: 'semantic', text: r.fact });
        }
    });

    // --- СТРИМ 2: КЛЮЧЕВЫЕ СЛОВА (SQL ILIKE) ---
    // Извлекаем "тяжелые" слова (длиннее 4 символов)
    const keywords = userMessage.split(/\s+/)
        .map(w => w.replace(/[.,!?;:()]/g, '').toLowerCase())
        .filter(w => w.length > 4);
    
    if (keywords.length > 0) {
        // Берем топ-2 самых длинных слова для поиска
        const bestKeywords = keywords.sort((a, b) => b.length - a.length).slice(0, 2);
        for (const kw of bestKeywords) {
            const textResults = await searchKnowledgeByText(chatId, kw, 3);
            textResults.forEach(r => {
                if (!allFoundFacts.has(r.fact)) {
                    allFoundFacts.add(r.fact);
                    finalFacts.push({ source: 'keyword', text: r.fact });
                }
            });
        }
    }

    // --- СТРИМ 3: ИСТОРИЯ ЛИЧНОСТИ (ПОСЛЕДНИЕ СОБЫТИЯ) ---
    if (userName) {
        const recentResults = await getRecentKnowledge(chatId, userName, 7);
        recentResults.forEach(r => {
            if (!allFoundFacts.has(r.fact)) {
                allFoundFacts.add(r.fact);
                finalFacts.push({ source: 'recent', text: r.fact });
            }
        });
    }

    if (finalFacts.length === 0) return "";

    // Сортируем: сначала семантика, потом ключевые слова, потом история, но ограничиваем общее число
    const factsText = finalFacts
        .slice(0, 15)
        .map((f, i) => `${i + 1}. [${f.source}] ${f.text}`)
        .join('\n');
    
    console.log(`[MEMORY] Гибридный поиск завершен: ${finalFacts.length} найдено всего.`);
    return factsText;
}



// Удаление факта из памяти (Безопасное забывание)
async function forgetFact(chatId, query) {
    if (!query || query.trim() === '') return false;

    // Ищем топ-1 факт с ОЧЕНЬ высокой точностью (0.80+)
    const embedding = await createEmbedding(query);
    const results = await searchKnowledge(chatId, embedding, 1, 0.75);

    if (results && results.length > 0) {
        const target = results[0];
        console.log(`[MEMORY] Найдено для удаления: "${target.fact}" (сходство: ${target.similarity})`);
        
        // Дополнительная проверка на безопасность: не удаляем пустые ID или странные совпадения
        if (target.id) {
            const success = await require('./database').deleteKnowledge(chatId, target.id);
            return success ? target.fact : false;
        }
    }
    
    console.log(`[MEMORY] Не найдено точного соответствия для удаления по запросу: "${query}"`);
    return false;
}

module.exports = {
    extractAndSaveFacts,
    getRelevantFacts,
    createEmbedding,
    forgetFact
};

