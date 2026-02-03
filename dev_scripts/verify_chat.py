import requests
import json
import sys
import time

def test_chat():
    url = "http://localhost:8000/api/chat"
    
    print("1. Testing new session...")
    payload = {"message": "Hello, my name is TestUser."}
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        print("Response:", json.dumps(data, indent=2))
        
        session_id = data.get("session_id")
        if not session_id:
            print("ERROR: No session_id returned")
            sys.exit(1)
        print(f"Session ID: {session_id}")
        
        print("\n2. Testing history with session_id...")
        payload2 = {"message": "What is my name?", "session_id": session_id}
        response2 = requests.post(url, json=payload2)
        response2.raise_for_status()
        data2 = response2.json()
        print("Response 2:", json.dumps(data2, indent=2))
        
        if "TestUser" in data2["response"]:
            print("SUCCESS: Context retained.")
        else:
            print("WARNING: Context might not be retained.")
            
    except requests.exceptions.ConnectionError:
        print("Error: Could not connect to server. Is it running?")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_chat()
