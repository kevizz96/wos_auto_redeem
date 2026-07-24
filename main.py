import csv
import hashlib
import io
import json
import logging
import os
import random
import time
import requests

# Dual Logging setup (outputs to console AND writes to redemption.log)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("redemption.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

API_URL = "https://wos-giftcode-api.centurygame.com/api/gift_code"
SALT = "tB87#kPtkxqOS2"
HISTORY_FILE = "redemption_history.json"
MAX_RETRIES = 2

# Google Sheet CSV Endpoint
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1ykHBJWvJNEpOYg-D4W8CDTjPs-gYvQ89cp9FPj8FNfQ/gviz/tq?tqx=out:csv"

headers = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://wos-giftcode.centurygame.com",
    "Referer": "https://wos-giftcode.centurygame.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def load_accounts_from_google_sheet(csv_url: str) -> list:
    """Fetches account list directly from Google Sheet CSV endpoint."""
    logging.info("=== Fetching Accounts from Google Sheet ===")
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
                
        logging.info(f"Successfully loaded {len(accounts)} accounts from Google Sheet.\n")
        return accounts
    except Exception as e:
        logging.error(f"Failed to fetch accounts from Google Sheet: {e}")
        return []

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.warning(f"Failed to load history ({e}). Starting fresh.")
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
        logging.warning("No accounts or gift codes provided. Aborting execution.")
        return

    history = load_history()
    stats = {"success": 0, "already_claimed": 0, "skipped_memory": 0, "expired": 0, "failed": 0}
    
    logging.info("=== Starting Batch Redemption Run ===")
    
    for cdk in gift_codes:
        if cdk in history["expired_codes"]:
            logging.info(f"--- Code: '{cdk}' [SKIPPED: Marked as Expired in Memory] ---")
            stats["expired"] += len(accounts)
            continue
            
        logging.info(f"--- Code: '{cdk}' ---")
        
        for acc in accounts:
            fid = acc["fid"]
            kid = acc["kid"]
            
            if fid in history["claimed"] and cdk in history["claimed"][fid]:
                logging.info(f"  [⏩] SKIPPED         | Player ID: {fid} (Already in history)")
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
                        logging.info(f"  [✓] SUCCESS         | Player ID: {fid}")
                        stats["success"] += 1
                        
                        if fid not in history["claimed"]: history["claimed"][fid] = []
                        if cdk not in history["claimed"][fid]: history["claimed"][fid].append(cdk)
                        save_history(history)
                        break
                            
                    # 2. Already Claimed or SAME TYPE EXCHANGE
                    elif any(k in msg for k in ["CLAIMED", "RECEIVED", "SAME TYPE", "EXCHANGE"]) or err_code in [40008, 40011]:
                        logging.info(f"  [-] ALREADY CLAIMED | Player ID: {fid} (Same Event Type)")
                        stats["already_claimed"] += 1
                        
                        if fid not in history["claimed"]: history["claimed"][fid] = []
                        if cdk not in history["claimed"][fid]: history["claimed"][fid].append(cdk)
                        save_history(history)
                        break
                            
                    # 3. Expired Code
                    elif "EXPIRED" in msg or err_code == 40007:
                        logging.info(f"  [!] EXPIRED CODE    | '{cdk}' is expired. Saving to memory...")
                        stats["expired"] += 1
                        if cdk not in history["expired_codes"]:
                            history["expired_codes"].append(cdk)
                            save_history(history)
                        break
                        
                    # 4. Actual Failures
                    else:
                        logging.error(f"  [✗] FAILED         | Player ID: {fid} -> {data.get('msg', data)}")
                        stats["failed"] += 1
                        break

                except (requests.exceptions.Timeout, requests.exceptions.RequestException) as e:
                    if attempt < MAX_RETRIES:
                        logging.warning(f"  [⏳] TIMEOUT/ERROR   | Player ID: {fid} -> Retrying... ({attempt + 1}/{MAX_RETRIES})")
                        time.sleep(2)
                    else:
                        logging.error(f"  [✗] FAILED (TIMEOUT) | Player ID: {fid} -> Exhausted retries")
                        stats["failed"] += 1

            if cdk in history["expired_codes"]:
                break

            delay = round(random.uniform(3.0, 7.0), 2)
            time.sleep(delay)

    # Summary
    total_processed = sum(stats.values())
    total_successful = stats["success"] + stats["already_claimed"] + stats["skipped_memory"]
    
    logging.info("==========================================")
    logging.info("         REDEMPTION SUMMARY REPORT        ")
    logging.info("==========================================")
    logging.info(f"  [✓] Newly Redeemed         : {stats['success']}")
    logging.info(f"  [-] Already Claimed (API)  : {stats['already_claimed']}")
    logging.info(f"  [⏩] Skipped (Local Memory) : {stats['skipped_memory']}")
    logging.info(f"  [!] Expired Codes           : {stats['expired']}")
    logging.info(f"  [✗] Failed Requests         : {stats['failed']}")
    logging.info("------------------------------------------")
    logging.info(f"  TOTAL SUCCESSFUL / CLAIMED : {total_successful} / {total_processed}")
    logging.info("==========================================\n")

if __name__ == "__main__":
    env_codes = os.getenv("INPUT_GIFT_CODES")
    if env_codes:
        raw_list = env_codes.replace(",", " ").split()
        GIFT_CODES = list(dict.fromkeys([c.strip() for c in raw_list if c.strip()]))
    else:
        GIFT_CODES = ["JULHD2026JP", "0706FORU", "1stYoutubeKR", "2ndYoutubeKR", "gogoWOS"]

    ACCOUNTS = load_accounts_from_google_sheet(SHEET_CSV_URL)
    batch_redeem(ACCOUNTS, GIFT_CODES)
