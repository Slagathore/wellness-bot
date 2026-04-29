-- =============================================================================
-- CORE IDENTITY & SESSIONS
-- =============================================================================

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER UNIQUE NOT NULL,
    telegram_username TEXT,
    display_name TEXT,
    onboarding_completed INTEGER DEFAULT 0,
    onboarding_data TEXT DEFAULT '{}',
    feature_flags TEXT DEFAULT '{"mood_journaling": false, "medication_tracking": false, "sleep_tracking": false, "wellness_goals": false, "social_reminders": false}',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_active_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scope TEXT DEFAULT 'standard' CHECK(scope IN ('standard', 'roleplay', 'downbad')),
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_message_at TEXT DEFAULT CURRENT_TIMESTAMP,
    message_count INTEGER DEFAULT 0,
    token_count INTEGER DEFAULT 0,
    ctx_token_budget INTEGER DEFAULT 8000,
    summary TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'archived')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_user_scope_status ON sessions(user_id, scope, status);

-- =============================================================================
-- MESSAGES (APPEND-ONLY LOG)
-- =============================================================================

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    scope TEXT DEFAULT 'standard' CHECK(scope IN ('standard', 'roleplay', 'downbad')),
    telegram_message_id INTEGER,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system', 'server_event')),
    content TEXT NOT NULL,
    media_type TEXT,
    media_path TEXT,
    tokens INTEGER,
    shard_path TEXT,
    processed INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    UNIQUE(user_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_user_time ON messages(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_unprocessed ON messages(processed);
CREATE INDEX IF NOT EXISTS idx_messages_user_session_id ON messages(user_id, session_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_user_scope_time ON messages(user_id, scope, timestamp);

-- =============================================================================
-- SENTIMENT ANALYSIS
-- =============================================================================

CREATE TABLE IF NOT EXISTS sentiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER UNIQUE NOT NULL,
    valence REAL,
    arousal REAL,
    dominance REAL,
    emotion_label TEXT,
    confidence REAL,
    processed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sentiments_message ON sentiments(message_id);
CREATE INDEX IF NOT EXISTS idx_sentiments_valence ON sentiments(valence);

-- =============================================================================
-- VECTOR EMBEDDINGS
-- =============================================================================

CREATE TABLE IF NOT EXISTS embedding_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER UNIQUE NOT NULL,
    backend TEXT NOT NULL,
    backend_key TEXT NOT NULL,
    model TEXT DEFAULT 'nomic-embed-text',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_embed_links_msg ON embedding_links(message_id);
CREATE INDEX IF NOT EXISTS idx_embed_links_backend ON embedding_links(backend);

-- =============================================================================
-- REMINDERS & COACHING
-- =============================================================================

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    next_run_at TEXT NOT NULL,
    cadence_cron TEXT,
    enabled INTEGER DEFAULT 1,
    last_delivered_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reminders_next_run ON reminders(next_run_at, enabled);

-- =============================================================================
-- MEDICATIONS
-- =============================================================================

CREATE TABLE IF NOT EXISTS medications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    dosage TEXT,
    schedule_times TEXT,
    schedule_days TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS medication_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    medication_id INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    taken_at TEXT,
    confirmed_by_user INTEGER DEFAULT 0,
    reminder_sent INTEGER DEFAULT 0,
    notes TEXT,
    FOREIGN KEY (medication_id) REFERENCES medications(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_med_logs_scheduled ON medication_logs(medication_id, scheduled_at);

-- =============================================================================
-- WEARABLE DATA
-- =============================================================================

CREATE TABLE IF NOT EXISTS sleep_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    source TEXT,
    date TEXT NOT NULL,
    metrics TEXT NOT NULL,
    uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    file_path TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(user_id, source, date)
);

CREATE INDEX IF NOT EXISTS idx_sleep_date ON sleep_data(user_id, date);

-- =============================================================================
-- MOOD JOURNALING
-- =============================================================================

CREATE TABLE IF NOT EXISTS mood_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    mood_label TEXT,
    mood_score INTEGER CHECK (mood_score BETWEEN 1 AND 10),
    note TEXT,
    tags TEXT,
    triggered_by_reminder INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mood_timestamp ON mood_journal(user_id, timestamp);

-- =============================================================================
-- MODERATION & SAFETY
-- =============================================================================

CREATE TABLE IF NOT EXISTS moderation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    severity INTEGER CHECK (severity BETWEEN 1 AND 5),
    details TEXT DEFAULT '{}',
    resolved INTEGER DEFAULT 0,
    resolved_at TEXT,
    resolved_by TEXT,
    notes TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_moderation_unresolved ON moderation_events(user_id, resolved, timestamp);
CREATE INDEX IF NOT EXISTS idx_moderation_severity ON moderation_events(severity, resolved, timestamp);

CREATE TABLE IF NOT EXISTS rate_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    violations INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ok' CHECK(status IN ('ok', 'warned', 'blocked', 'banned')),
    blocked_until TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rate_limits_active ON rate_limits(user_id, window_end, status);

-- =============================================================================
-- PROFILE ASSESSMENT SESSIONS
-- =============================================================================

CREATE TABLE IF NOT EXISTS profile_assessment_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    focus_area TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'completed', 'cancelled')),
    question_data TEXT NOT NULL,
    current_index INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profile_assessment_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    question_index INTEGER NOT NULL,
    question TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES profile_assessment_sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_assessment_sessions_user ON profile_assessment_sessions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_assessment_responses_session ON profile_assessment_responses(session_id);

-- =============================================================================
-- USER FEEDBACK
-- =============================================================================

CREATE TABLE IF NOT EXISTS user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    feedback_type TEXT NOT NULL CHECK(feedback_type IN ('bug', 'suggestion')),
    content TEXT NOT NULL,
    status TEXT DEFAULT 'new' CHECK(status IN ('new', 'reviewing', 'resolved', 'wont_fix')),
    admin_notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feedback_status ON user_feedback(status);
CREATE INDEX IF NOT EXISTS idx_feedback_type ON user_feedback(feedback_type);

-- =============================================================================
-- AUDIT LOG
-- =============================================================================

-- =============================================================================
-- CONVERSATION MEMORY (V2)
-- =============================================================================

CREATE TABLE IF NOT EXISTS conversation_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    scope TEXT DEFAULT 'standard' CHECK(scope IN ('standard', 'roleplay', 'downbad')),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    topics TEXT,
    context_window TEXT,
    embedding TEXT NOT NULL,
    importance_score REAL DEFAULT 5.0,
    emotional_salience REAL DEFAULT 0.0,
    user_value_score REAL DEFAULT 0.0,
    context_score REAL DEFAULT 0.0,
    reference_count INTEGER DEFAULT 0,
    last_referenced_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conv_embeddings_user ON conversation_embeddings(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_embeddings_message ON conversation_embeddings(message_id);
CREATE INDEX IF NOT EXISTS idx_conv_embeddings_user_lastref ON conversation_embeddings(user_id, last_referenced_at DESC);
CREATE INDEX IF NOT EXISTS idx_conv_embeddings_user_importance ON conversation_embeddings(user_id, importance_score DESC);
CREATE INDEX IF NOT EXISTS idx_conv_embeddings_user_scope_lastref ON conversation_embeddings(user_id, scope, last_referenced_at DESC);

-- =============================================================================
-- PROFILE IMPORT DOCUMENTS
-- =============================================================================

CREATE TABLE IF NOT EXISTS profile_import_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    file_name TEXT,
    source TEXT,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_profile_import_user ON profile_import_documents(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    actor TEXT,
    action TEXT NOT NULL,
    target_user_id INTEGER,
    details TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);

CREATE TABLE IF NOT EXISTS turn_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    user_message_id INTEGER,
    assistant_message_id INTEGER,
    correlation_id TEXT,
    user_text TEXT,
    assistant_text TEXT,
    plan_json TEXT DEFAULT '{}',
    route_json TEXT DEFAULT '[]',
    status TEXT DEFAULT 'created',
    followup_json TEXT DEFAULT '{}',
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_turn_audit_user ON turn_audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_turn_audit_correlation ON turn_audit_log(correlation_id);

CREATE TABLE IF NOT EXISTS profile_fact_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    message_id INTEGER,
    correlation_id TEXT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    source TEXT,
    reason TEXT,
    contradiction INTEGER DEFAULT 0,
    existing_value TEXT,
    status TEXT DEFAULT 'pending',
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_profile_fact_user ON profile_fact_candidates(user_id, created_at DESC);

-- =============================================================================
-- WELLNESS GOALS & HABITS
-- =============================================================================

CREATE TABLE IF NOT EXISTS wellness_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    goal_type TEXT NOT NULL,
    target_value REAL,
    target_unit TEXT,
    current_streak INTEGER DEFAULT 0,
    best_streak INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS habit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL,
    logged_at TEXT DEFAULT CURRENT_TIMESTAMP,
    value REAL,
    notes TEXT,
    FOREIGN KEY (goal_id) REFERENCES wellness_goals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_habit_logs_goal ON habit_logs(goal_id, logged_at);

-- =============================================================================
-- SOCIAL CONNECTIONS
-- =============================================================================

CREATE TABLE IF NOT EXISTS social_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    contact_name TEXT NOT NULL,
    relationship TEXT,
    last_mentioned_at TEXT,
    mention_count INTEGER DEFAULT 0,
    reminder_cadence_days INTEGER,
    enabled INTEGER DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- =============================================================================
-- PROFILE CONTEXT (EDITABLE USER MEMORY)
-- =============================================================================

CREATE TABLE IF NOT EXISTS profile_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE(user_id, key)
);

CREATE INDEX IF NOT EXISTS idx_profile_context_user ON profile_context(user_id);

-- =============================================================================
-- USER STREAKS (DAILY ACTIVITY TRACKING)
-- =============================================================================

CREATE TABLE IF NOT EXISTS user_streaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    current_streak INTEGER DEFAULT 0,
    longest_streak INTEGER DEFAULT 0,
    last_activity_date TEXT,
    total_active_days INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- =============================================================================
-- CONVERSATION EXPORTS
-- =============================================================================

CREATE TABLE IF NOT EXISTS conversation_exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    format TEXT NOT NULL CHECK(format IN ('json', 'txt', 'pdf')),
    file_path TEXT,
    start_date TEXT,
    end_date TEXT,
    message_count INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_exports_user ON conversation_exports(user_id, created_at);

-- =============================================================================
-- TELEGRAM OUTBOX (EXACTLY-ONCE DELIVERY)
-- =============================================================================

CREATE TABLE IF NOT EXISTS telegram_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    reply_to_message_id INTEGER,
    sent INTEGER DEFAULT 0,
    sent_at TEXT,
    telegram_message_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON telegram_outbox(sent, created_at);

-- =============================================================================
-- TRANSCRIPT SHARD TRACKING
-- =============================================================================

CREATE TABLE IF NOT EXISTS transcript_shards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    start_msg_id INTEGER,
    end_msg_id INTEGER,
    message_count INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shards_user_session ON transcript_shards(user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_shards_active ON transcript_shards(user_id, session_id, closed_at);

-- =============================================================================
-- PSYCHOLOGICAL PROFILES (200+ METRICS)
-- =============================================================================

CREATE TABLE IF NOT EXISTS psychological_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    profile_data TEXT NOT NULL,                  -- JSON with all 200+ metrics
    mental_health_indicators TEXT,               -- JSON: depression, anxiety, PTSD, OCD, bipolar, ADHD, psychotic, eating disorder, dissociation, body dysmorphia, substance use, addiction, autism
    big_five TEXT,                               -- JSON: openness, conscientiousness, extraversion, agreeableness, neuroticism
    cognitive_metrics TEXT,                      -- JSON: estimated IQ, analytical thinking, emotional intelligence, resilience
    messages_analyzed INTEGER DEFAULT 0,
    confidence_score REAL DEFAULT 0.0,           -- Overall confidence (0.0-1.0)
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_psych_profiles_user ON psychological_profiles(user_id, updated_at DESC);

-- =============================================================================
-- IMAGE UPLOADS (VISION ANALYSIS)
-- =============================================================================

CREATE TABLE IF NOT EXISTS image_uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    mime_type TEXT,
    caption TEXT,
    vision_analysis TEXT,                        -- JSON with LLM vision analysis
    user_comments TEXT,                          -- User's notes/comments about the image
    uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
    processed INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_image_uploads_user ON image_uploads(user_id, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_image_uploads_session ON image_uploads(session_id);
CREATE INDEX IF NOT EXISTS idx_image_uploads_unprocessed ON image_uploads(processed);

-- =============================================================================
-- CHECK-IN CONFIGURATIONS (AUTONOMOUS CHECK-INS)
-- =============================================================================

CREATE TABLE IF NOT EXISTS checkin_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE,
    frequency TEXT NOT NULL CHECK(frequency IN ('daily', 'weekly', 'bi-weekly', 'monthly')),
    personalized_prompt TEXT,                    -- LLM-generated base prompt
    last_checkin_at TEXT,
    next_checkin_at TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_checkin_configs_next ON checkin_configs(next_checkin_at, is_active);
CREATE INDEX IF NOT EXISTS idx_checkin_configs_user ON checkin_configs(user_id);

-- =============================================================================
-- GENERATED MEDIA (IMAGE/VIDEO GENERATION TRACKING)
-- =============================================================================

CREATE TABLE IF NOT EXISTS generated_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    media_type TEXT NOT NULL CHECK(media_type IN ('image', 'video')),
    prompt TEXT NOT NULL,
    negative_prompt TEXT,
    model_used TEXT,
    file_path TEXT,
    file_size INTEGER,
    generation_params TEXT,                      -- JSON: seed, steps, cfg_scale, width, height, etc.
    generation_time_ms INTEGER,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'generating', 'completed', 'failed')),
    error_message TEXT,
    vision_notes TEXT,                           -- Vision model analysis of generated image
    user_comments TEXT,                          -- User's notes/comments
    thread_message_id INTEGER,                   -- Link to conversation thread
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (thread_message_id) REFERENCES messages(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_generated_media_user ON generated_media(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_generated_media_status ON generated_media(status);
CREATE INDEX IF NOT EXISTS idx_generated_media_type ON generated_media(media_type, created_at DESC);

-- =============================================================================
-- CUSTOM CHARACTERS (USER-CREATED / IMPORTED ROLEPLAY CHARACTERS)
-- =============================================================================

CREATE TABLE IF NOT EXISTS custom_characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    emoji TEXT DEFAULT '🎭',
    system_prompt TEXT NOT NULL,
    temperature REAL DEFAULT 0.85,
    top_p REAL DEFAULT 0.9,
    repeat_penalty REAL DEFAULT 1.1,
    initial_message TEXT,
    avatar_url TEXT,
    lore TEXT,
    creator_user_id INTEGER,
    is_global INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (creator_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_custom_characters_creator ON custom_characters(creator_user_id);
CREATE INDEX IF NOT EXISTS idx_custom_characters_global ON custom_characters(is_global);

CREATE TABLE IF NOT EXISTS user_character_access (
    user_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, character_id),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES custom_characters(id) ON DELETE CASCADE
);

-- =============================================================================
-- ROLEPLAY ADVENTURES
-- =============================================================================

CREATE TABLE IF NOT EXISTS adventures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    lore TEXT,                    -- JSON: world-building details for this adventure
    settings TEXT,                -- JSON: player role, reply length, choice mode, lore refresh checkpoints
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'paused', 'completed')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS adventure_characters (
    adventure_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    role TEXT DEFAULT 'npc',      -- 'protagonist', 'npc', 'antagonist', 'companion'
    notes TEXT,                   -- character-specific notes within this adventure
    PRIMARY KEY (adventure_id, character_id),
    FOREIGN KEY (adventure_id) REFERENCES adventures(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES custom_characters(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS adventure_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adventure_id INTEGER NOT NULL,
    character_id INTEGER,         -- NULL = narrator / user action
    role TEXT NOT NULL CHECK(role IN ('user', 'character', 'narrator', 'system')),
    content TEXT NOT NULL,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (adventure_id) REFERENCES adventures(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES custom_characters(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_adventures_user ON adventures(user_id);
CREATE INDEX IF NOT EXISTS idx_adventure_messages_adv ON adventure_messages(adventure_id);
