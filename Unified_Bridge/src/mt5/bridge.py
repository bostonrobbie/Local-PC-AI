import MetaTrader5 as mt5
import json
import os
import sys
import logging
import datetime
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [MT5] %(message)s')
logger = logging.getLogger("MT5_Bridge")

def load_config():
    # Load from parent dir
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'config.json')
    with open(path, 'r') as f:
        return json.load(f)

CONFIG = load_config()
MT5_CONF = CONFIG['mt5']

# Global State
STATE = {
    "connected": False,
    "last_trade": "None"
}

def initialize_mt5():
    """Connects to MT5 terminal."""
    try:
        if not mt5.initialize(path=MT5_CONF['path']):
            logger.error(f"Failed to init MT5: {mt5.last_error()}")
            return False
            
        # Login
        if not mt5.login(
            login=int(MT5_CONF['login']), 
            password=MT5_CONF['password'], 
            server=MT5_CONF['server']
        ):
            logger.error(f"MT5 Login failed: {mt5.last_error()}")
            return False
            
        STATE["connected"] = True
        logger.info(f"✅ Connected to MT5: {MT5_CONF['server']}")
        return True
    except Exception as e:
        logger.error(f"Init Error: {e}")
        return False

def close_positions(symbol):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return {"status": "success", "message": f"No positions for {symbol}"}

    count = 0
    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if not tick: continue
        
        type_order = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": type_order,
            "position": pos.ticket,
            "price": price,
            "magic": MT5_CONF.get('magic_number', 0),
            "comment": "Unified-Bridge-Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            count += 1
            
    return {"status": "success", "closed": count}

def execute_trade(data):
    # 1. Map Symbol
    raw = data.get('symbol', '').upper()
    mapping = MT5_CONF.get('symbol_map', {}).get(raw)
    
    symbol = raw
    mult = 1.0
    
    if mapping:
        if isinstance(mapping, dict):
            symbol = mapping['name']
            mult = mapping['multiplier']
        else:
            symbol = mapping

    logger.info(f"Trade: {data.get('action')} {raw} -> {symbol} (x{mult})")

    # 2. Action
    action = data.get('action', '').upper()
    if action in ['CLOSE', 'EXIT', 'FLATTEN']:
        return close_positions(symbol)

    # 3. Volume
    vol = float(data.get('volume', 1.0)) * mult
    # Round logic could go here (min volume check)
    
    # 4. Netting Logic (Simulate Netting on Hedging Account)
    # Check for opposite positions
    opposite_type = mt5.ORDER_TYPE_SELL if action == 'BUY' else mt5.ORDER_TYPE_BUY
    positions = mt5.positions_get(symbol=symbol)
    
    if positions:
        for pos in positions:
            if pos.type == opposite_type:
                logger.info(f"Netting: Closing opposite position {pos.ticket} ({pos.volume})")
                
                # Close this position
                # Determine close price
                tick = mt5.symbol_info_tick(symbol)
                close_price = tick.ask if pos.type == mt5.ORDER_TYPE_SELL else tick.bid # Buy to close Sell (Ask), Sell to close Buy (Bid)
                
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": pos.volume,
                    "type": mt5.ORDER_TYPE_BUY if pos.type == mt5.ORDER_TYPE_SELL else mt5.ORDER_TYPE_SELL,
                    "position": pos.ticket,
                    "price": close_price,
                    "magic": MT5_CONF.get('magic_number', 0),
                    "comment": "Unified-Bridge-Netting",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                res = mt5.order_send(req)
                if res.retcode != mt5.TRADE_RETCODE_DONE:
                    logger.error(f"Netting Fail: {res.comment}")
                else:
                    # Reduce incoming volume by closed volume
                    vol -= pos.volume
                    
    # 5. Order Setup (Remaining Volume)
    if vol <= 0.0001: # EPSILON check
        logger.info("Netting Completed. No remaining volume to open.")
        return {"success": True, "message": "Closed via Netting"}

    logger.info(f"Opening New Position: {action} {vol} {symbol}")
    
    tick = mt5.symbol_info_tick(symbol)
    if not tick: return {"error": f"No Price for {symbol}"}
    
    order_type_str = data.get('type', 'MARKET').upper()
    price = float(data.get('price', 0.0))
    
    if order_type_str == 'LIMIT' and price > 0:
        action_type = mt5.TRADE_ACTION_PENDING
        ot = mt5.ORDER_TYPE_BUY_LIMIT if action == 'BUY' else mt5.ORDER_TYPE_SELL_LIMIT
        ex_price = price
    else:
        action_type = mt5.TRADE_ACTION_DEAL
        ot = mt5.ORDER_TYPE_BUY if action == 'BUY' else mt5.ORDER_TYPE_SELL
        ex_price = tick.ask if action == 'BUY' else tick.bid

    req = {
        "action": action_type,
        "symbol": symbol,
        "volume": vol,
        "type": ot,
        "price": ex_price,
        "magic": MT5_CONF.get('magic_number', 0),
        "comment": "Unified-Bridge",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC if action_type == mt5.TRADE_ACTION_DEAL else mt5.ORDER_FILLING_RETURN
    }
    
    res = mt5.order_send(req)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        return {"error": f"MT5 Fail: {res.comment} ({res.retcode})"}
        
    return {"success": True, "order": res.order}

# Flask
app = Flask(__name__)
CORS(app)

# Helper to forward to IBKR
def forward_to_ibkr(data):
    """Forwards the webhook payload to the IBKR bridge."""
    try:
        # Prepare URL
        ibkr_port = CONFIG['server'].get('ibkr_port', 5001)
        url = f"http://127.0.0.1:{ibkr_port}/webhook"
        
        # Clone data to avoid mutating original
        payload = data.copy()
        payload['secret'] = CONFIG['security']['webhook_secret']
        
        # Symbol Cleanup for IBKR
        # MT5 uses "MNQ1!", IBKR uses "MNQ" (usually continuous).
        # We strip digits and ! from the end if it looks like a TradingView ticker
        raw = payload.get('symbol', '').upper()
        if '1!' in raw:
            # Assume formatted like "MNQ1!" -> "MNQ"
            clean = raw.replace('1!', '').replace('2!', '')
            payload['symbol'] = clean
            payload['secType'] = 'FUT' # Force Future if it was a TV future ticker
            payload['exchange'] = 'GLOBEX' # Good default for US Futures
        
        # Send
        # We use a short timeout so MT5 doesn't hang waiting for IBKR
        try:
            requests.post(url, json=payload, timeout=0.5) 
        except requests.exceptions.ReadTimeout:
            pass # We don't care about response, just fire and forget roughly
        except Exception as e:
            logger.error(f"Forwarding Fail: {e}")
            
    except Exception as e:
        logger.error(f"Forwarding Error: {e}")

@app.route('/health', methods=['GET'])
def health():
    connected = mt5.terminal_info() is not None
    STATE['connected'] = connected
    return jsonify({"status": "connected" if connected else "disconnected", "last_trade": STATE['last_trade']})

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data.get('secret') != CONFIG['security']['webhook_secret']:
         return jsonify({"error": "Unauthorized"}), 401
         
    # 1. Forward to IBKR (Mirroring)
    forward_to_ibkr(data)

    try:
        start_time = time.time()
        res = execute_trade(data)
        duration = (time.time() - start_time) * 1000
        
        STATE["last_trade"] = f"{data.get('action')} {data.get('symbol')}"
        
        # Analytics Log
        # We assume CWD is project root (via main.py)
        with open('logs/analytics.csv', 'a') as f: 
            ts = datetime.datetime.now().isoformat()
            f.write(f"{ts},MT5,{data.get('symbol')},{data.get('action')},{duration:.2f},{'success' if 'order' in res else 'error'}\n")
            
        return jsonify(res)
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    if not initialize_mt5():
        logger.warning("MT5 Init Failed - Running in Offline Mode")
        
    port = CONFIG['server']['mt5_port']
    logger.info(f"Starting MT5 Bridge on {port}")
    app.run(host="0.0.0.0", port=port)
