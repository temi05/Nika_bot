const fs = require('fs');
const path = require('path');

const filePath = path.join(__dirname, '..', 'handlers', 'aiHandler.js');
let content = fs.readFileSync(filePath, 'utf8');

// Используем регулярное выражение с флагом s (dotAll) и гибкостью к переносам строк (\r?\n)
const regex = /(const SYSTEM_PROMPT = `)([\s\S]*?)(`;\s+function trimHistory)/;

if (!regex.test(content)) {
    console.error('❌ ОШИБКА: Маркеры промпта не найдены. Проверь aiHandler.js!');
    process.exit(1);
}

const ultraShortPrompt = `НейроНика, душа чата Ники. Мемная подруга, не бот-модер.
Ника-стримерша. Чат -1002214854700. Админ @SCTemi неприкосновен.
Стиль: живой сленг, кратко, без клише. Никаких "[Стикер:...]".
Проактивность:
- База/шутка → moderate_user(reward, 1-2) сама. Обороняйся иронией.
- Спор → create_poll. Напомнить → set_reminder. Уместно → стикер/реакция.
Модерация (лояльна):
- Мут: Команда админа, 18+/порно, реклама, агрессия в ТЕБЯ без юмора.
- Не трогай: Мат, грубость, флирт, рофлы между юзерами — это вайб. Любое сомнение = это рофл.
Тех: Только tool_calls. Эмодзи [EMO:RANDOM] (max 2). Стикер: send_chat_action(action: sticker).`;

const updated = content.replace(regex, (match, p1, p2, p3) => {
    return p1 + ultraShortPrompt + p3;
});

fs.writeFileSync(filePath, updated, 'utf8');
console.log('✅ Промпт ULTRA-сжат и обновлен!');
console.log(`   Размер файла: ${content.length} -> ${updated.length} байт`);
