require('dotenv').config();
const TelegramBot = require('node-telegram-bot-api');
const { createClient } = require('@supabase/supabase-js');

// Проверяем наличие токенов
const token = process.env.TELEGRAM_BOT_TOKEN;
const supabaseUrl = process.env.SUPABASE_URL;
const supabaseKey = process.env.SUPABASE_KEY;

if (!token || !supabaseUrl || !supabaseKey) {
    console.error('Ошибка: Не заданы переменные окружения (TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY)');
    process.exit(1);
}

// Инициализация Supabase
const supabase = createClient(supabaseUrl, supabaseKey);

// Создаем бота (без Polling)
const bot = new TelegramBot(token);

module.exports = {
    bot,
    supabase,
    token,
    ANONYMOUS_ADMIN_ID: 1087968824
};
