#!/usr/bin/env python3
"""
meta_capi_whatsapp_sync.py
--------------------------
Inject Studio Yasa's WhatsApp / offline orders into Meta via the Conversions API.

WHY
  The browser pixel + website CAPI only see WEBSITE checkouts (~33% of revenue).
  ~67% of revenue comes via WhatsApp (chat / langganan / follow-up) and is invisible
  to Meta. This reads the orders, keeps ONLY the WA/offline ones, and sends them as
  server-side Purchase events so Meta can optimize on the full picture and the WA
  campaigns finally become measurable.

  Website rows ("Cust dr web") are SKIPPED on purpose (already tracked by the pixel).

DATA SOURCE (pick one)
  SHEET_CSV_URL : a Google Sheet published as CSV (recommended - always fresh, no PII in repo)
  CSV_FILE      : local CSV path (fallback). Default: yasa_orders.csv
  Expected columns (same headers as the sheet; extras are ignored):
    Tanggal | Waktu Transaksi | Nama Cust | No. Tlp | Loc. Domisili | Total | Leads Closing

SAFETY
  - All PII is SHA-256 hashed locally before leaving the machine.
  - Deterministic event_id => re-runs never duplicate (Meta dedups on event_name+event_id;
    a local sent_events.json ledger is also kept).
  - DRY_RUN=1 prints a sample without sending.
  - Set META_TEST_EVENT_CODE and watch Events Manager > Test Events before going live.

ENV VARS
  META_PIXEL_ID, META_ACCESS_TOKEN            (required for live)
  SHEET_CSV_URL  or  CSV_FILE                 (data source)
  META_TEST_EVENT_CODE                        (optional; test mode)
  DRY_RUN=1                                   (optional; print only)
  MAX_AGE_DAYS                                (optional; default 7. Use 62 for one-time backfill)
"""

import os
import sys
import json
import time
import hashlib
import re
import datetime as dt
from io import StringIO
from pathlib import Path

import requests
import pandas as pd

API_VERSION = "v21.0"
CURRENCY = "IDR"
SENT_LOG = Path("sent_events.json")     # local dedup ledger; commit back (or keep as artifact)

# tolerant column lookup (handles the sheet's headers or simpler ones)
COLS = {
    "date":  ["Tanggal", "tanggal", "date"],
    "time":  ["Waktu Transaksi", "waktu", "time"],
    "name":  ["Nama Cust", "nama", "name", "customer"],
    "phone": ["No. Tlp", "No Tlp", "phone", "Phone", "telp"],
    "city":  ["Loc. Domisili", "city", "kota", "domisili"],
    "total": ["Total", "total", "value", "nilai"],
    "chan":  ["Leads Closing", "channel", "leads"],
}


def pick(df_cols, keys):
    for k in keys:
        if k in df_cols:
            return k
    # fuzzy: header contains the key word
    for c in df_cols:
        for k in keys:
            if k.lower() in str(c).lower():
                return c
    return None


def sha256(v: str) -> str:
    return hashlib.sha256(str(v).strip().lower().encode("utf-8")).hexdigest()


def to_num(v):
    """Parse Total even when formatted like '749,000.00' or 'Rp 749.000'."""
    if pd.isna(v):
        return float("nan")
    s = str(v).strip()
    # US-style here (comma=thousands, dot=decimal), per the sheet. Keep digits/dot/minus.
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in ("", "-", "."):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def norm_phone_id(raw) -> str:
    """Indonesian phone -> E.164 digits, no '+'. Excel/CSV often drops the leading 0,
    or stores the number as a float (8.2e10 / '...188.0'). Normalize all of it."""
    if pd.isna(raw):
        return ""
    if isinstance(raw, float):                 # avoid 8.2389936188e10 / trailing .0
        raw = "{:.0f}".format(raw)
    s = str(raw).strip()
    if s.endswith(".0"):
        s = s[:-2]
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("0"):
        digits = "62" + digits[1:]             # 0823... -> 62823...
    elif digits.startswith("62"):
        pass                                   # already E.164
    elif digits.startswith("8"):
        digits = "62" + digits                 # admin forgot the 0: 823... -> 62823...
    return digits


def classify(label) -> str:
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


def read_source() -> pd.DataFrame:
    url = os.environ.get("SHEET_CSV_URL", "").strip()
    if url:
        print(f"Reading orders from SHEET_CSV_URL ...")
        text = requests.get(url, timeout=60).text
        return pd.read_csv(StringIO(text), dtype=str)      # dtype=str => phones stay intact
    path = os.environ.get("CSV_FILE", "yasa_orders.csv")
    if not os.path.exists(path):
        sys.exit(f"ERROR: no SHEET_CSV_URL and CSV_FILE '{path}' not found. Files: {os.listdir('.')}")
    print(f"Reading orders from {path} ...")
    return pd.read_csv(path, dtype=str)


def load_wa_orders() -> list[dict]:
    df = read_source()
    df.columns = [str(c).strip() for c in df.columns]
    c = {k: pick(df.columns, v) for k, v in COLS.items()}
    if not (c["total"] and c["name"]):
        sys.exit(f"ERROR: required columns missing. Found: {list(df.columns)}")
    if c["date"]:
        df[c["date"]] = df[c["date"]].ffill()     # merged/blank date cells

    orders = []
    for _, r in df.iterrows():
        if pd.isna(r.get(c["name"])):              # skip recap/subtotal rows
            continue
        total = to_num(r.get(c["total"]))
        if pd.isna(total) or total <= 0 or total > 5_000_000:
            continue
        bucket = classify(r.get(c["chan"]) if c["chan"] else None)
        if bucket not in WA_BUCKETS:               # skip website + unlabeled
            continue

        phone_id = norm_phone_id(r.get(c["phone"])) if c["phone"] else ""
        name = str(r.get(c["name"])).strip()
        parts = name.split()
        fn = parts[0] if parts else ""
        ln = parts[-1] if len(parts) > 1 else ""
        city = ""
        if c["city"] and not pd.isna(r.get(c["city"])):
            city = "".join(ch for ch in str(r.get(c["city"])).lower() if ch.isalnum())

        try:
            d = pd.to_datetime(r.get(c["date"]), dayfirst=True).date() if c["date"] else dt.date.today()
            tstr = str(r.get(c["time"])) if c["time"] else ""
            tt = pd.to_datetime(tstr).time() if tstr and tstr != "nan" else dt.time(12, 0)
            ev_time = int(dt.datetime.combine(d, tt).timestamp())
        except Exception:
            continue

        ev_id = sha256(f"{phone_id}|{int(total)}|{ev_time}|{name.lower()}")
        orders.append({
            "event_id": ev_id, "event_time": ev_time, "value": float(total),
            "bucket": bucket, "phone_id": phone_id, "fn": fn, "ln": ln, "city": city,
        })
    return orders


def to_capi_event(o: dict) -> dict:
    ud = {"country": [sha256("id")]}
    if o["phone_id"]:
        ud["ph"] = [sha256(o["phone_id"])]
    if o["fn"]:
        ud["fn"] = [sha256(o["fn"])]
    if o["ln"]:
        ud["ln"] = [sha256(o["ln"])]
    if o["city"]:
        ud["ct"] = [sha256(o["city"])]
    return {
        "event_name": "Purchase",
        "event_time": o["event_time"],
        "event_id": o["event_id"],
        "action_source": "business_messaging",
        "messaging_channel": "whatsapp",
        "page_id": "100075700851363",
        "user_data": ud,
        "custom_data": {"currency": CURRENCY, "value": round(o["value"], 2)},
    }


def main():
    pixel_id = os.environ.get("META_PIXEL_ID")
    token = os.environ.get("META_ACCESS_TOKEN")
    test_code = os.environ.get("META_TEST_EVENT_CODE")
    dry_run = os.environ.get("DRY_RUN") == "1"
    max_age_days = int(os.environ.get("MAX_AGE_DAYS", "7"))

    if not (pixel_id and token) and not dry_run:
        sys.exit("ERROR: set META_PIXEL_ID and META_ACCESS_TOKEN (or DRY_RUN=1).")

    already = set(json.loads(SENT_LOG.read_text())) if SENT_LOG.exists() else set()
    cutoff = time.time() - max_age_days * 86400

    orders = load_wa_orders()
    fresh = [o for o in orders if o["event_time"] >= cutoff and o["event_id"] not in already]

    by_bucket = {}
    for o in fresh:
        by_bucket[o["bucket"]] = by_bucket.get(o["bucket"], 0) + 1
    print(f"WA/offline orders: {len(orders)} | new & within {max_age_days}d: {len(fresh)} | {by_bucket}")
    print(f"value to send: Rp{sum(o['value'] for o in fresh):,.0f}")
    if not fresh:
        print("Nothing new to send. Done.")
        return

    events = [to_capi_event(o) for o in fresh]
    if dry_run:
        print("DRY_RUN - sample event (PII hashed):")
        print(json.dumps(events[0], indent=2))
        return

    url = f"https://graph.facebook.com/{API_VERSION}/{pixel_id}/events"
    sent_ids = []
    for i in range(0, len(events), 1000):
        batch = events[i:i + 1000]
        payload = {"data": batch, "access_token": token}    # NOTE: list, not json.dumps()
        if test_code:
            payload["test_event_code"] = test_code
        resp = requests.post(url, json=payload, timeout=60)
        if resp.status_code != 200:
            print(f"  batch {i//1000}: HTTP {resp.status_code} -> {resp.text}")
            break
        print(f"  batch {i//1000}: received={resp.json().get('events_received')}")
        sent_ids += [e["event_id"] for e in batch]
        time.sleep(1)

    if sent_ids and not test_code:
        SENT_LOG.write_text(json.dumps(sorted(already | set(sent_ids))))
        print(f"Logged {len(sent_ids)} event_ids to {SENT_LOG}.")


if __name__ == "__main__":
    main()
