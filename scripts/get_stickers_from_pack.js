const TelegramBot = require('node-telegram-bot-api');
const fs = require('fs');
const path = require('path');
require('dotenv').config();

const token = process.env.TELEGRAM_BOT_TOKEN;
const packName = 'xuan_sol_by_fStikBot';
const outputPath = path.join(__dirname, '..', 'data', 'stickers.json');

if (!token) {
    console.error('❌ Ошибка: В .env не найден TELEGRAM_BOT_TOKEN');
    process.exit(1);
}

const bot = new TelegramBot(token);

async function fetchStickers() {
    try {
        console.log(`📡 Запрашиваю информацию о паке: ${packName}...`);
        const stickerSet = await bot.getStickerSet(packName);
        
        const newStickers = stickerSet.stickers.map(s => ({
            file_id: s.file_id,
            emoji: s.emoji,
            type: 'sticker'
        }));

        // Загружаем текущие, если есть
        let existingStickers = [];
        if (fs.existsSync(outputPath)) {
            existingStickers = JSON.parse(fs.readFileSync(outputPath, 'utf8'));
        }

        // Фильтруем дубликаты
        const allStickers = [...existingStickers];
        newStickers.forEach(ns => {
            if (!allStickers.find(es => es.file_id === ns.file_id || es.emoji_id === ns.file_id)) {
                allStickers.push(ns);
            }
        });

        fs.writeFileSync(outputPath, JSON.stringify(allStickers, null, 2));
        console.log(`✅ Добавлено ${newStickers.length} стикеров из пака. Всего в базе: ${allStickers.length}`);
        process.exit(0);
    } catch (error) {
        console.error('❌ Ошибка при получении пака:', error.message);
        process.exit(1);
    }
}

fetchStickers();
