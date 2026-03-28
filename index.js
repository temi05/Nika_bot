const express = require('express');
const path = require('path');
const { bot, token } = require('./config');
const { registerCommands } = require('./handlers/commandHandler');
const { registerAdminCommands } = require('./handlers/adminCommands');
const { registerMessageHandlers, handleReaction } = require('./handlers/messageHandler');
const { registerVerificationHandlers } = require('./handlers/verificationHandler');
const apiRouter = require('./handlers/apiHandler');

// 1. Настройка Express и Webhook
const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const RENDER_URL = process.env.RENDER_EXTERNAL_URL;

app.use('/miniapp', express.static(path.join(__dirname, 'miniapp')));
app.use('/api', apiRouter);

app.get('/', (req, res) => res.send('Бот работает! 🤖 (Modular Edition)'));

app.post(`/bot${token}`, (req, res) => {
    // Безопасность: Проверка секретного токена вебхука
    const secretToken = req.headers['x-telegram-bot-api-secret-token'];
    if (RENDER_URL && secretToken !== process.env.WEBHOOK_SECRET_TOKEN) {
        console.warn('⚠️ Остановка неавторизованного запроса на вебхук!');
        return res.sendStatus(403);
    }

    if (req.body.message_reaction) {
        handleReaction(req.body.message_reaction);
    }
    bot.processUpdate(req.body);
    res.sendStatus(200);
});

app.listen(PORT, async () => {
    console.log(`🚀 Сервер запущен на порту ${PORT}`);
    
    // Проверка токена
    try {
        const me = await bot.getMe();
        console.log(`✅ Токен валиден! Бот: @${me.username} (ID: ${me.id})`);
    } catch (e) {
        console.error(`❌ КРИТИЧЕСКАЯ ОШИБКА: Токен невалиден! Telegram ответил: ${e.message}`);
        const tokenSafe = token ? `${token.substring(0, 4)}...${token.substring(token.length - 4)}` : 'NULL';
        console.log(`ℹ️ Используемый токен: [${tokenSafe}], длина: ${token ? token.length : 0}`);
        console.log(`⚠️ Пожалуйста, перевыпустите токен в @BotFather и обновите его в Render.`);
    }

    if (RENDER_URL) {
        try {
            const baseUrl = RENDER_URL.replace(/\/$/, '');
            const webhookUrl = `${baseUrl}/bot${token}`;
            // Генерируем или берем секретный токен
            const secretToken = process.env.WEBHOOK_SECRET_TOKEN || Math.random().toString(36).substring(2, 15);
            process.env.WEBHOOK_SECRET_TOKEN = secretToken; // Сохраняем в памяти для проверки

            await bot.setWebHook(webhookUrl, {
                allowed_updates: ['message', 'message_reaction', 'chat_member', 'callback_query'],
                secret_token: secretToken
            });
            console.log(`✅ Вебхук установлен: ${webhookUrl}`);
        } catch (e) {
            console.error(`❌ Ошибка вебхука: ${e.message}`);
        }
    }
});

// 2. Инициализация обработчиков
registerVerificationHandlers(); // Капча первой
registerMessageHandlers();      // Логика сообщений
registerCommands();             // Командные обработчики
registerAdminCommands();        // Команды администраторов

console.log('Бот успешно инициализирован в модульном режиме.');
