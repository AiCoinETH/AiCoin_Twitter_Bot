-- Создание таблицы, если её ещё нет
CREATE TABLE IF NOT EXISTS plan_items (
    user_id      INTEGER NOT NULL,
    item_id      INTEGER NOT NULL,
    text         TEXT NOT NULL,
    when_hhmm    TEXT NOT NULL,
    done         INTEGER DEFAULT 0,
    media_file_id TEXT,
    media_type    TEXT,
    PRIMARY KEY (user_id, item_id)
);

-- Тестовые данные для проверки
INSERT INTO plan_items (user_id, item_id, text, when_hhmm, done, media_file_id, media_type)
VALUES
  (12345, 1, 'Test post 1 — добро пожаловать в планировщик!', '09:00', 0, NULL, NULL),
  (12345, 2, 'Test post 2 — проверка публикации', '14:00', 0, NULL, NULL),
  (12345, 3, 'Test post 3 — финальный тестовый пост', '22:00', 0, NULL, NULL);