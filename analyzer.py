import json
import re

class MarketAnalyzer:
    def __init__(self, restaurant_db, config_path='config.json'):
        with open(restaurant_db, 'r') as f:
            self.restaurants = json.load(f)
        
        with open(config_path, 'r') as f:
            self.config = json.load(f)['market_logic']

    def _coerce_price_level(self, store):
        price_level = store.get('price_level')
        if isinstance(price_level, int) and price_level > 0:
            return min(price_level, 4)

        price_bucket = store.get('price_bucket') or store.get('priceRange') or ""
        if isinstance(price_bucket, str) and "$" in price_bucket:
            return min(max(price_bucket.count("$"), 1), 4)

        return 2

    def _coerce_minutes(self, value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).lower()
        if "available at" in text or any(day in text for day in [
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
        ]):
            return None
        if "min" not in text:
            return None
        match = re.search(r"(\d+)", text)
        return float(match.group(1)) if match else None

    def _category_demand_factor(self, store):
        categories = [str(c).lower() for c in store.get('categories', [])]
        name = store.get('name', '').lower()
        joined = " ".join(categories + [name])

        high_velocity_terms = [
            'fast food', 'burger', 'chicken', 'pizza', 'mexican', 'taco',
            'breakfast', 'coffee', 'sandwich', 'wings', 'thai', 'chinese',
            'mcdonald', 'chick-fil-a', 'white castle', 'jack in the box',
            'donut', 'dunkin', 'starbucks'
        ]
        low_velocity_terms = ['grocery', 'convenience', 'liquor', 'pet', 'hardware', 'beauty', 'retail']

        factor = 1.0
        if any(term in joined for term in high_velocity_terms):
            factor += 0.08
        if any(term in joined for term in low_velocity_terms):
            factor -= 0.12
        if store.get('promotion_active'):
            factor += 0.05

        return max(0.75, min(factor, 1.2))

    def _fallback_wait_minutes(self, store, price_level):
        categories = [str(c).lower() for c in store.get('categories', [])]
        name = store.get('name', '').lower()
        joined = " ".join(categories + [name])

        if any(term in joined for term in ['mcdonald', 'taco bell', 'white castle', 'jack in the box', 'burger king']):
            return 7
        if any(term in joined for term in ['chick-fil-a', 'chicken', 'wings']):
            return 9
        if any(term in joined for term in ['coffee', 'donut', 'smoothie', 'sandwich']):
            return 8
        if any(term in joined for term in ['pizza', 'breakfast', 'brunch']):
            return 13
        if any(term in joined for term in ['grocery', 'liquor', 'retail', 'hardware', 'pet', 'beauty']):
            return 18

        wait_time_map = {1: 6, 2: 10, 3: 18, 4: 25}
        return wait_time_map.get(price_level, 10)

    def calculate_potential(self, store_id, surge_multiplier=1.0, distance_to_north_oldham_miles=0):
        store = self.restaurants.get(store_id)
        if not store: 
            print(f"Warning: Store ID '{store_id}' not found in restaurant database.")
            return 0

        # Load assumptions from config
        store_surge = store.get('surge_multiplier') or surge_multiplier
        base_pay = self.config['base_pay_unit'] * store_surge
        # JSON keys are strings, so we convert price_level to str
        price_level = self._coerce_price_level(store)
        price_level_str = str(price_level)
        avg_check = self.config['avg_check_per_level'].get(price_level_str, self.config.get('default_avg_check', 25))

        # Wait-Time Penalty: prefer live Uber ETA when available, otherwise infer from price level.
        eta_minutes = self._coerce_minutes(store.get('eta_minutes') or store.get('eta_text'))
        prep_wait_minutes = eta_minutes if eta_minutes is not None else self._fallback_wait_minutes(store, price_level)
        
        # Hourly Logic
        # Standardize: orders_per_hour assumes a certain cycle time. 
        # If prep is long, we penalize the throughput slightly.
        # Efficiency Bonus: High ratings (4.7+) reduce prep penalty by 20%
        rating = store.get('rating') or 4.5
        efficiency_factor = 1.08 if rating >= 4.7 else 1.0
        prep_penalty = max(0.35, 1 - (prep_wait_minutes / 90))
        orders_per_hour = self.config['orders_per_hour'] * prep_penalty * efficiency_factor
        orders_per_hour *= self._category_demand_factor(store)

        if store.get('is_open') is False:
            orders_per_hour *= 0.15
        availability = str(store.get('availability_text') or store.get('eta_text') or '').lower()
        if 'not available' in availability or 'currently unavailable' in availability:
            orders_per_hour = 0
        elif 'available at' in availability or any(day in availability for day in [
            'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'
        ]):
            orders_per_hour = 0
        elif availability.startswith('available in'):
            orders_per_hour *= 0.35

        # Tip Logic
        avg_tip = avg_check * self.config['tip_pct']
        if store.get('promotion_active'):
            avg_tip *= 1.04
        
        potential_hourly = (base_pay + avg_tip) * orders_per_hour

        # Apply Distance Penalty
        distance_miles = store.get('distance_miles')
        if distance_miles is None:
            distance_miles = distance_to_north_oldham_miles
        potential_hourly -= (distance_miles * self.config['distance_penalty_per_mile'] * orders_per_hour)

        return round(max(potential_hourly, 0), 2)

    def classify_potential(self, potential):
        """Classifies the market health based on configured thresholds."""
        thresholds = self.config.get('thresholds', {'golden_window': 30, 'dead_zone': 15})
        if potential > thresholds['golden_window']:
            return "GOLDEN WINDOW"
        elif potential < thresholds['dead_zone']:
            return "DEAD ZONE"
        return "FAIR MARKET"
