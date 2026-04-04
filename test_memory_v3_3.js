require('dotenv').config();
const { getRelevantFacts } = require('./vectorMemory');
const { transliterate } = require('./database');

async function test() {
    const chatId = -1002349071063; // ID из логов пользователя
    const userName = "Temirlan";
    const participants = [
        { firstName: "Temirlan", username: "temi05" },
        { firstName: "Чика", username: "chika_boss" }
    ];

    console.log("--- ТЕСТ 1: Поиск по транслитерации (Temirlan -> Темирлан) ---");
    const res1 = await getRelevantFacts(chatId, "Что любит Темирлан?", userName, participants);
    console.log("Результат 1:", res1 || "НИЧЕГО НЕ НАЙДЕНО");

    console.log("\n--- ТЕСТ 2: Поиск по 'Я' (Личный запрос) ---");
    const res2 = await getRelevantFacts(chatId, "Что я люблю?", userName, participants);
    console.log("Результат 2:", res2 || "НИЧЕГО НЕ НАЙДЕНО");

    console.log("\n--- ТЕСТ 3: Поиск по ключевому слову (Among Us) ---");
    const res3 = await getRelevantFacts(chatId, "Кто играет в Among Us?", userName, participants);
    console.log("Результат 3:", res3 || "НИЧЕГО НЕ НАЙДЕНО");

    console.log("\n--- ТЕСТ 4: Проверка транслитерации функции ---");
    console.log("Temirlan ->", transliterate("Temirlan"));
    console.log("Темирлан ->", transliterate("Темирлан"));
}

test().then(() => process.exit());
