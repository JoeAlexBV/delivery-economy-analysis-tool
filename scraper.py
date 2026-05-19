import asyncio
from playwright.async_api import async_playwright
import json
import math

class CrestwoodScraper:
    def __init__(self, config_path='config.json'):
        with open(config_path, 'r') as f:
            config = json.load(f)['scraper']
        
        self.search_depth = config.get('search_depth_scrolls', 5)
        self.headless = config.get('headless', False)
        self.base_url = config['base_url']
        self.coords = config['coords']
        self.search_radius_miles = config.get('search_radius_miles', 10)
        self.single_origin_coverage_miles = config.get('single_origin_coverage_miles', 10)
        self.spillover_search_depth = config.get('spillover_search_depth_scrolls', max(3, min(self.search_depth, 5)))
        self.captured_data = []

    def _offset_coords(self, coords, north_miles=0, east_miles=0):
        latitude = coords["latitude"]
        longitude = coords["longitude"]
        lat_offset = north_miles / 69.0
        lon_offset = east_miles / (69.0 * math.cos(math.radians(latitude)))
        return {
            "latitude": round(latitude + lat_offset, 6),
            "longitude": round(longitude + lon_offset, 6),
        }

    def _crawl_origins(self):
        """
        Uber does not expose a reliable radius parameter on city pages, so wider
        coverage is approximated by repeating the crawl from nearby geolocations.
        """
        origins = [{"label": "center", "coords": self.coords, "search_depth": self.search_depth}]
        spillover_miles = max(0, self.search_radius_miles - self.single_origin_coverage_miles)
        if spillover_miles <= 0:
            return origins

        diagonal = spillover_miles / math.sqrt(2)
        offsets = [
            ("north", spillover_miles, 0),
            ("south", -spillover_miles, 0),
            ("east", 0, spillover_miles),
            ("west", 0, -spillover_miles),
            ("northeast", diagonal, diagonal),
            ("northwest", diagonal, -diagonal),
            ("southeast", -diagonal, diagonal),
            ("southwest", -diagonal, -diagonal),
        ]
        for label, north_miles, east_miles in offsets:
            origins.append({
                "label": label,
                "coords": self._offset_coords(self.coords, north_miles=north_miles, east_miles=east_miles),
                "search_depth": self.spillover_search_depth,
            })
        return origins

    def _contains_store_data(self, obj):
        if isinstance(obj, dict):
            if "storesMap" in obj or {"title", "uuid", "etaRange"}.issubset(obj.keys()):
                return True
            return any(self._contains_store_data(value) for value in obj.values())
        if isinstance(obj, list):
            return any(self._contains_store_data(item) for item in obj)
        return False

    async def _handle_response(self, response):
        """
        Intercepts network responses and captures JSON data from relevant URLs.
        """
        url = response.url.lower()
        relevant_url = any(keyword in url for keyword in [
            "getpaginatedstores",
            "get_store",
            "storefront",
            "feed",
            "graphql",
            "batch",
        ])
        if relevant_url:
            try:
                # Broaden check: capture any JSON-like response even if headers are weird
                ct = (response.headers.get("content-type") or "").lower()
                if any(t in ct for t in ["json", "javascript", "text/plain", "octet-stream"]):
                    data = await response.json()
                    if data and self._contains_store_data(data):
                        self.captured_data.append({"url": response.url, "data": data})
                        print(f"[SUCCESS] Captured data from: {url[:60]}...")
            except Exception:
                # Some octet-streams aren't JSON, skip silently
                pass

    async def fetch_live_data(self):
        self.captured_data = [] # Reset captured data for each new fetch operation
        try:
            async with async_playwright() as p:
                origins = self._crawl_origins()
                print(
                    f"[*] Launching browser (Headless={self.headless}, "
                    f"Radius={self.search_radius_miles}mi, Origins={len(origins)})..."
                )
                # Use the setting from config.json
                browser = await p.chromium.launch(headless=self.headless)
                for origin_index, origin in enumerate(origins, 1):
                    context = await browser.new_context(
                        viewport={'width': 1280, 'height': 800},
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                        geolocation=origin["coords"],
                        permissions=["geolocation"]
                    )
                    page = await context.new_page()

                    page.on("response", self._handle_response)

                    print(
                        f"[*] Accessing Crestwood Market "
                        f"({origin_index}/{len(origins)}: {origin['label']} @ "
                        f"{origin['coords']['latitude']}, {origin['coords']['longitude']})..."
                    )
                    # Wait for load and add a hard sleep to allow dynamic UI elements to settle
                    await page.goto(self.base_url, wait_until="load", timeout=60000)
                    await page.wait_for_timeout(8000)
                    
                    # Dismiss common "Got it", "Accept", or "Opt out" overlays
                    print("[*] Checking for blocking overlays...")
                    for btn_text in ["Got it", "Accept", "Opt out", "Close"]:
                        try:
                            # Search for buttons by text and click if they exist
                            btn = page.get_by_role("button", name=btn_text, exact=False)
                            if await btn.is_visible(timeout=3000):
                                await btn.click()
                                print(f"[+] Dismissed '{btn_text}' overlay.")
                        except:
                            continue

                    try:
                        print("[*] Waiting for restaurant list to populate...")
                        # Wait for a store link OR a "No stores" message
                        await page.wait_for_selector('a[href*="/store/"], [data-testid="no-results-message"]', timeout=20000)
                        
                        origin_search_depth = origin.get("search_depth", self.search_depth)
                        print(f"[*] Navigating pagination (Goal: {origin_search_depth} pages)...")
                        for i in range(origin_search_depth):
                            # Scroll to the bottom to reveal pagination controls and trigger any lazy-loading
                            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            await page.wait_for_timeout(6000)
                            
                            # Capture current page names from HTML as a backup for every page
                            selector = 'a[href*="/store/"] h3'
                            html_names = await page.eval_on_selector_all(selector, 'elements => elements.map(e => e.innerText)')
                            if html_names:
                                self.captured_data.append({
                                    "url": f"HTML_BACKUP_{origin['label'].upper()}_PAGE_{i+1}",
                                    "data": {"names": html_names, "origin": origin},
                                })
                                print(f" [PAGE {i+1}] Found {len(html_names)} stores in HTML.")
                            
                            # Prioritize the specific button you identified via DevTools
                            pagination_selectors = [
                                'button[aria-label*="Next" i]',
                                'a[aria-label*="Next" i] button',
                                'button:has-text("Next")',
                                'a:has(svg title:text-is("Next")) button',
                                'button:has(svg title:text-is("Next"))',
                                'nav[aria-label*="pagination"] button:has(svg)',
                                # The specific CSS path you provided as a final fallback
                                '#main-content > div:nth-child(5) > div > div > div._el._hn._iu._hp._hq._hr._iv > div > div > div._al._j7._j8._dq._j9._ja > a:nth-child(7) > button'
                            ]
                            
                            next_btn = None
                            for selector in pagination_selectors:
                                try:
                                    candidate = page.locator(selector).first
                                    if await candidate.is_visible(timeout=3000) and await candidate.is_enabled():
                                        next_btn = candidate
                                        break
                                except:
                                    continue

                            if next_btn:
                                print(f" [ACTION] Clicking 'Next' to reach Page {i+2}...")
                                await next_btn.scroll_into_view_if_needed()
                                await next_btn.click()
                                # Increased wait for content to swap and stabilize
                                await page.wait_for_timeout(8000)
                            else:
                                print(f" [!] No 'Next' button found on Page {i+1}. Current URL: {page.url}")
                                # Insightful Debugging: Log all buttons in nav to see what we missed
                                nav_buttons = await page.locator('nav button').all()
                                if nav_buttons:
                                    print(f" [DEBUG] Found {len(nav_buttons)} alternative buttons in nav:")
                                    for btn in nav_buttons:
                                        text = await btn.inner_text()
                                        html = await btn.evaluate("el => el.outerHTML")
                                        print(f"  > Text: '{text.strip()}' | HTML: {html[:70]}...")
                                break
                        
                    except Exception:
                        print("[!] No store elements appeared. Taking debug screenshot...")
                    
                    # Always take a screenshot for visual confirmation
                    await page.screenshot(path=f"debug_market_view_{origin['label']}.png")
                    print(f"[*] Saved debug_market_view_{origin['label']}.png for review.")

                    await context.close()

                await browser.close()
                return self.captured_data
        except Exception as e:
            print(f"[!] Scraper Error: {e}")
            return []
