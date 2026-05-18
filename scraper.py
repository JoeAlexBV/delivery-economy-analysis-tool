import asyncio
from playwright.async_api import async_playwright
import json
import random

class CrestwoodScraper:
    def __init__(self, config_path='config.json'):
        with open(config_path, 'r') as f:
            config = json.load(f)['scraper']
        
        self.search_depth = config.get('search_depth_scrolls', 5)
        self.headless = config.get('headless', False)
        self.base_url = config['base_url']
        self.coords = config['coords']
        self.captured_data = []

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
                print(f"[*] Launching browser (Headless={self.headless})...")
                # Use the setting from config.json
                browser = await p.chromium.launch(headless=self.headless)
                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    geolocation=self.coords,
                    permissions=["geolocation"]
                )
                page = await context.new_page()

                page.on("response", self._handle_response)

                print(f"[*] Accessing Crestwood Market...")
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
                    
                    print(f"[*] Navigating pagination (Goal: {self.search_depth} pages)...")
                    for i in range(self.search_depth):
                        # Scroll to the bottom to reveal pagination controls and trigger any lazy-loading
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await page.wait_for_timeout(6000)
                        
                        # Capture current page names from HTML as a backup for every page
                        selector = 'a[href*="/store/"] h3'
                        html_names = await page.eval_on_selector_all(selector, 'elements => elements.map(e => e.innerText)')
                        if html_names:
                            self.captured_data.append({"url": f"HTML_BACKUP_PAGE_{i+1}", "data": {"names": html_names}})
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
                            print(f" [ACTION] Clicking 'Next' (SVG Title match) to reach Page {i+2}...")
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
                await page.screenshot(path="debug_market_view.png")
                print("[*] Saved debug_market_view.png for review.")

                await browser.close()
                return self.captured_data
        except Exception as e:
            print(f"[!] Scraper Error: {e}")
            return []
