-- Foundation schema. Closes audit findings D1 (no integrity), A4 (duplicate registrations),
-- A5 (registrations for missing events or users), A6 (no capacity) and prepares S1/S2/S7
-- (hashed passwords, server-side sessions, login throttling).

CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      text        NOT NULL,
    password_hash text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT users_username_format CHECK (username ~ '^[A-Za-z0-9_.-]{3,32}$')
);
-- Usernames are unique regardless of case: "Asha" and "asha" are the same person.
CREATE UNIQUE INDEX users_username_lower_key ON users (lower(username));

-- The browser holds a random token; only its SHA-256 is stored, so a leaked table is not a
-- set of working sessions.
CREATE TABLE sessions (
    token_hash bytea       PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX sessions_user_id_idx ON sessions (user_id);
CREATE INDEX sessions_expires_at_idx ON sessions (expires_at);

CREATE TABLE events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text        NOT NULL UNIQUE,
    description text        NOT NULL DEFAULT '',
    fee         integer     NOT NULL CHECK (fee >= 0),       -- whole rupees
    capacity    integer     NOT NULL CHECK (capacity > 0),
    seats_taken integer     NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- The last line of defence: whatever the application does, the database refuses to
    -- record more registrations than seats.
    CONSTRAINT events_seats_within_capacity CHECK (seats_taken BETWEEN 0 AND capacity)
);

CREATE TABLE registrations (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    event_id   bigint      NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT registrations_one_per_user_event UNIQUE (user_id, event_id)
);
CREATE INDEX registrations_event_id_idx ON registrations (event_id);

-- Failed sign-ins, kept to slow down password guessing (audit S7).
CREATE TABLE login_failures (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username     text        NOT NULL,   -- lower-cased, may not exist
    client       text        NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX login_failures_lookup_idx ON login_failures (username, client, attempted_at);
