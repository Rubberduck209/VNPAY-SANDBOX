"""Standalone VNPay payment-link, callback-verification, and API-signing helpers.

This module uses only the Python standard library. Website-specific routing,
order storage, and HTTP response handling remain the responsibility of the app.
"""

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import urlencode, urlsplit


DEFAULT_PAYMENT_URL = 'https://sandbox.vnpayment.vn/paymentv2/vpcpay.html'


class VNPayError(ValueError):
    """Raised when VNPay settings or request data are invalid."""


class VNPayCallbackError(VNPayError):
    """Raised when a VNPay callback cannot be trusted or matched."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def hmac_sha512(secret_key, data):
    """Return the lowercase HMAC-SHA512 hex digest for text input."""
    if not secret_key:
        raise VNPayError('VNPay Hash Secret Key is required.')
    return hmac.new(
        str(secret_key).encode('utf-8'),
        str(data).encode('utf-8'),
        hashlib.sha512,
    ).hexdigest()


def _as_mapping(data):
    if hasattr(data, 'dict'):
        data = data.dict()
    if not isinstance(data, Mapping):
        raise VNPayError('VNPay parameters must be a mapping.')
    return {str(key): str(value) for key, value in data.items()}


def _query_string(parameters):
    return urlencode(sorted(parameters.items()))


def build_payment_url(payment_url, parameters, hash_secret_key):
    """Sign caller-supplied VNPay payment parameters and return the URL."""
    payment_url = str(payment_url or '').strip()
    parsed_url = urlsplit(payment_url)
    if parsed_url.scheme not in ('http', 'https') or not parsed_url.netloc:
        raise VNPayError('VNPay payment URL must be an absolute HTTP(S) URL.')

    values = _as_mapping(parameters)
    values.pop('vnp_SecureHash', None)
    values.pop('vnp_SecureHashType', None)
    query_string = _query_string(values)
    secure_hash = hmac_sha512(hash_secret_key, query_string)
    separator = '&' if '?' in payment_url else '?'
    return '{}{}{}&vnp_SecureHash={}'.format(
        payment_url, separator, query_string, secure_hash
    )


def verify_response_signature(parameters, hash_secret_key):
    """Verify a signed VNPay callback without modifying its input mapping."""
    try:
        values = _as_mapping(parameters)
    except VNPayError:
        return False

    received_hash = values.get('vnp_SecureHash', '')
    if not received_hash:
        return False

    signed_values = {
        key: value
        for key, value in values.items()
        if key.startswith('vnp_')
        and key not in ('vnp_SecureHash', 'vnp_SecureHashType')
    }
    expected_hash = hmac_sha512(hash_secret_key, _query_string(signed_values))
    return hmac.compare_digest(received_hash.lower(), expected_hash)


class VNPayClient:
    """Build VNPay payment links and verify/sign VNPay integration data."""

    def __init__(
        self,
        tmn_code,
        hash_secret_key,
        payment_url=DEFAULT_PAYMENT_URL,
    ):
        self.tmn_code = str(tmn_code or '').strip()
        self.hash_secret_key = str(hash_secret_key or '')
        self.payment_url = str(payment_url or '').strip()
        if not self.tmn_code:
            raise VNPayError('VNPay TMN Code is required.')
        if not self.hash_secret_key:
            raise VNPayError('VNPay Hash Secret Key is required.')
        parsed_url = urlsplit(self.payment_url)
        if parsed_url.scheme not in ('http', 'https') or not parsed_url.netloc:
            raise VNPayError('VNPay payment URL must be an absolute HTTP(S) URL.')

    def build_payment_url(self, parameters):
        return build_payment_url(
            self.payment_url, parameters, self.hash_secret_key
        )

    def create_payment_url(
        self,
        amount,
        order_id,
        order_info,
        return_url,
        ip_addr,
        order_type='other',
        locale='vn',
        bank_code=None,
        created_at=None,
    ):
        """Create a payment URL; amount must be a positive integer in VND."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise VNPayError('Payment amount must be a positive integer in VND.')
        if order_id is None or not str(order_id).strip():
            raise VNPayError('VNPay order ID is required.')
        if return_url is None or not str(return_url).strip():
            raise VNPayError('VNPay return URL is required.')
        return_url_parts = urlsplit(str(return_url))
        if return_url_parts.scheme not in ('http', 'https') or not return_url_parts.netloc:
            raise VNPayError('VNPay return URL must be an absolute HTTP(S) URL.')
        if ip_addr is None or not str(ip_addr).strip():
            raise VNPayError('Customer IP address is required.')

        if created_at is None:
            created_at = datetime.now()
        if isinstance(created_at, datetime):
            created_at = created_at.strftime('%Y%m%d%H%M%S')

        parameters = {
            'vnp_Version': '2.1.0',
            'vnp_Command': 'pay',
            'vnp_TmnCode': self.tmn_code,
            'vnp_Amount': amount * 100,
            'vnp_CurrCode': 'VND',
            'vnp_TxnRef': order_id,
            'vnp_OrderInfo': order_info,
            'vnp_OrderType': order_type,
            'vnp_Locale': locale or 'vn',
            'vnp_CreateDate': created_at,
            'vnp_IpAddr': ip_addr,
            'vnp_ReturnUrl': return_url,
        }
        if bank_code:
            parameters['vnp_BankCode'] = bank_code
        return self.build_payment_url(parameters)

    def build_query_request(
        self,
        request_id,
        order_id,
        transaction_date,
        create_date,
        ip_addr,
        order_info='kiem tra gd',
        version='2.1.0',
    ):
        """Build and sign a VNPay querydr API request."""
        parameters = {
            'vnp_RequestId': request_id,
            'vnp_Version': version,
            'vnp_Command': 'querydr',
            'vnp_TmnCode': self.tmn_code,
            'vnp_TxnRef': order_id,
            'vnp_TransactionDate': transaction_date,
            'vnp_CreateDate': create_date,
            'vnp_IpAddr': ip_addr,
            'vnp_OrderInfo': order_info,
        }
        return self.sign_api_request(parameters, (
            'vnp_RequestId', 'vnp_Version', 'vnp_Command', 'vnp_TmnCode',
            'vnp_TxnRef', 'vnp_TransactionDate', 'vnp_CreateDate',
            'vnp_IpAddr', 'vnp_OrderInfo',
        ))

    def build_refund_request(
        self,
        request_id,
        transaction_type,
        order_id,
        amount,
        transaction_date,
        create_date,
        ip_addr,
        order_info,
        transaction_no='0',
        create_by='user01',
        version='2.1.0',
    ):
        """Build and sign a VNPay refund API request."""
        parameters = {
            'vnp_RequestId': request_id,
            'vnp_Version': version,
            'vnp_Command': 'refund',
            'vnp_TmnCode': self.tmn_code,
            'vnp_TransactionType': transaction_type,
            'vnp_TxnRef': order_id,
            'vnp_Amount': amount,
            'vnp_TransactionNo': transaction_no,
            'vnp_TransactionDate': transaction_date,
            'vnp_CreateBy': create_by,
            'vnp_CreateDate': create_date,
            'vnp_IpAddr': ip_addr,
            'vnp_OrderInfo': order_info,
        }
        return self.sign_api_request(parameters, (
            'vnp_RequestId', 'vnp_Version', 'vnp_Command', 'vnp_TmnCode',
            'vnp_TransactionType', 'vnp_TxnRef', 'vnp_Amount',
            'vnp_TransactionNo', 'vnp_TransactionDate', 'vnp_CreateBy',
            'vnp_CreateDate', 'vnp_IpAddr', 'vnp_OrderInfo',
        ))

    def verify_callback(
        self,
        parameters,
        expected_amount=None,
        expected_order_id=None,
    ):
        """Verify callback signature, merchant, required fields, and optional order data.

        expected_amount is expressed in VND, while VNPay's callback amount is
        expressed in VND multiplied by 100.
        """
        try:
            values = _as_mapping(parameters)
        except VNPayError as exc:
            raise VNPayCallbackError('invalid_request', str(exc)) from exc

        required_fields = (
            'vnp_SecureHash',
            'vnp_TxnRef',
            'vnp_Amount',
            'vnp_ResponseCode',
            'vnp_TmnCode',
        )
        if any(not values.get(field) for field in required_fields):
            raise VNPayCallbackError(
                'invalid_request', 'Required VNPay callback fields are missing.'
            )
        if not verify_response_signature(values, self.hash_secret_key):
            raise VNPayCallbackError(
                'invalid_signature', 'VNPay callback signature is invalid.'
            )
        if values['vnp_TmnCode'] != self.tmn_code:
            raise VNPayCallbackError(
                'wrong_merchant', 'VNPay merchant code does not match.'
            )

        try:
            callback_amount = int(values['vnp_Amount'])
        except (TypeError, ValueError) as exc:
            raise VNPayCallbackError(
                'invalid_amount', 'VNPay callback amount is invalid.'
            ) from exc
        if callback_amount <= 0:
            raise VNPayCallbackError(
                'invalid_amount', 'VNPay callback amount must be positive.'
            )
        if expected_amount is not None:
            if (
                isinstance(expected_amount, bool)
                or not isinstance(expected_amount, int)
                or expected_amount <= 0
                or callback_amount != expected_amount * 100
            ):
                raise VNPayCallbackError(
                    'invalid_amount', 'VNPay callback amount does not match the order.'
                )
        if (
            expected_order_id is not None
            and values['vnp_TxnRef'] != str(expected_order_id)
        ):
            raise VNPayCallbackError(
                'unknown_order', 'VNPay callback order ID does not match.'
            )
        return values

    def sign_api_request(self, parameters, hash_fields):
        """Sign a VNPay JSON API request using its documented pipe-separated fields.

        hash_fields must list the exact fields, in the exact order, specified
        for the API command being sent.
        """
        values = _as_mapping(parameters)
        values.pop('vnp_SecureHash', None)
        values.pop('vnp_SecureHashType', None)
        try:
            hash_data = '|'.join(values[field] for field in hash_fields)
        except KeyError as exc:
            raise VNPayError(
                'VNPay API signing field is missing: {}'.format(exc.args[0])
            ) from exc
        values['vnp_SecureHash'] = hmac_sha512(self.hash_secret_key, hash_data)
        return values
