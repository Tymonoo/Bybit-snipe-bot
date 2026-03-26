import json
import requests
import urllib3
import time
import hmac
import hashlib
import os
import asyncio
import logging
from urllib.parse import urlencode
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.filters import Command
import re

# Load API keys from bybit.env file
load_dotenv('bybit.env')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
apiKey = os.getenv("BYBIT_API_KEY")
secret = os.getenv("BYBIT_API_SECRET")

if TELEGRAM_TOKEN is None or apiKey is None or secret is None:
    logging.error("One or more environment variables are not set.")
    raise ValueError("Required environment variables are not set.")

secret = secret.encode('utf-8') if secret else None

# Initialize bot and dispatcher
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

BASE_URL = "https://api.bybit.com/v5"
recvWindow = "5000" 

# Wzorce do wyszukiwania tickerów
PATTERNS = [
    r'\b[A-Z]{3,10}USDT\b',  # Dla par USDT
    r'\([A-Z]{3,10}\)',      # Dla tickerów w nawiasach
]

# Konfiguracja logowania
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Ustawienie marginu i dźwigni jako stałe wartości
MARGIN = 1  # Przykładowa wartość marginu jak chcemy margin 200 to tu 200
LEVERAGE = 20  # Przykładowa wartość dźwigni i tak ją ustawia z konta na bybit

def get_symbols_info():
    url = f"{BASE_URL}/market/instruments-info"
    params = {
        "category": "linear"
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()['result']['list']

def get_current_price(symbol):
    url = f"{BASE_URL}/market/tickers"
    params = {
        "category": "linear",
        "symbol": symbol
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return float(response.json()['result']['list'][0]['lastPrice'])

def create_order_with_stop_loss(apiKey, secretKey, symbol, side, order_type, qty, price):
    try:
        if secretKey is None:
            raise ValueError("Secret key is not set.")

        timestamp = int(time.time() * 1000)
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "price": price if order_type != "Market" else "",
            "timeInForce": "PostOnly" if order_type == "Limit" else "IOC",
        }
        
        query_string = urlencode({k: str(v).lower() if isinstance(v, bool) else v for k, v in sorted(params.items())})
        sign = f"{timestamp}{apiKey}{recvWindow}{json.dumps(params, separators=(',', ':'))}"
        
        hash = hmac.new(secretKey, sign.encode("utf-8"), hashlib.sha256)
        signature = hash.hexdigest()
        
        headers = {
            'X-BAPI-API-KEY': apiKey,
            'X-BAPI-SIGN': signature,
            'X-BAPI-SIGN-TYPE': '2',
            'X-BAPI-TIMESTAMP': str(timestamp),
            'X-BAPI-RECV-WINDOW': recvWindow,
            'Content-Type': 'application/json'
        }
        
        order_url = f"{BASE_URL}/order/create"
        order_body = json.dumps(params, separators=(',', ':'))
        
        order_response = requests.post(order_url, data=order_body, headers=headers, verify=False)
        order_response.raise_for_status()
        
        logging.info(f"Order Response status code: {order_response.status_code}")
        logging.info(f"Order Response text: {order_response.text}")
        
        # Ustawienie stop loss po pomyślnym złożeniu zlecenia
        if "retCode" in order_response.json() and order_response.json()["retCode"] == 0:
            # Założenie, że cena wejścia to cena zlecenia dla zleceń limitowych, lub aktualna cena rynkowa dla zleceń rynkowych
            entry_price = float(price) if order_type == "Limit" else float(get_current_price(symbol))
            # Stop loss na 50% straty przy dźwigni 20 to spadek o 2.5% (bo 1/20 * 50% = 2.5%)
            stop_loss_price = entry_price * 0.975 if side == "Buy" else entry_price * 1.025

           # Ustawienie take profit na 200% zysku
            take_profit_price = entry_price * 1.1 if side == "Buy" else entry_price * 0.95  # 200% zysku dla long, 50%strata dla short
            
            stop_loss_params = {
                "category": "linear",
                "symbol": symbol,
                "stopLoss": str(stop_loss_price),
                "takeProfit": str(take_profit_price),
                "positionIdx": 0  # Dla pojedynczej pozycji, dla hedged mode może być 1 lub 2
            }
            
            stop_loss_sign = f"{timestamp}{apiKey}{recvWindow}{json.dumps(stop_loss_params, separators=(',', ':'))}"
            
            stop_loss_hash = hmac.new(secretKey, stop_loss_sign.encode("utf-8"), hashlib.sha256)
            stop_loss_signature = stop_loss_hash.hexdigest()
            
            stop_loss_headers = {
                'X-BAPI-API-KEY': apiKey,
                'X-BAPI-SIGN': stop_loss_signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': str(timestamp),
                'X-BAPI-RECV-WINDOW': recvWindow,
                'Content-Type': 'application/json'
            }
            
            stop_loss_url = f"{BASE_URL}/position/trading-stop"
            stop_loss_body = json.dumps(stop_loss_params, separators=(',', ':'))
            
            stop_loss_response = requests.post(stop_loss_url, data=stop_loss_body, headers=stop_loss_headers, verify=False)
            stop_loss_response.raise_for_status()
            
            logging.info(f"Stop Loss Response status code: {stop_loss_response.status_code}")
            logging.info(f"Stop Loss Response text: {stop_loss_response.text}")
        
        return order_response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Request Exception: {e}")
        if hasattr(e, 'response'):
            logging.error(f"Response status code: {e.response.status_code}")
            logging.error(f"Response text: {e.response.text}")
        return None
    except json.JSONDecodeError:
        logging.error("Failed to decode JSON response")
        return None
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return None
def get_position_pnl(apiKey, secretKey, symbol):
    try:
        if secretKey is None:
            raise ValueError("Secret key is not set.")

        timestamp = int(time.time() * 1000)
        params = {
            "category": "linear",
            "symbol": symbol
        }
        
        query_string = urlencode({k: str(v).lower() if isinstance(v, bool) else v for k, v in sorted(params.items())})
        sign = f"{timestamp}{apiKey}{recvWindow}{json.dumps(params, separators=(',', ':'))}"
        
        hash = hmac.new(secretKey, sign.encode("utf-8"), hashlib.sha256)
        signature = hash.hexdigest()
        
        headers = {
            'X-BAPI-API-KEY': apiKey,
            'X-BAPI-SIGN': signature,
            'X-BAPI-SIGN-TYPE': '2',
            'X-BAPI-TIMESTAMP': str(timestamp),
            'X-BAPI-RECV-WINDOW': recvWindow,
            'Content-Type': 'application/json'
        }
        
        url = f"{BASE_URL}/position/list"
        response = requests.get(url, headers=headers, verify=False)
        response.raise_for_status()
        
        result = response.json()['result']['list']
        for pos in result:
            if pos['symbol'] == symbol:
                return pos['unrealisedPnl']
        
        return None  # Jeśli nie znaleziono pozycji dla danego symbolu
    except requests.exceptions.RequestException as e:
        logging.error(f"Request Exception while getting PNL: {e}")
        return None
    except json.JSONDecodeError:
        logging.error("Failed to decode JSON response for PNL")
        return None
    except Exception as e:
        logging.error(f"An error occurred while getting PNL: {e}")
        return None

@dp.message()
async def handle_message(message: Message):
    logging.info(f"Odebrano wiadomość: {message.text}")
    found_tickers = []
    for pattern in PATTERNS:
        found_tickers.extend(re.findall(pattern, message.text))

    found_tickers = list(set(found_tickers))
    logging.info(f"Znalezione tickery: {found_tickers}")
    
    if found_tickers:
        symbols_info = get_symbols_info()
        for ticker in found_tickers:
            if ticker.startswith('(') and ticker.endswith(')'):
                ticker = ticker[1:-1]
            if 'USDT' in ticker:
                ticker = ticker.replace('USDT', '').strip()
            symbol = f"{ticker}USDT"
            
            symbol_info = next((item for item in symbols_info if item['symbol'] == symbol), None)
            if not symbol_info:
                logging.error(f"No information found for symbol {symbol}")
                await message.answer(f"Nie znaleziono informacji dla symbolu {symbol}.")
                continue
            
            # Pobranie aktualnej ceny
            price = get_current_price(symbol)
            
            # Obliczanie wielkości pozycji (quantity) na podstawie marginu, dźwigni i aktualnej ceny
            position_value = MARGIN * LEVERAGE
            quantity = position_value / price
            
            # Dopasowanie do minimalnej jednostki handlowej
            lot_size_filter = symbol_info['lotSizeFilter']
            min_qty = float(lot_size_filter['minOrderQty'])
            qty_step = float(lot_size_filter['qtyStep'])
            adjusted_qty = min_qty + (quantity // qty_step) * qty_step
            
            await message.answer(f"Rozpoznano ticker: {ticker}. Ustawiam pozycję długą z marginem {MARGIN} i dźwignią {LEVERAGE}...")
            response = create_order_with_stop_loss(apiKey, secret, symbol, "Buy", "Market", str(adjusted_qty), "0")
            
            if response is not None:
                if "error" in response:
                    logging.error(f"Błąd API dla {ticker}: {response['error']}")
                    await message.answer(f"Nie udało się ustawić pozycji dla {ticker}. Błąd: {response['error']}")
                else:
                    logging.info(f"Rezultat z BYBIT API dla {ticker}: {response}")
                    await message.answer(f"Wynik z API BYBIT dla {ticker}: {response}")
                    
                    # Pobranie i wysłanie PNL
                    pnl = get_position_pnl(apiKey, secret, symbol)
                    if pnl is not None:
                        await message.answer(f"Aktualny PNL dla {symbol}: {pnl}")
                    else:
                        await message.answer(f"Nie udało się pobrać PNL dla {symbol}")
            else:
                logging.error(f"Response is None for ticker: {ticker}")
                await message.answer(f"Nie udało się uzyskać odpowiedzi z API dla {ticker}")
    else:
        await message.answer("Nie znaleziono żadnego tickera w wiadomości.")

async def main():
    logging.info("Bot is running and polling...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
