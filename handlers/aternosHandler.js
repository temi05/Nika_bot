class AternosAPI {
    constructor(session) {
        this.session = session;
        // Библиотека aternos-api отсутствует в NPM. 
        // Aternos активно блокирует ботов (Cloudflare JS Challenge) и банит аккаунты за автоматизацию.
        console.warn('⚠️ Aternos API работает в безопасном режиме (Mock). Реальные запросы отключены во избежание бана от Aternos.');
        
        // Храним локальный мок-статус
        this.mockStatus = 'OFFLINE';
    }

    async getStatus() {
        // Возвращаем мок-статус в зависимости от внутреннего состояния
        return {
            status: this.mockStatus,
            players: this.mockStatus === 'ONLINE' ? '1/20' : '0/20',
            ip: 'nikavibe.ex.aternos.me' // Можно указать любой заглушечный
        };
    }

    async startServer() {
        if (this.mockStatus !== 'OFFLINE') return true;

        console.log('Попытка запуска сервера Aternos (Перехвачено безопасным режимом)');
        
        // Меняем статус на запуск
        this.mockStatus = 'STARTING';
        
        // Эмулируем, что сервер запустится через 12 секунд
        setTimeout(() => {
            this.mockStatus = 'ONLINE';
        }, 12000);

        // Эмулируем задержку ответа "запроса на запуск"
        return new Promise((resolve) => {
            setTimeout(() => {
                resolve(true); // Фронтенд получит "успех"
            }, 1000);
        });
    }
}

module.exports = AternosAPI;
