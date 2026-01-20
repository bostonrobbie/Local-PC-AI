import MetaTrader5 as mt5
import json
import os
import sys
import logging
import datetime
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_cors import CORS
import requests
import concurrent.futures
from logging.handlers import RotatingFileHandler
from waitress import serve

# Add parent dir to path to find client
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.topstep.client import TopStepClient
from src.utils.alerts import AlertManager
from src.utils.database import DatabaseManager

# Logging
# Logging
# Use RotatingFileHandler: Max 5MB, keep 5 backups
log_handler = RotatingFileHandler('logs/mt5.log', maxBytes=5*1024*1024, backupCount=5)
log_handler.setFormatter(logging.Formatter('%(asctime)s [MT5] %(message)s'))
log_handler.setLevel(logging.INFO)

logger = logging.getLogger("MT5_Bridge")
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)
# Also log to console for dev visibility
console = logging.StreamHandler()
console.setFormatter(logging.Formatter('%(asctime)s [MT5] %(message)s'))
logger.addHandler(console)

def load_config():
    # Load from parent dir
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'config.json')
    with open(path, 'r') as f:
        return json.load(f)

CONFIG = load_config()
MT5_CONF = CONFIG['mt5']

# Initialize TopStep Client
ts_client = TopStepClient(CONFIG)
# Initialize Utils
alerts = AlertManager(CONFIG)
db = DatabaseManager('trades.db')

# Global Executor for Parallel Tasks
executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

# Non-blocking validation on startup
try:
    ts_client.validate_connection()
except Exception as e:
    logger.error(f"TopStep Setup Error: {e}")

# Log Eval Mode Status
eval_mode_status = CONFIG.get('topstep', {}).get('eval_mode', False)
logger.info(f"TopStep Eval Mode: {'ENABLED (1 Mini)' if eval_mode_status else 'DISABLED (Funded/7 Micros)'}")

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

def close_positions(symbol, raw_symbol=None):
    """
    Closes all positions for a given symbol, using fuzzy matching to handle
    broker suffix mismatches (e.g. NQ1! vs NQ_H).
    """
    # Build robust search set
    search_symbols = {symbol}
    if raw_symbol:
        search_symbols.add(raw_symbol)
        clean = raw_symbol.replace('1!', '').replace('2!', '')
        search_symbols.add(clean)
        search_symbols.add(clean + "_H")
        
    logger.info(f"Closing Positions for {symbol}. Scanning for: {search_symbols}")

    all_positions = mt5.positions_get()
    if not all_positions:
        return {"status": "success", "message": "No open positions to close."}

    # Filter positions
    target_positions = [p for p in all_positions if p.symbol in search_symbols]
    
    if not target_positions:
        return {"status": "success", "message": f"No positions found matching {search_symbols}"}

    count = 0
    for pos in target_positions:
        tick = mt5.symbol_info_tick(pos.symbol) # Use the ACTUAL symbol of the position
        if not tick: 
            logger.warning(f"No tick for {pos.symbol}, skipping close.")
            continue
        
        type_order = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol, # Use exact position symbol
            "volume": pos.volume,
            "type": type_order,
            "position": pos.ticket, # CRITICAL: Close by Ticket
            "price": price,
            "magic": MT5_CONF.get('magic_number', 0),
            "comment": "Unified-Bridge-Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            count += 1
            logger.info(f"Closed position {pos.ticket} ({pos.symbol})")
        else:
            logger.error(f"Failed to close {pos.ticket}: {res.comment}")
            
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
    
    # Force uppercase for safety
    symbol = symbol.upper()

    # 2. Action
    action = data.get('action', '').upper()
    if action in ['CLOSE', 'EXIT', 'FLATTEN']:
        return close_positions(symbol, raw_symbol=raw)

    # 3. Volume
    vol = float(data.get('volume', 1.0)) * mult
    # Round logic could go here (min volume check)
    
    # 4. Netting Logic (Simulate Netting on Hedging Account)
    # Check for opposite positions
    opposite_type = mt5.ORDER_TYPE_SELL if action == 'BUY' else mt5.ORDER_TYPE_BUY
    
    # 4.1 Get all positions to debug mismatch
    all_positions = mt5.positions_get()
    if all_positions:
        logger.info(f"Open Positions in MT5: {[p.symbol for p in all_positions]}")
    else:
        logger.info("No Open Positions in MT5.")

    # 4.2 specific symbol lookup (Try raw, mapped, and common variations)
    search_symbols = {symbol, raw, raw.replace('1!', ''), raw.replace('1!', '') + "_H"}
    positions = []
    
    # Filter all positions that match any of our search symbols
    if all_positions:
        for p in all_positions:
            if p.symbol in search_symbols:
                positions.append(p)
    
    if positions:
        for pos in positions:
            if pos.type == opposite_type:
                logger.info(f"Netting: Closing opposite position {pos.ticket} ({pos.volume})")
                
                # Close this position
                # Determine close price
                tick = mt5.symbol_info_tick(pos.symbol) # Use pos.symbol to be safe
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
    
    # Check TopStep Status
    ts_connected = ts_client.connected
    
    return jsonify({
        "status": "connected" if connected else "disconnected", 
        "last_trade": STATE['last_trade'],
        "topstep_status": "connected" if ts_connected else "disconnected"
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data.get('secret') != CONFIG['security']['webhook_secret']:
         logger.warning(f"Unauthorized Webhook Attempt: {request.remote_addr}")
         return jsonify({"error": "Unauthorized"}), 401
         
    logger.info(f"📥 Received Webhook: {json.dumps(data)}")
         
    # 1. Forward to IBKR (Parallel - Fire & Forget)
    executor.submit(forward_to_ibkr, data)

    # 2. Forward to TopStepX (Parallel)
    executor.submit(handle_topstep_logic, data)

    # 3. Execute MT5 (Main Thread - Critical Path)
    try:
        start_time = time.time()
        res = execute_trade(data)
        duration = (time.time() - start_time) * 1000
        
        STATE["last_trade"] = f"{data.get('action')} {data.get('symbol')}"
        
        status = 'success' if 'order' in res or res.get('status') == 'success' else 'error-mt5'
        
        # Database Log
        db.log_trade("MT5", data, status, duration, details=str(res))
        
        # Alert (Only on success to avoid spamming errors if simple check fail)
        if status == 'success':
            alerts.send_trade_alert(data, platform="MT5")
            
        return jsonify(res)
    except Exception as e:
        logger.error(f"Error: {e}")
        alerts.send_error_alert(str(e), context="MT5_Bridge_Main")
        return jsonify({"error": str(e)}), 500

def handle_topstep_logic(data):
    """Encapsulated Topstep Logic for Parallel Execution"""
    try:
         # Logic: If source is NQ, send 7x to TopStep as MNQ
         raw_symbol = data.get('symbol', '').upper()
         base_symbol = raw_symbol.replace('1!', '').replace('2!', '')
         
         # Check mapping
         eval_mode = CONFIG.get('topstep', {}).get('eval_mode', False)
         
         multiplier = 1.0
         ts_symbol = base_symbol
         
         if eval_mode:
             # EVAL MODE: Use Mini (NQ) directly, 1x Multiplier (Modified)
             if base_symbol in ["NQ", "ES"]:
                 ts_symbol = base_symbol # Force Mini
                 multiplier = 1.0
                 logger.info(f"TopStep Eval Mode: Using 1 Mini {ts_symbol} (x1)")
             else:
                 # Fallback to map for others
                 ts_symbol = CONFIG.get('topstep', {}).get('symbol_map', {}).get(base_symbol, base_symbol)
         else:
             # FUNDED MODE: Use Micro (MNQ), 1:7 (for NQ)
             ts_symbol = CONFIG.get('topstep', {}).get('symbol_map', {}).get(base_symbol, base_symbol)
             
             if base_symbol == "NQ":
                 multiplier = 7.0
                 logger.info(f"TopStep Funded Mode: Applying 7x Multiplier for NQ ({data.get('volume')} -> {float(data.get('volume',0))*7})")
             
         if CONFIG.get('topstep', {}).get('enabled', False):
             # Prepare specialized payload
             ts_payload = {
                 "symbol": ts_symbol,
                 "action": data.get('action'),
                 "volume": float(data.get('volume', 0)) * multiplier
             }
             # execute_trade handles mock mode internally
             ts_res = ts_client.execute_trade(ts_payload)
             
             # Enhanced Logging
             log_level = logging.INFO if ts_res.get('status') == 'success' else logging.ERROR
             logger.log(log_level, f"TopStep Response: {ts_res}")
             
             # DB Log for Topstep
             status = ts_res.get('status', 'unknown')
             db.log_trade("TopStep", ts_payload, status, details=str(ts_res))

    except Exception as e:
        logger.error(f"TopStep Logic Error: {e}")

if __name__ == "__main__":
    if not initialize_mt5():
        logger.warning("MT5 Init Failed - Running in Offline Mode")
        
    port = CONFIG['server']['mt5_port']
    logger.info(f"Starting MT5 Bridge on {port} (Waitress Production Server)")
    serve(app, host="0.0.0.0", port=port, threads=6)
