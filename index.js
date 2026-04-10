const express = require('express');
const path = require('path');
const { bot, token } = require('./config');
const { registerCommands } = require('./handlers/commandHandler');
const { registerAdminCommands } = require('./handlers/adminCommands');
const { registerMessageHandlers, handleReaction } = require('./handlers/messageHandler');
const { registerVerificationHandlers } = require('./handlers/verificationHandler');
const apiRouter = require('./handlers/apiHandler');
const { getDueReminders, markReminderAsSent } = require('./database');

// ─────────────────────────────────────────────────────────────
// СТРУКТУРИРОВАННЫЙ LOGGER ДЛЯ RENDER
// ─────────────────────────────────────────────────────────────
const startTime = Date.now();

global.log = {
    info: (...args) => process.stdout.write(`[${new Date().toISOString()}] [INFO]  ${args.join(' ')}\n`),
    warn: (...args) => process.stdout.write(`[${new Date().toISOString()}] [WARN]  ${args.join(' ')}\n`),
    error: (...args) => process.stderr.write(`[${new Date().toISOString()}] [ERROR] ${args.join(' ')}\n`),
    debug: (...args) => process.stdout.write(`[${new Date().toISOString()}] [DEBUG] ${args.join(' ')}\n`),
};

// Перехватываем console чтобы все логи имели timestamp
const _origLog = console.log.bind(console);
const _origWarn = console.warn.bind(console);
const _origErr = console.error.bind(console);
console.log = (...a) => _origLog(`[${new Date().toISOString()}]`, ...a);
console.warn = (...a) => _origWarn(`[${new Date().toISOString()}] ⚠️`, ...a);
console.error = (...a) => _origErr(`[${new Date().toISOString()}] ❌`, ...a);

// ─────────────────────────────────────────────────────────────
// НАСТРОЙКА EXPRESS И ВЕБХУК
// ─────────────────────────────────────────────────────────────
const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const RENDER_URL = process.env.RENDER_EXTERNAL_URL;
const LOG_WEBHOOK_MESSAGES = process.env.LOG_WEBHOOK_MESSAGES === '1';
const LOG_WEBHOOK_REACTIONS = process.env.LOG_WEBHOOK_REACTIONS === '1';
const LOG_WEBHOOK_CALLBACKS = process.env.LOG_WEBHOOK_CALLBACKS !== '0';

app.use('/miniapp', express.static(path.join(__dirname, 'miniapp')));
app.use('/api', apiRouter);

// Статус бота (для Render health check)
app.get('/', (req, res) => res.json({
    status: 'running',
    bot: 'НейроНика',
    version: require('./package.json').version || '1.0.0',
    uptime_seconds: Math.floor((Date.now() - startTime) / 1000),
    timestamp: new Date().toISOString()
}));

// /health — детальный статус для мониторинга Render
app.get('/health', (req, res) => {
    const uptimeSec = Math.floor((Date.now() - startTime) / 1000);
    const h = Math.floor(uptimeSec / 3600);
    const m = Math.floor((uptimeSec % 3600) / 60);
    const s = uptimeSec % 60;
    res.json({
        status: 'ok',
        uptime: `${h}ч ${m}м ${s}с`,
        uptime_seconds: uptimeSec,
        memory_mb: Math.round(process.memoryUsage().heapUsed / 1024 / 1024),
        node_version: process.version,
        timestamp: new Date().toISOString(),
        env: process.env.NODE_ENV || 'production'
    });
});

// Webhook endpoint — с логированием каждого входящего события
app.post(`/bot${token}`, (req, res) => {
    const secretToken = req.headers['x-telegram-bot-api-secret-token'];
    if (RENDER_URL && secretToken !== process.env.WEBHOOK_SECRET_TOKEN) {
        console.warn('Остановка неавторизованного запроса на вебхук!');
        return res.sendStatus(403);
    }

    // Логируем тип входящего события
    const body = req.body;
    if (body.message && LOG_WEBHOOK_MESSAGES) {
        const m = body.message;
        const chatId = m.chat?.id;
        const userId = m.from?.id;
        const text = m.text || m.caption || '[медиа]';
        console.log(`📨 [WEBHOOK] msg | chat:${chatId} | user:${userId} | text: ${text.slice(0, 50)}`);
    } else if (body.message_reaction && LOG_WEBHOOK_REACTIONS) {
        console.log(`👍 [WEBHOOK] reaction | chat:${body.message_reaction.chat?.id}`);
    } else if (body.callback_query && LOG_WEBHOOK_CALLBACKS) {
        console.log(`🔘 [WEBHOOK] callback | user:${body.callback_query.from?.id} | data:${body.callback_query.data}`);
    } else {
        const updateType = Object.keys(body).filter(k => k !== 'update_id')[0] || 'unknown';
        console.log(`📡 [WEBHOOK] update_type:${updateType}`);
    }

    if (body.message_reaction) {
        handleReaction(body.message_reaction);
    }
    bot.processUpdate(body);
    res.sendStatus(200);
});

app.listen(PORT, async () => {
    console.log(`🚀 ══════════════════════════════════════`);
    console.log(`🚀  НЕЙРОНИКА ЗАПУСКАЕТСЯ`);
    console.log(`🚀  Порт: ${PORT} | Env: ${process.env.NODE_ENV || 'production'}`);
    console.log(`🚀 ══════════════════════════════════════`);

    try {
        const me = await bot.getMe();
        console.log(`✅ Бот авторизован: @${me.username} (ID: ${me.id})`);
    } catch (e) {
        const tokenSafe = token ? `${token.substring(0, 6)}...${token.substring(token.length - 4)}` : 'NULL';
        console.error(`Токен невалиден! Используемый: [${tokenSafe}]`);
        console.error(`Ответ Telegram: ${e.message}`);
        console.warn(`Перевыпусти токен в @BotFather и обнови переменную в Render.`);
    }

    if (RENDER_URL) {
        try {
            const baseUrl = RENDER_URL.replace(/\/$/, '');
            const webhookUrl = `${baseUrl}/bot${token}`;
            const secretToken = process.env.WEBHOOK_SECRET_TOKEN || Math.random().toString(36).substring(2, 15);
            process.env.WEBHOOK_SECRET_TOKEN = secretToken;

            await bot.setWebHook(webhookUrl, {
                allowed_updates: ['message', 'message_reaction', 'chat_member', 'callback_query'],
                secret_token: secretToken
            });
            console.log(`✅ Вебхук установлен: ${webhookUrl}`);
        } catch (e) {
            console.error(`Ошибка вебхука: ${e.message}`);
        }
    } else {
        console.warn(`RENDER_EXTERNAL_URL не задан — вебхук не установлен (локальный режим?)`);
    }
});

// ─────────────────────────────────────────────────────────────
// ИНИЦИАЛИЗАЦИЯ ОБРАБОТЧИКОВ
// ─────────────────────────────────────────────────────────────
registerVerificationHandlers(); // Капча первой
registerMessageHandlers();      // Логика сообщений
registerCommands();             // Командные обработчики
registerAdminCommands();        // Команды администраторов

// ─────────────────────────────────────────────────────────────
// ФОНОВЫЕ ЗАДАЧИ
// ─────────────────────────────────────────────────────────────
setInterval(async () => {
    try {
        const dueReminders = await getDueReminders();
        if (dueReminders && dueReminders.length > 0) {
            for (const r of dueReminders) {
                const mention = r.user_name && r.user_name !== 'Инкогнито' ? `${r.user_name}` : `(ID: ${r.user_id})`;
                await bot.sendMessage(r.chat_id, `⏰ Дзынь-дзынь! Напоминалочка для ${mention}:\n\n${r.text}`);
                await markReminderAsSent(r.id);
            }
        }
    } catch (e) {
        console.error('[REMINDER SYSTEM ERROR]', e.message);
    }
}, 60000); // Каждую минуту проверяем таймеры

console.log('✅ Все обработчики зарегистрированы.');
