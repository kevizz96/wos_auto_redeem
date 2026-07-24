import csv
import hashlib
import io
import json
import logging
import os
import random
import time
import requests

# Set up logging for redemption.log file
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("redemption.log", encoding="utf-8")
    ]
)

API_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
SALT = "tB87#kPtkxqOS2"
HISTORY_FILE = "redemption_history.json"
MAX_RETRIES = 2

# Google Sheet Configuration
SHEET_ID = "1ykHBJWvJNEpOYg-D4W8CDTjPs-gYvQ89cp9FPj8FNfQ"
PLAYERS_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet=player_ids"
CODES_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet=g_codes"

headers = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://wos-giftcode.centurygame.com",
    "Referer": "https://wos-giftcode.centurygame.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def log_and_print(msg: str, level: str = "info"):
    """Helper function to simultaneously print to console and write to log file."""
    print(msg, flush=True)  # flush=True forces immediate output in GitHub Actions
    if level == "warning":
        logging.warning(msg)
    elif level == "error":
        logging.error(msg)
    else:
        logging.info(msg)

def load_accounts_from_sheet(csv_url: str) -> list:
    """Fetches account list directly from the 'player_ids' tab."""
    log_and_print("::group::Fetching Accounts from Google Sheet (tab: player_ids)")
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
                
        log_and_print(f"[✓] Successfully loaded {len(accounts)} accounts from 'player_ids'.")
        print("::endgroup::")
        return accounts
    except Exception as e:
        log_and_print(f"[!] Error fetching accounts from Google Sheet: {e}", level="error")
        print("::endgroup::")
        return []

def load_codes_from_sheet(csv_url: str) -> list:
    """Fetches gift codes directly from the 'g_codes' tab."""
    log_and_print("::group::Fetching Gift Codes from Google Sheet (tab: g_codes)")
    try:
        res = requests.get(csv_url, timeout=10)
        res.raise_for_status()
        
        codes = []
        reader = csv.DictReader(io.StringIO(res.text))
        for row in reader:
            code = str(row.get("code") or row.get("cdk") or next(iter(row.values()), "")).strip()
            if code and code.lower() not in ["code", "cdk"]:
                codes.append(code)
                
        log_and_print(f"[✓] Successfully loaded {len(codes)} codes from 'g_codes'.")
        print("::endgroup::")
        return codes
    except Exception as e:
        log_and_print(f"[!] Error fetching gift codes from Google Sheet: {e}", level="error")
        print("::endgroup::")
        return []

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            log_and_print(f"[!] Warning: Failed to load history ({e}). Starting fresh.", level="warning")
    return {"claimed": {}, "expired_codes": []}

def save_history(history: dict):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=4)

def generate_sign(params: dict) -> str:
    sorted_str = "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))
    raw_str = f"{sorted_str}{SALT}"
    return hashlib.md5(raw_str.encode('utf-8')).hexdigest()

def batch_redeem(accounts: list, gift_codes: list):
    if not accounts or not gift_codes:
        log_and_print("[!] No accounts or gift codes provided. Aborting execution.", level="warning")
        return

    history = load_history()
    stats = {"success": 0, "already_claimed": 0, "skipped_memory": 0, "expired": 0, "failed": 0}
    
    log_and_print("=== Starting Batch Redemption Run ===")
    
    for cdk in gift_codes:
        if cdk in history["expired_codes"]:
            log_and_print(f"--- Code: '{cdk}' [SKIPPED: Marked as Expired in Memory] ---")
            stats["expired"] += len(accounts)
            continue
            
        log_and_print(f"--- Code: '{cdk}' ---")
        
        for acc in accounts:
            fid = acc["fid"]
            kid = acc["kid"]
            
            if fid in history["claimed"] and cdk in history["claimed"][fid]:
                log_and_print(f"  [⏩] SKIPPED         | Player ID: {fid} (Already in history)")
                stats["skipped_memory"] += 1
                continue
                
            for attempt in range(MAX_RETRIES + 1):
                ts = int(time.time())
                payload = {"cdk": cdk, "fid": fid, "kid": kid, "time": ts}
                payload["sign"] = generate_sign(payload)
                
                try:
                    res = requests.post(API_URL, headers=headers, data=payload, timeout=10)
                    data = res.json()
                    
                    err_code = data.get("err_code")
                    msg = str(data.get("msg", "")).upper()
                    
                    # 1. Fresh Success
                    if err_code == 0 or msg == "SUCCESS":
                        log_and_print(f"  [✓] SUCCESS         | Player ID: {fid}")
                        stats["success"] += 1
                        
                        if fid not in history["claimed"]: history["claimed"][fid] = []
                        if cdk not in history["claimed"][fid]: history["claimed"][fid].append(cdk)
                        save_history(history)
                        break
                            
                    # 2. Already Claimed or SAME TYPE EXCHANGE
                    elif any(k in msg for k in ["CLAIMED", "RECEIVED", "SAME TYPE", "EXCHANGE"]) or err_code in [40008, 40011]:
                        log_and_print(f"  [-] ALREADY CLAIMED | Player ID: {fid} (Same Event Type)")
                        stats["already_claimed"] += 1
                        
                        if fid not in history["claimed"]: history["claimed"][fid] = []
                        if cdk not in history["claimed"][fid]: history["claimed"][fid].append(cdk)
                        save_history(history)
                        break
                            
                    # 3. Expired Code
                    elif "EXPIRED" in msg or err_code == 40007:
                        log_and_print(f"  [!] EXPIRED CODE    | '{cdk}' is expired. Saving to memory...", level="warning")
                        stats["expired"] += 1
                        if cdk not in history["expired_codes"]:
                            history["expired_codes"].append(cdk)
                            save_history(history)
                        break
                        
                    # 4. Failures
                    else:
                        log_and_print(f"  [✗] FAILED         | Player ID: {fid} -> {data.get('msg', data)}", level="error")
                        stats["failed"] += 1
                        break

                except (requests.exceptions.Timeout, requests.exceptions.RequestException) as e:
                    if attempt < MAX_RETRIES:
                        log_and_print(f"  [⏳] TIMEOUT/ERROR   | Player ID: {fid} -> Retrying... ({attempt + 1}/{MAX_RETRIES})", level="warning")
                        time.sleep(2)
                    else:
                        log_and_print(f"  [✗] FAILED (TIMEOUT) | Player ID: {fid} -> Exhausted retries", level="error")
                        stats["failed"] += 1

            if cdk in history["expired_codes"]:
                break

            delay = round(random.uniform(3.0, 7.0), 2)
            time.sleep(delay)

    # Summary
    total_processed = sum(stats.values())
    total_successful = stats["success"] + stats["already_claimed"] + stats["skipped_memory"]
    
    summary_report = f"""
==========================================
         REDEMPTION SUMMARY REPORT        
==========================================
  [✓] Newly Redeemed         : {stats['success']}
  [-] Already Claimed (API)  : {stats['already_claimed']}
  [⏩] Skipped (Local Memory) : {stats['skipped_memory']}
  [!] Expired Codes           : {stats['expired']}
  [✗] Failed Requests         : {stats['failed']}
------------------------------------------
  TOTAL SUCCESSFUL / CLAIMED : {total_successful} / {total_processed}
==========================================
"""
    log_and_print(summary_report)

if __name__ == "__main__":
    # 1. Fetch gift codes from Google Sheet tab 'g_codes'
    sheet_codes = load_codes_from_sheet(CODES_CSV_URL)
    
    # 2. Fetch input codes from GitHub Actions UI (if provided)
    env_codes = os.getenv("INPUT_GIFT_CODES")
    input_codes = env_codes.replace(",", " ").split() if env_codes else []
    
    # 3. Combine and deduplicate codes
    all_raw_codes = sheet_codes + [c.strip() for c in input_codes if c.strip()]
    GIFT_CODES = list(dict.fromkeys(all_raw_codes))

    ACCOUNTS = load_accounts_from_sheet(PLAYERS_CSV_URL)
    batch_redeem(ACCOUNTS, GIFT_CODES)
