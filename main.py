import csv
import hashlib
import io
import json
import os
import random
import time
import requests

API_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
SALT = "tB87#kPtkxqOS2"
HISTORY_FILE = "redemption_history.json"
MAX_RETRIES = 2

# Your Google Sheet CSV Export URL
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1ykHBJWvJNEpOYg-D4W8CDTjPs-gYvQ89cp9FPj8FNfQ/gviz/tq?tqx=out:csv"

headers = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://wos-giftcode.centurygame.com",
    "Referer": "https://wos-giftcode.centurygame.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def load_accounts_from_google_sheet(csv_url: str) -> list:
    """Fetches account list directly from Google Sheet CSV endpoint."""
    print("=== Fetching Accounts from Google Sheet ===")
    try:
        res = requests.get(csv_url, timeout=10)
        res.raise_for_status()
        
        accounts = []
        reader = csv.DictReader(io.StringIO(res.text))
        for row in reader:
            fid = str(row.get("fid", "")).strip()
            kid = str(row.get("kid", "")).strip()
            if fid and kid:
                accounts.append({"fid": fid, "kid": kid})
                
        print(f"[✓] Successfully loaded {len(accounts)} accounts from Google Sheet.\n")
        return accounts
    except Exception as e:
        print(f"[!] Critical Error: Failed to fetch accounts from Google Sheet: {e}")
        return []

def load_history() -> dict:
    """Load redemption history from JSON file."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Warning: Failed to load history ({e}). Starting fresh.")
    return {"claimed": {}, "expired_codes": []}

def save_history(history: dict):
    """Save updated redemption history to JSON file."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=4)

def generate_sign(params: dict) -> str:
    """Alphabetical sorting of keys + SALT hash generation."""
    sorted_str = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    raw_str = f"{sorted_str}{SALT}"
    return hashlib.md5(raw_str.encode('utf-8')).hexdigest()

def batch_redeem(accounts: list, gift_codes: list):
    if not accounts or not gift_codes:
        print("[!] No accounts or gift codes provided. Aborting execution.")
        return

    history = load_history()
    
    stats = {
        "success": 0,
        "already_claimed": 0,
        "skipped_memory": 0,
        "expired": 0,
        "failed": 0
    }
    
    print("=== Running Batch Redemption with History Tracking ===")
    print(f"Loaded Memory: {sum(len(v) for v in history['claimed'].values())} past redemptions | {len(history['expired_codes'])} expired codes\n")
    
    for cdk in gift_codes:
        if cdk in history["expired_codes"]:
            print(f"--- Code: '{cdk}' [SKIPPED: Marked as Expired in Memory] ---")
            stats["expired"] += len(accounts)
            continue
            
        print(f"--- Code: '{cdk}' ---")
        
        for acc in accounts:
            fid = acc["fid"]
            kid = acc["kid"]
            
            if fid in history["claimed"] and cdk in history["claimed"][fid]:
                print(f"  [⏩] SKIPPED         | Player ID: {fid} (Already in history)")
                stats["skipped_memory"] += 1
                continue
                
            # Retry loop
            for attempt in range(MAX_RETRIES + 1):
                ts = int(time.time())
                payload = {
                    "cdk": cdk,
                    "fid": fid,
                    "kid": kid,
                    "time": ts
                }
                payload["sign"] = generate_sign(payload)
                
                try:
                    res = requests.post(API_URL, headers=headers, data=payload, timeout=10)
                    data = res.json()
                    
                    err_code = data.get("err_code")
                    msg = str(data.get("msg", "")).upper()
                    
                    # 1. Fresh success
                    if err_code == 0 or msg == "SUCCESS":
                        print(f"  [✓] SUCCESS         | Player ID: {fid}")
                        stats["success"] += 1
                        
                        if fid not in history["claimed"]:
                            history["claimed"][fid] = []
                        if cdk not in history["claimed"][fid]:
                            history["claimed"][fid].append(cdk)
                            save_history(history)
                        break
                            
                    # 2. Already claimed / Same Type Exchange
                    elif any(k in msg for k in ["CLAIMED", "RECEIVED", "SAME TYPE", "EXCHANGE"]) or err_code in [40008, 40011]:
                        print(f"  [-] ALREADY CLAIMED | Player ID: {fid} (Same Event Type)")
                        stats["already_claimed"] += 1
                        
                        if fid not in history["claimed"]:
                            history["claimed"][fid] = []
                        if cdk not in history["claimed"][fid]:
                            history["claimed"][fid].append(cdk)
                            save_history(history)
                        break
                            
                    # 3. Expired code
                    elif "EXPIRED" in msg or err_code == 40007:
                        print(f"  [!] EXPIRED CODE    | '{cdk}' is expired. Saving to memory...")
                        stats["expired"] += 1
                        if cdk not in history["expired_codes"]:
                            history["expired_codes"].append(cdk)
                            save_history(history)
                        break
                        
                    # 4. Server Error
                    else:
                        print(f"  [✗] FAILED         | Player ID: {fid} -> {data.get('msg', data)}")
                        stats["failed"] += 1
                        break

                except (requests.exceptions.Timeout, requests.exceptions.RequestException) as e:
                    if attempt < MAX_RETRIES:
                        print(f"  [⏳] TIMEOUT/ERROR   | Player ID: {fid} -> Retrying... ({attempt + 1}/{MAX_RETRIES})")
                        time.sleep(2)
                    else:
                        print(f"  [✗] FAILED (TIMEOUT) | Player ID: {fid} -> Exhausted retries")
                        stats["failed"] += 1

            if cdk in history["expired_codes"]:
                break

            delay = round(random.uniform(3.0, 7.0), 2)
            time.sleep(delay)
            
        print()

    # Final Report
    total_processed = sum(stats.values())
    total_successful = stats["success"] + stats["already_claimed"] + stats["skipped_memory"]
    
    print("==========================================")
    print("         REDEMPTION SUMMARY REPORT        ")
    print("==========================================")
    print(f"  [✓] Newly Redeemed         : {stats['success']}")
    print(f"  [-] Already Claimed (API)  : {stats['already_claimed']}")
    print(f"  [⏩] Skipped (Local Memory) : {stats['skipped_memory']}")
    print(f"  [!] Expired Codes           : {stats['expired']}")
    print(f"  [✗] Failed Requests         : {stats['failed']}")
    print("------------------------------------------")
    print(f"  TOTAL SUCCESSFUL / CLAIMED : {total_successful} / {total_processed}")
    print("==========================================\n")

if __name__ == "__main__":
    # Get codes from GitHub Actions environment input, or fallback to default list
    env_codes = os.getenv("INPUT_GIFT_CODES")
    if env_codes:
        raw_list = env_codes.replace(",", " ").split()
        GIFT_CODES = list(dict.fromkeys([c.strip() for c in raw_list if c.strip()]))
    else:
        GIFT_CODES = ["JULHD2026JP", "gogoWOS", "2ndYoutubeKR", "1stYoutubeKR"]

    ACCOUNTS = load_accounts_from_google_sheet(SHEET_CSV_URL)
    batch_redeem(ACCOUNTS, GIFT_CODES)