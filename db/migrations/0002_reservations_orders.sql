-- Phase 2: seats are claimed by short-lived holds, paid for through orders, and confirmed by a
-- worker. This replaces the phase 1 `registrations` table and `events.seats_taken`.
--
-- Phase 1 registrations were free placeholders with no payment attached and nothing was ever
-- deployed on them, so they are dropped rather than converted.

DROP TABLE registrations;
ALTER TABLE events DROP CONSTRAINT events_seats_within_capacity;
ALTER TABLE events DROP COLUMN seats_taken;

-- `claimed` is the counter used by the atomic_update strategy only. It is a cache of the number
-- of active reservations, rebuilt by rebuild_inventory(); reservations are the source of truth.
ALTER TABLE events ADD COLUMN claimed integer NOT NULL DEFAULT 0;
ALTER TABLE events ADD CONSTRAINT events_claimed_within_capacity
    CHECK (claimed BETWEEN 0 AND capacity);

CREATE TABLE orders (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    status     text        NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'paid', 'confirmed', 'payment_failed', 'expired', 'refunded')),
    total      integer     NOT NULL CHECK (total >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_user_id_idx ON orders (user_id);

CREATE TABLE reservations (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    event_id   bigint      NOT NULL REFERENCES events (id) ON DELETE RESTRICT,
    status     text        NOT NULL DEFAULT 'held'
               CHECK (status IN ('held', 'confirmed', 'expired', 'cancelled')),
    order_id   bigint      REFERENCES orders (id) ON DELETE SET NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- A student holds or owns at most one seat per event. Expired and cancelled rows are history.
CREATE UNIQUE INDEX reservations_one_active_per_user_event
    ON reservations (user_id, event_id) WHERE status IN ('held', 'confirmed');
CREATE INDEX reservations_active_by_event ON reservations (event_id) WHERE status IN ('held', 'confirmed');
CREATE INDEX reservations_holds_by_expiry ON reservations (expires_at) WHERE status = 'held';
CREATE INDEX reservations_order_id_idx ON reservations (order_id);

-- One row per physical seat. Only the skip_locked strategy assigns them to reservations.
CREATE TABLE seats (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id       bigint NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    label          text   NOT NULL,
    reservation_id bigint UNIQUE REFERENCES reservations (id) ON DELETE SET NULL,
    UNIQUE (event_id, label)
);
CREATE INDEX seats_free_by_event ON seats (event_id, id) WHERE reservation_id IS NULL;

CREATE TABLE payments (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     bigint      NOT NULL UNIQUE REFERENCES orders (id) ON DELETE CASCADE,
    provider_key text        NOT NULL UNIQUE,   -- the idempotency key sent to the provider
    amount       integer     NOT NULL CHECK (amount >= 0),
    status       text        NOT NULL DEFAULT 'initiated'
                 CHECK (status IN ('initiated', 'processing', 'unknown', 'succeeded', 'failed', 'refunded')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX payments_unsettled_idx ON payments (updated_at) WHERE status IN ('processing', 'unknown');

-- Stored responses for retried requests (see docs/concurrency.md).
CREATE TABLE idempotency_keys (
    user_id         bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    key             text        NOT NULL CHECK (char_length(key) BETWEEN 1 AND 255),
    request_hash    text        NOT NULL,
    response_status integer     NOT NULL,
    response_body   jsonb       NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
CREATE INDEX idempotency_keys_created_idx ON idempotency_keys (created_at);

-- Transactional outbox: written in the same transaction as the change it announces.
CREATE TABLE outbox (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic        text        NOT NULL,
    payload      jsonb       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    available_at timestamptz NOT NULL DEFAULT now(),
    attempts     integer     NOT NULL DEFAULT 0,
    last_error   text,
    processed_at timestamptz,
    dead_at      timestamptz
);
CREATE INDEX outbox_pending_idx ON outbox (available_at, id)
    WHERE processed_at IS NULL AND dead_at IS NULL;

-- The "emails". Nothing is sent anywhere: a confirmation is a row, once per order and kind.
CREATE TABLE mailbox (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id   bigint      NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    user_id    bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind       text        NOT NULL,
    subject    text        NOT NULL,
    body       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (order_id, kind)
);

-- The mock payment provider's own ledger. It stands in for an external system, so it is
-- written only by festrush.mockpay, through its own connections.
CREATE TABLE mock_charges (
    provider_key text        PRIMARY KEY,
    amount       integer     NOT NULL,
    outcome      text        NOT NULL CHECK (outcome IN ('succeeded', 'declined')),
    refunded     boolean     NOT NULL DEFAULT false,
    captured_at  timestamptz NOT NULL DEFAULT now()
);

-- State machines enforced by the database, so no code path can write an illegal move.
CREATE FUNCTION enforce_order_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF ROW(OLD.status, NEW.status) NOT IN (
        ROW('pending', 'paid'), ROW('paid', 'confirmed'),
        ROW('pending', 'payment_failed'),
        ROW('pending', 'expired'), ROW('payment_failed', 'expired'),
        ROW('expired', 'refunded')
    ) THEN
        RAISE EXCEPTION 'illegal order transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER orders_transition BEFORE UPDATE OF status ON orders
    FOR EACH ROW EXECUTE FUNCTION enforce_order_transition();

CREATE FUNCTION enforce_reservation_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF ROW(OLD.status, NEW.status) NOT IN (
        ROW('held', 'confirmed'), ROW('held', 'expired'), ROW('held', 'cancelled')
    ) THEN
        RAISE EXCEPTION 'illegal reservation transition % -> %', OLD.status, NEW.status
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER reservations_transition BEFORE UPDATE OF status ON reservations
    FOR EACH ROW EXECUTE FUNCTION enforce_reservation_transition();

-- Rebuilds the derived inventory state (seat rows, seat assignments, the claimed counter) from
-- the reservations. Run it after changing a capacity or switching the inventory strategy.
-- If more reservations are active than capacity (what the naive strategy can cause), only the
-- first `capacity` of them get seats and `claimed` is capped.
CREATE FUNCTION rebuild_inventory(p_event bigint DEFAULT NULL) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO seats (event_id, label)
    SELECT e.id, 'S' || lpad(n::text, 4, '0')
    FROM events e CROSS JOIN LATERAL generate_series(1, e.capacity) AS n
    WHERE p_event IS NULL OR e.id = p_event
    ON CONFLICT (event_id, label) DO NOTHING;

    DELETE FROM seats s USING events e
    WHERE s.event_id = e.id AND (p_event IS NULL OR e.id = p_event)
      AND substring(s.label FROM 2)::int > e.capacity;

    UPDATE seats SET reservation_id = NULL
    WHERE (p_event IS NULL OR event_id = p_event) AND reservation_id IS NOT NULL;

    WITH r AS (
        SELECT id, event_id, row_number() OVER (PARTITION BY event_id ORDER BY id) AS rn
        FROM reservations
        WHERE status IN ('held', 'confirmed') AND (p_event IS NULL OR event_id = p_event)
    ), s AS (
        SELECT id, event_id, row_number() OVER (PARTITION BY event_id ORDER BY label) AS rn
        FROM seats WHERE p_event IS NULL OR event_id = p_event
    )
    UPDATE seats SET reservation_id = r.id
    FROM r JOIN s ON s.event_id = r.event_id AND s.rn = r.rn
    WHERE seats.id = s.id;

    UPDATE events e SET claimed = LEAST(e.capacity, (
        SELECT count(*) FROM reservations r
        WHERE r.event_id = e.id AND r.status IN ('held', 'confirmed')))
    WHERE p_event IS NULL OR e.id = p_event;
END $$;

-- New events get their seats straight away.
CREATE FUNCTION events_create_seats() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM rebuild_inventory(NEW.id);
    RETURN NULL;
END $$;
CREATE TRIGGER events_after_insert AFTER INSERT ON events
    FOR EACH ROW EXECUTE FUNCTION events_create_seats();

-- Backfill seats for events that already exist.
SELECT rebuild_inventory();
