import asyncio, httpx, os
print("Starting bot...")
print(f"TOKEN: {os.environ.get('TELEGRAM_BOT_TOKEN','MISSING')[:15]}...")
print("httpx version:", httpx.__version__)
print("All OK - bot would start here")
