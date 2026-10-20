-- Whole schema. Safe to re-run: it drops everything first.
DROP TABLE IF EXISTS idempotency_keys, activity, work_items, memberships, teams, users CASCADE;
DROP TYPE IF EXISTS item_status, item_priority, item_type CASCADE;

CREATE TYPE item_status AS ENUM
    ('awaiting_approval', 'open', 'in_progress', 'blocked', 'done', 'cancelled');
-- Declaration order is sort order: ORDER BY priority puts urgent first.
CREATE TYPE item_priority AS ENUM ('urgent', 'high', 'medium', 'low');
CREATE TYPE item_type AS ENUM
    ('customer_issue', 'engineering', 'payment', 'incident', 'compliance', 'task');

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    name          text NOT NULL,
    password_hash text NOT NULL
);

CREATE TABLE teams (
    id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE
);

CREATE TABLE memberships (
    user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    team_id uuid NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    role    text NOT NULL CHECK (role IN ('member', 'lead')),
    PRIMARY KEY (user_id, team_id)
);
CREATE INDEX memberships_team_idx ON memberships (team_id);

CREATE TABLE work_items (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id           uuid NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    title             text NOT NULL,
    description       text NOT NULL DEFAULT '',
    type              item_type NOT NULL,
    priority          item_priority NOT NULL,
    status            item_status NOT NULL,
    assignee_id       uuid REFERENCES users (id) ON DELETE SET NULL,
    requires_approval boolean NOT NULL,
    due_at            timestamptz,
    created_by        uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    last_activity_at  timestamptz NOT NULL DEFAULT now(),
    version           integer NOT NULL DEFAULT 1,
    search            tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', title), 'A') ||
        setweight(to_tsvector('english', description), 'B')
    ) STORED
);
CREATE INDEX work_items_search_idx ON work_items USING gin (search);
CREATE INDEX work_items_team_status_idx
    ON work_items (team_id, status, priority, last_activity_at DESC);
CREATE INDEX work_items_assignee_idx ON work_items (assignee_id, status);

-- Append-only history of everything that happened to an item (comments included).
CREATE TABLE activity (
    id           bigserial PRIMARY KEY,
    work_item_id uuid NOT NULL REFERENCES work_items (id) ON DELETE CASCADE,
    actor_id     uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    kind         text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX activity_item_idx ON activity (work_item_id, id DESC);

-- Stored responses so a repeated request returns the first result instead of acting twice.
CREATE TABLE idempotency_keys (
    user_id      uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    key          text NOT NULL,
    request_hash text NOT NULL,
    status_code  integer NOT NULL,
    response     jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
