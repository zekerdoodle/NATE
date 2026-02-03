import os
import requests
from dotenv import load_dotenv

load_dotenv("api_keys.env")

CLIENT_ID = os.getenv("NinjaOne_ClientID")
CLIENT_SECRET = os.getenv("NinjaOne_ClientSecret")
REFRESH_TOKEN = os.getenv("NINJA_REFRESH_TOKEN")
BASE_URL = os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com").rstrip("/")

print(f"Client ID: {CLIENT_ID}")
# print(f"Client Secret: {CLIENT_SECRET}") # Don't print secrets
print(f"Refresh Token: {REFRESH_TOKEN[:10]}...")
print(f"Base URL: {BASE_URL}")

url = f"{BASE_URL}/ws/oauth/token"
payload = {
    "grant_type": "refresh_token",
    "client_id": CLIENT_ID,
    "client_secret": CLIENT_SECRET,
    "refresh_token": REFRESH_TOKEN,
}

print(f"Sending request to {url}...")
response = requests.post(url, data=payload)

print(f"Status Code: {response.status_code}")
print(f"Response: {response.text}")
