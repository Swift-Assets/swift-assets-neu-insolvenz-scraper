# Backup before dropping dead AI job queue + insolvenzindex (step 2, 2026-06-28)

Dropped: swift_v2.enrichment_jobs (9657, all dead job types: company_ai_enrichment,
company_custom_search, nachlass_detection), swift_v2.source_insolvenzindex_http_samples (370),
swift_v2.registry_events (0), schema insolvenzindex (cases, company_openings, company_closures).
Functions dropped: claim/complete/fail/enqueue_enrichment_job, cron_enqueue_ai_evaluator,
cron_enqueue_custom_search, record_custom_search_result. KEPT cron_invoke_edge (retention cron).
Rewired: v_cockpit_enrichment_jobs -> 0-row stub; v_cockpit_data_coverage_summary -> jobs_* columns
return 0/NULL. Original defs recoverable from supabase_migrations history + this note.
