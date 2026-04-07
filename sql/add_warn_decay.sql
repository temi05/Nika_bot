ALTER TABLE users
ADD COLUMN IF NOT EXISTS last_warn_at timestamptz;
