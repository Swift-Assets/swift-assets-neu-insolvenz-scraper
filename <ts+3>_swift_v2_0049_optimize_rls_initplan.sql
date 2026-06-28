alter policy cockpit_users_self_read on swift_v2.cockpit_user_profiles
  using (user_id = (select auth.uid()));

alter policy cockpit_authenticated_select_apify_cases on public.apify_cases
  using (exists (
    select 1 from swift_v2.cockpit_user_profiles cup
    where cup.user_id = (select auth.uid()) and cup.is_active = true));

alter policy cockpit_admin_update_apify_cases on public.apify_cases
  using (exists (
    select 1 from swift_v2.cockpit_user_profiles cup
    where cup.user_id = (select auth.uid()) and cup.role = 'admin'::swift_v2.cockpit_role and cup.is_active = true))
  with check (exists (
    select 1 from swift_v2.cockpit_user_profiles cup
    where cup.user_id = (select auth.uid()) and cup.role = 'admin'::swift_v2.cockpit_role and cup.is_active = true));
