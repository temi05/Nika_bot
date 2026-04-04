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
                // Семантическая дедупликация (если мысль совпадает на 90%+, пропускаем)
                const duplicates = await searchKnowledge(chatId, embedding, 1, 0.90);
                if (duplicates && duplicates.length > 0) {
                    console.log(`[MEMORY] Семантический дубликат пропущен. Фраза "${fact}" -> похожа на: "${duplicates[0].fact}"`);
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

// Поиск фактов для ответа (Двухэтапная ассоциативная модель)
async function getRelevantFacts(chatId, userMessage, userName = "") {
    if (!userMessage || userMessage.trim() === '') return "";

    // 1. ПЕРВЫЙ ЭТАП: Семантический поиск по запросу
    const searchQuery = userName ? `${userName}: ${userMessage}` : userMessage;
    const directResults = await searchKnowledge(chatId, await createEmbedding(searchQuery), 5, 0.40);
    
    if (directResults.length === 0) return "";

    // Собираем найденные факты и ищем в них Сущности для ассоциаций
    let facts = directResults.map(r => r.fact);
    let entities = new Set();
    
    // Простой поиск имен (Слова с большой буквы или @username)
    const entityRegex = /(@[a-zA-Z0-9_]+|[А-Я][а-я]+)/g;
    facts.forEach(f => {
        const matches = f.match(entityRegex);
        if (matches) matches.forEach(m => entities.add(m));
    });

    // 2. ВТОРОЙ ЭТАП: Добор связанных фактов для найденных сущностей
    let extraFacts = [];
    if (entities.size > 0) {
        const entityList = Array.from(entities).slice(0, 3); // Ограничимся 3 сущностями для скорости
        console.log(`[MEMORY] Найдено сущностей для связи: ${entityList.join(', ')}`);
        
        for (const entity of entityList) {
            // Поиск по ключевому слову сущности (без эмбеддинга, просто текст)
            const branchResults = await searchKnowledge(chatId, await createEmbedding(entity), 2, 0.60);
            branchResults.forEach(r => {
                if (!facts.includes(r.fact)) extraFacts.push(r.fact);
            });
        }
    }

    const allFacts = [...facts, ...extraFacts].slice(0, 10);
    const factsText = allFacts.map((f, i) => `${i + 1}. ${f}`).join('\n');
    
    console.log(`[MEMORY] Цепочка ассоциаций (Глубина 2):\n${directResults.length} прямо\n${extraFacts.length} по связям\nИтого: ${allFacts.length} фактов.`);
    return factsText;
}

module.exports = {
    extractAndSaveFacts,
    getRelevantFacts,
    createEmbedding
};
