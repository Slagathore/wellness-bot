-- Add per-user personality tracking
-- This fixes the bug where changing personality affects ALL users globally

ALTER TABLE users ADD COLUMN personality TEXT DEFAULT 'friendly';

-- Update existing users to have default personality
UPDATE users SET personality = 'friendly' WHERE personality IS NULL;

-- Create index for faster lookups
CREATE INDEX IF NOT EXISTS idx_users_personality ON users(personality);
