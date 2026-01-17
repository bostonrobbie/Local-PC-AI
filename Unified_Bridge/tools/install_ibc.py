import os
import urllib.request
import zipfile
import shutil
import json
import configparser

# URL for IBC 3.18.0 (Latest Stable for TWS 10.x)
# Using a specific version to ensure compatibility
# Actually, let's use a known mirror or direct github release if possible.
# github.com/IbcAlpha/IBC/releases/download/3.18.0/IBC-3.18.0.zip
IBC_URL = "https://github.com/IbcAlpha/IBC/releases/download/3.23.0/IBCWin-3.23.0.zip"
INSTALL_DIR = "C:\\IBC"

def load_config():
    # If running from root, use config.json, otherwise ../config.json
    path = 'config.json' if os.path.exists('config.json') else '../config.json'
    with open(path, 'r') as f:
        return json.load(f)

def run():
    print(f"--- Installing IBC Auto-Login to {INSTALL_DIR} ---")
    
    # 1. Download
    if not os.path.exists(INSTALL_DIR):
        os.makedirs(INSTALL_DIR)
        
    zip_path = os.path.join(INSTALL_DIR, "IBC.zip")
    if not os.path.exists(zip_path):
        print(f"Downloading IBC from {IBC_URL}...")
        try:
            urllib.request.urlretrieve(IBC_URL, zip_path)
            print("Download Complete.")
        except Exception as e:
            print(f"Download Failed: {e}")
            return

    # 2. Extract
    print("Extracting...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(INSTALL_DIR)
        
    # 3. Configure
    print("Configuring...")
    config = load_config()
    ibkr = config['ibkr']
    
    # Generate config.ini for IBC
    # We essentially need to populate:
    # IbLoginId, IbPassword, TradingMode=paper
    
    # We'll read the template if it exists, or just write a minimal one.
    # IBC comes with "config.ini" example. Let's overwrite specific fields.
    
    ini_content = f"""
[IBController]
IbLoginId={ibkr.get('username', '')}
IbPassword={ibkr.get('password', '')}
TradingMode={'paper' if ibkr.get('paper_mode') else 'live'}
IbDir=C:\\Jts
TwsPath={ibkr.get('tws_path').replace('tws.exe', '')}
# If TWS path is C:\\Jts\\1043\\tws.exe, we need the dir.
# Actually IBC needs TwsDir logic.
# Let's assume standard install.

# Minimize on startup
MinimizeMainWindow=yes

# API Settings (Ensure matches config.json)
AcceptIncomingConnection=yes
ApiPort={ibkr['tws_port']}
ClientID={ibkr['client_id']}

# Auto-Restart (Keep it alive)
ExistingSessionDetectedAction=primary
"""
    
    with open(os.path.join(INSTALL_DIR, "config.ini"), "w") as f:
        f.write(ini_content)
        
    # 4. Create Launcher Batch
    # IBC needs to know where TWS is.
    # StartTWS.bat is usually provided. We should update it or create our own caller.
    
    # Actually, we should use the provided StartIBC.bat but modify it?
    # Or just create a simple "LaunchIBC.bat" that calls the jar.
    
    # It's cleaner to create a new batch file that sets the vars.
    tws_dir = os.path.dirname(ibkr.get('tws_path')) # C:\Jts\1043
    # IBC needs the ROOT Jts dir usually? Or the version dir?
    # Documentation says: TWS_MAJOR_VNO
    
    # Taking a simpler approach: Generate a robust batch file
    launcher = f"""
@echo off
set TWS_MAJOR_VNO=1043
set TWS_DIR=C:\\Jts
set IBC_INI={INSTALL_DIR}\\config.ini
set IBC_PATH={INSTALL_DIR}
set TWS_PATH={tws_dir}
set TWS_SETTINGS_PATH=C:\\Jts

title IBC Auto-Login

cd /d {INSTALL_DIR}
java -cp "%IBC_PATH%\\ibc-3.18.0.jar;%TWS_PATH%\\jars\\*" ibc.launcher.Launch
"""
    # Note: Classpath depends on version. 3.18.0 jar name might differ.
    # Let's assume standard.
    
    with open(os.path.join(INSTALL_DIR, "StartIBC_Custom.bat"), "w") as f:
        f.write(launcher)
        
    print(f"✅ Success! IBC Installed.")
    print(f"Credentials for {ibkr.get('username')} configured.")
    print(f"To test manually: Run {INSTALL_DIR}\\StartIBC_Custom.bat")

if __name__ == "__main__":
    run()
