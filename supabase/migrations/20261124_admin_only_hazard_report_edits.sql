-- Live issues are community-submitted and voted on, but only admins may edit or delete them.
-- Run after the existing Supabase schema and policies have been applied.
BEGIN;

DROP POLICY IF EXISTS hazard_reports_update_own_open ON public.hazard_reports;
DROP POLICY IF EXISTS hazard_reports_delete_own_open ON public.hazard_reports;

COMMIT;
