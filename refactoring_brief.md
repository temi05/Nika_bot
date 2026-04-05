# 🚀 ТЕХНИЧЕСКИЙ БРИФ ДЛЯ РЕФАКТОРИНГА НЕЙРОНИКИ (V5.5)
## ФОКУС: ГЛАЗА ДЛЯ СТИКЕРОВ И ТЕГИРОВАНИЕ НАПОМИНАНИЙ

Последняя версия брифа. Все прошлые идеи по стикерам улучшены до "честного зрения".

---

### 📝 Текст задания для внешнего ИИ:
> "Привет! Обнови логику файла `aiHandler.js`:
> 1. **Зрение для стикеров (Sticker Vision)**: Не доверяй встроенным эмодзи стикеров (они часто врут). Реализуй функцию `describeSticker(fileId)`, которая аналогично `describePhoto` отправляет картинку стикера в Vision модель. 
> 2. **Логика в `processAI`**: Если пришел стикер, Ника должна получить: `[Стикер (эмодзи: 😂): Описание того, что реально на картинке]`.
> 3. **Премиум и Анимация**: Обязательно помечай, если стикер анимированный или премиальный (для саркастичных реакций Ники).
> 4. **Инструмент `send_sticker`**: Вызов `bot.sendSticker`.
> 5. **Фикс пингов (Reminders)**: В `insertReminder` и `startReminderWorker` добавить `username`. Пинговать через `@username` (приоритет) или HTML ссылку на ID (если ника нет).
> 
> Пожалуйста, выдай полный код файла."

---

### 📂 1. Новая функция `describeSticker`:
*(Передай ИИ этот пример для реализации)*:
```javascript
async function describeSticker(fileId) {
    try {
        // Телеграм отдает стикеры в WebP/TGS. 
        // Vision модели хорошо понимают WebP.
        const fileLink = await bot.getFileLink(fileId);
        const description = await openai.chat.completions.create({
            model: "google/gemini-2.0-flash-lite", // или твоя vision модель
            messages: [
                { role: "user", content: [
                    { type: "text", text: "Что изображено на этом стикере? Опиши кратко настроение и персонажа." },
                    { type: "image_url", image_url: { url: fileLink } }
                ]}
            ]
        });
        return description.choices[0].message.content;
    } catch (e) {
        return null;
    }
}
```

---

### 📂 2. Обновленный блок в `processAI`:
```javascript
if (msg.sticker) {
    const s = msg.sticker;
    const visualDesc = await describeSticker(s.file_id); // "Честное зрение"
    const type = s.is_animated ? "Анимированный " : (s.is_video ? "Видео-" : "");
    const info = `${type}стикер (эмодзи: ${s.emoji || "?"})`;
    
    userText = `[${info}: ${visualDesc || "Ника не смогла разглядеть"}]`;
    // Теперь Ника поймет, что если там 'Кот сидит', а эмодзи '😂', то это может быть ирония.
}
```

---

### 📂 3. Фикс напоминаний (для ПИНГА):
В `startReminderWorker` обязательно использовать этот паттерн для текста:
```javascript
const mention = rem.username ? `@${rem.username}` : `<a href="tg://user?id=${rem.user_id}">${rem.user_name}</a>`;
// Важно: Сохранять username в таблицу reminders при вызове инструмента set_reminder!
```

---

### 💡 Совет для пользователя по анимированным стикерам:
Анимированные стикеры (`.tgs`) — это векторные файлы. Некоторые Vision-модели (как Gemini) могут не "видеть" их напрямую. Если Ника не увидит анимацию, другой ИИ может добавить в `describeSticker` логику получения **thumbnail** (превью-картинки) стикера: `s.thumbnail.file_id`. По превью она точно поймет, что там за кот!
