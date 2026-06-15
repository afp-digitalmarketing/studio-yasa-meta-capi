#!/usr/bin/env python3
"""
meta_capi_whatsapp_sync.py
--------------------------
Inject Studio Yasa's WhatsApp / offline orders into Meta via the Conversions API.

WHY THIS EXISTS
  The browser pixel + the current CAPI only see WEBSITE checkouts (~33% of revenue).
  ~67% of real revenue comes through WhatsApp (chat / langganan / follow-up) and is
  invisible to Meta. This script reads the sales workbook, keeps ONLY the WA/offline
  orders, and sends them to Meta as server-side Purchase events so the algorithm can
  optimize on the full picture (and so the WA campaigns finally become measurable).

WHAT IT DOES NOT DO
  It deliberately SKIPS "Cust dr web" rows — those are already tracked by the pixel.
  Sending them again as offline would risk double counting.

SAFETY
  - All PII is SHA-256 hashed locally before it ever leaves the machine.
  - Deterministic event_id per order => re-running never creates duplicates
    (Meta dedups on event_name + event_id; we also keep a local sent-log).
  - DRY_RUN=1 prints what WOULD be sent without calling Meta.
  - Set META_TEST_EVENT_CODE first and watch Events Manager > Test Events before going live.

ENV VARS (map these to your existing GitHub Secrets)
  META_PIXEL_ID        : dataset/pixel id, e.g. 413265951175780
  META_ACCESS_TOKEN    : system-user token with ads_management / business_management
  SALES_FILE           : path to the .xlsx (default: Penjualan-Online_2026_YASA.xlsx)
  META_TEST_EVENT_CODE : (optional) e.g. TEST12345 — use until you confirm events land
  DRY_RUN              : (optional) "1" to print only, no network call
  MAX_AGE_DAYS         : (optional) only send orders newer than N days (default 7;
                         7 = optimization window. Use 62 for a one-time audience backfill)
"""

import os
import sys
import json
import time
import hashlib
import datetime as dt
from pathlib import Path

import requests
import pandas as pd

API_VERSION = "v21.0"
CURRENCY = "IDR"
MONTH_SHEETS = [
    "Transaksi Januari", "Transaksi Februari", "Transaksi Maret",
    "Transaksi April", "Transaksi Mei", "Transaksi Juni",
]
SENT_LOG = Path("sent_events.json")     # local dedup ledger, commit it back to the repo


# ----------------------------------------------------------------------------- helpers
def sha256(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def norm_phone_id(raw) -> str:
    """Indonesian phone -> E.164 digits (no +). Excel often drops the leading 0."""
    if pd.isna(raw):
        return ""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    elif digits.startswith("62"):
        pass
    elif digits.startswith("8"):
        digits = "62" + digits
    return digits


def classify(label) -> str:
    """Map the 'Leads Closing' column to a channel bucket."""
    if pd.isna(label):
        return "unlabeled"
    s = str(label).lower()
    if "web" in s:
        return "website"          # already tracked by pixel -> SKIP
    if "langganan" in s:
        return "wa_langganan"
    if "chat" in s or s.startswith("fu") or "follow" in s:
        return "wa_chat"
    return "wa_other"             # creative/vid-named, per ops also chat-driven


WA_BUCKETS = {"wa_langganan", "wa_chat", "wa_other"}


# ----------------------------------------------------------------------------- load
def load_wa_orders(path: str) -> list[dict]:
    xl = pd.ExcelFile(path)
    orders = []
    for sheet in MONTH_SHEETS:
        if sheet not in xl.sheet_names:
            continue
        df = pd.read_excel(xl, sheet_name=sheet, header=0)
        df.columns = [str(c).strip() for c in df.columns]
        df["Tanggal"] = df["Tanggal"].ffill()           # dates are merged cells
        chan_col = next((c for c in df.columns if "Leads" in c), None)
        for _, r in df.iterrows():
            # skip recap/subtotal rows (no customer name) and non-orders
            if pd.isna(r.get("Nama Cust")):
                continue
            total = pd.to_numeric(r.get("Total"), errors="coerce")
            if pd.isna(total) or total <= 0 or total > 5_000_000:
                continue
            bucket = classify(r.get(chan_col))
            if bucket not in WA_BUCKETS:                 # skip website + unlabeled
                continue

            phone_id = norm_phone_id(r.get("No. Tlp"))
            name = str(r.get("Nama Cust")).strip()
            parts = name.split()
            fn = parts[0] if parts else ""
            ln = parts[-1] if len(parts) > 1 else ""
            city = "" if pd.isna(r.get("Loc. Domisili")) else \
                "".join(ch for ch in str(r.get("Loc. Domisili")).lower() if ch.isalnum())

            # event time from Tanggal (+ Waktu Transaksi if present)
            try:
                d = pd.to_datetime(r["Tanggal"]).date()
                tstr = str(r.get("Waktu Transaksi"))
                tt = pd.to_datetime(tstr).time() if tstr and tstr != "nan" else dt.time(12, 0)
                ev_time = int(dt.datetime.combine(d, tt).timestamp())
            except Exception:
                continue

            # deterministic id => idempotent across runs
            ev_id = sha256(f"{sheet}|{r.get('No.')}|{phone_id}|{int(total)}|{ev_time}")

            orders.append({
                "event_id": ev_id, "event_time": ev_time, "value": float(total),
                "bucket": bucket, "phone_id": phone_id, "fn": fn, "ln": ln, "city": city,
            })
    return orders


# ----------------------------------------------------------------------------- build
def to_capi_event(o: dict) -> dict:
    user_data = {}
    if o["phone_id"]:
        user_data["ph"] = [sha256(o["phone_id"])]
    if o["fn"]:
        user_data["fn"] = [sha256(o["fn"])]
    if o["ln"]:
        user_data["ln"] = [sha256(o["ln"])]
    if o["city"]:
        user_data["ct"] = [sha256(o["city"])]
    user_data["country"] = [sha256("id")]
    return {
        "event_name": "Purchase",
        "event_time": o["event_time"],
        "event_id": o["event_id"],
        "action_source": "business_messaging",   # WhatsApp-driven, NOT website -> no dedup clash
        "user_data": user_data,
        "custom_data": {"currency": CURRENCY, "value": round(o["value"], 2)},
    }


# ----------------------------------------------------------------------------- main
def main():
    pixel_id = os.environ.get("META_PIXEL_ID")
    token = os.environ.get("META_ACCESS_TOKEN")
    sales_file = os.environ.get("SALES_FILE", "Penjualan-Online_2026_YASA.xlsx")
    test_code = os.environ.get("META_TEST_EVENT_CODE")
    dry_run = os.environ.get("DRY_RUN") == "1"
    max_age_days = int(os.environ.get("MAX_AGE_DAYS", "7"))

    if not (pixel_id and token) and not dry_run:
        sys.exit("ERROR: set META_PIXEL_ID and META_ACCESS_TOKEN (or DRY_RUN=1).")

    already = set()
    if SENT_LOG.exists():
        already = set(json.loads(SENT_LOG.read_text()))

    cutoff = time.time() - max_age_days * 86400
    orders = load_wa_orders(sales_file)
    fresh = [o for o in orders if o["event_time"] >= cutoff and o["event_id"] not in already]

    by_bucket = {}
    for o in fresh:
        by_bucket[o["bucket"]] = by_bucket.get(o["bucket"], 0) + 1
    print(f"WA/offline orders found: {len(orders)} | "
          f"new & within {max_age_days}d: {len(fresh)} | by bucket: {by_bucket}")
    print(f"value to send: Rp{sum(o['value'] for o in fresh):,.0f}")

    if not fresh:
        print("Nothing new to send. Done.")
        return

    events = [to_capi_event(o) for o in fresh]

    if dry_run:
        print("DRY_RUN — sample event (PII hashed):")
        print(json.dumps(events[0], indent=2))
        return

    url = f"https://graph.facebook.com/{API_VERSION}/{pixel_id}/events"
    sent_ids = []
    for i in range(0, len(events), 1000):           # Meta allows up to 1000 events/call
        batch = events[i:i + 1000]
        payload = {"data": batch, "access_token": token}
        if test_code:
            payload["test_event_code"] = test_code
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code != 200:
            print(f"  batch {i//1000}: HTTP {resp.status_code} -> {resp.text}")
            break
        print(f"  batch {i//1000}: received={resp.json().get('events_received')}")
        sent_ids += [e["event_id"] for e in batch]
        time.sleep(1)

    if sent_ids and not test_code:                  # don't log test events as 'sent'
        SENT_LOG.write_text(json.dumps(sorted(already | set(sent_ids))))
        print(f"Logged {len(sent_ids)} event_ids to {SENT_LOG} (commit this back).")


if __name__ == "__main__":
    main()
