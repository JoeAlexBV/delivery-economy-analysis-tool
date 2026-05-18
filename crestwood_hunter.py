import asyncio
from playwright.async_api import async_playwright

async def get_delivery_data():
    async with async_playwright() as p:
        # 1. Launch Browser (Headless=False lets you see it work)
        browser = await p.chromium.launch(headless=True)
        
        # 2. Set Geolocation to Crestwood, KY (38.3306, -85.4852)
        context = await browser.new_context(
            geolocation={"latitude": 38.3306, "longitude": -85.4852},
            permissions=["geolocation"]
        )
        page = await context.new_page()

        # 3. Intercept Network Calls
        # Instead of scraping HTML, we 'catch' the JSON data DoorDash sends
        async def handle_response(response):
            if "graphql" in response.url or "get_store" in response.url:
                try:
                    data = await response.json()
                    print(f"Captured Data from {response.url[:50]}...")
                    # Logic: Save to your gig_earnings.json here
                except:
                    pass

        page.on("response", handle_response)

        # 4. Navigate to the target
        print("Navigating to Uber Eats in Crestwood...")
        await page.goto("https://www.ubereats.com/city/crestwood-ky")
        
        # Wait for the restaurant list to load (uses your 'Auto-wait' logic)
        await page.wait_for_selector('text=Crestwood Bistro', timeout=10000)
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(get_delivery_data())