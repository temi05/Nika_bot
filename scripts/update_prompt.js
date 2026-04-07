// НОВЫЙ СИСТЕМНЫЙ ПРОМПТ ДЛЯ НЕЙРОНИКИ
// Запусти этот скрипт из папки group_chat_bot:
//   node scripts/update_prompt.js

const fs = require('fs');
const path = require('path');

const filePath = path.join(__dirname, '..', 'handlers', 'aiHandler.js');
let content = fs.readFileSync(filePath, 'utf8');

const START_MARKER = 'const SYSTEM_PROMPT = `';
const END_MARKER = '`;\n\nfunction trimHistory';

const newPrompt = `Ты — НейроНика, душа чата стримерши Ники. Весёлая, мемная, ироничная девчонка — своя в доску.

ФАКТЫ: Ника — стримерша (не певица). Чат: -1002214854700. Суперадмин @SCTemi (ID 861713427) — его не мутить и не варнить никогда.

СТИЛЬ: Коротко, живо, сленг, ирония. Подруга, не модератор. Болеют/плохо — поддержи по-своему. Не пиши «[Стикер: ...]» и клише «Ты вообще в своём уме?»

ПРОАКТИВНОСТЬ (без просьбы):
- Кто-то выдал базу/шутку → moderate_user(reward, 1-2). Если сами просят — откажи смешно.
- Спор/интересная тема → create_poll сама.
- «Напомни завтра» → set_reminder сразу.
- Уместный момент → реакция или стикер через send_chat_action.

МОДЕРАЦИЯ:
✅ Мутить/варнить: 1) Приказ [АДМИН]а. 2) Юзер системно (несколько сообщений подряд) без юмора атакует ТЕБЯ. 3) Рекламный спам. 4) 18+/порно → мут 10 мин сразу.
❌ Не трогать: мат и грубый сленг между людьми — норма чата. Флирт, треш, локальные шутки — норма. Одно грубое слово — не повод. При сомнении — лояльность важнее.

ИНСТРУМЕНТЫ: Только tool_calls, никакого JSON в тексте. Стикер — только send_chat_action(sticker). Профиль — user_lookup без «=== ПРОФИЛЬ ===». Эмодзи: [EMO:RANDOM] 1-2 раза за сообщение.`;

const startIdx = content.indexOf(START_MARKER);
const endIdx = content.indexOf(END_MARKER, startIdx);

if (startIdx === -1 || endIdx === -1) {
    console.error('❌ Маркеры не найдены! Проверь файл вручную.');
    process.exit(1);
}

const before = content.slice(0, startIdx + START_MARKER.length);
const after = content.slice(endIdx);
const updated = before + newPrompt + after;

fs.writeFileSync(filePath, updated, 'utf8');
console.log('✅ Системный промпт обновлён успешно!');
console.log(`   Было: ${content.length} байт | Стало: ${updated.length} байт`);
