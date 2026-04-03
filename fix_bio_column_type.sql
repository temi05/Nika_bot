-- Меняем тип bio с varchar на text (безлимитный)
ALTER TABLE users 
  ALTER COLUMN bio TYPE TEXT;
