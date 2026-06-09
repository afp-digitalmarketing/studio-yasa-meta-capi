#!/usr/bin/env python3
"""
Studio Yasa Meta CAPI + GitHub - Simplified Version
Reads from environment variables. No command-line arguments.
"""

import os
import requests
import csv
import hashlib
import re
import json
from datetime import datetime
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    handlers=[
        logging.FileHandler('sync.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# Read environment variables
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
GITHUB_OWNER = os.getenv('GITHUB_OWNER', 'afp-digitalmarketing')
GITHUB_REPO = os.getenv('GITHUB_REPO', 'studio-yasa-meta-capi')
CSV_FILE = os.getenv('CSV_FILE', 'yasa_capi_phone_upload.csv')
META_PIXEL_ID = os.getenv('META_PIXEL_ID', '413265851175780')
META_TOKEN = os.getenv('META_ACCESS_TOKEN')

log.info("╔════════════════════════════════════════╗")
log.info("║  Studio Yasa Meta CAPI Sync            ║")
log.info("╚════════════════════════════════════════╝")

# Validate environment
if not GITHUB_TOKEN or not META_TOKEN:
    log.error("❌ Missing GitHub token or Meta token!")
    exit(1)

def normalize_phone(phone):
    """Normalize phone to E.164 format"""
    if not phone:
        return None
    phone = str(phone).strip()
    digits =
