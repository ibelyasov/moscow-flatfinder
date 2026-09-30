CREATE TABLE listings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL CHECK(length(source) > 0),
    source_listing_id TEXT NOT NULL CHECK(length(source_listing_id) > 0),
    source_url TEXT NOT NULL CHECK(length(source_url) > 0),
    availability TEXT NOT NULL DEFAULT 'available' CHECK(availability IN ('available','unavailable','unknown')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    current_observation_id INTEGER,
    current_photo_set_id INTEGER,
    UNIQUE(source, source_listing_id),
    FOREIGN KEY(current_observation_id, id) REFERENCES observations(id, listing_id),
    FOREIGN KEY(current_photo_set_id, id) REFERENCES photo_sets(id, listing_id)
);

CREATE TABLE observations (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    kind TEXT NOT NULL CHECK(kind IN ('collection','enrichment','import')),
    parser_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    facts_json TEXT NOT NULL CHECK(json_valid(facts_json)),
    captured_at TEXT NOT NULL,
    UNIQUE(id, listing_id)
);
CREATE INDEX observations_listing ON observations(listing_id, id);

CREATE TABLE manual_decisions (
    listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
    personal_score REAL NOT NULL DEFAULT 0 CHECK(personal_score >= 0),
    favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0,1)),
    disliked INTEGER NOT NULL DEFAULT 0 CHECK(disliked IN (0,1)),
    personal_rated_at TEXT,
    favorited_at TEXT,
    disliked_at TEXT
);

CREATE TABLE assessments (
    listing_id INTEGER PRIMARY KEY REFERENCES listings(id),
    observation_id INTEGER NOT NULL,
    photo_set_id INTEGER,
    accepted_vision_id INTEGER,
    policy_fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(observation_id, listing_id) REFERENCES observations(id, listing_id),
    FOREIGN KEY(photo_set_id, listing_id) REFERENCES photo_sets(id, listing_id),
    FOREIGN KEY(accepted_vision_id, listing_id) REFERENCES vision_runs(id, listing_id)
);

CREATE TABLE assessment_history (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    observation_id INTEGER NOT NULL,
    photo_set_id INTEGER,
    accepted_vision_id INTEGER,
    policy_fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK(json_valid(result_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(observation_id, listing_id) REFERENCES observations(id, listing_id),
    FOREIGN KEY(photo_set_id, listing_id) REFERENCES photo_sets(id, listing_id),
    FOREIGN KEY(accepted_vision_id, listing_id) REFERENCES vision_runs(id, listing_id)
);
CREATE INDEX assessment_history_listing ON assessment_history(listing_id, id);

CREATE TABLE searches (
    url TEXT PRIMARY KEY CHECK(length(url) > 0),
    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    checked_at TEXT,
    complete INTEGER CHECK(complete IN (0,1))
);
CREATE TABLE search_memberships (
    search_url TEXT NOT NULL REFERENCES searches(url),
    source_url TEXT NOT NULL CHECK(length(source_url) > 0),
    seen_at TEXT NOT NULL,
    PRIMARY KEY(search_url, source_url)
);
CREATE INDEX search_membership_offer ON search_memberships(source_url);

CREATE TABLE photo_sets (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    input_hash TEXT NOT NULL,
    photos_json TEXT NOT NULL CHECK(json_valid(photos_json)),
    created_at TEXT NOT NULL,
    UNIQUE(id, listing_id)
);
CREATE INDEX photo_sets_listing ON photo_sets(listing_id, id);

CREATE TABLE checks (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    observation_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(length(kind) > 0),
    input_hash TEXT NOT NULL CHECK(length(input_hash) > 0),
    status TEXT NOT NULL CHECK(status IN ('success','partial','unknown','failed','blocked')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(observation_id, listing_id) REFERENCES observations(id, listing_id)
);
CREATE INDEX checks_cache ON checks(listing_id, kind, input_hash, id);

CREATE TABLE vision_runs (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    photo_set_id INTEGER NOT NULL,
    contract_json TEXT NOT NULL CHECK(json_valid(contract_json)),
    contract_fingerprint TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','pending','accepted','rejected','failed')),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    reviewed_at TEXT,
    UNIQUE(id, listing_id),
    FOREIGN KEY(photo_set_id, listing_id) REFERENCES photo_sets(id, listing_id)
);
CREATE INDEX vision_current ON vision_runs(listing_id, contract_fingerprint, input_hash, status, id);

CREATE TABLE duplicate_links (
    left_listing_id INTEGER NOT NULL REFERENCES listings(id),
    right_listing_id INTEGER NOT NULL REFERENCES listings(id),
    method TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    dismissed_at TEXT,
    CHECK(left_listing_id < right_listing_id),
    PRIMARY KEY(left_listing_id, right_listing_id)
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(length(kind) > 0),
    status TEXT NOT NULL CHECK(status IN ('running','success','partial','failed','blocked','cancelled')),
    summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE technical_archive (
    id INTEGER PRIMARY KEY,
    listing_id INTEGER REFERENCES listings(id),
    source_schema TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_key TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    archived_at TEXT NOT NULL,
    UNIQUE(source_schema, source_table, source_key)
);
