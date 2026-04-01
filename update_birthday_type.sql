-- Расширяем колонку, чтобы она могла хранить год (DD.MM.YYYY)
ALTER TABLE users ALTER COLUMN birthday TYPE VARCHAR(10);
