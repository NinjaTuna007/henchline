-- A schema-1 host database (call/host_store.py), one of each kind of row. Test-only values.
BEGIN TRANSACTION;
CREATE TABLE devices(
                    device_id TEXT PRIMARY KEY,
                    algorithm TEXT NOT NULL CHECK(algorithm IN ('ed25519', 'p256')),
                    public_key BLOB NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    enrolled_at REAL NOT NULL,
                    revoked_at REAL
                );
INSERT INTO "devices" VALUES('11111111-1111-4111-8111-111111111111','ed25519',X'03A107BFF3CE10BE1D70DD18E74BC09967E4D6309BA50D5F1DDC8664125531B8','Test phone',1795000000.0,NULL);
INSERT INTO "devices" VALUES('44444444-4444-4444-8444-444444444444','ed25519',X'0000000000000000000000000000000000000000000000000000000000000000','Lost phone',1795000000.0,1795000500.0);
CREATE TABLE host_events(
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body BLOB NOT NULL,
                    created_at REAL NOT NULL
                );
INSERT INTO "host_events" VALUES('55555555-5555-4555-8555-555555555555','claude-1','message',X'7B2274657874223A2268656C6C6F227D',1795000005.0);
CREATE TABLE idempotency(
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending', 'complete')),
                    status INTEGER,
                    content_type TEXT,
                    response BLOB,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    acknowledged_at REAL,
                    PRIMARY KEY(device_id, request_id)
                );
INSERT INTO "idempotency" VALUES('11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222','ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff','complete',200,'application/json',X'7B226576656E745F6964223A226531222C227374617465223A226163636570746564227D',1795000001.0,1795000002.0,1795000003.0);
INSERT INTO "idempotency" VALUES('11111111-1111-4111-8111-111111111111','33333333-3333-4333-8333-333333333333','eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee','pending',NULL,NULL,NULL,1795000004.0,NULL,NULL);
CREATE TABLE pairing_nonces(
                    digest BLOB PRIMARY KEY,
                    expires_at REAL NOT NULL,
                    attempts_left INTEGER NOT NULL,
                    consumed_at REAL
                );
CREATE TABLE replay_nonces(
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    nonce TEXT NOT NULL,
                    seen_at REAL NOT NULL,
                    PRIMARY KEY(device_id, nonce)
                );
CREATE TABLE schema_meta(
                    version INTEGER NOT NULL
                );
INSERT INTO "schema_meta" VALUES(1);
CREATE INDEX host_events_session_time
                    ON host_events(session_id, created_at, event_id);
COMMIT;
