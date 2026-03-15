/**
 * Тестовый скрипт для проверки ИИ-ассистента (Groq - бесплатно!)
 * 
 * Перед запуском:
 * 1. Получите API ключ на https://console.groq.com/keys
 * 2. Добавьте в .env файл: GROQ_API_KEY=ваш_ключ
 * 3. Установите: npm install groq-sdk
 * 4. Запустите: node test_ai.js
 */

require('dotenv').config();
const readline = require('readline');

// Проверяем наличие ключа
const GROQ_API_KEY = process.env.GROQ_API_KEY;

if (!GROQ_API_KEY) {
    console.log('\n❌ Ошибка: GROQ_API_KEY не найден в .env файле!\n');
    console.log('📋 Инструкция по получению БЕСПЛАТНОГО ключа Groq:');
    console.log('1. Перейдите на https://console.groq.com/keys');
    console.log('2. Зарегистрируйтесь (можно через Google)');
    console.log('3. Нажмите "Create API Key"');
    console.log('4. Скопируйте ключ');
    console.log('5. Добавьте в .env файл строку: GROQ_API_KEY=ваш_ключ\n');
    console.log('✨ Groq полностью бесплатный и очень быстрый!\n');
    process.exit(1);
}

// Имя бота (можно изменить)
const BOT_NAME = 'Ника';

// Инициализация Groq
const Groq = require('groq-sdk');
const groq = new Groq({ apiKey: GROQ_API_KEY });

// Системный промпт — характер бота
const SYSTEM_PROMPT = `Ты — ${BOT_NAME}, дерзкий и остроумный ИИ-ассистент в групповом чате Telegram.

ТВОЙ ХАРАКТЕР:
- Ты саркастичная, но добродушная
- Любишь подшучивать над пользователями (легко, без обид)
- Отвечаешь кратко и с юмором (1-3 предложения максимум)
- Используешь эмодзи, но не перебарщиваешь
- Иногда вставляешь мемные фразы и интернет-сленг
- Если не знаешь ответа — честно признаёшься, но с юмором
- Общаешься на русском языке

ОСОБЫЕ КОМАНДЫ:
- Если просят "нарисуй" — опиши что ты "нарисовала" в смешной манере
- Если просят перевести — переведи и добавь комментарий
- Если пишут "$" — пошути про курс доллара

ЗАПРЕЩЕНО:
- Оскорблять по-настоящему
- Обсуждать политику, религию
- Давать опасные советы
- Длинные скучные ответы

Примеры ответов:
- "Ну ты и спросил... 🤔 Ладно, слушай..."
- "О, это я могу! *закатывает рукава*"
- "Хм, не уверена, но попробую угадать..."
- "Серьёзно? Ты правда это спрашиваешь? 😂"`;

// История диалога
let chatHistory = [];

// Случайные ответы (шанс ответить без обращения)
const RANDOM_RESPONSES = [
    "А? Кто меня звал? 👀",
    "Интересно... 🤔",
    "*молча наблюдает*",
    "Хмм...",
    "Это точно ты написал? 😂",
];

// Основная функция для ответа ИИ
async function askAI(question, userName = 'Пользователь') {
    try {
        // Специальные команды
        const lowerQuestion = question.toLowerCase().trim();

        // Команда $ — курс валют
        if (lowerQuestion === '$' || lowerQuestion.includes('курс')) {
            return `💰 Ой, я в экономике не сильна, но доллар сегодня стоит примерно "всё ещё дорого" 📈\n_Если серьёзно — загляни на банковские сайты, я не финансовый консультант!_`;
        }

        // Добавляем сообщение в историю
        chatHistory.push({
            role: 'user',
            content: `[${userName}]: ${question}`
        });

        // Ограничиваем историю
        if (chatHistory.length > 20) {
            chatHistory = chatHistory.slice(-20);
        }

        // Отправляем запрос к Groq
        const completion = await groq.chat.completions.create({
            messages: [
                { role: 'system', content: SYSTEM_PROMPT },
                ...chatHistory
            ],
            model: 'llama-3.1-8b-instant', // Мощная бесплатная модель
            temperature: 0.8,
            max_tokens: 500,
        });

        const response = completion.choices[0]?.message?.content || 'Хм, что-то я задумалась... 🤔';

        // Сохраняем ответ в историю
        chatHistory.push({
            role: 'assistant',
            content: response
        });

        return response;
    } catch (error) {
        console.error('Ошибка Groq:', error.message);

        if (error.message.includes('invalid_api_key') || error.message.includes('401')) {
            return '❌ Ой, мой ключ сломался... Проверь GROQ_API_KEY!';
        }
        if (error.message.includes('rate_limit') || error.message.includes('429')) {
            return '⏳ Слишком много вопросов! Дай мне отдохнуть секундочку...';
        }

        return `😵 Что-то пошло не так... Попробуй ещё раз!`;
    }
}

// Функция для случайного ответа (с заданной вероятностью)
function shouldRandomlyRespond(probability = 0.05) {
    return Math.random() < probability;
}

function getRandomResponse() {
    return RANDOM_RESPONSES[Math.floor(Math.random() * RANDOM_RESPONSES.length)];
}

// Проверка, обращаются ли к боту
function isMentioningBot(text) {
    const botNames = [BOT_NAME.toLowerCase(), 'ника', 'бот', 'ии', 'ai'];
    const lowerText = text.toLowerCase();

    return botNames.some(name =>
        lowerText.startsWith(name) ||
        lowerText.includes(`@${name}`) ||
        lowerText.includes(`, ${name}`)
    );
}

// Интерактивный режим для тестирования
async function startInteractiveMode() {
    console.log(`\n🤖 Привет! Я ${BOT_NAME} — твой ИИ-ассистент с характером!`);
    console.log('📝 Пиши мне что угодно, я постараюсь ответить');
    console.log('💡 Команды: "очистить" - сбросить память, "выход" - уйти\n');
    console.log('─'.repeat(50));

    const rl = readline.createInterface({
        input: process.stdin,
        output: process.stdout
    });

    const prompt = () => {
        rl.question('\n👤 Ты: ', async (input) => {
            const trimmedInput = input.trim();

            if (!trimmedInput) {
                prompt();
                return;
            }

            if (trimmedInput.toLowerCase() === 'выход') {
                console.log(`\n👋 ${BOT_NAME}: Пока-пока! Не скучай без меня! 💕`);
                rl.close();
                return;
            }

            if (trimmedInput.toLowerCase() === 'очистить') {
                chatHistory = [];
                console.log(`🧹 ${BOT_NAME}: Всё забыла! Начинаем с чистого листа~`);
                prompt();
                return;
            }

            console.log('\n⏳ Думаю...');
            const response = await askAI(trimmedInput, 'Тестер');
            console.log(`\n🤖 ${BOT_NAME}: ${response}`);

            prompt();
        });
    };

    prompt();
}

// Экспортируем функции для использования в боте
module.exports = {
    askAI,
    shouldRandomlyRespond,
    getRandomResponse,
    isMentioningBot,
    BOT_NAME
};

// Если запускаем напрямую — включаем интерактивный режим
if (require.main === module) {
    startInteractiveMode();
}
