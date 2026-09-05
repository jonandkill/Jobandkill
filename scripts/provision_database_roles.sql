-- Job&Kill PostgreSQL runtime-role provisioning.
--
-- Run this only after `python -m jobandkill init`, while connected as the
-- Render database owner/admin. The script deliberately never receives or sets
-- a password: use psql's interactive `\password ROLE` command afterwards so
-- plaintext credentials cannot appear in shell history, SQL text, or server
-- statement/error logs.
--
-- The script is idempotent: rerunning it removes inherited memberships and
-- stale grants, and reapplies the exact privileges without rotating credentials.

\set ON_ERROR_STOP on
\set ECHO none

BEGIN;

SELECT format('CREATE ROLE %I LOGIN', requested.name)
FROM (VALUES
  ('jobandkill_web'),
  ('jobandkill_collector'),
  ('jobandkill_cleanup')
) AS requested(name)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=requested.name)
\gexec

ALTER ROLE jobandkill_web WITH
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE jobandkill_collector WITH
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE jobandkill_cleanup WITH
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

-- Existing memberships could confer privileges even after direct grants are
-- revoked.  Remove every membership held by a runtime role before continuing.
SELECT format('REVOKE %I FROM %I', parent.rolname, member.rolname)
FROM pg_auth_members membership
JOIN pg_roles parent ON parent.oid=membership.roleid
JOIN pg_roles member ON member.oid=membership.member
WHERE member.rolname IN ('jobandkill_web', 'jobandkill_collector', 'jobandkill_cleanup')
\gexec

-- A dedicated application database should not let PUBLIC create schemas or
-- temporary objects.  Runtime roles retain CONNECT, but no database-level DDL.
SELECT format(
  'REVOKE CREATE, TEMPORARY ON DATABASE %I FROM PUBLIC, jobandkill_web, jobandkill_collector, jobandkill_cleanup',
  current_database()
)
\gexec
SELECT format(
  'GRANT CONNECT ON DATABASE %I TO jobandkill_web, jobandkill_collector, jobandkill_cleanup',
  current_database()
)
\gexec

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA public
  FROM jobandkill_web, jobandkill_collector, jobandkill_cleanup;
GRANT USAGE ON SCHEMA public
  TO jobandkill_web, jobandkill_collector, jobandkill_cleanup;

-- Reset all direct and PUBLIC data grants before installing the allowlists.
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public
  FROM PUBLIC, jobandkill_web, jobandkill_collector, jobandkill_cleanup;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public
  FROM PUBLIC, jobandkill_web, jobandkill_collector, jobandkill_cleanup;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC;

-- Every process performs this read-only startup compatibility check.
GRANT SELECT ON TABLE public.app_metadata
  TO jobandkill_web, jobandkill_collector, jobandkill_cleanup;

-- Web: public corpus is read-only; account/auth/draft data is read-write.
GRANT SELECT ON TABLE
  public.sources,
  public.sync_runs,
  public.institutions,
  public.postings,
  public.attachments,
  public.document_objects,
  public.document_gc_queue,
  public.storage_namespaces,
  public.job_profiles,
  public.extraction_evidence,
  public.rights_decisions
TO jobandkill_web;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  public.users,
  public.user_consents,
  public.login_tokens,
  public.sessions,
  public.user_drafts,
  public.auth_request_events
TO jobandkill_web;
GRANT USAGE ON SEQUENCE
  public.user_consents_id_seq,
  public.auth_request_events_id_seq
TO jobandkill_web;

-- Collector/document processor: corpus only, with no personal-data grants.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  public.sources,
  public.sync_runs,
  public.institutions,
  public.postings,
  public.attachments,
  public.document_objects,
  public.document_gc_queue,
  public.storage_namespaces,
  public.job_profiles,
  public.extraction_evidence,
  public.rights_decisions
TO jobandkill_collector;
GRANT USAGE ON SEQUENCE
  public.sources_id_seq,
  public.sync_runs_id_seq,
  public.institutions_id_seq,
  public.postings_id_seq,
  public.attachments_id_seq,
  public.job_profiles_id_seq,
  public.extraction_evidence_id_seq,
  public.rights_decisions_id_seq
TO jobandkill_collector;

-- Retention cleanup: DELETE is table-level, while SELECT is restricted to the
-- predicate/join columns used by cleanup_personal_data.  In particular, this
-- role cannot read email addresses, consent records, or draft payloads.
GRANT DELETE ON TABLE
  public.users,
  public.user_consents,
  public.login_tokens,
  public.sessions,
  public.user_drafts,
  public.auth_request_events
TO jobandkill_cleanup;
GRANT SELECT (id, email_verified_at, created_at)
  ON TABLE public.users TO jobandkill_cleanup;
GRANT SELECT (verified_at, accepted_at)
  ON TABLE public.user_consents TO jobandkill_cleanup;
GRANT SELECT (user_id, expires_at, used_at)
  ON TABLE public.login_tokens TO jobandkill_cleanup;
GRANT SELECT (expires_at, idle_expires_at, revoked_at)
  ON TABLE public.sessions TO jobandkill_cleanup;
GRANT SELECT (user_id, updated_at)
  ON TABLE public.user_drafts TO jobandkill_cleanup;
GRANT SELECT (created_at)
  ON TABLE public.auth_request_events TO jobandkill_cleanup;

-- Fail rather than silently accepting a pre-existing service-owned object or
-- an inherited CREATE/TEMP capability.  Owners cannot be constrained by ACLs.
DO $least_privilege$
DECLARE
  service_role TEXT;
BEGIN
  FOREACH service_role IN ARRAY ARRAY[
    'jobandkill_web', 'jobandkill_collector', 'jobandkill_cleanup'
  ] LOOP
    IF EXISTS (
      SELECT 1
      FROM pg_class relation
      JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
      JOIN pg_roles owner_role ON owner_role.oid=relation.relowner
      WHERE owner_role.rolname=service_role
        AND relation.relpersistence<>'t'
        AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        AND namespace.nspname NOT LIKE 'pg_toast%'
    ) OR EXISTS (
      SELECT 1
      FROM pg_namespace namespace
      JOIN pg_roles owner_role ON owner_role.oid=namespace.nspowner
      WHERE owner_role.rolname=service_role
        AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
        AND namespace.nspname NOT LIKE 'pg_toast%'
    ) THEN
      RAISE EXCEPTION '% owns a database object; transfer ownership to the admin role first',
        service_role;
    END IF;

    -- pg_shdepend also covers owner-managed object types outside pg_class
    -- (for example functions and types), plus shared objects such as databases.
    IF EXISTS (
      SELECT 1
      FROM pg_shdepend ownership
      JOIN pg_roles owner_role ON owner_role.oid=ownership.refobjid
      WHERE owner_role.rolname=service_role
        AND ownership.deptype='o'
        AND (
          ownership.dbid=0
          OR ownership.dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
        )
    ) THEN
      RAISE EXCEPTION '% owns an object and therefore retains ALTER/DROP capability',
        service_role;
    END IF;

    IF has_database_privilege(service_role, current_database(), 'CREATE')
       OR has_database_privilege(service_role, current_database(), 'TEMPORARY')
       OR EXISTS (
         SELECT 1 FROM pg_namespace namespace
         WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
           AND namespace.nspname NOT LIKE 'pg_toast%'
           AND has_schema_privilege(service_role, namespace.oid, 'CREATE')
       ) THEN
      RAISE EXCEPTION '% still has a DDL privilege', service_role;
    END IF;
  END LOOP;
END;
$least_privilege$;

COMMIT;
