-- The virtual waiting room: students join a queue and are admitted at a rate the system can serve.
CREATE TABLE queue_entries (
    user_id        bigint      PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    seq            bigint      GENERATED ALWAYS AS IDENTITY UNIQUE,   -- order of arrival
    joined_at      timestamptz NOT NULL DEFAULT now(),
    admitted_at    timestamptz,
    admitted_until timestamptz
);
CREATE INDEX queue_entries_waiting_idx ON queue_entries (seq) WHERE admitted_at IS NULL;
