#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import random
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
        for k, d in enumerate(days):
            base = rng.randint(0, 5)
            if k == len(days)//3:
                base = rng.randint(8, 12)
            if k == (2*len(days))//3:
                base = rng.randint(20, 28)
            inj[(d, node)] = base

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
            controlled[(day, node)] += 1
            audit.append({"audit_id": str(audit_id), "original_id": r["id"], "demo_id": r["id"], "node_id": node,
                         "measured_at": r["measured_at"], "changed_fields": ";".join(REQ[node]), "original_status": old,
                         "demo_status": "missing", "method": "controlled_missing_injection", "confidence": "high",
                         "source_rows_used": "", "source_nodes_used": "",
                         "reason": "controlled demo missingness for dashboard QC/reliability testing",
                         "limitations": "synthetic demo scenario; not field-truth"})
            audit_id += 1

    OUT_DIR.mkdir(exist_ok=True)
    with (OUT_DIR / "sensor_readings_demo.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sensor_cols); w.writeheader(); w.writerows(reconstructed)

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
        if cnt == 0: continue
        ts = f"{day}T12:00:00+01:00"
        for et,sev,msg,err,dh in [
            ("signal_quality_degraded","warning","Controlled synthetic degradation for dashboard QC test.","demo_missing",0),
            ("node_missing","warning","Node telemetry gaps observed in demo scenario.","demo_missing",0),
            ("telemetry_restored","info","Telemetry restored in demo scenario.","",2),
            ("signal_quality_improved","info","Signal quality improved in demo scenario.","",3),
        ]:
            t = (datetime.fromisoformat(ts)+timedelta(hours=dh)).isoformat()
            if t < field_start.isoformat() or t > field_end.isoformat():
                continue
            out_events.append({"id": str(next_id), "event_id": f"DEMO_EVT_{next_id}", "upload_id": "", "gateway_id": "",
                               "node_id": node, "event_type": et, "severity": sev, "event_time": t, "received_at": t,
                               "message": msg, "details": f"missing_injected={cnt}", "error_code": err})
            next_id += 1

    with (OUT_DIR / "system_events_demo.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=event_cols); w.writeheader(); w.writerows(out_events)

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

    assert all("2026-05-12" <= r["date"] <= "2026-05-20" for r in qrows)
    assert all(field_start <= parse_dt(a["measured_at"]) <= field_end for a in audit)

    main_missing = sum(1 for r in out_rows if r["node_id"] == "MAIN" and r["status"] == "missing")
    assert main_missing == 0
    for r in qrows:
        if r["node_id"] in ("N2", "N3") and r["expected_records"] >= 60:
            assert r["final_remaining_missing"] <= 35

    assert src_hash == {p.name: sha(p) for p in [sensor_path, events_path, uploads_path]}

    return {"field_start": field_start.isoformat(), "field_end": field_end.isoformat(),
            "inferred_cluster_start": inferred_start.isoformat() if inferred_start else None,
            "inferred_cluster_end": inferred_end.isoformat() if inferred_end else None,
            "rtc_excluded": len(rtc_invalid), "outside_field_excluded": len(outside_field),
            "sensor_demo_rows": len(out_rows), "events_demo_rows": len(ev_out), "audit_rows": len(audit)}


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
