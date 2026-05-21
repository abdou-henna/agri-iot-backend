#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import random
import uuid
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

SEED = 20260521
RTC_CUTOFF = datetime.fromisoformat("2025-01-01T00:00:00+00:00")
FIELD_START_DEFAULT = datetime.fromisoformat("2026-05-12T00:00:00+01:00")
FIELD_END_DEFAULT = datetime.fromisoformat("2026-05-20T23:59:59+01:00")
ROOT = Path(__file__).resolve().parent.parent
CSV_DIR = ROOT / "csv"
OUT_DIR = ROOT / "demo_dataset"

REQ = {
    "MAIN": ["soil_temperature_c", "soil_moisture_percent", "soil_ec_us_cm"],
    "N2": ["soil_temperature_c", "soil_moisture_percent", "soil_ec_us_cm"],
    "N3": ["air_temperature_c", "air_humidity_percent", "air_pressure_hpa"],
}


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def floatv(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def fmt(x):
    return "" if x is None else f"{x:.2f}".rstrip("0").rstrip(".")


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def load_csv(path):
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return r.fieldnames, list(r)


def split_cycle_segments(start_at, duration):
    pause_start = start_at.replace(hour=17, minute=0, second=0, microsecond=0)
    pause_end = start_at.replace(hour=22, minute=0, second=0, microsecond=0)
    end_at = start_at + duration
    if start_at < pause_start and end_at > pause_start:
        seg1 = (start_at, pause_start)
        remaining = end_at - pause_start
        seg2_start = pause_end
        seg2_end = seg2_start + remaining
        return [seg1, (seg2_start, seg2_end)]
    return [(start_at, end_at)]


def build_irrigation_cycles(field_start, field_end, start_at, interval_hours=8, duration_hours=8):
    out = []
    cur = start_at
    duration = timedelta(hours=duration_hours)
    idx = 0
    while cur <= field_end:
        if cur >= field_start:
            out.append({"cycle_idx": idx, "cycle_start": cur, "segments": split_cycle_segments(cur, duration)})
            idx += 1
        cur += timedelta(hours=interval_hours)
    return out


def add_if_col(row, col, value):
    if col in row:
        row[col] = value


def deterministic_uuid(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(parts)))


def normalize_json_str(v, fallback="{}"):
    s = (v or "").strip()
    if not s:
        return fallback
    try:
        json.loads(s)
        return s
    except Exception:
        return fallback


def infer_cluster_range(rows):
    valid = sorted(parse_dt(r["measured_at"]) for r in rows if parse_dt(r["measured_at"]) >= RTC_CUTOFF)
    if not valid:
        return None, None
    day_counts = Counter(d.date().isoformat() for d in valid)
    active = [d for d, c in sorted(day_counts.items()) if c >= 30]
    if not active:
        return min(valid), max(valid)
    return datetime.fromisoformat(active[0] + "T00:00:00+00:00"), datetime.fromisoformat(active[-1] + "T23:59:59+00:00")


def classify(rows, field_start, field_end):
    rtc_invalid, outside_field, field = [], [], []
    for r in rows:
        dt = parse_dt(r["measured_at"])
        if dt < RTC_CUTOFF:
            rtc_invalid.append(r)
            continue
        if dt < field_start or dt > field_end:
            outside_field.append(r)
            continue
        field.append(r)
    return rtc_invalid, outside_field, field


def generate(seed=SEED, field_start=FIELD_START_DEFAULT, field_end=FIELD_END_DEFAULT):
    rng = random.Random(seed)
    sensor_path = CSV_DIR / "sensor_readings.csv"
    events_path = CSV_DIR / "system_events.csv"
    uploads_path = CSV_DIR / "uploads.csv"
    src_hash = {p.name: sha(p) for p in [sensor_path, events_path, uploads_path]}

    sensor_cols, sensor_rows = load_csv(sensor_path)
    event_cols, event_rows = load_csv(events_path)
    inferred_start, inferred_end = infer_cluster_range(sensor_rows)
    rtc_invalid, outside_field, field_rows = classify(sensor_rows, field_start, field_end)

    base_rows = [deepcopy(r) for r in field_rows]
    node_rows = defaultdict(list)
    for r in base_rows:
        node_rows[r["node_id"]].append(r)
    for n in node_rows:
        node_rows[n].sort(key=lambda x: parse_dt(x["measured_at"]))

    stats = defaultdict(dict)
    med = defaultdict(dict)
    hour_vals = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in base_rows:
        n = r["node_id"]
        hr = parse_dt(r["measured_at"]).hour
        if r["status"] == "ok":
            for c in REQ[n]:
                v = floatv(r[c])
                if v is not None:
                    hour_vals[n][hr][c].append(v)
    for n in REQ:
        for c in REQ[n]:
            vals = [floatv(r[c]) for r in base_rows if r["node_id"] == n and floatv(r[c]) is not None]
            if vals:
                stats[n][c] = (min(vals), max(vals))
        for hr, cvals in hour_vals[n].items():
            med[n][hr] = {c: sorted(v)[len(v)//2] for c, v in cvals.items() if v}

    main_time = {r["measured_at"]: r for r in node_rows.get("MAIN", [])}
    audit = []
    audit_id = 1
    reconstructed = []

    for n in ["MAIN", "N2", "N3"]:
        rows = node_rows.get(n, [])
        for i, r in enumerate(rows):
            d = deepcopy(r)
            if n == "MAIN" or d["status"] not in ("missing", "error"):
                reconstructed.append(d)
                continue
            changed, methods = [], set()
            prev = rows[i - 1] if i > 0 else None
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            for c in REQ[n]:
                if floatv(d[c]) is not None:
                    continue
                pv = floatv(prev[c]) if prev else None
                nv = floatv(nxt[c]) if nxt else None
                val = None
                if pv is not None and nv is not None:
                    val = (pv + nv) / 2
                    methods.add("temporal_interpolation")
                elif parse_dt(d["measured_at"]).hour in med[n] and c in med[n][parse_dt(d["measured_at"]).hour]:
                    val = med[n][parse_dt(d["measured_at"]).hour][c]
                    methods.add("node_hourly_pattern")
                elif n == "N2" and d["measured_at"] in main_time and c in stats["N2"] and c in stats["MAIN"]:
                    mv = floatv(main_time[d["measured_at"]][c])
                    if mv is not None:
                        n2c, mc = stats["N2"][c], stats["MAIN"][c]
                        val = mv + ((n2c[0] + n2c[1]) - (mc[0] + mc[1])) / 2
                        methods.add("main_offset_reference")
                if val is None and c in stats[n]:
                    lo, hi = stats[n][c]
                    val = rng.uniform(lo, hi)
                    methods.add("bounded_field_fallback")
                if val is not None and c in stats[n]:
                    lo, hi = stats[n][c]
                    d[c] = fmt(max(lo, min(hi, val + rng.uniform(-0.03, 0.03))))
                    changed.append(c)
            done = all(floatv(d[c]) is not None for c in REQ[n])
            err = (d.get("error_code") or "").lower()
            if done and (d["status"] == "missing" or any(k in err for k in ["missing", "no_data", "timeout", "node_missing"])):
                old = d["status"]
                d["status"] = "ok"
                if any(k in err for k in ["missing", "no_data", "timeout", "node_missing"]):
                    d["error_code"] = ""
                audit.append({"audit_id": str(audit_id), "original_id": r["id"], "demo_id": d["id"], "node_id": n,
                              "measured_at": d["measured_at"], "changed_fields": ";".join(sorted(set(changed))),
                              "original_status": old, "demo_status": "ok", "method": ";".join(sorted(methods)),
                              "confidence": "medium" if methods else "low", "source_rows_used": "", "source_nodes_used": "MAIN" if "main_offset_reference" in methods else "",
                              "reason": "status reconstructed for dashboard demo from derived measurements",
                              "limitations": "synthetic demo reconstruction; not field-truth"})
                audit_id += 1
            reconstructed.append(d)

    by_day_node = defaultdict(list)
    for i, r in enumerate(reconstructed):
        by_day_node[(r["measured_at"][:10], r["node_id"])].append(i)

    inj = {}
    for node in ["N2", "N3"]:
        days = sorted(d for d, n in by_day_node if n == node)
        for d in days:
            # Keep missingness realistic but controlled for dashboard continuity.
            inj[(d, node)] = rng.randint(0, 2) if node == "N3" else rng.randint(0, 4)

    controlled = Counter(); rare = set()
    for (day, node), target in inj.items():
        idxs = [i for i in by_day_node[(day, node)] if reconstructed[i]["status"] == "ok"]
        rng.shuffle(idxs)
        take = min(target, len(idxs))
        if take >= 20: rare.add((day, node))
        for i in idxs[:take]:
            r = reconstructed[i]; old = r["status"]
            r["status"] = "missing"
            for c in REQ[node]: r[c] = ""
            if (r.get("error_code") or "").strip() == "":
                r["error_code"] = "NODE_TELEMETRY_MISSING"
            controlled[(day, node)] += 1
            audit.append({"audit_id": str(audit_id), "original_id": r["id"], "demo_id": r["id"], "node_id": node,
                         "measured_at": r["measured_at"], "changed_fields": ";".join(REQ[node]), "original_status": old,
                         "demo_status": "missing", "method": "controlled_missing_injection", "confidence": "high",
                         "source_rows_used": "", "source_nodes_used": "",
                         "reason": "controlled demo missingness for dashboard QC/reliability testing",
                         "limitations": "synthetic demo scenario; not field-truth"})
            audit_id += 1

    OUT_DIR.mkdir(exist_ok=True)
    filtered_events = []
    for e in event_rows:
        try: dt = parse_dt(e["event_time"])
        except Exception: continue
        if dt < RTC_CUTOFF or dt < field_start or dt > field_end:
            continue
        filtered_events.append(e)
    by_ev = defaultdict(list)
    for e in filtered_events:
        by_ev[(e["event_time"][:10], e["node_id"])].append(e)
    out_events = []
    for _, evs in by_ev.items(): out_events.extend(evs[:3])
    next_id = max([int(e["id"]) for e in out_events] + [0]) + 1
    for (day, node), cnt in controlled.items():
        if cnt == 0:
            continue

    # Rebuild system events from sensor timeline for realistic operational logs.
    out_events = []
    event_id_seq = defaultdict(int)
    node_last = {}
    for r in sorted(reconstructed, key=lambda x: parse_dt(x["measured_at"])):
        node = r["node_id"]
        ts = parse_dt(r["measured_at"])
        ts_key = ts.strftime("%Y%m%d-%H%M%S")
        event_id_seq[(node, ts_key)] += 1
        seq = event_id_seq[(node, ts_key)]
        ev_id = f"EVT-GW01-{ts_key}-{node}-{seq:04d}"
        st = r["status"]
        missing_like = st in ("missing", "error")
        err = (r.get("error_code") or "").strip()
        if err.lower() in {"demo_missing", "controlled_missing", "synthetic_missing", "generated_missing"}:
            err = "NODE_TELEMETRY_MISSING"
        packet = {
            "id": str(next_id),
            "event_id": ev_id,
            "upload_id": r.get("upload_id", ""),
            "gateway_id": (r.get("gateway_id") or "").strip() or "GW01",
            "node_id": node,
            "event_type": "node_missing" if missing_like else "packet_received",
            "severity": "warning" if missing_like else "info",
            "event_time": r["measured_at"],
            "received_at": r.get("received_at") or r["measured_at"],
            "message": "Expected node telemetry missing." if missing_like else "Sensor packet received.",
            "details": json.dumps({
                "record_id": r["id"],
                "frame_id": r.get("frame_id"),
                "node_type": r.get("node_type"),
                "status": "missing" if missing_like else "ok",
            }),
            "error_code": (err or "NODE_TELEMETRY_MISSING") if missing_like else "",
        }
        out_events.append(packet)
        next_id += 1
        prev = node_last.get(node)
        if prev in ("missing", "error") and st == "ok":
            event_id_seq[(node, ts_key)] += 1
            seq2 = event_id_seq[(node, ts_key)]
            out_events.append({
                "id": str(next_id),
                "event_id": f"EVT-GW01-{ts_key}-{node}-{seq2:04d}",
                "upload_id": r.get("upload_id", ""),
                "gateway_id": (r.get("gateway_id") or "").strip() or "GW01",
                "node_id": node,
                "event_type": "node_back_online",
                "severity": "info",
                "event_time": r["measured_at"],
                "received_at": r.get("received_at") or r["measured_at"],
                "message": "Node telemetry back online after missing interval.",
                "details": json.dumps({"previous_status": "missing", "current_status": "ok", "record_id": r["id"]}),
                "error_code": "",
            })
            next_id += 1
        node_last[node] = st
    with (OUT_DIR / "system_events_demo.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=event_cols); w.writeheader(); w.writerows(out_events)

    # Build uploads metadata consistent with generated readings and events.
    upload_cols, upload_rows = load_csv(uploads_path)
    by_upload_readings = defaultdict(list)
    for r in reconstructed:
        up = (r.get("upload_id") or "").strip() or f"UP-{r['measured_at'][:13].replace(':','').replace('T','-')}-{r['node_id']}"
        r["upload_id"] = up
        by_upload_readings[up].append(r)
    by_upload_events = defaultdict(list)
    for e in out_events:
        up = (e.get("upload_id") or "").strip()
        by_upload_events[up].append(e)
    max_upload_id = 0
    for u in upload_rows:
        try:
            max_upload_id = max(max_upload_id, int((u.get("id") or "").strip()))
        except Exception:
            continue
    next_upload_id = max_upload_id + 1 if max_upload_id > 0 else 1
    uploads_out = []
    for up_id, rr in sorted(by_upload_readings.items(), key=lambda x: min(parse_dt(i["measured_at"]) for i in x[1])):
        ev = by_upload_events.get(up_id, [])
        start = min(parse_dt(i["measured_at"]) for i in rr).isoformat()
        finish = max(parse_dt(i["measured_at"]) for i in rr).isoformat()
        rec = {c: "" for c in upload_cols}
        add_if_col(rec, "id", str(next_upload_id))
        next_upload_id += 1
        add_if_col(rec, "upload_id", up_id)
        add_if_col(rec, "gateway_id", "GW01")
        add_if_col(rec, "status", "completed")
        add_if_col(rec, "started_at", start)
        add_if_col(rec, "finished_at", finish)
        add_if_col(rec, "received_at", finish)
        add_if_col(rec, "records_count", str(len(rr)))
        add_if_col(rec, "events_count", str(len(ev)))
        add_if_col(rec, "raw_summary", json.dumps({"records_count": len(rr), "events_count": len(ev)}))
        uploads_out.append(rec)
    with (OUT_DIR / "uploads_demo.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=upload_cols); w.writeheader(); w.writerows(uploads_out)
    # Persist readings after deterministic upload_id normalization.
    with (OUT_DIR / "sensor_readings_demo.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sensor_cols); w.writeheader(); w.writerows(reconstructed)

    agr_path = CSV_DIR / "agronomic_events.csv"
    if not agr_path.exists():
        raise RuntimeError("csv/agronomic_events.csv not found; schema cannot be inferred.")
    agr_cols, agr_rows = load_csv(agr_path)
    if not agr_cols:
        raise RuntimeError("csv/agronomic_events.csv empty header; schema cannot be inferred.")
    in_window = []
    for r in agr_rows:
        try:
            s = parse_dt(r["started_at"])
            if field_start <= s <= field_end:
                in_window.append(deepcopy(r))
        except Exception:
            continue
    def is_irr(r):
        return r.get("event_category", "").lower() == "irrigation" or "irrigation" in r.get("event_type", "").lower() or r.get("agro_event_id", "").startswith("IRR")
    manual = [r for r in in_window if is_irr(r)]
    non_irrigation = [r for r in in_window if not is_irr(r)]
    durs = [parse_dt(r["ended_at"]) - parse_dt(r["started_at"]) for r in manual if r.get("ended_at")]
    duration = sorted(durs)[len(durs)//2] if durs else timedelta(hours=8)
    cycles = build_irrigation_cycles(field_start, field_end, datetime.fromisoformat("2026-05-12T07:00:00+01:00"), 8, int(duration.total_seconds()//3600) or 8)
    manual = sorted(manual, key=lambda r: parse_dt(r["started_at"]))
    style = "IRR-{dt}-P{pivot}"
    if any((r.get("agro_event_id") or "").startswith("IRR-") for r in manual):
        style = "IRR-{dt}-P{pivot}"
    target_cycle = [("MAIN", "pivot_1", "P1"), ("N2", "pivot_2", "P2")]
    irrig_details = json.dumps({"schedule": "8h_pause_resume", "pause_window": "17:00-22:00", "source": "scheduled_field_operation"})
    corrected, audit_rows, used_cycles = [], [], set()
    for i, r in enumerate(manual):
        if i >= len(cycles):
            break
        cycle = cycles[i]
        exp_start = cycle["segments"][0][0]
        exp_end = cycle["segments"][0][1]
        node_id, scope, pivot = target_cycle[cycle["cycle_idx"] % 2]
        old_s, old_e = r.get("started_at", ""), r.get("ended_at", "")
        old_t = r.get("target_scope", "") or r.get("node_id", "")
        nr = deepcopy(r)
        nr["started_at"] = exp_start.isoformat()
        nr["ended_at"] = exp_end.isoformat()
        add_if_col(nr, "node_id", node_id); add_if_col(nr, "target_scope", scope)
        add_if_col(nr, "event_category", "irrigation"); add_if_col(nr, "event_type", "irrigation_session")
        final_id = style.format(dt=exp_start.strftime("%Y%m%d-%H%M"), pivot="1" if pivot == "P1" else "2")
        add_if_col(nr, "agro_event_id", final_id); add_if_col(nr, "gateway_id", "GW01")
        add_if_col(nr, "source", "scheduled_field_operation"); add_if_col(nr, "confidence", "exact")
        add_if_col(nr, "created_at", nr["started_at"]); add_if_col(nr, "updated_at", nr["started_at"])
        add_if_col(nr, "details", irrig_details)
        add_if_col(nr, "id", deterministic_uuid("agronomic_irrigation", nr.get("agro_event_id", ""), nr.get("started_at", ""), nr.get("ended_at", ""), nr.get("target_scope", "") or nr.get("node_id", "")))
        if len(cycle["segments"]) > 1:
            add_if_col(nr, "notes", "paused_at_17:00_resumed_at_22:00")
        action = "kept" if (old_s == nr["started_at"] and (old_e or "") == nr["ended_at"] and old_t in (scope, node_id) and r.get("event_type","") == "irrigation_session") else "corrected"
        audit_rows.append({"audit_id": f"AUD-{len(audit_rows)+1:04d}", "original_agro_event_id": r.get("agro_event_id",""), "final_agro_event_id": nr.get("agro_event_id",""),
                           "action": action, "old_started_at": old_s, "new_started_at": nr["started_at"], "old_ended_at": old_e, "new_ended_at": nr["ended_at"],
                           "old_target": old_t, "new_target": scope, "reason": "scheduled irrigation operation generated from declared real field schedule",
                           "limitations": "schedule-derived operational event; verify exact manual execution if used as field evidence"})
        corrected.append(nr); used_cycles.add(i)
        if len(cycle["segments"]) > 1:
            resume = deepcopy(nr)
            resume["started_at"] = cycle["segments"][1][0].isoformat()
            resume["ended_at"] = cycle["segments"][1][1].isoformat()
            add_if_col(resume, "notes", "paused_at_17:00_resumed_at_22:00")
            add_if_col(resume, "agro_event_id", f"{final_id}-R")
            add_if_col(resume, "created_at", resume["started_at"]); add_if_col(resume, "updated_at", resume["started_at"])
            add_if_col(resume, "details", irrig_details)
            add_if_col(resume, "id", deterministic_uuid("agronomic_irrigation", resume.get("agro_event_id", ""), resume.get("started_at", ""), resume.get("ended_at", ""), resume.get("target_scope", "") or resume.get("node_id", "")))
            corrected.append(resume)
            audit_rows.append({"audit_id": f"AUD-{len(audit_rows)+1:04d}", "original_agro_event_id": r.get("agro_event_id",""), "final_agro_event_id": resume.get("agro_event_id",""),
                               "action": "corrected", "old_started_at": old_s, "new_started_at": resume["started_at"], "old_ended_at": old_e, "new_ended_at": resume["ended_at"],
                               "old_target": old_t, "new_target": scope, "reason": "scheduled irrigation operation generated from declared real field schedule",
                               "limitations": "schedule-derived operational event; verify exact manual execution if used as field evidence"})
    last_idx = max(used_cycles) if used_cycles else -1
    generated = []
    for i in range(last_idx + 1, len(cycles)):
        cycle = cycles[i]
        node_id, scope, pivot = target_cycle[cycle["cycle_idx"] % 2]
        base_id = style.format(dt=cycle["cycle_start"].strftime("%Y%m%d-%H%M"), pivot="1" if pivot == "P1" else "2")
        for seg_idx, (st, end) in enumerate(cycle["segments"]):
            if st > field_end or end < field_start:
                continue
            row = {c: "" for c in agr_cols}
            add_if_col(row, "id", deterministic_uuid("agronomic_irrigation", base_id, st.isoformat(), str(seg_idx)))
            add_if_col(row, "node_id", node_id)
            add_if_col(row, "agro_event_id", base_id if seg_idx == 0 else f"{base_id}-R")
            add_if_col(row, "gateway_id", "GW01")
            add_if_col(row, "event_category", "irrigation")
            add_if_col(row, "event_type", "irrigation_session")
            add_if_col(row, "target_scope", scope)
            add_if_col(row, "started_at", st.isoformat())
            add_if_col(row, "ended_at", end.isoformat())
            add_if_col(row, "source", "scheduled_field_operation")
            add_if_col(row, "confidence", "exact")
            add_if_col(row, "created_at", row["started_at"]); add_if_col(row, "updated_at", row["started_at"])
            add_if_col(row, "details", irrig_details)
            add_if_col(row, "id", deterministic_uuid("agronomic_irrigation", row.get("agro_event_id", ""), row.get("started_at", ""), row.get("ended_at", ""), row.get("target_scope", "") or row.get("node_id", "")))
            note = "paused_at_17:00_resumed_at_22:00" if len(cycle["segments"]) > 1 else f"Scheduled 8-hour irrigation cycle for {'Pivot 1' if pivot=='P1' else 'Pivot 2'}."
            add_if_col(row, "notes", note)
            generated.append(row)
            audit_rows.append({"audit_id": f"AUD-{len(audit_rows)+1:04d}", "original_agro_event_id": "", "final_agro_event_id": row.get("agro_event_id",""),
                               "action": "generated", "old_started_at": "", "new_started_at": row["started_at"], "old_ended_at": "", "new_ended_at": row["ended_at"],
                               "old_target": "", "new_target": scope, "reason": "scheduled irrigation operation generated from declared real field schedule",
                               "limitations": "schedule-derived operational event; verify exact manual execution if used as field evidence"})
    agr_out = sorted(non_irrigation + corrected + generated, key=lambda r: parse_dt(r["started_at"]))
    for r in agr_out:
        is_irrigation = "irrigation" in (r.get("event_type", "") + "|" + r.get("event_category", "")).lower() or (r.get("agro_event_id", "") or "").startswith("IRR")
        if is_irrigation:
            add_if_col(r, "details", irrig_details)
            add_if_col(r, "gateway_id", (r.get("gateway_id") or "").strip() or "GW01")
            add_if_col(r, "created_at", (r.get("created_at") or "").strip() or r.get("started_at", ""))
        add_if_col(r, "details", normalize_json_str(r.get("details", "")))
        add_if_col(r, "created_at", (r.get("created_at") or "").strip() or r.get("started_at", ""))
    with (OUT_DIR / "agronomic_events_demo.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=agr_cols); w.writeheader(); w.writerows(agr_out)

    with (OUT_DIR / "irrigation_schedule_audit.csv").open("w", newline="", encoding="utf-8") as f:
        cols = ["audit_id", "original_agro_event_id", "final_agro_event_id", "action", "old_started_at", "new_started_at", "old_ended_at", "new_ended_at", "old_target", "new_target", "reason", "limitations"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(audit_rows)

    summary = []
    by_date = defaultdict(list)
    expected_by_date = defaultdict(int)
    for cycle in cycles:
        expected_by_date[cycle["cycle_start"].date().isoformat()] += len(cycle["segments"])
    for a in audit_rows:
        by_date[a["new_started_at"][:10]].append(a)
    for d in sorted(expected_by_date):
        items = by_date.get(d, [])
        summary.append({
            "date": d,
            "expected_cycles": expected_by_date[d],
            "kept_manual_cycles": sum(1 for i in items if i["action"] == "kept"),
            "corrected_manual_cycles": sum(1 for i in items if i["action"] == "corrected"),
            "generated_cycles": sum(1 for i in items if i["action"] == "generated"),
            "first_cycle": min([i["new_started_at"] for i in items], default=""),
            "last_cycle": max([i["new_started_at"] for i in items], default=""),
            "notes": "8-hour scheduled irrigation cycles constrained to 05:00-22:00 and field window",
        })
    with (OUT_DIR / "irrigation_schedule_summary.csv").open("w", newline="", encoding="utf-8") as f:
        cols = ["date", "expected_cycles", "kept_manual_cycles", "corrected_manual_cycles", "generated_cycles", "first_cycle", "last_cycle", "notes"]
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(summary)

    acols = ["audit_id", "original_id", "demo_id", "node_id", "measured_at", "changed_fields", "original_status", "demo_status", "method", "confidence", "source_rows_used", "source_nodes_used", "reason", "limitations"]
    with (OUT_DIR / "demo_generation_audit.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=acols); w.writeheader(); w.writerows(audit)

    qrows = []
    daily_before, daily_recon, daily_final = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    for s in field_rows: daily_before[(s["measured_at"][:10], s["node_id"])][s["status"]] += 1
    injected = {(a["demo_id"], a["node_id"], a["measured_at"]) for a in audit if a["method"] == "controlled_missing_injection"}
    for r in reconstructed:
        k = (r["measured_at"][:10], r["node_id"])
        final_status = r["status"]
        pre_status = "ok" if (r["id"], r["node_id"], r["measured_at"]) in injected else final_status
        daily_recon[k][pre_status] += 1
        daily_final[k][final_status] += 1
    for (day,node) in sorted(daily_final):
        exp = sum(daily_final[(day,node)].values())
        om = daily_before[(day,node)].get("missing",0)
        rm = daily_recon[(day,node)].get("missing",0)
        fm = daily_final[(day,node)].get("missing",0)
        add = max(0,fm-rm)
        level = "excellent" if fm <= 7 else ("moderate" if fm <= 15 else ("incident" if (day,node) in rare else "degraded"))
        qrows.append({"date":day,"node_id":node,"expected_records":exp,"original_missing":om,
                      "reconstructed_missing_before_controlled_injection":rm,"controlled_missing_added":add,
                      "final_remaining_missing":fm,"missing_rate_after":round(fm/max(1,exp),4),"quality_level":level,
                      "notes":"complete day" if exp>=60 else "partial day"})
    qcols=["date","node_id","expected_records","original_missing","reconstructed_missing_before_controlled_injection","controlled_missing_added","final_remaining_missing","missing_rate_after","quality_level","notes"]
    with (OUT_DIR / "daily_operational_quality.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=qcols); w.writeheader(); w.writerows(qrows)

    (OUT_DIR / "demo_generation_rules.md").write_text("Demo data is synthetic/derived for dashboard use only. Raw CSV files are untouched. Uploads are unchanged. Not field-truth evidence.\n", encoding="utf-8")
    (OUT_DIR / "dashboard_demo_notes.md").write_text("Dataset is for dashboard demo and QC visualization only. Synthetic missingness and reconstructions were applied.\n", encoding="utf-8")

    out_cols, out_rows = load_csv(OUT_DIR / "sensor_readings_demo.csv")
    assert out_cols == sensor_cols
    assert len({r["id"] for r in out_rows}) == len(out_rows)
    assert all(RTC_CUTOFF <= parse_dt(r["measured_at"]) <= field_end and parse_dt(r["measured_at"]) >= field_start for r in out_rows)

    with (OUT_DIR / "system_events_demo.csv").open(newline="", encoding="utf-8") as f:
        ev_out = list(csv.DictReader(f))
    assert all(RTC_CUTOFF <= parse_dt(e["event_time"]) <= field_end and parse_dt(e["event_time"]) >= field_start for e in ev_out)

    u_cols_out, u_rows_out = load_csv(OUT_DIR / "uploads_demo.csv")
    assert u_cols_out == upload_cols
    assert all((r.get("id") or "").strip() != "" for r in u_rows_out)
    assert len({r.get("upload_id", "") for r in u_rows_out}) == len(u_rows_out)
    sr_upload_ids = {(r.get("upload_id") or "").strip() for r in out_rows}
    u_upload_ids = {(r.get("upload_id") or "").strip() for r in u_rows_out}
    assert sr_upload_ids.issubset(u_upload_ids)
    ev_upload_ids = {(e.get("upload_id") or "").strip() for e in ev_out if (e.get("upload_id") or "").strip()}
    assert ev_upload_ids.issubset(u_upload_ids)
    assert sum(int((r.get("records_count") or "0").strip() or 0) for r in u_rows_out) == len(out_rows)
    assert sum(int((r.get("events_count") or "0").strip() or 0) for r in u_rows_out) == len(ev_out)
    for r in u_rows_out:
        rs = (r.get("raw_summary") or "").strip()
        if rs:
            json.loads(rs)

    assert all("2026-05-12" <= r["date"] <= "2026-05-20" for r in qrows)
    assert all(field_start <= parse_dt(a["measured_at"]) <= field_end for a in audit)

    main_missing = sum(1 for r in out_rows if r["node_id"] == "MAIN" and r["status"] == "missing")
    assert main_missing == 0
    for r in qrows:
        if r["node_id"] in ("N2", "N3") and r["expected_records"] >= 60:
            assert r["final_remaining_missing"] <= 35

    assert src_hash == {p.name: sha(p) for p in [sensor_path, events_path, uploads_path]}

    a_cols_out, a_rows_out = load_csv(OUT_DIR / "agronomic_events_demo.csv")
    assert a_cols_out == agr_cols
    ids = [r.get("agro_event_id", "") for r in a_rows_out if r.get("agro_event_id", "")]
    assert len(ids) == len(set(ids))
    pk_ids = [r.get("id", "") for r in a_rows_out if (r.get("id", "") or "").strip()]
    assert len(pk_ids) == len(set(pk_ids))
    seen_start_target = set()
    for r in a_rows_out:
        assert (r.get("details", "") or "").strip() != ""
        json.loads(r["details"])
        assert (r.get("created_at", "") or "").strip() != ""
        rid = (r.get("id", "") or "").strip()
        if rid:
            uuid.UUID(rid)
        st = parse_dt(r["started_at"])
        assert field_start <= st <= field_end
        is_irrigation = "irrigation" in (r.get("event_type", "") + "|" + r.get("event_category", "")).lower()
        if is_irrigation:
            ps = st.replace(hour=17, minute=0, second=0, microsecond=0)
            pe = st.replace(hour=22, minute=0, second=0, microsecond=0)
            assert not (ps <= st < pe)
            assert (r.get("gateway_id", "") or "").strip() != ""
        if r.get("ended_at"):
            et = parse_dt(r["ended_at"])
            assert et >= st
            if is_irrigation:
                ps = st.replace(hour=17, minute=0, second=0, microsecond=0)
                pe = st.replace(hour=22, minute=0, second=0, microsecond=0)
                assert et <= ps or st >= pe
        tgt = r.get("target_scope", "") or r.get("node_id", "") or "farm"
        key = (r["started_at"], tgt)
        assert key not in seen_start_target
        seen_start_target.add(key)

    return {"field_start": field_start.isoformat(), "field_end": field_end.isoformat(),
            "inferred_cluster_start": inferred_start.isoformat() if inferred_start else None,
            "inferred_cluster_end": inferred_end.isoformat() if inferred_end else None,
            "rtc_excluded": len(rtc_invalid), "outside_field_excluded": len(outside_field),
            "sensor_demo_rows": len(out_rows), "events_demo_rows": len(ev_out), "audit_rows": len(audit),
            "generated_irrigation_events": len(generated)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--field-start", default=FIELD_START_DEFAULT.isoformat())
    ap.add_argument("--field-end", default=FIELD_END_DEFAULT.isoformat())
    args = ap.parse_args()
    fs = datetime.fromisoformat(args.field_start)
    fe = datetime.fromisoformat(args.field_end)
    a = generate(SEED, fs, fe)
    h1 = sha(OUT_DIR / "sensor_readings_demo.csv")
    b = generate(SEED, fs, fe)
    h2 = sha(OUT_DIR / "sensor_readings_demo.csv")
    assert a == b and h1 == h2
    print(json.dumps(a))
