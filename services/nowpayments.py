"""Typed wrappers around external crypto payment APIs."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests

from config import APIConfig, BotConfig

logger = logging.getLogger(__name__)


class NowPaymentsError(RuntimeError):
    """Raised when a NOWPayments request fails."""


class CoinGeckoError(RuntimeError):
    """Raised when the CoinGecko price API fails."""


@dataclass
class PaymentInvoice:
    """Simplified representation of a NOWPayments invoice."""

    payment_id: str
    pay_address: str
    pay_amount: Decimal
    price_amount: Decimal
    price_currency: str
    pay_currency: str


CRYPTO_TO_COINGECKO_ID = {
    'btc': 'bitcoin',
    'ltc': 'litecoin',
}


class CoinGeckoClient:
    """Lightweight CoinGecko wrapper with back-off on rate limit issues."""

    def __init__(self, base_url: str | None = None, timeout: int | None = None):
        self.base_url = (base_url or BotConfig.COINGECKO_API_BASE).rstrip('/')
        self.timeout = timeout or APIConfig.COINGECKO_TIMEOUT
        self.session = requests.Session()

    def convert_to_crypto(self, fiat_amount: Decimal, currency: str, crypto_symbol: str) -> Decimal:
        """Convert the given fiat amount to a crypto amount."""

        crypto_symbol = crypto_symbol.lower()
        crypto_id = CRYPTO_TO_COINGECKO_ID.get(crypto_symbol, crypto_symbol)
        currency = currency.lower()
        endpoint = f"{self.base_url}/simple/price"
        params = {
            'ids': crypto_id,
            'vs_currencies': currency,
        }

        last_error: Optional[Exception] = None
        for attempt in range(APIConfig.MAX_RETRIES):
            try:
                response = self.session.get(endpoint, params=params, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
                if crypto_id not in payload or currency not in payload[crypto_id]:
                    raise CoinGeckoError(f"Missing price for {crypto_symbol}/{currency}")
                price = Decimal(str(payload[crypto_id][currency]))
                if price <= 0:
                    raise CoinGeckoError(f"Invalid price received: {price}")
                return (fiat_amount / price).quantize(Decimal('0.00000001'))
            except (requests.RequestException, ValueError, InvalidOperation, CoinGeckoError) as error:
                last_error = error
                logger.warning("CoinGecko request failed (attempt %s/%s): %s", attempt + 1, APIConfig.MAX_RETRIES, error)
                time.sleep(APIConfig.RETRY_DELAY)

        raise CoinGeckoError(f"Unable to fetch {crypto_symbol.upper()} price: {last_error}")


class NowPaymentsClient:
    """NOWPayments API wrapper providing typed helpers."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: int | None = None,
    ):
        if not api_key:
            raise NowPaymentsError("NOWPayments API key is required")

        self.api_key = api_key
        self.base_url = (base_url or BotConfig.NOWPAYMENTS_API_BASE).rstrip('/')
        self.timeout = timeout or APIConfig.NOWPAYMENTS_TIMEOUT
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = APIConfig.get_headers(self.api_key)
        headers.setdefault('Accept', 'application/json')
        return headers

    def create_invoice(
        self,
        price_amount: Decimal,
        price_currency: str,
        pay_currency: str,
        description: str,
        order_id: Optional[str] = None,
    ) -> PaymentInvoice:
        """Create a NOWPayments invoice and return its details."""

        payload: dict[str, object] = {
            'price_amount': float(price_amount),
            'price_currency': price_currency.lower(),
            'pay_currency': pay_currency.lower(),
            'order_description': description,
        }
        if order_id:
            payload['order_id'] = str(order_id)

        endpoint = f"{self.base_url}/payment"
        try:
            response = self.session.post(endpoint, json=payload, headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as error:
            raise NowPaymentsError(f"Failed to create invoice: {error}") from error

        data = response.json()
        payment_id = data.get('payment_id')
        pay_address = data.get('pay_address')
        pay_amount = data.get('pay_amount')

        if not payment_id or not pay_address or pay_amount is None:
            raise NowPaymentsError(f"Unexpected NOWPayments response: {data}")

        return PaymentInvoice(
            payment_id=str(payment_id),
            pay_address=str(pay_address),
            pay_amount=Decimal(str(pay_amount)),
            price_amount=Decimal(str(data.get('price_amount', price_amount))),
            price_currency=payload['price_currency'],
            pay_currency=payload['pay_currency'],
        )

    def get_payment_status(self, payment_id: str) -> str:
        """Fetch the status of a previously created invoice."""

        endpoint = f"{self.base_url}/payment/{payment_id}"
        try:
            response = self.session.get(endpoint, headers=self._headers(), timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as error:
            raise NowPaymentsError(f"Failed to fetch payment {payment_id}: {error}") from error

        payload = response.json()
        status = payload.get('payment_status')
        if not status:
            raise NowPaymentsError(f"Payment {payment_id} returned unexpected payload: {payload}")
        return str(status)
