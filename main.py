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
from scraper import CrestwoodScraper, DoorDashScraper
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
DEFAULT_PLATFORM_CODE = "UE"
VALID_PLATFORM_CODES = {"UE", "DD"}

DAY_ORDER = {
    "Monday": 1,
    "Tuesday": 2,
    "Wednesday": 3,
    "Thursday": 4,
    "Friday": 5,
    "Saturday": 6,
    "Sunday": 7,
}

SHIFT_BLOCKS = [
    (5, 10, "Breakfast"),
    (11, 14, "Lunch"),
    (15, 16, "Afternoon"),
    (17, 21, "Dinner"),
]

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
    "doordash",
    "dashpass",
    "sponsored",
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

def classify_shift(hour):
    for start_hour, end_hour, label in SHIFT_BLOCKS:
        if start_hour <= hour <= end_hour:
            return label
    return "Late Night"

def classify_day_type(day_of_week):
    return "Weekend" if day_of_week in {"Saturday", "Sunday"} else "Weekday"

def infer_market_zone(store_info):
    text = " ".join([
        str(store_info.get("name") or ""),
        str(store_info.get("address") or ""),
        str(store_info.get("slug") or ""),
    ]).lower()

    zone_patterns = [
        ("Crestwood Core", ["crestwood", "40014", "westwind", "potts", "central avenue", "pleasant colony", "hwy 22", "highway 22", "claymont"]),
        ("La Grange / Buckner", ["la grange", "lagrange", "40031", "buckner"]),
        ("Prospect / Norton Commons", ["prospect", "norton commons", "meeting street", "river beauty"]),
        ("Springhurst / Paddock", ["springhurst", "paddock", "summit plaza", "towne center", "chamberlain"]),
        ("Westport / Brownsboro", ["westport", "brownsboro", "fischer park", "von allmen", "goose creek"]),
        ("Middletown", ["middletown", "shelbyville"]),
        ("Terra Crossing", ["terra crossing"]),
    ]
    for zone, patterns in zone_patterns:
        if any(pattern in text for pattern in patterns):
            return zone
    return "Regional Spillover"

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

def normalize_platform_code(value):
    platform_code = str(value or DEFAULT_PLATFORM_CODE).upper()
    if platform_code in {"UBER", "UBER_EATS"}:
        return "UE"
    if platform_code in {"DOORDASH", "DOOR_DASH"}:
        return "DD"
    return platform_code if platform_code in VALID_PLATFORM_CODES else DEFAULT_PLATFORM_CODE

def provider_store_id(platform_code, raw_store_id, name):
    platform_code = normalize_platform_code(platform_code)
    raw_store_id = str(raw_store_id or "").strip()
    if platform_code == "UE":
        return raw_store_id or f"{slugify(name)}_001"
    safe_raw_id = slugify(raw_store_id) if raw_store_id else slugify(name)
    return f"{platform_code.lower()}_{safe_raw_id}"

def first_present(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None

def iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from iter_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dicts(item)

def extract_name_from_nested(data, *keys):
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            nested_name = first_present(
                value.get("name"),
                value.get("title"),
                value.get("displayName"),
                value.get("storeName"),
            )
            if nested_name:
                return nested_name
        elif value:
            return value
    return None

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

def extract_structured_store(store_id, store, platform_code=DEFAULT_PLATFORM_CODE):
    if not isinstance(store, dict):
        return None
    platform_code = normalize_platform_code(platform_code)

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
        "store_id": provider_store_id(platform_code, store.get("uuid") or store_id, name),
        "platform_code": platform_code,
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
        "market_zone": infer_market_zone({
            "name": name,
            "address": location.get("formattedAddress"),
            "slug": store.get("slug"),
        }),
        "source": "getPaginatedStoresV1",
        "last_seen": datetime.now().isoformat(),
    }

def extract_doordash_structured_store(store_id, store):
    if not isinstance(store, dict):
        return None

    name = first_present(
        store.get("storeName"),
        store.get("name"),
        store.get("title"),
        store.get("displayName"),
        extract_name_from_nested(store, "business", "merchant", "store", "restaurant"),
    )
    if not name or not is_valid_store_name(name):
        return None

    raw_id = first_present(
        store.get("storeId"),
        store.get("id"),
        store.get("businessId"),
        store.get("merchantSuppliedId"),
        store.get("uuid"),
        store_id,
    )
    location = first_present(store.get("location"), store.get("address"), store.get("storeAddress"))
    location = location if isinstance(location, dict) else {}
    lat = first_present(store.get("latitude"), location.get("lat"), location.get("latitude"))
    lon = first_present(store.get("longitude"), location.get("lng"), location.get("longitude"))
    distance_miles = first_present(store.get("distanceMiles"), store.get("distance_miles"), store.get("distance"))
    if isinstance(distance_miles, str):
        match = re.search(r"(\d+(?:\.\d+)?)", distance_miles)
        distance_miles = float(match.group(1)) if match else None

    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and lat and lon:
        distance_miles = round(haversine_miles(MARKET_CENTER["latitude"], MARKET_CENTER["longitude"], lat, lon), 2)
        if distance_miles > MARKET_RADIUS_MILES:
            return None

    eta_text = first_present(
        store.get("deliveryTime"),
        store.get("deliveryTimeText"),
        store.get("etaText"),
        store.get("asapTime"),
        store.get("status"),
    )
    price_bucket = first_present(store.get("priceRange"), store.get("priceLevel"), store.get("price_bucket"))
    offers = first_present(store.get("offers"), store.get("promotions"), store.get("deals"), store.get("consumerPromotions"))
    promotion = offers[0] if isinstance(offers, list) and offers else offers
    categories = first_present(
        store.get("cuisines"),
        store.get("cuisineTags"),
        store.get("categories"),
        store.get("tags"),
    )
    if isinstance(categories, list):
        categories = [
            item.get("name") if isinstance(item, dict) else item
            for item in categories
        ]
    else:
        categories = []

    formatted_address = first_present(
        location.get("formattedAddress"),
        location.get("displayAddress"),
        store.get("address"),
    )
    if isinstance(formatted_address, dict):
        formatted_address = first_present(
            formatted_address.get("formattedAddress"),
            formatted_address.get("displayAddress"),
            formatted_address.get("street"),
        )

    return {
        "store_id": provider_store_id("DD", raw_id, name),
        "platform_code": "DD",
        "name": str(name).split("\n")[0].strip(),
        "slug": store.get("slug"),
        "price_level": infer_price_level(str(price_bucket or "")),
        "price_bucket": price_bucket or "",
        "eta_text": eta_text or "",
        "eta_minutes": parse_eta_minutes(eta_text),
        "availability_text": eta_text or store.get("status") or "",
        "delivery_fee": first_present(store.get("deliveryFee"), store.get("displayDeliveryFee")),
        "promotion_active": bool(promotion),
        "promotion_description": promotion_description(promotion) if isinstance(promotion, dict) else str(promotion or "") or None,
        "is_open": first_present(store.get("isOpen"), store.get("isAvailable"), store.get("open")),
        "categories": categories,
        "address": formatted_address,
        "latitude": lat if isinstance(lat, (int, float)) else None,
        "longitude": lon if isinstance(lon, (int, float)) else None,
        "distance_miles": distance_miles if isinstance(distance_miles, (int, float)) else None,
        "search_radius_miles": MARKET_RADIUS_MILES,
        "market_zone": infer_market_zone({
            "name": name,
            "address": formatted_address,
            "slug": store.get("slug"),
        }),
        "rating": first_present(store.get("averageRating"), store.get("rating")),
        "source": "doordash_network",
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
                    platform_code TEXT DEFAULT 'UE',
                    store_id TEXT,
                    store_name TEXT,
                    estimated_potential REAL
                )
            ''')
            existing_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(restaurant_history)")
            }
            columns_to_add = {
                "platform_code": "TEXT DEFAULT 'UE'",
                "run_date": "TEXT",
                "hour_of_day": "INTEGER",
                "day_of_week": "TEXT",
                "day_of_week_num": "INTEGER",
                "day_type": "TEXT",
                "shift_block": "TEXT",
                "market_zone": "TEXT",
                "market_status": "TEXT",
                "price_level": "INTEGER",
                "eta_minutes": "REAL",
                "is_open": "INTEGER",
                "promotion_active": "INTEGER",
                "distance_miles": "REAL",
                "search_radius_miles": "REAL",
            }
            for column_name, column_type in columns_to_add.items():
                if column_name not in existing_columns:
                    conn.execute(f"ALTER TABLE restaurant_history ADD COLUMN {column_name} {column_type}")
            conn.execute("""
                UPDATE restaurant_history
                SET platform_code = 'UE'
                WHERE platform_code IS NULL OR platform_code = ''
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_restaurant_history_platform_time
                ON restaurant_history (platform_code, timestamp)
            """)

    def log_trend(self, store_id, name, potential, metadata=None):
        metadata = metadata or {}
        timestamp = datetime.now()
        day_of_week = timestamp.strftime("%A")
        hour_of_day = timestamp.hour
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO restaurant_history (
                    timestamp,
                    platform_code,
                    store_id,
                    store_name,
                    estimated_potential,
                    run_date,
                    hour_of_day,
                    day_of_week,
                    day_of_week_num,
                    day_type,
                    shift_block,
                    market_zone,
                    market_status,
                    price_level,
                    eta_minutes,
                    is_open,
                    promotion_active,
                    distance_miles,
                    search_radius_miles
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                timestamp.isoformat(),
                normalize_platform_code(metadata.get("platform_code")),
                store_id,
                name,
                potential,
                timestamp.strftime("%Y-%m-%d"),
                hour_of_day,
                day_of_week,
                DAY_ORDER[day_of_week],
                classify_day_type(day_of_week),
                classify_shift(hour_of_day),
                metadata.get("market_zone"),
                metadata.get("market_status"),
                metadata.get("price_level"),
                metadata.get("eta_minutes"),
                int(bool(metadata.get("is_open"))) if metadata.get("is_open") is not None else None,
                int(bool(metadata.get("promotion_active"))) if metadata.get("promotion_active") is not None else None,
                metadata.get("distance_miles"),
                metadata.get("search_radius_miles"),
            ))

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
    if 'platform_code' not in df.columns:
        df['platform_code'] = DEFAULT_PLATFORM_CODE
    df['platform_code'] = df['platform_code'].map(normalize_platform_code)
    platform_col = df.pop('platform_code')
    df['timestamp'] = pd.to_datetime(datetime.now())
    # Denormalize time features for easier BI analysis
    df['hour_of_day'] = df['timestamp'].dt.hour
    df['day_of_week'] = df['timestamp'].dt.day_name()
    df['day_of_week_num'] = df['day_of_week'].map(DAY_ORDER)
    df['day_type'] = df['day_of_week'].map(classify_day_type)
    df['shift_block'] = df['hour_of_day'].map(classify_shift)
    df['platform_code'] = platform_col
    df.to_parquet(output_path, index=False)
    return output_path

def _prepare_history_dataframe(db_path, stores):
    with sqlite3.connect(db_path) as conn:
        history = pd.read_sql_query("SELECT * FROM restaurant_history", conn)
    if history.empty:
        return history

    history['timestamp'] = pd.to_datetime(history['timestamp'], errors='coerce')
    history = history.dropna(subset=['timestamp', 'estimated_potential'])
    history['estimated_potential'] = pd.to_numeric(history['estimated_potential'], errors='coerce')
    history = history.dropna(subset=['estimated_potential'])

    history['run_date'] = history.get('run_date', pd.Series(index=history.index, dtype='object')).fillna(history['timestamp'].dt.strftime('%Y-%m-%d'))
    history['hour_of_day'] = pd.to_numeric(history.get('hour_of_day'), errors='coerce').fillna(history['timestamp'].dt.hour).astype(int)
    history['day_of_week'] = history.get('day_of_week', pd.Series(index=history.index, dtype='object')).fillna(history['timestamp'].dt.day_name())
    history['day_of_week_num'] = pd.to_numeric(history.get('day_of_week_num'), errors='coerce').fillna(history['day_of_week'].map(DAY_ORDER)).astype(int)
    history['day_type'] = history.get('day_type', pd.Series(index=history.index, dtype='object')).fillna(history['day_of_week'].map(classify_day_type))
    history['shift_block'] = history.get('shift_block', pd.Series(index=history.index, dtype='object')).fillna(history['hour_of_day'].map(classify_shift))
    history['platform_code'] = history.get('platform_code', pd.Series(index=history.index, dtype='object')).fillna(DEFAULT_PLATFORM_CODE)
    history['platform_code'] = history['platform_code'].map(normalize_platform_code)
    history['sample_window'] = history['timestamp'].dt.floor('15min').astype(str)

    store_zone = {store_id: infer_market_zone(info) for store_id, info in stores.items()}
    store_price = {store_id: info.get("price_level") for store_id, info in stores.items()}
    history['market_zone'] = history.get('market_zone', pd.Series(index=history.index, dtype='object'))
    history['market_zone'] = history['market_zone'].fillna(history['store_id'].map(store_zone)).fillna("Unknown Zone")
    history['price_level'] = pd.to_numeric(history.get('price_level'), errors='coerce').fillna(history['store_id'].map(store_price))
    history['promotion_active'] = pd.to_numeric(history.get('promotion_active'), errors='coerce').fillna(0)
    history['is_open'] = pd.to_numeric(history.get('is_open'), errors='coerce')
    history['eta_minutes'] = pd.to_numeric(history.get('eta_minutes'), errors='coerce')
    history['distance_miles'] = pd.to_numeric(history.get('distance_miles'), errors='coerce')
    return history

def _rollup_history(history, group_cols):
    if history.empty:
        return pd.DataFrame()

    grouped = history.groupby(group_cols, dropna=False)
    summary = grouped.agg(
        observations=('estimated_potential', 'count'),
        sample_runs=('sample_window', 'nunique'),
        distinct_run_dates=('run_date', 'nunique'),
        restaurants_observed=('store_id', 'nunique'),
        avg_hourly_pay=('estimated_potential', 'mean'),
        median_hourly_pay=('estimated_potential', 'median'),
        std_hourly_pay=('estimated_potential', 'std'),
        min_hourly_pay=('estimated_potential', 'min'),
        max_hourly_pay=('estimated_potential', 'max'),
        promo_rate=('promotion_active', 'mean'),
        open_rate=('is_open', 'mean'),
        avg_eta_minutes=('eta_minutes', 'mean'),
        avg_distance_miles=('distance_miles', 'mean'),
    ).reset_index()

    p25 = grouped['estimated_potential'].quantile(0.25).reset_index(name='p25_hourly_pay')
    p75 = grouped['estimated_potential'].quantile(0.75).reset_index(name='p75_hourly_pay')
    summary = summary.merge(p25, on=group_cols, how='left').merge(p75, on=group_cols, how='left')

    summary['std_hourly_pay'] = summary['std_hourly_pay'].fillna(0)
    summary['p25_hourly_pay'] = summary['p25_hourly_pay'].fillna(summary['avg_hourly_pay'])
    summary['p75_hourly_pay'] = summary['p75_hourly_pay'].fillna(summary['avg_hourly_pay'])
    summary['sample_confidence'] = pd.concat([
        (summary['sample_runs'] / 12).clip(upper=1),
        (summary['distinct_run_dates'] / 3).clip(upper=1),
    ], axis=1).min(axis=1)
    summary['consistency_score'] = (1 - (summary['std_hourly_pay'] / summary['avg_hourly_pay'].clip(lower=1))).clip(lower=0, upper=1)
    summary['predictability_score'] = ((summary['consistency_score'] * 0.65) + (summary['sample_confidence'] * 0.35)) * 100
    summary['predictable_hourly_pay'] = summary['p25_hourly_pay'].where(
        summary['observations'] >= 3,
        (summary['avg_hourly_pay'] - summary['std_hourly_pay']).clip(lower=0)
    )
    summary['recommendation_score'] = summary['predictable_hourly_pay'] * (0.5 + (summary['sample_confidence'] * 0.5))
    summary['reliability_label'] = 'Emerging'
    summary.loc[summary['sample_runs'] >= 4, 'reliability_label'] = 'Learning'
    summary.loc[
        (summary['sample_runs'] >= 12)
        & (summary['distinct_run_dates'] >= 3)
        & (summary['consistency_score'] >= 0.65),
        'reliability_label'
    ] = 'Reliable'
    summary.loc[
        (summary['sample_runs'] >= 8)
        & (summary['distinct_run_dates'] >= 2)
        & (summary['consistency_score'] < 0.5),
        'reliability_label'
    ] = 'Volatile'
    return summary.sort_values(['recommendation_score', 'predictable_hourly_pay'], ascending=False)

def save_historical_insight_parquets(db_path, stores, output_dir='analytics_exports'):
    """Builds Power BI-ready trend tables from accumulated SQLite history."""
    history = _prepare_history_dataframe(db_path, stores)
    if history.empty:
        print("[INSIGHTS] No historical rows available yet. Skipping prediction mart.")
        return {}

    os.makedirs(output_dir, exist_ok=True)
    generated = {}

    restaurant_cols = [
        'store_id', 'store_name', 'market_zone', 'day_of_week', 'day_of_week_num',
        'day_type', 'hour_of_day', 'shift_block', 'platform_code'
    ]
    restaurant_hourly = _rollup_history(history, restaurant_cols)
    restaurant_hourly['rank_for_day_hour'] = restaurant_hourly.groupby(
        ['day_of_week_num', 'hour_of_day']
    )['recommendation_score'].rank(method='first', ascending=False).astype(int)
    restaurant_hourly_path = os.path.join(output_dir, 'restaurant_hourly_recommendations.parquet')
    restaurant_hourly.to_parquet(restaurant_hourly_path, index=False)
    generated['restaurant_hourly_recommendations'] = restaurant_hourly_path

    zone_cols = ['market_zone', 'day_of_week', 'day_of_week_num', 'day_type', 'hour_of_day', 'shift_block', 'platform_code']
    zone_hourly = _rollup_history(history, zone_cols)
    zone_hourly['rank_for_day_hour'] = zone_hourly.groupby(
        ['platform_code', 'day_of_week_num', 'hour_of_day']
    )['recommendation_score'].rank(method='first', ascending=False).astype(int)
    zone_hourly_path = os.path.join(output_dir, 'zone_hourly_recommendations.parquet')
    zone_hourly.to_parquet(zone_hourly_path, index=False)
    generated['zone_hourly_recommendations'] = zone_hourly_path

    shift_cols = ['market_zone', 'day_of_week', 'day_of_week_num', 'day_type', 'shift_block', 'platform_code']
    zone_shift = _rollup_history(history, shift_cols)
    zone_shift['rank_for_day_shift'] = zone_shift.groupby(
        ['platform_code', 'day_of_week_num', 'shift_block']
    )['recommendation_score'].rank(method='first', ascending=False).astype(int)
    zone_shift_path = os.path.join(output_dir, 'zone_shift_recommendations.parquet')
    zone_shift.to_parquet(zone_shift_path, index=False)
    generated['zone_shift_recommendations'] = zone_shift_path

    print(
        "[INSIGHTS] Built prediction mart: "
        f"{len(restaurant_hourly)} restaurant-hour rows, "
        f"{len(zone_hourly)} zone-hour rows, "
        f"{len(zone_shift)} zone-shift rows."
    )
    return generated

def upload_insight_parquets(files):
    if not files:
        return
    with open('config.json', 'r') as f:
        aws_config = json.load(f).get('aws', {})
    analytics_prefix = aws_config.get('analytics_prefix', 'analytics').strip('/')
    analytics_history_prefix = aws_config.get('analytics_history_prefix', 'analytics_history').strip('/')
    run_date = datetime.now().strftime('%Y-%m-%d')
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    table_folders = {
        "restaurant_hourly_recommendations": "restaurant_hourly",
        "zone_hourly_recommendations": "zone_hourly",
        "zone_shift_recommendations": "zone_shift",
    }
    for table_name, file_path in files.items():
        table_folder = table_folders.get(table_name, table_name)
        table_file_name = f"{table_folder}.parquet"

        # Clean Athena table location: one dataset, one schema, one stable folder.
        upload_to_s3(
            file_path,
            file_type="parquet",
            s3_key=f"{analytics_prefix}/{table_folder}/{table_file_name}",
        )

        # Dated history lives outside the Athena table folders to avoid mixed layouts.
        upload_to_s3(
            file_path,
            file_type="parquet",
            s3_key=(
                f"{analytics_history_prefix}/{table_folder}/run_date={run_date}/"
                f"{table_folder}_{timestamp}.parquet"
            ),
        )

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
            platform_code = normalize_platform_code(
                entry.get('platform_code') or ("DD" if str(url).startswith("DD_") else DEFAULT_PLATFORM_CODE)
            )
            
            if url.startswith("HTML_BACKUP") or url.startswith("DD_HTML_BACKUP"):
                for name in data.get('names', []):
                    clean_name = name.split('\n')[0].strip()
                    if is_valid_store_name(clean_name):
                        html_backup_names.append((platform_code, clean_name))
                continue

            api_data = data.get('data') if isinstance(data, dict) else None
            if not isinstance(api_data, dict):
                api_data = data if isinstance(data, dict) else None
            if not isinstance(api_data, dict):
                continue

            if platform_code == "UE":
                stores_map = api_data.get('storesMap')
                if isinstance(stores_map, dict):
                    for raw_store_id, store in stores_map.items():
                        store_data = extract_structured_store(raw_store_id, store, platform_code=platform_code)
                        if not store_data:
                            continue
                        found_stores[store_data['store_id']] = store_data
            elif platform_code == "DD":
                stores_map = api_data.get('storesMap')
                if isinstance(stores_map, dict):
                    for raw_store_id, store in stores_map.items():
                        store_data = extract_doordash_structured_store(raw_store_id, store)
                        if not store_data:
                            continue
                        found_stores[store_data['store_id']] = store_data
                for candidate in iter_dicts(api_data):
                    store_data = extract_doordash_structured_store(None, candidate)
                    if not store_data:
                        continue
                    found_stores[store_data['store_id']] = store_data

        # HTML backup fills gaps for server-rendered first-page stores that never appear in XHR.
        existing_found_names = {
            (normalize_platform_code(store.get('platform_code')), store['name'].lower())
            for store in found_stores.values()
        }
        for platform_code, name in sorted(set(html_backup_names)):
            if (normalize_platform_code(platform_code), name.lower()) not in existing_found_names:
                store_id = provider_store_id(platform_code, None, name)
                found_stores[store_id] = {
                    "store_id": store_id,
                    "platform_code": platform_code,
                    "name": name,
                    "price_level": 2,
                    "search_radius_miles": MARKET_RADIUS_MILES,
                    "market_zone": infer_market_zone({"name": name}),
                    "source": f"{platform_code.lower()}_html_backup",
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
        for info in db.values():
            info['platform_code'] = normalize_platform_code(info.get('platform_code'))
        new_entries_count = 0
        updated_entries_count = 0
        active_store_ids = set()
        
        for discovered_id, metadata in found_stores.items():
            clean_name = metadata.get('name', '').strip()
            platform_code = normalize_platform_code(metadata.get('platform_code'))
            metadata['platform_code'] = platform_code
            
            # Self-healing check: If the store name is missing or generic, parse the ID
            if (not clean_name or clean_name.lower() == "unknown store") and "__" in discovered_id:
                clean_name = auto_parse_missing_store_name(discovered_id)
                metadata['name'] = clean_name

            if not is_valid_store_name(clean_name):
                continue

            # Check if this precise ID or matching name exists
            store_id = next(
                (
                    sid for sid, info in db.items()
                    if sid == discovered_id
                    or (
                        normalize_platform_code(info.get('platform_code')) == platform_code
                        and info.get('name', '').lower() == clean_name.lower()
                    )
                ),
                None
            )
            
            if not store_id:
                store_id = provider_store_id(platform_code, discovered_id, clean_name)
                # Initialize structured placeholder for the new edge/spillover store
                db[store_id] = {
                    "platform_code": platform_code,
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
            db[store_id]['market_zone'] = db[store_id].get('market_zone') or infer_market_zone(db[store_id])
            db[store_id]['platform_code'] = normalize_platform_code(db[store_id].get('platform_code'))
            updated_entries_count += 1
        
        if new_entries_count > 0 or updated_entries_count > 0:
            with open(db_path, 'w') as f:
                json.dump(dict(sorted(db.items(), key=lambda item: item[1].get('name', ''))), f, indent=4)
            print(f"[DATABASE] Stats: {new_entries_count} new spillover tracks, {updated_entries_count} metadata refinements applied.")
        return active_store_ids
            
    except Exception as e:
        print(f"[!] Database update error: {e}")
        return set()

async def run_analysis(live_mode=False, platform='ue'):
    # 1. Initialize components
    db = MarketDatabase()
    platform = (platform or 'ue').lower()
    
    print("--- 2026 Crestwood Market Report ---")
    
    if live_mode:
        print(f"Collecting live market data for platform={platform}...")
        scrapers = []
        if platform in {'ue', 'all'}:
            scrapers.append(CrestwoodScraper())
        if platform in {'dd', 'doordash', 'all'}:
            scrapers.append(DoorDashScraper())
        if not scrapers:
            print(f"[!] Unknown platform '{platform}'. Use ue, dd, or all.")
            return

        captured_data = []
        for scraper in scrapers:
            captured_data.extend(await scraper.fetch_live_data())
        try:
            with open('market_snapshots.json', 'w') as f:
                json.dump(captured_data, f, indent=4)
            print(f"Captured {len(captured_data)} network responses.")
            with open('config.json', 'r') as f:
                raw_prefix = json.load(f).get('aws', {}).get('raw_prefix', 'raw').strip('/')
            raw_key = (
                f"{raw_prefix}/platform={platform}/run_date={datetime.now().strftime('%Y-%m-%d')}/"
                f"market_snapshots_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            )
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
        platform_code = normalize_platform_code(info.get("platform_code"))
        potential = analyzer.calculate_potential(store_id, surge_multiplier=1.2)
        status = analyzer.classify_potential(potential)
        market_zone = info.get("market_zone") or infer_market_zone(info)

        db.log_trend(store_id, info['name'], potential, metadata={
            "platform_code": platform_code,
            "market_zone": market_zone,
            "market_status": status,
            "price_level": info.get("price_level"),
            "eta_minutes": info.get("eta_minutes"),
            "is_open": info.get("is_open"),
            "promotion_active": info.get("promotion_active"),
            "distance_miles": info.get("distance_miles"),
            "search_radius_miles": info.get("search_radius_miles", MARKET_RADIUS_MILES),
        })
        results_for_parquet.append({
            "platform_code": platform_code,
            "store_id": store_id, 
            "name": info['name'], 
            "restaurant_name": info['name'],
            "market_zone": market_zone,
            "potential": potential,
            "estimated_hourly_potential": potential,
            "status": status,
            "market_status": status,
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
    for info in stores.values():
        info['platform_code'] = normalize_platform_code(info.get('platform_code'))
    dim_df = pd.DataFrame.from_dict(stores, orient='index').reset_index()
    dim_df.rename(columns={'index': 'store_id'}, inplace=True)
    dim_parquet = 'restaurants_dim.parquet'
    try:
        dim_df.to_parquet(dim_parquet, index=False)
        upload_to_s3(dim_parquet, file_type="parquet", is_dimension=True)
    finally:
        if os.path.exists(dim_parquet):
            os.remove(dim_parquet)

    insight_files = save_historical_insight_parquets(db.db_path, stores)
    upload_insight_parquets(insight_files)

    # 6. Quick Check: Print Top 10 Leaderboard
    print("\n" + "="*50)
    print(f"   CRESTWOOD LIVE LEADERBOARD ({datetime.now().strftime('%I:%M %p')})")
    print("="*50)
    top_spots = sorted(results_for_parquet, key=lambda x: x['potential'], reverse=True)[:10]
    for i, spot in enumerate(top_spots, 1):
        print(f"{i}. ${spot['potential']}/hr | {spot['platform_code']} | {spot['status']:<15} | {spot['name']}")
    print("="*50 + "\n")

    print(f"[HISTORICAL] Data logged to SQLite for {len(stores)} restaurants.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Execute live scraper')
    parser.add_argument('--platform', choices=['ue', 'dd', 'all'], default='ue', help='Live scrape platform: ue, dd, or all')
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
                kwargs={'live_mode': args.live, 'platform': args.platform},
                next_run_time=datetime.now()
            )
            scheduler.start()
            print(
                f"[SCHEDULER] Engine active. Interval: {args.interval}m. "
                f"Mode: {'Live' if args.live else 'Static'} Platform: {args.platform}"
            )
            print("Press Ctrl+C to terminate the process.")
            await asyncio.Event().wait()

        try:
            asyncio.run(scheduler_loop())
        except (KeyboardInterrupt, SystemExit):
            print("\n[SCHEDULER] Service stopped.")
    else:
        asyncio.run(run_analysis(live_mode=args.live, platform=args.platform))
