-- Epoch 49 schema retained solely for explicit conversion tests.

    CREATE TABLE steward_schema (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        epoch INTEGER NOT NULL,
        created_at TEXT NOT NULL
    ) STRICT
    ;

    CREATE TABLE turns (
        turn_id TEXT PRIMARY KEY CHECK (
            length(turn_id) = 37
            AND turn_id GLOB 'turn_[0-9a-f]*'
            AND substr(turn_id, 6) NOT GLOB '*[^0-9a-f]*'
        ),
        conversation_id TEXT NOT NULL CHECK (conversation_id GLOB '?*:?*'),
        source_event_key TEXT NOT NULL CHECK (length(source_event_key) > 0),
        operator_id TEXT NOT NULL CHECK (length(operator_id) > 0),
        state TEXT,
        input_text TEXT NOT NULL,
        execution_turn_id TEXT REFERENCES turns(turn_id),
        input_disposition TEXT CHECK (input_disposition IN ('unresolved', 'accepted', 'rejected')),
        status_reason TEXT,
        provider TEXT,
        generation INTEGER CHECK (generation IS NULL OR generation >= 1),
        provider_session_id TEXT,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        CHECK ((execution_turn_id IS NULL) = (input_disposition IS NULL)),
        FOREIGN KEY (execution_turn_id, conversation_id) REFERENCES turns(turn_id, conversation_id),
        UNIQUE (turn_id, conversation_id),
        UNIQUE (conversation_id, source_event_key),
        CHECK (
            (execution_turn_id IS NOT NULL AND execution_turn_id != turn_id
             AND state IS NULL AND status_reason IS NULL AND completed_at IS NULL
             AND provider IS NULL AND generation IS NULL AND provider_session_id IS NULL)
            OR (execution_turn_id IS NULL AND (
                (state IS 'completed'
                 AND status_reason IS NULL AND provider IS NOT NULL
                 AND generation IS NOT NULL AND completed_at IS NOT NULL)
                OR (state IS 'running'
                 AND status_reason IS NULL
                 AND completed_at IS NULL)
                OR (state IS 'interrupted'
                 AND status_reason IS NOT NULL
                 AND length(status_reason) > 0
                 AND completed_at IS NOT NULL)
            ))
        )
    ) STRICT
    ;

    -- Chat and rhythm only. A task's exclusion is its `flock`, which this
    -- index could never have supplied: it excluded a second *row*, and two
    -- daemons over one state directory would each have taken their own.
    CREATE UNIQUE INDEX one_running_turn_per_conversation
    ON turns(conversation_id) WHERE state = 'running' AND execution_turn_id IS NULL
    ;

    CREATE TABLE incidents (
        pipeline TEXT PRIMARY KEY CHECK (
            length(pipeline) BETWEEN 1 AND 64
            AND pipeline GLOB '[A-Za-z0-9]*'
            AND pipeline NOT GLOB '*[^A-Za-z0-9_-]*'
        ),
        cycle INTEGER NOT NULL CHECK (cycle >= 1),
        status TEXT NOT NULL CHECK (
            status IN ('healthy', 'failing', 'confirmed', 'escalated')
        ),
        consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
        transient_rechecks_left INTEGER NOT NULL CHECK (transient_rechecks_left >= 0),
        repair_task_id TEXT UNIQUE,
        escalation_task_id TEXT UNIQUE,
        last_details TEXT NOT NULL CHECK (length(last_details) BETWEEN 1 AND 1000),
        last_observed_at TEXT NOT NULL,
        last_recovered_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK (
            (status = 'healthy' AND consecutive_failures = 0
             AND transient_rechecks_left = 0
             AND repair_task_id IS NULL AND escalation_task_id IS NULL)
            OR
            (status = 'failing' AND consecutive_failures > 0
             AND transient_rechecks_left = 0
             AND repair_task_id IS NULL AND escalation_task_id IS NULL)
            OR
            (status = 'confirmed' AND consecutive_failures > 0
             AND escalation_task_id IS NULL)
            OR
            (status = 'escalated' AND consecutive_failures > 0
             AND transient_rechecks_left = 0 AND repair_task_id IS NULL)
        )
    ) STRICT
    ;

CREATE TABLE world_turns (
    event_id TEXT PRIMARY KEY,
    world_root TEXT,
    base_sha TEXT,
    candidate_sha TEXT,
    applied_base TEXT,
    applied_sha TEXT,
    output TEXT NOT NULL,
    provider TEXT NOT NULL CHECK (length(provider) > 0),
    model TEXT NOT NULL CHECK (length(model) > 0),
    provider_session_id TEXT,
    generation INTEGER CHECK (generation IS NULL OR generation >= 1),
    profile TEXT NOT NULL CHECK (profile IN ('fast', 'balanced', 'deep')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted')),
    reply_text TEXT,
    task_id TEXT,
    rejection TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    CHECK ((world_root IS NULL AND base_sha IS NULL AND candidate_sha IS NULL)
        OR (world_root IS NOT NULL AND base_sha IS NOT NULL AND candidate_sha IS NOT NULL)),
    CHECK ((applied_base IS NULL) = (applied_sha IS NULL)),
    CHECK ((status = 'accepted') = (reply_text IS NOT NULL))
) STRICT
;
