require('dotenv').config();
const TelegramBot = require('node-telegram-bot-api');
const { createClient } = require('@supabase/supabase-js');

// Проверяем наличие токенов
const token = process.env.TELEGRAM_BOT_TOKEN ? process.env.TELEGRAM_BOT_TOKEN.trim() : null;
const supabaseUrl = process.env.SUPABASE_URL ? process.env.SUPABASE_URL.trim() : null;
const supabaseKey = process.env.SUPABASE_KEY ? process.env.SUPABASE_KEY.trim() : null;
const aternos_session = process.env.ATERNOS_SESSION || null;
const aternos_server_id = process.env.ATERNOS_SERVER_ID || null;

if (!token || !supabaseUrl || !supabaseKey) {
    console.error('❌ Ошибка: Не заданы переменные окружения (TELEGRAM_BOT_TOKEN, SUPABASE_URL или SUPABASE_KEY)');
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
    aternos_session,
    aternos_server_id,
    ANONYMOUS_ADMIN_ID: 1087968824
};
