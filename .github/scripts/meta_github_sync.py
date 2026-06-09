name: Meta CAPI Weekly Sync
on:
  schedule:
    - cron: '0 10 * * 0'
  workflow_dispatch:

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - uses: actions/setup-python@v4
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: pip install requests

      - name: Upload to Meta CAPI
        env:
          META_PIXEL_ID: '413265851175780'
          META_ACCESS_TOKEN: ${{ secrets.META_ACCESS_TOKEN }}
          CSV_FILE: 'yasa_capi_phone_upload.csv'
        run: |
          python3 << 'SCRIPT'
          import os, requests, csv, hashlib, re, json
          from io import StringIO

          print("=== Studio Yasa Meta CAPI Sync ===")
          TOKEN = os.environ.get("META_ACCESS_TOKEN","")
          PIXEL = os.environ.get("META_PIXEL_ID","413265851175780")
          CSVF = os.environ.get("CSV_FILE","yasa_capi_phone_upload.csv")
          if not TOKEN:
              print("ERROR: META_ACCESS_TOKEN not set")
              exit(1)
          if not os.path.exists(CSVF):
              print(f"ERROR: {CSVF} not found")
              print(f"Files: {os.listdir('.')}")
              exit(1)
          with open(CSVF,"r",encoding="utf-8") as f:
              content=f.read()
          print(f"CSV loaded: {len(content)} bytes")
          rows=list(csv.DictReader(StringIO(content)))
          print(f"Total rows: {len(rows)}")
          payload=[]
          err=0
          for r in rows:
              p=r.get("phone") or r.get("No. Tlp") or ""
              d=re.sub(r"\D","",str(p).strip())
              if not d:
                  err+=1
                  continue
              if d.startswith("0"):
                  d="62"+d[1:]
              if not d.startswith("62"):
                  d="62"+d
              n="+"+d
              if len(n)<12:
                  err+=1
                  continue
              h=lambda v:hashlib.sha256(str(v).strip().lower().encode()).hexdigest() if v else None
              fn=str(r.get("first_name") or r.get("Nama Cust") or "").split()[0] if (r.get("first_name") or r.get("Nama Cust")) else ""
              ct=r.get("city") or r.get("Loc. Domisili") or ""
              ud={"ph":h(n),"country":"ID"}
              if fn:
                  ud["fn"]=h(fn)
              if ct:
                  ud["ct"]=h(ct)
              payload.append({"user_data":ud,"action_source":"offline_crm"})
          print(f"Valid: {len(payload)} | Skipped: {err}")
          if not payload:
              print("ERROR: No valid records")
              exit(1)
          print(f"Uploading {len(payload)} records...")
          resp=requests.post(f"https://graph.facebook.com/v17.0/{PIXEL}/events",json={"data":json.dumps(payload),"access_token":TOKEN},timeout=60)
          print(f"Status: {resp.status_code}")
          print(f"Response: {resp.text}")
          print("=== Done ===")
          SCRIPT
