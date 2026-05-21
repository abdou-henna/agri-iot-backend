# Demo Dataset Validation Report

Generated with:

```bash
python scripts/generate_demo_dataset.py
```

Key checks performed:
- N3 totals and missing-by-day constraints
- Event volume and type distribution
- Forbidden error_code scan
- JSON validity (`details`, `raw_summary`)
- Unique IDs (`id`, `event_id`, agronomic IDs)
- Upload sums parity with demo readings/events
- Schema/order parity against source CSV headers

Current validated outcomes:
- N3 total 598, missing 12 (<=15), each day <=2 missing
- system_events_demo rows 2313 (>=1700)
- packet_received 2272, node_missing 21, node_back_online 20
- No forbidden demo-style error_code values
- Upload sum(records_count)=2293 equals sensor rows; sum(events_count)=2313 equals event rows
- All checked JSON fields parse successfully
- All schema headers match source CSVs
