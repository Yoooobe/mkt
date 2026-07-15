-- 4unik.mail tracking — schema do D1 (4unik_mail_tracking)
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- cursor de sincronização
  type        TEXT,                               -- open | click
  track_id    TEXT,
  email       TEXT DEFAULT '',
  campaign_id INTEGER,
  etapa       TEXT DEFAULT '',
  url         TEXT DEFAULT '',
  ts          TEXT DEFAULT '',
  ua          TEXT DEFAULT '',
  cf_country  TEXT DEFAULT '',
  prefetch    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_track ON events(track_id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events(type);
