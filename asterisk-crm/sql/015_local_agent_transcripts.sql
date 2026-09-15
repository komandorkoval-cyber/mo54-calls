-- The home Windows agent sends verified text and segment metadata only.
-- Provider audio, browser cookies, recording URLs, and passwords never enter CRM.
ALTER TABLE transcripts
    ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'legacy'
        CHECK (source_kind IN ('legacy', 'local_browser_agent')),
    ADD COLUMN IF NOT EXISTS source_audio_sha256 TEXT;

ALTER TABLE transcript_segments
    ADD COLUMN IF NOT EXISTS speaker_label TEXT;

-- The audio hash gives retry-safe delivery semantics without retaining audio.
CREATE UNIQUE INDEX IF NOT EXISTS transcripts_local_agent_audio_uidx
    ON transcripts(call_id, source_kind, source_audio_sha256)
    WHERE source_kind = 'local_browser_agent' AND source_audio_sha256 IS NOT NULL;
