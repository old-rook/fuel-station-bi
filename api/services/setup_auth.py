# setup_auth.py
# Run this one time to generate the token.json file for Gmail API access.

import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def generate_token():
    credentials_file = "credentials.json"
    token_file = "token.json"

    if not os.path.exists(credentials_file):
        raise FileNotFoundError(f"Missing '{credentials_file}' in current directory!")

    print("Launching browser for Google OAuth sign-in...")
    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
    creds = flow.run_local_server(port=0)

    # Save the token
    with open(token_file, "w") as token:
        token.write(creds.to_json())

    print(f"\n[SUCCESS] Token successfully saved to '{token_file}'!")
    print("Your service can now run unattended via cron or UI triggers.")

if __name__ == "__main__":
    generate_token()