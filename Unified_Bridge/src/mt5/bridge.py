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
from waitress import serve

# Add parent dir to path to find client
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.topstep.client import TopStepClient
from src.utils.alerts import AlertManager
from src.utils.database import DatabaseManager
from src.utils.logger import LogManager

# Logging
logger = LogManager.get_logger("MT5_Bridge", log_file="logs/mt5.log")

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
executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

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
    "connected": False,
    "last_trade": "None"
}

# Optimization: Symbol Cache to avoid IPC calls for static data (Point, Digits)
SYMBOL_CACHE = {}

def warm_cache(symbols):
    """Pre-loads symbol info into cache."""
    for s in symbols:
        info = mt5.symbol_info(s)
        if info:
            SYMBOL_CACHE[s] = info
            logger.info(f"Cached Info for {s}: Point={info.point}")
        else:
            logger.warning(f"Failed to cache {s}")

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
        STATE["connected"] = True
        logger.info(f"✅ Connected to MT5: {MT5_CONF['server']}")
        
        # Warm Cache
        common_symbols = ["NQ", "MNQ", "ES", "MES", "NQ_H", "ES_H"]
        warm_cache(common_symbols)
        
        return True
    except Exception as e:
        logger.error(f"Init Error: {e}")
        return False

def validate_terminal_state():
    """Checks if MT5 is connected and ready before trading."""
    if not mt5.terminal_info():
        logger.warning("MT5 Terminal Info failed. Attempting Reconnect...")
        return initialize_mt5()
    return True

def safe_order_send(request, max_retries=3):
    """Wraps order_send with retry logic for transient errors."""
    for i in range(max_retries):
        try:
            res = mt5.order_send(request)
            if res is None:
                logger.error(f"Order Send returned None (Attempt {i+1})")
                time.sleep(0.5)
                continue
                
            if res.retcode == mt5.TRADE_RETCODE_DONE:
                return res
            elif res.retcode in [mt5.TRADE_RETCODE_TIMEOUT, mt5.TRADE_RETCODE_CONNECTION]:
                logger.warning(f"Transient Error {res.retcode}: {res.comment}. Retrying...")
                time.sleep(1.0)
            else:
                # Fatal error (e.g. Invalid Volume)
                logger.error(f"Fatal Order Error {res.retcode}: {res.comment}")
                return res
        except Exception as e:
            logger.error(f"Exception during order send: {e}")
            time.sleep(0.5)
            
    return None

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

    # 0. Pre-Trade Validation
    if not validate_terminal_state():
        return {"error": "MT5 Terminal Disconnected"}

    logger.info(f"Opening New Position: {action} {vol} {symbol}")
    
    # 5. Order Type & Price Logic -- STRICT LIMIT ORDER ENFORCEMENT
    # We ignore the incoming 'type' unless it's specifically controlling limit behavior.
    # We ALWAYS send LIMIT orders.
    
    tick = mt5.symbol_info_tick(symbol)
    if not tick: return {"error": f"No Price for {symbol}"}

    action_type = mt5.TRADE_ACTION_PENDING
    ot = mt5.ORDER_TYPE_BUY_LIMIT if action == 'BUY' else mt5.ORDER_TYPE_SELL_LIMIT
    
    # 6. Price Calculation (Marketable Limit or Specific Price)
    # Get Configs
    exec_conf = MT5_CONF.get('execution', {})
    slippage_ticks = exec_conf.get('slippage_offset_ticks', 2)
    
    # Optimization: Use Cache for Point
    info = SYMBOL_CACHE.get(symbol)
    if not info:
         info = mt5.symbol_info(symbol)
         if info: SYMBOL_CACHE[symbol] = info
    
    point = info.point if info else 0.0001
    offset_val = point * slippage_ticks
    
    requested_price = float(data.get('price', 0.0))
    if requested_price > 0:
        ex_price = requested_price
    else:
        # Marketable Limit: Ask + Offset (Buy), Bid - Offset (Sell)
        # This ensures we cross the spread to get filled, but caps our bad fill price.
        if action == 'BUY':
             ex_price = tick.ask + offset_val
        else:
             ex_price = tick.bid - offset_val
             
    # 7. TP/SL Calculation
    # User Request: NO Auto Defaults. Only if given.
    
    input_sl = float(data.get('sl', 0))
    input_tp = float(data.get('tp', 0))
    
    sl_price = 0.0
    tp_price = 0.0
    
    # Calculate SL
    if input_sl > 0:
        sl_price = input_sl
            
    # Calculate TP
    if input_tp > 0:
        tp_price = input_tp

    logger.info(f"Order Params: Price={ex_price:.5f}, SL={sl_price:.5f}, TP={tp_price:.5f}")

    req = {
        "action": action_type,
        "symbol": symbol,
        "volume": vol,
        "type": ot,
        "price": ex_price,
        "sl": sl_price,
        "tp": tp_price,
        "magic": MT5_CONF.get('magic_number', 0),
        "comment": "Unified-Bridge-Limit",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN, # Return allowed for pending
    }
    
    # ... (Order Sending with Retry)
    try:
        res = safe_order_send(req)
    except Exception as e:
        logger.error(f"MT5 Order Send Exception: {e}")
        return {"error": f"MT5 Exception: {e}"}

    if res is None:
        return {"error": "MT5 order_send returned None after retries"}
        
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        return {"error": f"MT5 Fail: {res.comment} ({res.retcode})"}
        
    # Calculate Slippage (Approximate since it's a Limit Order placed, fill might happen later)
    # But for Marketable Limit that fills instantly, res.price is the fill price of the DEAL?
    # No, for Pending Order, res.price is just the order price?
    # Actually for Marketable Limit that executes immediately, retcode is DONE_PARTIAL or DONE?
    # We will report the requested price.
    actual_price = res.price 
    if actual_price == 0: actual_price = ex_price # Pending order might not have fill price yet
    
    slippage = 0.0
    if ex_price > 0 and actual_price > 0:
        slippage = abs(actual_price - ex_price)

    return {
        "success": True, 
        "order": res.order, 
        "expected_price": ex_price, 
        "executed_price": actual_price, 
        "slippage": slippage
    }

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
        
        # Database Log (Enhanced)
        db.log_trade(
            "MT5", 
            data, 
            status, 
            duration, 
            details=str(res),
            expected_price=res.get('expected_price', 0.0),
            executed_price=res.get('executed_price', 0.0),
            slippage=res.get('slippage', 0.0)
        )
        
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
    serve(app, host="0.0.0.0", port=port, threads=12)
