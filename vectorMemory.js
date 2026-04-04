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

[ПРАВИЛА ИЗВЛЕЧЕНИЯ ФАКТОВ]
1. ПЕРВОЕ ЛИЦО И ФАКТЫ О ЖИЗНИ: Записывай факт, если юзер говорит О СЕБЕ: "У меня есть...", "Я купил...", "Я хочу купить...", "Я работаю...", "Люблю..." (включая покупки техники, машин, еды, хобби)и то что ты считаеншь нужным для фактов.
2. ЧУЖИЕ ДАННЫЕ: Записывай факт, если один юзер прямо просит запомнить информацию о другом человеке.
3. ЧТО ЗАПОМИНАТЬ: Крупные покупки (телефоны, машины, квартиры), планы на жизнь, место жительства, питомцев, любимую еду (курицу, пиццу и т.д.), увлечения, место работы.
4. ЧТО ИГНОРИРОВАТЬ (МУСОР): Игнорируй простое общение, приветствия, временные эмоции ("я сегодня злой"), шутки, оскорбления, слухи и вопросы боту.
5. ФОРМАТ: Формулируй максимально кратко от третьего лица: "Имя (или @username) [купил/имеет/любит/хочет] [факт]".

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
