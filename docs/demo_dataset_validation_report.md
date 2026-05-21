# Demo Dataset Validation Report

Generator validations enforce:
- Source SHA256 integrity preserved for `sensor_readings.csv`, `system_events.csv`, and `uploads.csv`.
- Explicit output date window enforcement (`2026-05-12T00:00:00+01:00` to `2026-05-20T23:59:59+01:00`) unless CLI override is provided.
- RTC-invalid rows and outside-window rows excluded from demo outputs.
- `sensor_readings_demo.csv` schema/order exactly matches source.
- Duplicate IDs are disallowed in demo sensor output.
- MAIN missingness remains excellent.
- Complete N2/N3 day limits enforced (<=35 missing).
- Deterministic reproducibility enforced by fixed seed and two-run hash equality.
- `agronomic_events_demo.csv` header/order must exactly match `csv/agronomic_events.csv`.
- Irrigation validation checks enforce:
  - `started_at` inside `2026-05-12T00:00:00+01:00` to `2026-05-20T23:59:59+01:00`
  - irrigation segment starts are not inside pause window `17:00`–`22:00`
  - irrigation segments do not overlap the pause window
  - `ended_at >= started_at`
  - no duplicate `agro_event_id`
  - no duplicate `(started_at, target_scope|node_id|farm)` key

Generated demo outputs are local-only under `demo_dataset/` and are synthetic for dashboard testing, not field-truth analysis.
