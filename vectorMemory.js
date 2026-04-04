const OpenAI = require('openai');
const { insertKnowledge, searchKnowledge } = require('./database');

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
        const prompt = `Проанализируй этот сегмент диалога чата. Извлеки из него ТОЛЬКО ДОЛГОСРОЧНУЮ фактоориентированную информацию о пользователях (их реальные имена, профессия, хобби, город, домашние животные, важные события).
СТРОГИЕ ЗАПРЕТЫ:
- НЕ извлекай мнения пользователей о боте, правилах чата или других людях (например, "считает, что бот должен быть...").
- НЕ извлекай их настроения, сиюминутные мысли или желания (например, "думает, чем бы заняться").
- НЕ извлекай факты о боте (о тебе).
- ЗАПРЕЩАЕТСЯ извлекать любую пустую болтовню.

Формат ответа: JSON объект с ключом "facts", содержащим массив атомарных строк-фактов.
Например: 
{
  "facts": [
    "Пользователь Алексей (@alex) любит собак",
    "Темирлан вчера купил машину"
  ]
}
Если фактов нет, верни {"facts": []}.

Диалог:
${historyText}`;

        const completion = await openai.chat.completions.create({
            model: AI_MODEL,
            messages: [{ role: 'system', content: prompt }],
            temperature: 0.1,
            response_format: { type: 'json_object' }
        });

        const result = JSON.parse(completion.choices[0].message.content);
        const facts = result.facts || [];

        for (const fact of facts) {
            const embedding = await createEmbedding(fact);
            if (embedding) {
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
    
    // Ищем топ 3 факта с совпадением (similarity) больше 30%
    const results = await searchKnowledge(chatId, embedding, 3, 0.3);
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
