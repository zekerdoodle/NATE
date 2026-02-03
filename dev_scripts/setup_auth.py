import os
import sys
import webbrowser
import requests
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv, set_key

# Load existing environment variables
load_dotenv("api_keys.env")

CLIENT_ID = os.getenv("NinjaOne_ClientID")
CLIENT_SECRET = os.getenv("NinjaOne_ClientSecret")
BASE_URL = os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com").rstrip("/")
REDIRECT_URI = "http://localhost:8080"
# Scopes required for Nate's operations
# 'ticketing' is not a valid scope in the UI. 'management' likely covers it.
# We include 'control' to match the UI options and 'offline_access' for the refresh token.
SCOPE = "monitoring management control offline_access"

if not CLIENT_ID or not CLIENT_SECRET:
    print("Error: NinjaOne_ClientID and NinjaOne_ClientSecret must be set in api_keys.env")
    sys.exit(1)

print(f"Using NinjaOne Base URL: {BASE_URL}")

auth_code = None

class AuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        
        if "code" in params:
            auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authentication Successful!</h1><p>You can close this window and return to the terminal.</p>")
        else:
            self.send_response(400)
            self.wfile.write(b"Missing code parameter")
    
    def log_message(self, format, *args):
        return # Silence server logs

def get_authorization_code():
    server = HTTPServer(("localhost", 8080), AuthHandler)
    
    # Construct authorization URL
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": "nate_auth_setup" 
    }
    # Reverting to /ws/oauth/authorize as /oauth/authorize returned 404
    auth_url = f"{BASE_URL}/ws/oauth/authorize?{urllib.parse.urlencode(params)}"
    
    print(f"\n--- NinjaOne Authentication Setup ---")
    print(f"1. Opening browser to: {auth_url}")
    print(f"2. Please log in with the user account you want Nate to use.")
    print(f"3. Accept the permissions request.")
    
    webbrowser.open(auth_url)
    
    print("Waiting for callback on port 8080...")
    while auth_code is None:
        server.handle_request()
    
    return auth_code

def exchange_code_for_token(code):
    token_url = f"{BASE_URL}/ws/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    
    print(f"Exchanging code for tokens...")
    response = requests.post(token_url, data=data)
    
    if response.status_code != 200:
        print(f"Error exchanging code: {response.text}")
        response.raise_for_status()
        
    return response.json()

def main():
    try:
        code = get_authorization_code()
        print(f"Received authorization code.")
        
        tokens = exchange_code_for_token(code)
        refresh_token = tokens.get("refresh_token")
        
        if refresh_token:
            print(f"Successfully retrieved refresh token.")
            # Update api_keys.env
            env_path = "api_keys.env"
            # Ensure the file exists
            if not os.path.exists(env_path):
                with open(env_path, 'w') as f:
                    f.write("")
            
            set_key(env_path, "NINJA_REFRESH_TOKEN", refresh_token)
            print(f"SUCCESS: Saved NINJA_REFRESH_TOKEN to {env_path}")
            print("You can now run the application with user-level authentication.")
        else:
            print("Error: No refresh token received in response.")
            print("Response:", tokens)
            
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    main()
