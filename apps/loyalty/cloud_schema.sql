-- Private fixed mirror in the explicitly approved existing project.
-- No grants, revokes, role changes, public-schema objects or API exposure.
CREATE SCHEMA lsa_loyalty_backup;

CREATE TABLE lsa_loyalty_backup.members (
    key text PRIMARY KEY,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);
CREATE TABLE lsa_loyalty_backup.referrals (
    key text PRIMARY KEY,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);
CREATE TABLE lsa_loyalty_backup.ledger (
    key text PRIMARY KEY,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);
CREATE TABLE lsa_loyalty_backup.context (
    key text PRIMARY KEY,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);
CREATE TABLE lsa_loyalty_backup.manifest (
    id smallint PRIMARY KEY CHECK (id = 1),
    payload jsonb CHECK (payload IS NULL OR jsonb_typeof(payload) = 'object'),
    project_ref text NOT NULL CHECK (project_ref = 'rsqgwepnowpnpealrzbn'),
    format_version smallint NOT NULL CHECK (format_version = 1)
);
CREATE TABLE lsa_loyalty_backup.status (
    id smallint PRIMARY KEY CHECK (id = 1),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);
INSERT INTO lsa_loyalty_backup.manifest
    (id, payload, project_ref, format_version)
    VALUES (1, NULL, 'rsqgwepnowpnpealrzbn', 1);
INSERT INTO lsa_loyalty_backup.status (id, payload)
    VALUES (1, '{"status":"never_run","last_success":null}'::jsonb);

COMMENT ON SCHEMA lsa_loyalty_backup IS
    'Private single latest loyalty mirror. Provisioned is not a successful backup.';
