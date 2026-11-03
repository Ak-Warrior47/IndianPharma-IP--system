-- ============================================================================
-- Close the Supabase Data API off from this application's tables.
--
-- WHY
-- Supabase puts a public REST API (PostgREST) in front of the "public" schema
-- and issues an anon key that is meant to be embedded in browsers. That is only
-- safe when Row-Level Security is enabled. These tables were created with RLS
-- off, which is what the advisor reported as rls_disabled_in_public:
--
--   "Anyone with your project URL can read, edit, and delete all data in this
--    table because Row-Level Security is not enabled."
--
-- That exposure covers staff names, emails, phone numbers, performance scores
-- and the delivery riders' GPS trails.
--
-- WHY THIS IS SAFE FOR THE APP
-- The application does not use the Data API. It connects straight to Postgres
-- over DATABASE_URL as the table owner, and an owner bypasses RLS. Enabling RLS
-- therefore changes nothing for the app and everything for the public endpoint.
-- Grants to anon and authenticated are revoked too, so the tables leave the API
-- surface rather than merely failing a policy check.
--
-- FORCE ROW LEVEL SECURITY is deliberately NOT used. That form applies RLS to
-- the owner as well and would lock the application out of its own data.
--
-- HOW TO RUN
-- Supabase dashboard -> SQL Editor -> paste -> Run. Takes a second, needs no
-- deploy. The app also runs the same thing on every boot, so this is only for
-- closing the hole right now.
--
-- TO VERIFY
-- Re-run the last query at the bottom: every row should read rls_enabled = true.
-- ============================================================================

ALTER TABLE public.employees                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.kpi_entries              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.multitask_entries        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.delivery_biller_notes    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.delivery_assignments     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.delivery_stops           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.delivery_trips           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.breadcrumb_points        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.supervisor_bills         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bill_validations         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.complaints               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.announcements            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.announcement_reads       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.badges                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.staff_badges             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.staff_goals              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_staff_notes        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_logs               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notifications            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.past_entry_windows       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.past_entry_requests      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.password_reset_otps      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monthly_report_archives  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checklist_templates      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checklist_completions    ENABLE ROW LEVEL SECURITY;

-- Remove them from the Data API surface entirely. No policies are added, so
-- with RLS on and grants revoked the anon key can reach nothing.
DO $$
DECLARE t text;
BEGIN
  FOR t IN
    SELECT tablename FROM pg_tables WHERE schemaname = 'public'
  LOOP
    EXECUTE format('REVOKE ALL ON public.%I FROM anon', t);
    EXECUTE format('REVOKE ALL ON public.%I FROM authenticated', t);
  END LOOP;
END $$;

-- Stop future tables being granted to the API automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM authenticated;

-- ---------------------------------------------------------------------------
-- VERIFY: every row should show rls_enabled = true and no anon/authenticated
-- privileges remaining.
-- ---------------------------------------------------------------------------
SELECT
    c.relname                                   AS table_name,
    c.relrowsecurity                            AS rls_enabled,
    COALESCE(has_table_privilege('anon',          'public.' || c.relname, 'SELECT'), false) AS anon_can_read,
    COALESCE(has_table_privilege('authenticated', 'public.' || c.relname, 'SELECT'), false) AS auth_can_read
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relrowsecurity, c.relname;
