const express = require('express');
const { bot, token } = require('./config');
const { registerCommands } = require('./handlers/commandHandler');
const { registerMessageHandlers, handleReaction } = require('./handlers/messageHandler');
const { registerVerificationHandlers } = require('./handlers/verificationHandler');

// 1. Настройка Express и Webhook
const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const RENDER_URL = process.env.RENDER_EXTERNAL_URL;

app.get('/', (req, res) => res.send('Бот работает! 🤖 (Modular Edition)'));

app.post(`/bot${token}`, (req, res) => {
    if (req.body.message_reaction) {
        handleReaction(req.body.message_reaction);
    }
    bot.processUpdate(req.body);
    res.sendStatus(200);
});

app.listen(PORT, async () => {
    console.log(`🚀 Сервер запущен на порту ${PORT}`);
    if (RENDER_URL) {
        try {
            // Очищаем URL от возможных двойных слэшей в конце
            const baseUrl = RENDER_URL.replace(/\/$/, '');
            const webhookUrl = `${baseUrl}/bot${token}`;
            
            console.log(`📡 Попытка установки вебхука на ${webhookUrl}...`);
            await bot.setWebHook(webhookUrl, {
                allowed_updates: ['message', 'message_reaction', 'chat_member', 'callback_query']
            });
            console.log(`✅ Вебхук успешно установлен!`);
        } catch (e) {
            console.error(`❌ Ошибка установки вебхука: ${e.message}`);
            // Безопасный вывод метаданных токена для отладки
            const tokenSafe = token ? `${token.substring(0, 4)}...${token.substring(token.length - 4)}` : 'NULL';
            console.log(`ℹ️ Параметры отладки:`);
            console.log(`   - Токен (маска): [${tokenSafe}]`);
            console.log(`   - Длина токена: ${token ? token.length : 0} симв.`);
            console.log(`   - RENDER_EXTERNAL_URL: ${RENDER_URL}`);
            console.log(`⚠️ Пожалуйста, проверьте правильность TELEGRAM_BOT_TOKEN в панели Render.`);
        }
    } else {
        console.log('⚠️ RENDER_EXTERNAL_URL не найден, вебхук не установлен.');
    }
});

// 2. Инициализация обработчиков
registerVerificationHandlers(); // Капча первой
registerMessageHandlers();      // Логика сообщений
registerCommands();             // Командные обработчики

console.log('Бот успешно инициализирован в модульном режиме.');
