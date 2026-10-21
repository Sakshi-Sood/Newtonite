-- Whole schema. Safe to re-run: it drops everything first.
DROP TABLE IF EXISTS idempotency_keys, activity, work_items, task_types, statuses, project_teams, projects, memberships, teams, users CASCADE;
DROP TYPE IF EXISTS item_status, item_priority, item_type CASCADE;  -- item_type: the old fixed list of task types

-- Declaration order is sort order: ORDER BY priority puts urgent first.
CREATE TYPE item_priority AS ENUM ('urgent', 'high', 'medium', 'low');

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text NOT NULL UNIQUE,
    name          text NOT NULL,
    password_hash text NOT NULL,
    is_admin      boolean NOT NULL DEFAULT false,         -- manages teams, users and membership
    active        boolean NOT NULL DEFAULT true,          -- false = cannot log in; history stays
    must_change_password boolean NOT NULL DEFAULT false   -- set while the password is a temporary one
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

-- Projects are made by admins and assigned to any number of teams (and a team to any
-- number of projects). A task may be tagged with one project of its own team.
CREATE TABLE projects (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX projects_name_idx ON projects (lower(name));

CREATE TABLE project_teams (
    project_id uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    team_id    uuid NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, team_id)
);
CREATE INDEX project_teams_team_idx ON project_teams (team_id);

-- Per-team task types. A new team starts with six; its leads and admins rename, add and remove them.
CREATE TABLE task_types (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id  uuid NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    name     text NOT NULL,
    position integer NOT NULL
);
CREATE UNIQUE INDEX task_types_team_name_idx ON task_types (team_id, lower(name));

-- Per-team board columns. Four defaults per team (todo / in_progress / completed /
-- unassigned) plus custom ones; `finished` ones take their tasks out of the "needs
-- attention" views. `unassigned` is where tasks land when their project is taken away.
CREATE TABLE statuses (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id  uuid NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    name     text NOT NULL,
    kind     text NOT NULL CHECK (kind IN ('todo', 'in_progress', 'completed', 'unassigned', 'custom')),
    color    text NOT NULL DEFAULT 'slate',
    finished boolean NOT NULL DEFAULT false,
    position integer NOT NULL,
    CHECK (kind <> 'completed' OR finished),
    CHECK (kind NOT IN ('todo', 'in_progress') OR NOT finished)
);
CREATE UNIQUE INDEX statuses_team_name_idx ON statuses (team_id, lower(name));
CREATE UNIQUE INDEX statuses_team_kind_idx ON statuses (team_id, kind) WHERE kind <> 'custom';
CREATE INDEX statuses_team_pos_idx ON statuses (team_id, position);

CREATE TABLE work_items (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id           uuid NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    title             text NOT NULL,
    description       text NOT NULL DEFAULT '',
    type_id           uuid NOT NULL REFERENCES task_types (id),  -- must be a type of team_id
    priority          item_priority NOT NULL,
    status_id         uuid NOT NULL REFERENCES statuses (id),
    project_id        uuid REFERENCES projects (id) ON DELETE SET NULL,  -- must be a project of team_id
    assignee_id      uuid REFERENCES users (id) ON DELETE SET NULL,
    assigned_by       uuid REFERENCES users (id) ON DELETE SET NULL,  -- who made the current assignment
    requires_approval boolean NOT NULL,
    approval          text CHECK (approval IN ('pending', 'approved', 'rejected')),  -- NULL = no approval needed
    approval_by       uuid REFERENCES users (id) ON DELETE SET NULL,
    restricted        boolean NOT NULL DEFAULT false,  -- details editable by leads + creator only
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
    ON work_items (team_id, status_id, priority, last_activity_at DESC);
CREATE INDEX work_items_assignee_idx ON work_items (assignee_id, status_id);
CREATE INDEX work_items_created_by_idx ON work_items (created_by);
CREATE INDEX work_items_type_idx ON work_items (type_id);
CREATE INDEX work_items_project_idx ON work_items (project_id) WHERE project_id IS NOT NULL;

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
