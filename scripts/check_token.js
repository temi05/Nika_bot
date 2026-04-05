const TelegramBot = require('node-telegram-bot-api');
require('dotenv').config();

const token = process.env.TELEGRAM_BOT_TOKEN;
if (!token) {
    console.error('❌ Нет токена');
    process.exit(1);
}

const bot = new TelegramBot(token);
bot.getMe().then(me => {
    console.log(`✅ Токен валиден! Бот: @${me.username}`);
    process.exit(0);
}).catch(err => {
    console.error('❌ Ошибка токена:', err.message);
    process.exit(1);
});
