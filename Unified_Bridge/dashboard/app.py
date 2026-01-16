import streamlit as st
import requests
import json
import time
import pandas as pd
import os

st.set_page_config(page_title="Unified Bridge Monitor", page_icon="🌉", layout="wide")

# Config
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.json')
with open(CONFIG_PATH, 'r') as f:
    CONFIG = json.load(f)

IBKR_Url = f"http://127.0.0.1:{CONFIG['server']['ibkr_port']}"
MT5_Url = f"http://127.0.0.1:{CONFIG['server']['mt5_port']}"

def check_status(url):
    try:
        r = requests.get(f"{url}/health", timeout=2)
        if r.status_code == 200:
            return True, r.json()
    except:
        pass
    return False, {"status": "offline", "last_trade": "Unknown"}

# Layout
st.title("🌉 Unified Bridge Monitor")

col1, col2 = st.columns(2)

# --- IBKR STATUS ---
with col1:
    st.subheader("Interactive Brokers")
    ib_online, ib_data = check_status(IBKR_Url)
    
    if ib_online:
        if ib_data.get("status") == "connected":
            st.success("✅ BRIDGE ONLINE & CONNECTED")
        else:
            st.warning("⚠️ BRIDGE ONLINE (TWS Disconnected)")
    else:
        st.error("🛑 BRIDGE OFFLINE")
        
    st.metric("Last Trade", ib_data.get("last_trade", "None"))
    
    with st.expander("Controls", expanded=True):
        if st.button("🔔 Test Trade (IBKR)"):
            payload = {
                "secret": CONFIG['security']['webhook_secret'],
                "action": "BUY",
                "symbol": "MNQ", # Micro Nasdaq
                "secType": "FUT",
                "exchange": "GLOBEX", 
                "volume": 1
            }
            try:
                requests.post(f"{IBKR_Url}/webhook", json=payload, timeout=10)
                st.toast("Sent IBKR Test Trade")
            except Exception as e:
                st.error(f"Failed: {e}")

        if st.button("🚨 PANIC CLOSE (IBKR)"):
             payload = {"secret": CONFIG['security']['webhook_secret'], "action": "CLOSE", "symbol": "MNQ"}
             requests.post(f"{IBKR_Url}/webhook", json=payload)
             st.toast("Sent Close Signal")

# --- MT5 STATUS ---
with col2:
    st.subheader("MetaTrader 5")
    mt5_online, mt5_data = check_status(MT5_Url)
    
    if mt5_online:
        if mt5_data.get("status") == "connected":
            st.success("✅ BRIDGE ONLINE & CONNECTED")
        else:
            st.warning("⚠️ BRIDGE ONLINE (MT5 Disconnected)")
    else:
        st.error("🛑 BRIDGE OFFLINE")
        
    st.metric("Last Trade", mt5_data.get("last_trade", "None"))
    
    with st.expander("Controls", expanded=True):
         if st.button("🔔 Test Trade (MT5)"):
            payload = {
                "secret": CONFIG['security']['webhook_secret'],
                "action": "BUY",
                "symbol": "MNQ1!", # TradingView Ticker
                "volume": 1
            }
            try:
                requests.post(f"{MT5_Url}/webhook", json=payload, timeout=2)
                st.toast("Sent MT5 Test Trade")
            except Exception as e:
                st.error(f"Failed: {e}")
                
         if st.button("🚨 PANIC CLOSE (MT5)"):
             payload = {"secret": CONFIG['security']['webhook_secret'], "action": "CLOSE", "symbol": "MNQ1!"}
             requests.post(f"{MT5_Url}/webhook", json=payload)
             st.toast("Sent Close Signal")

st.divider()

# --- LOGS ---
st.subheader("📜 System Logs (Supervisor)")
log_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'supervisor.log')
if os.path.exists(log_path):
    with open(log_path, 'r') as f:
        lines = f.readlines()[-20:] # Last 20 lines
        for line in lines:
            st.text(line.strip())
else:
    st.info("No logs available yet.")

time.sleep(2)
st.rerun()
