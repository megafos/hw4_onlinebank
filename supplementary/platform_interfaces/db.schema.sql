-- PostgreSQL. Illustrative one-shard transactional schema, not a full migration plan.
-- caller supplies UUIDs; no extensions or production credentials are required.
BEGIN;
CREATE TABLE notification (
 notification_id uuid PRIMARY KEY,
 producer_id text NOT NULL,
 event_id text NOT NULL,
 recipient_id text NOT NULL,
 purpose text NOT NULL,
 payload_hash char(64) NOT NULL,
 payload_ref text NOT NULL,
 template_id text NOT NULL,
 template_version integer NOT NULL CHECK(template_version > 0),
 policy_version integer NOT NULL CHECK(policy_version > 0),
 state text NOT NULL CHECK(state IN ('ACCEPTED','IN_PROGRESS','WAITING_RETRY','CONFIRMED','EXPIRED_UNCONFIRMED','MANUAL_REVIEW')),
 version bigint NOT NULL DEFAULT 0,
 accepted_at timestamptz NOT NULL,
 fast_deadline timestamptz NOT NULL,
 expires_at timestamptz NOT NULL,
 confirmed_at timestamptz,
 late_confirmation boolean NOT NULL DEFAULT false,
 UNIQUE(producer_id,event_id,recipient_id,purpose),
 CHECK(fast_deadline > accepted_at AND expires_at >= fast_deadline),
 CHECK((state='CONFIRMED') = (confirmed_at IS NOT NULL))
);
CREATE TABLE delivery_attempt (
 attempt_id uuid PRIMARY KEY,
 notification_id uuid NOT NULL REFERENCES notification(notification_id),
 ordinal integer NOT NULL CHECK(ordinal > 0),
 generation bigint NOT NULL,
 channel text NOT NULL CHECK(channel IN ('push','sms','email')),
 provider text NOT NULL,
 request_key text NOT NULL,
 provider_message_id text,
 state text NOT NULL CHECK(state IN ('RESERVED','SENDING','ACCEPTED','UNKNOWN','FAILED','CONFIRMED','CANCELLED')),
 active boolean NOT NULL DEFAULT true,
 lease_until timestamptz,
 created_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 UNIQUE(notification_id,ordinal),
 UNIQUE(provider,request_key),
 CHECK(NOT active OR state IN ('RESERVED','SENDING','ACCEPTED','UNKNOWN'))
);
CREATE UNIQUE INDEX one_active_attempt ON delivery_attempt(notification_id) WHERE active;
CREATE TABLE receipt (
 provider text NOT NULL,
 receipt_id text NOT NULL,
 attempt_id uuid NOT NULL REFERENCES delivery_attempt(attempt_id),
 provider_message_id text,
 outcome text NOT NULL CHECK(outcome IN ('accepted','failed','confirmed')),
 evidence_kind text NOT NULL CHECK(evidence_kind IN ('provider_acceptance','app_ack','sms_dlr','email_server_delivery','provider_failure')),
 event_time timestamptz NOT NULL,
 received_at timestamptz NOT NULL,
 PRIMARY KEY(provider,receipt_id)
);
CREATE TABLE outbox_event (
 event_id uuid PRIMARY KEY,
 notification_id uuid NOT NULL REFERENCES notification(notification_id),
 aggregate_version bigint NOT NULL,
 event_type text NOT NULL,
 payload jsonb NOT NULL,
 created_at timestamptz NOT NULL,
 published_at timestamptz
);
CREATE INDEX outbox_pending ON outbox_event(created_at) WHERE published_at IS NULL;
CREATE TABLE inbox_event (
 consumer_name text NOT NULL,
 event_id uuid NOT NULL,
 received_at timestamptz NOT NULL,
 PRIMARY KEY(consumer_name,event_id)
);
CREATE TABLE scheduled_wakeup (
 notification_id uuid NOT NULL REFERENCES notification(notification_id),
 expected_version bigint NOT NULL,
 due_at timestamptz NOT NULL,
 reason text NOT NULL,
 PRIMARY KEY(notification_id,expected_version,reason)
);
CREATE INDEX wakeup_due ON scheduled_wakeup(due_at);
CREATE TABLE audit_event (
 audit_id uuid PRIMARY KEY,
 notification_id uuid NOT NULL REFERENCES notification(notification_id),
 attempt_id uuid REFERENCES delivery_attempt(attempt_id),
 event_time timestamptz NOT NULL,
 actor text NOT NULL,
 reason text NOT NULL,
 old_state text,
 new_state text
);
COMMIT;
-- All transitions: lock notification first, then attempts, in that order.
-- Expire active attempt before creating its successor, in the SAME transaction.
-- Scheduler: SELECT ... FOR UPDATE SKIP LOCKED LIMIT ...; create outbox and
-- remove wakeup atomically. Do not hold a transaction during external HTTP.
-- Retention must archive audit before deleting referenced rows; tables here
-- intentionally do not implement automatic production retention or partitioning.
