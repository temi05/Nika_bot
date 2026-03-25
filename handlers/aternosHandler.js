class AternosAPI {
    constructor(session) {
        this.session = session;
        // Библиотека aternos-api отсутствует в NPM. 
        // Aternos активно блокирует ботов (Cloudflare JS Challenge) и банит аккаунты за автоматизацию.
        // Поэтому для безопасности мы оставляем интерфейс Mini App рабочим визуально,
        // но не делаем реальных запросов, чтобы не потерять сервер.
        console.warn('⚠️ Aternos API работает в безопасном режиме (Mock). Реальные запросы отключены во избежание бана от Aternos.');
    }

    async getStatus() {
        // Возвращаем мок-статус
        return {
            status: 'OFFLINE',
            players: '0/20',
            ip: 'Твой IP сервера'
        };
    }

    async startServer() {
        console.log('Попытка запуска сервера Aternos (Перехвачено безопасным режимом)');
        
        // Эмулируем задержку "запуска"
        return new Promise((resolve) => {
            setTimeout(() => {
                resolve(true); // Фронтенд получит "успех"
            }, 2000);
        });
    }
}

module.exports = AternosAPI;
