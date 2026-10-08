-- Optional operator-selected model for future jobs. Existing frozen jobs stay intact.
CREATE TABLE IF NOT EXISTS ai_content_settings (
    id integer PRIMARY KEY CHECK (id = 1),
    model text NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
