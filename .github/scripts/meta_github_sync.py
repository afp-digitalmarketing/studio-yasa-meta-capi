#!/usr/bin/env python3
"""
Studio Yasa Meta CAPI + GitHub Integration
Pull WhatsApp orders from GitHub → Upload to Meta → Push logs back to GitHub
"""

import os
import sys
import json
import base64
import requests
import hashlib
import re
from datetime import datetime
import logging
from typing import Optional, Dict, List

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('meta_github_sync.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class GitHubAPI:
    """GitHub API wrapper for repo management"""
    
    def __init__(self, token: str, owner: str, repo: str):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
        self.api_url = f'https://api.github.com/repos/{owner}/{repo}'
    
    def get_file(self, file_path: str) -> Optional[Dict]:
        """Get file content from GitHub"""
        try:
            url = f'{self.api_url}/contents/{file_path}'
            response = requests.get(url, headers=self.headers)
            
            if response.status_code == 200:
                data = response.json()
                # Decode base64 content
                content = base64.b64decode(data['content']).decode('utf-8')
                logger.info(f"✅ Downloaded from GitHub: {file_path}")
                return {
                    'content': content,
                    'sha': data['sha'],
                    'path': file_path
                }
            elif response.status_code == 404:
                logger.warning(f"File not found: {file_path}")
                return None
            else:
                logger.error(f"Error downloading {file_path}: {response.text}")
                return None
        except Exception as e:
            logger.error(f"Error getting file: {str(e)}")
            return None
    
    def push_file(self, file_path: str, content: str, commit_msg: str, file_sha: Optional[str] = None) -> bool:
        """Push file to GitHub"""
        try:
            url = f'{self.api_url}/contents/{file_path}'
            
            # Encode content to base64
            encoded_content = base64.b64encode(content.encode()).decode()
            
            payload = {
                'message': commit_msg,
                'content': encoded_content,
                'branch': 'main'
            }
            
            # If file exists, include SHA for update
            if file_sha:
                payload['sha'] = file_sha
            
            response = requests.put(url, headers=self.headers, json=payload)
            
            if response.status_code in [200, 201]:
                logger.info(f"✅ Pushed to GitHub: {file_path}")
                return True
            else:
                logger.error(f"Error pushing {file_path}: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Error pushing file: {str(e)}")
            return False
    
    def get_commit_info(self) -> Optional[str]:
        """Get latest commit SHA"""
        try:
            url = f'{self.api_url}/commits?per_page=1'
            response = requests.get(url, headers=self.headers)
            
            if response.status_code == 200:
                commits = response.json()
                if commits:
                    return commits[0]['sha'][:7]
            return None
        except Exception as e:
            logger.error(f"Error getting commit info: {str(e)}")
            return None


class MetaCAPIUploader:
    """Upload customer data to Meta Conversions API"""
    
    def __init__(self, pixel_id: str, access_token: str):
        self.pixel_id = pixel_id
        self.access_token = access_token
        self.api_url = f"https://graph.facebook.com/v17.0/{self.pixel_id}/events"
        self.success_count = 0
        self.error_count = 0
        self.results = []
    
    def normalize_phone(self, phone: str) -> Optional[str]:
        """Convert any phone format to E.164 (+62812xxx)"""
        if not phone or phone == '':
            return None
        
        phone = str(phone).strip()
        digits = re.sub(r'\D', '', phone)
        
        if not digits:
            return None
        
        if digits.startswith('0'):
            digits = '62' + digits[1:]
        
        if not digits.startswith('62'):
            digits = '62' + digits
        
        normalized = '+' + digits
        
        if len(normalized) >= 12 and normalized.startswith('+62'):
            return normalized
        
        return None
    
    def hash_field(self, value: str) -> Optional[str]:
        """SHA256 hash for PII fields"""
        if not value:
            return None
        value = str(value).strip().lower()
        return hashlib.sha256(value.encode()).hexdigest()
    
    def upload_from_csv_content(self, csv_content: str) -> bool:
        """Upload from CSV content string"""
        import csv
        from io import StringIO
        
        try:
            # Parse CSV
            customers = []
            reader = csv.DictReader(StringIO(csv_content))
            for row in reader:
                if row:
                    customers.append(row)
            
            logger.info(f"Parsed {len(customers)} customer records from CSV")
            
            if not customers:
                logger.warning("No customer data found")
                return False
            
            # Prepare payload
            payload = []
            
            for idx, customer in enumerate(customers):
                try:
                    # Extract fields
                    phone = customer.get('phone') or customer.get('No. Tlp')
                    phone_normalized = self.normalize_phone(phone)
                    
                    if not phone_normalized:
                        logger.warning(f"Row {idx+1}: Invalid phone - {phone}")
                        self.error_count += 1
                        continue
                    
                    first_name = str(customer.get('first_name') or customer.get('Nama Cust') or '').split()[0]
                    last_name = ' '.join(str(customer.get('last_name') or customer.get('Nama Cust') or '').split()[1:])
                    email = customer.get('email') or ''
                    city = customer.get('city') or customer.get('Loc. Domisili') or ''
                    
                    # Build user data (hashed)
                    user_data = {
                        'ph': self.hash_field(phone_normalized),
                        'em': self.hash_field(email) if email else None,
                        'fn': self.hash_field(first_name) if first_name else None,
                        'ln': self.hash_field(last_name) if last_name else None,
                        'ct': self.hash_field(city) if city else None,
                        'country': 'ID',
                    }
                    
                    # Remove None values
                    user_data = {k: v for k, v in user_data.items() if v}
                    
                    payload.append({
                        'user_data': user_data,
                        'action_source': 'offline_crm',
                    })
                    
                    self.success_count += 1
                
                except Exception as e:
                    logger.error(f"Row {idx+1}: Error - {str(e)}")
                    self.error_count += 1
                    continue
            
            if not payload:
                logger.error("No valid records to upload")
                return False
            
            logger.info(f"Uploading {len(payload)} records to Meta CAPI...")
            
            # Send to Meta
            response = requests.post(
                f"https://graph.facebook.com/v17.0/{self.pixel_id}/uploads",
                json={
                    'data': payload,
                    'access_token': self.access_token
                },
                timeout=30
            )
            
            if response.status_code in [200, 201]:
                result = response.json()
                logger.info("✅ Upload successful!")
                logger.info(f"   Response: {json.dumps(result, indent=2)}")
                self.results.append({
                    'status': 'success',
                    'timestamp': datetime.now().isoformat(),
                    'records_uploaded': len(payload),
                    'response': result
                })
                return True
            else:
                logger.error(f"❌ Upload failed: {response.status_code}")
                logger.error(f"   Error: {response.text}")
                self.results.append({
                    'status': 'failed',
                    'timestamp': datetime.now().isoformat(),
                    'status_code': response.status_code,
                    'error': response.text
                })
                return False
        
        except Exception as e:
            logger.error(f"Error uploading: {str(e)}")
            self.results.append({
                'status': 'error',
                'timestamp': datetime.now().isoformat(),
                'error': str(e)
            })
            return False
    
    def get_report(self) -> str:
        """Generate upload report"""
        report = f"""
╔════════════════════════════════════════╗
║   META CAPI UPLOAD REPORT              ║
╚════════════════════════════════════════╝

Timestamp: {datetime.now().isoformat()}
Pixel ID: {self.pixel_id}

RESULTS:
  Success: {self.success_count}
  Errors:  {self.error_count}
  Total:   {self.success_count + self.error_count}

DETAILS:
{json.dumps(self.results, indent=2)}
"""
        return report


class GitHubMetaSync:
    """Main sync orchestrator"""
    
    def __init__(self, github_token: str, github_owner: str, github_repo: str,
                 meta_pixel_id: str, meta_access_token: str):
        self.github = GitHubAPI(github_token, github_owner, github_repo)
        self.meta = MetaCAPIUploader(meta_pixel_id, meta_access_token)
    
    def run(self, csv_file_path: str = 'whatsapp_orders_current_week.csv') -> bool:
        """Run full sync: Pull from GitHub → Upload to Meta → Push logs back"""
        
        logger.info("╔════════════════════════════════════════╗")
        logger.info("║  GITHUB → META → GITHUB SYNC START     ║")
        logger.info("╚════════════════════════════════════════╝")
        
        # Step 1: Get latest commit for reference
        commit_sha = self.github.get_commit_info()
        logger.info(f"GitHub commit: {commit_sha or 'unknown'}")
        
        # Step 2: Download CSV from GitHub
        logger.info(f"\n📥 Downloading CSV from GitHub: {csv_file_path}")
        file_data = self.github.get_file(csv_file_path)
        
        if not file_data:
            logger.error("Failed to download CSV from GitHub")
            return False
        
        csv_content = file_data['content']
        csv_sha = file_data['sha']
        
        logger.info(f"✅ CSV downloaded ({len(csv_content)} bytes)")
        
        # Step 3: Upload to Meta
        logger.info(f"\n📤 Uploading to Meta Conversions API...")
        success = self.meta.upload_from_csv_content(csv_content)
        
        # Step 4: Generate report
        report = self.meta.get_report()
        logger.info(report)
        
        # Step 5: Push logs back to GitHub
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file_path = f'logs/upload_report_{timestamp}.json'
        
        logger.info(f"\n📤 Pushing report to GitHub: {log_file_path}")
        
        log_content = json.dumps(self.meta.results, indent=2)
        self.github.push_file(
            log_file_path,
            log_content,
            f"Meta CAPI upload report {timestamp}"
        )
        
        # Final status
        logger.info("\n╔════════════════════════════════════════╗")
        if success:
            logger.info("║  ✅ SYNC COMPLETED SUCCESSFULLY        ║")
        else:
            logger.info("║  ⚠️  SYNC COMPLETED WITH ERRORS        ║")
        logger.info("╚════════════════════════════════════════╝")
        
        return success


def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='GitHub → Meta CAPI sync for Studio Yasa'
    )
    parser.add_argument('--github-token', required=True, help='GitHub personal access token')
    parser.add_argument('--github-owner', required=True, help='GitHub repo owner')
    parser.add_argument('--github-repo', required=True, help='GitHub repo name')
    parser.add_argument('--csv-file', default='whatsapp_orders_current_week.csv', help='CSV file path in repo')
    parser.add_argument('--meta-pixel-id', required=True, help='Meta pixel ID')
    parser.add_argument('--meta-token', required=True, help='Meta access token')
    
    args = parser.parse_args()
    
    # Initialize sync
    sync = GitHubMetaSync(
        github_token=args.github_token,
        github_owner=args.github_owner,
        github_repo=args.github_repo,
        meta_pixel_id=args.meta_pixel_id,
        meta_access_token=args.meta_token
    )
    
    # Run sync
    success = sync.run(args.csv_file)
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
