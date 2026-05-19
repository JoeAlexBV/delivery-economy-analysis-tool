import asyncio
import json
import argparse
import sqlite3
import pandas as pd
from datetime import datetime
import os
import math
import re
# Updated imports to match the provided file structure
from scraper import CrestwoodScraper
from analyzer import MarketAnalyzer


def load_scraper_settings(config_path='config.json'):
    try:
        with open(config_path, 'r') as f:
            scraper_config = json.load(f).get('scraper', {})
    except (FileNotFoundError, json.JSONDecodeError):
        scraper_config = {}

    return {
        "coords": scraper_config.get("coords", {"latitude": 38.3306, "longitude": -85.4852}),
        "search_radius_miles": scraper_config.get("search_radius_miles", 10),
    }


SCRAPER_SETTINGS = load_scraper_settings()
MARKET_CENTER = SCRAPER_SETTINGS["coords"]
MARKET_RADIUS_MILES = SCRAPER_SETTINGS["search_radius_miles"]

NON_STORE_NAME_PATTERNS = [
    r"^\d+\s*min$",
    r"^\d{1,2}:\d{2}\s*(am|pm)$",
    r"^available\b",
    r"^currently unavailable$",
    r"^delivery not available$",
    r"^\d+%\s*off\b",
    r"^selection includes\b",
]

NON_STORE_TEXT_FRAGMENTS = [
    "uber eats",
    "minutes",
    "fees",
    "items on sale",
    "spend $",
    "delivery",
    "pickup",
    "ratings",
    "how can i",
    "where can i",
    "top rated",
    "buy 1",
    "free item",
]

def slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "unknown_store"

def is_valid_store_name(name):
    clean_name = (name or "").split("\n")[0].strip()
    if len(clean_name) < 3 or len(clean_name) > 80:
        return False

    lowered = clean_name.lower()
    if any(fragment in lowered for fragment in NON_STORE_TEXT_FRAGMENTS):
        return False
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in NON_STORE_NAME_PATTERNS):
        return False
    return True

def parse_eta_minutes(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).lower()
    if "not available" in text or "unavailable" in text:
        return None
    if "available at" in text or any(day in text for day in [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ]):
        return None
    if "min" not in text:
        return None
    match = re.search(r"(\d+)", text)
    return float(match.group(1)) if match else None

def infer_price_level(price_bucket):
    if isinstance(price_bucket, str) and "$" in price_bucket:
        return min(max(price_bucket.count("$"), 1), 4)
    return 2

def haversine_miles(lat1, lon1, lat2, lon2):
    radius_miles = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius_miles * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def promotion_description(promotion):
    if not isinstance(promotion, dict):
        return None
    return promotion.get("description") or promotion.get("title") or promotion.get("displayText")

def auto_parse_missing_store_name(store_id: str) -> str:
    """
    Parses a raw, unseeded spillover store_id from neighboring corridors
    into a clean, structured name with geographic context for Power BI.
    """
    parts = store_id.split('__')
    base_name = parts[0]
    clean_name = base_name.replace('_', ' ').title()
    
    # Standardize common acronyms/short names if hit
    clean_name = clean_name.replace('Oishii', 'Oishii Restaurant')
    
    location_desc = "Regional"
    if len(parts) > 1:
        match = re.search(r'(?:\d+_)?([a-z0-9_]+?)(?:_rd|_st|_dr|_ct|_blvd|_001|$)', parts[1])
        if match:
            slug = match.group(1)
            if any(k in store_id for k in ["summit_plaza", "von_allmen", "brownsboro"]):
                location_desc = "Paddock Shops"
            elif "westport" in store_id:
                location_desc = "Westport Rd"
            elif "middletown" in store_id:
                location_desc = "Middletown"
            else:
                location_desc = slug.replace('_', ' ').title()
                
    return f"{clean_name} ({location_desc})"

def extract_structured_store(store_id, store):
    if not isinstance(store, dict):
        return None

    name = store.get("title") or store.get("name") or store.get("storeName")
    
    # Fallback self-healing: if the API name is missing/blank, deduce it from the store_id
    if not name and store_id and "__" in store_id:
        name = auto_parse_missing_store_name(store_id)

    if not is_valid_store_name(name):
        return None

    meta = store.get("meta") if isinstance(store.get("meta"), dict) else {}
    eta_range = store.get("etaRange") if isinstance(store.get("etaRange"), dict) else {}
    location = store.get("location") if isinstance(store.get("location"), dict) else {}
    promotion = store.get("promotion")

    lat = location.get("latitude")
    lon = location.get("longitude")
    distance_miles = None
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and lat and lon:
        distance_miles = round(haversine_miles(MARKET_CENTER["latitude"], MARKET_CENTER["longitude"], lat, lon), 2)
        if distance_miles > MARKET_RADIUS_MILES:
            return None

    price_bucket = meta.get("priceBucket") or store.get("priceBucket") or store.get("priceRange")
    eta_text = eta_range.get("text") or store.get("etaText") or store.get("closedMessage")
    delivery_fee = meta.get("deliveryFee")
    categories = meta.get("categories") if isinstance(meta.get("categories"), list) else []

    return {
        "store_id": store.get("uuid") or store_id or f"{slugify(name)}_001",
        "name": name.split("\n")[0].strip(),
        "slug": store.get("slug"),
        "price_level": infer_price_level(price_bucket),
        "price_bucket": price_bucket or "",
        "eta_text": eta_text or "",
        "eta_minutes": parse_eta_minutes(eta_text),
        "availability_text": eta_text or store.get("closedMessage") or "",
        "delivery_fee": delivery_fee,
        "promotion_active": bool(promotion),
        "promotion_description": promotion_description(promotion),
        "is_open": store.get("isOpen"),
        "categories": categories,
        "address": location.get("formattedAddress"),
        "latitude": lat if lat else None,
        "longitude": lon if lon else None,
        "distance_miles": distance_miles,
        "search_radius_miles": MARKET_RADIUS_MILES,
        "source": "getPaginatedStoresV1",
        "last_seen": datetime.now().isoformat(),
    }

class MarketDatabase:
    def __init__(self, db_path='market_history.db'):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS restaurant_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    store_id TEXT,
                    store_name TEXT,
                    estimated_potential REAL
                )
            ''')

    def log_trend(self, store_id, name, potential):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO restaurant_history (timestamp, store_id, store_name, estimated_potential)
                VALUES (?, ?, ?, ?)
            ''', (datetime.now().isoformat(), store_id, name, potential))

def upload_to_s3(file_path, file_type="parquet", is_dimension=False, s3_key=None):
    """Uploads the captured snapshot to AWS S3 if enabled in config."""
    try:
        import boto3
        from botocore.exceptions import NoCredentialsError, PartialCredentialsError
    except ImportError:
        print("[!] S3 Upload Failed: boto3 is not installed. Run 'pip install -r requirements.txt'.")
        return

    try:
        with open('config.json', 'r') as f:
            config = json.load(f).get('aws', {})
        
        if config.get('enabled', False):
            s3 = boto3.client('s3', region_name=config.get('region', 'us-east-2'))
            now = datetime.now()
            datestamp = now.strftime('%Y-%m-%d')
            timestamp = now.strftime('%Y%m%d_%H%M%S')
            processed_prefix = config.get('processed_prefix', 'processed').strip('/')
            results_prefix = config.get('results_prefix', 'results').strip('/')
            dimensions_prefix = config.get('dimensions_prefix', 'dimensions').strip('/')
            explicit_key = s3_key is not None
            if s3_key:
                s3_key = s3_key
            elif is_dimension:
                s3_key = f"{dimensions_prefix}/{os.path.basename(file_path)}"
            else:
                s3_key = f"{processed_prefix}/run_date={datestamp}/crestwood_{timestamp}.{file_type}"
            s3.upload_file(file_path, config['bucket_name'], s3_key)
            print(f"[AWS] Uploaded {file_path} to s3://{config['bucket_name']}/{s3_key}")

            if not is_dimension and not explicit_key and results_prefix:
                results_key = f"{results_prefix}/run_date={datestamp}/crestwood_{timestamp}.{file_type}"
                latest_key = f"{results_prefix}/latest_analysis.{file_type}"
                if results_key != s3_key:
                    s3.upload_file(file_path, config['bucket_name'], results_key)
                    print(f"[AWS] Uploaded results copy to s3://{config['bucket_name']}/{results_key}")
                s3.upload_file(file_path, config['bucket_name'], latest_key)
                print(f"[AWS] Updated latest results at s3://{config['bucket_name']}/{latest_key}")
            
            # Only repair partitions for the Fact table (analysis), not Dimensions
            if not is_dimension and not explicit_key:
                repair_athena_partitions(config)

    except (NoCredentialsError, PartialCredentialsError):
        print("[!] S3 Upload Failed: No AWS credentials found. Run 'aws configure' in your terminal.")
    except Exception as e:
        print(f"[!] S3 Upload Failed: {e}")

def repair_athena_partitions(config, database='default'):
    """Triggers Athena to discover new partitions in S3."""
    try:
        import boto3
        bucket_name = config['bucket_name']
        region = config.get('region', 'us-east-2')
        athena_results_prefix = config.get('athena_results_prefix', 'athena-query-results').strip('/')
        athena = boto3.client('athena', region_name=region)
        query = "MSCK REPAIR TABLE crestwood_gig_analysis;"
        response = athena.start_query_execution(
            QueryString=query,
            QueryExecutionContext={'Database': database},
            ResultConfiguration={'OutputLocation': f"s3://{bucket_name}/{athena_results_prefix}/"}
        )
        query_id = response['QueryExecutionId']
        print(f"[AWS] Triggered Athena partition repair (ID: {query_id})")
    except Exception as e:
        print(f"[!] Athena repair failed: {e}")

def save_analysis_to_parquet(analysis_results, output_path='latest_analysis.parquet'):
    """Converts analysis list to a flattened Parquet file for Athena."""
    df = pd.DataFrame(analysis_results)
    df['timestamp'] = pd.to_datetime(datetime.now())
    # Denormalize time features for easier BI analysis
    df['hour_of_day'] = df['timestamp'].dt.hour
    df['day_of_week'] = df['timestamp'].dt.day_name()
    df.to_parquet(output_path, index=False)
    return output_path

def discover_real_stats(snapshots_path='market_snapshots.json'):
    """Deep-scans snapshots to extract actual restaurant names and surge data."""
    try:
        with open(snapshots_path, 'r') as f:
            snapshots = json.load(f)
        
        print("\n--- Live Market Discovery (Actual Data) ---")
        found_stores = {}
        html_backup_names = []

        for entry in snapshots:
            url = entry.get('url', '')
            data = entry.get('data', {})
            
            if url.startswith("HTML_BACKUP"):
                for name in data.get('names', []):
                    clean_name = name.split('\n')[0].strip()
                    if is_valid_store_name(clean_name):
                        html_backup_names.append(clean_name)
                continue

            api_data = data.get('data') if isinstance(data, dict) else None
            if not isinstance(api_data, dict):
                continue

            stores_map = api_data.get('storesMap')
            if isinstance(stores_map, dict):
                for raw_store_id, store in stores_map.items():
                    store_data = extract_structured_store(raw_store_id, store)
                    if not store_data:
                        continue
                    found_stores[store_data['store_id']] = store_data

        # HTML backup fills gaps for server-rendered first-page stores that never appear in XHR.
        existing_found_names = {store['name'].lower() for store in found_stores.values()}
        for name in sorted(set(html_backup_names)):
            if name.lower() not in existing_found_names:
                found_stores[f"{slugify(name)}_001"] = {
                    "store_id": f"{slugify(name)}_001",
                    "name": name,
                    "price_level": 2,
                    "search_radius_miles": MARKET_RADIUS_MILES,
                    "source": "html_backup",
                    "last_seen": datetime.now().isoformat(),
                }
        
        if found_stores:
            print(f"[SUCCESS] Total unique restaurants identified: {len(found_stores)}")
            print("Sample of discovered stores:")
            for meta in sorted(found_stores.values(), key=lambda item: item['name'])[:5]:
                print(
                    f" > {meta['name']} "
                    f"(Price: {meta.get('price_bucket') or meta.get('price_level')}, "
                    f"ETA: {meta.get('eta_text') or 'N/A'}, Promo: {meta.get('promotion_active')})"
                )
        else:
            print("[!] No restaurant names found yet. The snapshots may contain metadata only.")
            print("[TIP] Try scrolling further down in the browser window during the scrape.")
        
        return found_stores
    except Exception as e:
        print(f"[!] Error parsing real data: {e}")
        return {}

def update_restaurant_database(found_stores, db_path='restaurants.json'):
    """Integrates newly discovered names and auto-heals missing dim data for spillover stores."""
    if not found_stores:
        return set()

    try:
        with open(db_path, 'r') as f:
            content = f.read().strip()
            db = json.loads(content) if content else {}
    except (json.JSONDecodeError, FileNotFoundError):
        db = {}

    try:
        # Clean older bad records
        db = {
            store_id: info
            for store_id, info in db.items()
            if isinstance(info, dict) and is_valid_store_name(info.get('name'))
        }
        new_entries_count = 0
        updated_entries_count = 0
        active_store_ids = set()
        
        for discovered_id, metadata in found_stores.items():
            clean_name = metadata.get('name', '').strip()
            
            # Self-healing check: If the store name is missing or generic, parse the ID
            if (not clean_name or clean_name.lower() == "unknown store") and "__" in discovered_id:
                clean_name = auto_parse_missing_store_name(discovered_id)
                metadata['name'] = clean_name

            if not is_valid_store_name(clean_name):
                continue

            # Check if this precise ID or matching name exists
            store_id = next(
                (sid for sid, info in db.items() if sid == discovered_id or info.get('name', '').lower() == clean_name.lower()),
                None
            )
            
            if not store_id:
                store_id = discovered_id or f"{slugify(clean_name)}_001"
                # Initialize structured placeholder for the new edge/spillover store
                db[store_id] = {
                    "name": clean_name,
                    "price_level": metadata.get("price_level", 2),
                    "rating": metadata.get("rating", 4.2)
                }
                new_entries_count += 1
                
            active_store_ids.add(store_id)

            # Keep metadata fields in sync
            for key, value in metadata.items():
                if key == 'store_id':
                    continue
                if value is not None:
                    db[store_id][key] = value
            updated_entries_count += 1
        
        if new_entries_count > 0 or updated_entries_count > 0:
            with open(db_path, 'w') as f:
                json.dump(dict(sorted(db.items(), key=lambda item: item[1].get('name', ''))), f, indent=4)
            print(f"[DATABASE] Stats: {new_entries_count} new spillover tracks, {updated_entries_count} metadata refinements applied.")
        return active_store_ids
            
    except Exception as e:
        print(f"[!] Database update error: {e}")
        return set()

async def run_analysis(live_mode=False):
    # 1. Initialize components
    db = MarketDatabase()
    scraper = CrestwoodScraper()
    
    print("--- 2026 Crestwood Market Report ---")
    
    if live_mode:
        print("Collecting live market data...")
        captured_data = await scraper.fetch_live_data()
        try:
            with open('market_snapshots.json', 'w') as f:
                json.dump(captured_data, f, indent=4)
            print(f"Captured {len(captured_data)} network responses.")
            with open('config.json', 'r') as f:
                raw_prefix = json.load(f).get('aws', {}).get('raw_prefix', 'raw').strip('/')
            raw_key = f"{raw_prefix}/run_date={datetime.now().strftime('%Y-%m-%d')}/market_snapshots_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            upload_to_s3('market_snapshots.json', file_type="json", s3_key=raw_key)
        except IOError as e:
            print(f"Error saving snapshots: {e}")

    else:
        print("[INFO] Running analysis on existing snapshot data.")

    active_store_ids = None
    if os.path.exists('market_snapshots.json'):
        found_stores = discover_real_stats()
        active_store_ids = update_restaurant_database(found_stores)

    # 3. Analyze specific Crestwood segments
    try:
        with open('restaurants.json', 'r') as f:
            stores = {
                store_id: info
                for store_id, info in json.load(f).items()
                if isinstance(info, dict)
                and is_valid_store_name(info.get('name'))
                and (active_store_ids is None or store_id in active_store_ids)
            }
        if not stores:
            print("[INFO] restaurants.json is empty. Skipping analysis until discovery finds stores.")
            return
    except FileNotFoundError:
        print("[INFO] No restaurants.json found. Skipping analysis until discovery completes.")
        return

    analyzer = MarketAnalyzer('restaurants.json', config_path='config.json')
    results_for_parquet = []
    for store_id, info in stores.items():
        potential = analyzer.calculate_potential(store_id, surge_multiplier=1.2)
        status = analyzer.classify_potential(potential)

        db.log_trend(store_id, info['name'], potential)
        results_for_parquet.append({
            "store_id": store_id, 
            "name": info['name'], 
            "potential": potential,
            "status": status,
            "price_level": info.get("price_level"),
            "price_bucket": info.get("price_bucket"),
            "eta_text": info.get("eta_text"),
            "eta_minutes": info.get("eta_minutes"),
            "is_open": info.get("is_open"),
            "promotion_active": info.get("promotion_active"),
            "distance_miles": info.get("distance_miles"),
            "search_radius_miles": info.get("search_radius_miles", MARKET_RADIUS_MILES),
            "address": info.get("address"),
            "categories": ", ".join(info.get("categories", [])) if isinstance(info.get("categories"), list) else info.get("categories"),
        })
        
        if store_id == 'crestwood_bistro_001': 
            print(f"\nTarget: {info['name']} (Standard Delivery)")
            print(f"Est. Potential: ${potential}/hr")
            print(f"Status: [{status}]")

    # 4. Parquet Conversion & S3 Upload
    if not results_for_parquet:
        print("[INFO] No analysis rows produced. Skipping parquet and S3 upload.")
        return

    parquet_file = save_analysis_to_parquet(results_for_parquet)
    upload_to_s3(parquet_file, file_type="parquet")

    # 5. Sync Dimension Table to S3
    dim_df = pd.DataFrame.from_dict(stores, orient='index').reset_index()
    dim_df.rename(columns={'index': 'store_id'}, inplace=True)
    dim_parquet = 'restaurants_dim.parquet'
    try:
        dim_df.to_parquet(dim_parquet, index=False)
        upload_to_s3(dim_parquet, file_type="parquet", is_dimension=True)
    finally:
        if os.path.exists(dim_parquet):
            os.remove(dim_parquet)

    # 6. Quick Check: Print Top 10 Leaderboard
    print("\n" + "="*50)
    print(f"   CRESTWOOD LIVE LEADERBOARD ({datetime.now().strftime('%I:%M %p')})")
    print("="*50)
    top_spots = sorted(results_for_parquet, key=lambda x: x['potential'], reverse=True)[:10]
    for i, spot in enumerate(top_spots, 1):
        print(f"{i}. ${spot['potential']}/hr | {spot['status']:<15} | {spot['name']}")
    print("="*50 + "\n")

    print(f"[HISTORICAL] Data logged to SQLite for {len(stores)} restaurants.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Execute live scraper')
    parser.add_argument('--schedule', action='store_true', help='Enable the internal Python scheduler')
    parser.add_argument('--interval', type=int, default=15, help='Interval in minutes for scheduling')
    args = parser.parse_args()

    if args.schedule:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        
        async def scheduler_loop():
            scheduler = AsyncIOScheduler()
            scheduler.add_job(
                run_analysis, 
                'interval', 
                minutes=args.interval, 
                kwargs={'live_mode': args.live},
                next_run_time=datetime.now()
            )
            scheduler.start()
            print(f"[SCHEDULER] Engine active. Interval: {args.interval}m. Mode: {'Live' if args.live else 'Static'}")
            print("Press Ctrl+C to terminate the process.")
            await asyncio.Event().wait()

        try:
            asyncio.run(scheduler_loop())
        except (KeyboardInterrupt, SystemExit):
            print("\n[SCHEDULER] Service stopped.")
    else:
        asyncio.run(run_analysis(live_mode=args.live))
