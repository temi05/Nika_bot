const express = require('express');
const path = require('path');
const { bot, token } = require('./config');
const { registerCommands } = require('./handlers/commandHandler');
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
            await bot.setWebHook(webhookUrl, {
                allowed_updates: ['message', 'message_reaction', 'chat_member', 'callback_query']
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

console.log('Бот успешно инициализирован в модульном режиме.');
