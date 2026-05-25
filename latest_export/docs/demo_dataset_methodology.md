# Demo Dataset Methodology

- Reconstruct missing/error sensor rows for N2/N3 using nearest-neighbor interpolation, hourly node medians, and bounded fallback.
- Keep MAIN untouched.
- Apply low controlled missingness only for realism, with stricter cap for N3 continuity.
- Build `system_events_demo.csv` directly from `sensor_readings_demo.csv`:
  - `packet_received` for `ok`
  - `node_missing` for `missing/error`
  - `node_back_online` for missing->ok transitions
- Remove demo-style error codes and use `NODE_TELEMETRY_MISSING` only for missing telemetry where needed.
- Normalize non-empty gateway IDs to `GW01` when absent.
- Generate `uploads_demo.csv` from demo readings/events with deterministic upload grouping and exact counts.
- Preserve source CSV column order and PostgreSQL import compatibility.
- Keep deterministic output with fixed seed (`20260521`).
