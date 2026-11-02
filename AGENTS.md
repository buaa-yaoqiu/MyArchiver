# Working with MyArchiver

- Generic core lives in `src/experiment_archive/`; keep project vocabulary, report formats and metric semantics in external integrations.
- Preserve immutable archive revisions. Use the SDK/CLI to publish corrections with explicit reasons; never edit a published revision or manifest directly.
- Treat project sources as read-only. Archive maintenance does not authorize training, scoring, remote submission or raw-data transfers.
- Do not infer scientific validity from search ranking or local integrity checks. Preserve evaluation scope, prediction phase, baseline identity, provenance and limitations.
- Do not commit archives, source configurations, caches, models, credentials or generated reports. Use synthetic fixtures for tests and documentation.
- Test with `python -m unittest discover -s tests -v`; for UI changes also run `python scripts/validate_ui.py` with the optional Playwright dependency and Edge installed.
- Translation is optional derived reading material. Keep originals and numeric evidence authoritative; never silently switch an offline failure to an online service.
