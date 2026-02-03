import requests

base_urls = [
    # Replace with your NinjaOne instance URL
    # "https://your-instance.rmmservice.com",
    "https://app.ninjarmm.com",
    "https://eu.ninjarmm.com",
    "https://oc.ninjarmm.com"
]

paths = [
    "/ws/oauth/authorize",
    "/oauth/authorize",
    "/v2/oauth/authorize"
]

print("Checking endpoints...")
for base in base_urls:
    for path in paths:
        url = base + path
        try:
            # We expect a 400 (Missing parameters) or 200 (Login page). 
            # 404 means endpoint doesn't exist.
            response = requests.get(url, allow_redirects=True, timeout=10)
            print(f"[{response.status_code}] {url} -> {response.url}")
            if response.status_code == 404:
                 print(f"Response: {response.text[:200]}")
        except Exception as e:
            print(f"[ERR] {url}: {e}")
