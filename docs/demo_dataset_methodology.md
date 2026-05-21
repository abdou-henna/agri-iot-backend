# Demo Dataset Methodology

- `scripts/generate_demo_dataset.py` reads raw CSV exports and never edits source files.
- Default explicit demo field window is `2026-05-12T00:00:00+01:00` through `2026-05-20T23:59:59+01:00`.
- Rows outside that explicit window are excluded from generated demo outputs.
- Demo sensor output keeps exact `csv/sensor_readings.csv` schema and column order.
- Reconstruction first restores N2/N3 missing/error rows when required fields can be responsibly derived.
- Controlled synthetic missingness is injected mainly for N2/N3 for dashboard QC/reliability testing, with audit provenance.
- MAIN remains excellent/stable.
- All generated artifacts stay local in `demo_dataset/` and are git-ignored.
- Demo dataset is synthetic/derived and not field-truth evidence.
