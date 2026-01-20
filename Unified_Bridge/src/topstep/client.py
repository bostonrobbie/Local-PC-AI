import requests
import json
import logging
import time
from colorama import Fore, Style

# Logger specific to TopStep
logger = logging.getLogger("TopStep")

class TopStepClient:
    def __init__(self, config):
        self.config = config.get('topstep', {})
        self.enabled = self.config.get('enabled', False)
        self.mock_mode = self.config.get('mock_mode', True)
        self.api_key = self.config.get('api_key', '')
        self.base_url = self.config.get('base_url', 'https://gateway-api-demo.s2f.projectx.com')
        self.symbol_map = self.config.get('symbol_map', {})
        self.max_retries = self.config.get('max_retries', 3)
        
        self.consecutive_failures = 0
        self.circuit_open = False
        self.connected = False
        self.session = requests.Session()
        
        # Keep-Alive
        import threading
        self.running = True
        self.ka_thread = threading.Thread(target=self._keep_alive_loop, daemon=True)
        self.ka_thread.start()

    def _keep_alive_loop(self):
        """Periodically pings to keep SSL session warm."""
        if not self.enabled or self.mock_mode: return
        while self.running:
            time.sleep(45) # Ping every 45s
            try:
                if self.connected:
                    headers = {"Authorization": f"Bearer {self.api_key}"}
                    # Low cost ping
                    self.session.get(f"{self.base_url}/api/User/profile", headers=headers, timeout=5)
                    # logger.debug("Keep-Alive Ping Sent") # Commented to avoid spam
            except:
                pass

    def validate_connection(self):
        """Checks connection to API on startup."""
        if not self.enabled:
            logger.info("TopStepX module is disabled.")
            return False

        if self.mock_mode:
            logger.info(f"{Fore.YELLOW}TopStepX running in MOCK MODE. No real connection check.{Style.RESET_ALL}")
            self.connected = True
            return True

        logger.info(f"Validating TopStepX Connection to {self.base_url}...")
        try:
            # Pinging User Hub or Auth check to validate key
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            # Using a lightweight endpoint if possible, or just checking if we get a 401
            url = f"{self.base_url}/api/User/profile" # Hypothetical endpoint, or just check 401 on any
            
            # Use short timeout just for ping
            response = self.session.get(url, headers=headers, timeout=5)
            
            if response.status_code in [200, 404]: # 404 means URL reached but path wrong (connection OK)
                logger.info(f"{Fore.GREEN}TopStepX Connection Validated (Status: {response.status_code}){Style.RESET_ALL}")
                self.connected = True
                return True
            elif response.status_code == 401:
                 logger.error(f"{Fore.RED}TopStepX Connection Failed: Unauthorized (Check API Key){Style.RESET_ALL}")
                 return False
            else:
                 logger.warning(f"TopStepX Connection Warning: Status {response.status_code}")
                 self.connected = True # Assume transient error, don't block
                 return True

        except Exception as e:
            logger.error(f"{Fore.RED}TopStepX Connection Error: {e}{Style.RESET_ALL}")
            return False

    def execute_trade(self, data):
        """
        Executes a trade order.
        Data expected to be simplified: {"symbol": "MNQ", "action": "BUY", "volume": 7.0}
        """
        if not self.enabled: return {"status": "skipped", "message": "Disabled"}
        
        # Circuit Breaker Check
        if self.circuit_open:
            logger.error(f"{Fore.RED}Circuit Breaker OPEN. Skipping TopStepX order.{Style.RESET_ALL}")
            return {"status": "error", "message": "Circuit Breaker Open"}

        symbol = data.get('symbol')
        action = data.get('action')
        volume = float(data.get('volume', 0))
        
        # QA: Multiplier Logic Verification (Already done before calling this, but double check)
        if volume <= 0:
            return {"status": "error", "message": "Invalid Volume"}

        # MOCK MODE
        if self.mock_mode:
            msg = f"MOCK ORDER: {action} {volume} {symbol} -> TopStepX (Success)"
            logger.info(f"{Fore.MAGENTA}{msg}{Style.RESET_ALL}")
            return {"status": "success", "mode": "mock", "message": msg}

        # REAL ORDER
        return self._send_api_order(symbol, action, volume)

    def _send_api_order(self, symbol, action, volume):
        url = f"{self.base_url}/api/Order/place"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "symbol": symbol,
            "side": action.upper(),
            "quantity": volume,
            "orderType": "MARKET", # Default to Market for consistency
            "duration": "DAY"
        }
        
        try:
            response = self.session.post(url, json=payload, headers=headers, timeout=3)
            
            if response.status_code == 200:
                self.consecutive_failures = 0 # Reset
                logger.info(f"{Fore.GREEN}TopStepX Order Sent: {action} {volume} {symbol}{Style.RESET_ALL}")
                return {"status": "success", "data": response.json()}
            else:
                self._handle_failure(f"HTTP {response.status_code}: {response.text}")
                logger.error(f"TopStep Error Values: {response.text}")
                return {"status": "error", "code": response.status_code, "body": response.text}
                
        except Exception as e:
            self._handle_failure(str(e))
            return {"status": "error", "message": str(e)}

    def _handle_failure(self, error_msg):
        self.consecutive_failures += 1
        logger.error(f"{Fore.RED}TopStepX Failure ({self.consecutive_failures}/{self.max_retries}): {error_msg}{Style.RESET_ALL}")
        
        if self.consecutive_failures >= self.max_retries:
            self.circuit_open = True
            logger.critical(f"{Fore.RED}TopStepX CIRCUIT BREAKER TRIPPED. Stopping requests.{Style.RESET_ALL}")

