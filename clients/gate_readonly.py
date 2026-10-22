"""Gate spot GET-only adapter. It has no order creation or cancellation API."""
import hashlib
import hmac
import re
import time
from urllib.parse import urlencode


class ReadOnlyError(RuntimeError):
    pass


def pair_name(symbol):
    pair = symbol.replace('/', '_') if type(symbol) is str else ''
    if not re.fullmatch(r'[A-Z0-9]{2,20}_USDT', pair):
        raise ValueError('unsupported spot pair')
    return pair


class GateReadOnly:
    HOST = 'https://api.gateio.ws'

    def __init__(self, api_key='', api_secret='', session=None):
        if session is None:
            import requests
            session = requests.Session()
            session.trust_env = False
        self.requests_made = []
        self.session = session
        self._key = api_key
        self._secret = api_secret

    def _get(self, path, params=None, private=False):
        # All URLs are assembled from a fixed host and strict endpoint allowlist.
        permitted = path in ('/spot/tickers', '/spot/accounts', '/spot/orders', '/spot/my_trades',
                             '/spot/currency_pairs') or re.fullmatch(r'/spot/orders/[0-9]{1,32}', path)
        if not permitted:
            raise ReadOnlyError('endpoint_not_allowed')
        if private and (not self._key or not self._secret):
            raise ReadOnlyError('missing_credentials')
        query = urlencode(sorted((params or {}).items()))
        api_path = '/api/v4' + path
        headers = {'Accept': 'application/json'}
        if private:
            stamp = str(int(time.time()))
            payload = '\n'.join(('GET', api_path, query, hashlib.sha512(b'').hexdigest(), stamp))
            headers.update(KEY=self._key, Timestamp=stamp,
                           SIGN=hmac.new(self._secret.encode(), payload.encode(), hashlib.sha512).hexdigest())
        try:
            self.requests_made.append({'method': 'GET', 'path': path})
            response = self.session.request('GET', self.HOST + api_path + ('?' + query if query else ''),
                                            headers=headers, timeout=(5, 15), allow_redirects=False)
            if response.status_code != 200:
                raise ReadOnlyError('http_' + str(response.status_code))
            return response.json()
        except ReadOnlyError:
            raise
        except Exception:
            # Do not expose signed request headers, credentials or response text.
            raise ReadOnlyError('transport_or_decode_error') from None

    def ticker(self, symbol):
        rows = self._get('/spot/tickers', {'currency_pair': pair_name(symbol)})
        if type(rows) is not list or len(rows) != 1:
            raise ReadOnlyError('invalid_ticker')
        return rows[0]

    def accounts(self):
        rows = self._get('/spot/accounts', private=True)
        if type(rows) is not list:
            raise ReadOnlyError('invalid_accounts')
        return rows

    def orders(self, symbol, status='finished'):
        if status not in ('finished', 'open'):
            raise ValueError('unsupported order list status')
        rows = self._get('/spot/orders', {'currency_pair': pair_name(symbol), 'status': status,
                                        'account': 'spot', 'limit': 10}, private=True)
        if type(rows) is not list:
            raise ReadOnlyError('invalid_orders')
        return rows

    def order(self, order_id, symbol):
        if type(order_id) is not str or not re.fullmatch(r'[0-9]{1,32}', order_id):
            raise ValueError('invalid order id')
        row = self._get('/spot/orders/' + order_id, {'currency_pair': pair_name(symbol),
                                                   'account': 'spot'}, private=True)
        if type(row) is not dict:
            raise ReadOnlyError('invalid_order')
        return row

    def trades(self, order_id, symbol):
        if type(order_id) is not str or not re.fullmatch(r'[0-9]{1,32}', order_id):
            raise ValueError('invalid order id')
        rows = []
        for page in range(1, 101):
            batch = self._get('/spot/my_trades', {'currency_pair': pair_name(symbol), 'order_id': order_id,
                                                 'limit': 1000, 'page': page}, private=True)
            if type(batch) is not list:
                raise ReadOnlyError('invalid_trades')
            rows.extend(batch)
            if len(batch) < 1000:
                return rows
        raise ReadOnlyError('trade_pagination_limit')
