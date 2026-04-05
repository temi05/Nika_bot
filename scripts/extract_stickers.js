const fs = require('fs');
const path = require('path');

const htmlPath = path.join(__dirname, '..', 'Useless Emotes by @waifuch.html');
const outputPath = path.join(__dirname, '..', 'data', 'stickers.json');

try {
    const html = fs.readFileSync(htmlPath, 'utf8');
    
    // Регулярное выражение для извлечения emoji-id
    // Ищем: <tg-emoji emoji-id=\"(\d+)\">
    const emojiRegex = /emoji-id=\\"(\d+)\\"/g;
    const ids = new Set();
    let match;
    
    while ((match = emojiRegex.exec(html)) !== null) {
        if (match[1]) {
            ids.add(match[1]);
        }
    }

    const stickers = Array.from(ids).map(id => ({
        emoji_id: id,
        type: 'custom_emoji'
    }));

    fs.writeFileSync(outputPath, JSON.stringify(stickers, null, 2));
    console.log(`✅ Извлечено ${stickers.length} стикеров. Сохранено в ${outputPath}`);

} catch (error) {
    console.error('❌ Ошибка при чтении или парсинге HTML:', error.message);
}
