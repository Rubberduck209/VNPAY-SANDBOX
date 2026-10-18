import hashlib
import hmac
from urllib.parse import parse_qs, quote_plus, urlencode, urlsplit

from django.test import TestCase, override_settings
from django.urls import reverse

from vnpay_lib import (
    VNPayCallbackError,
    VNPayClient,
    hmac_sha512,
    verify_response_signature,
)
from vnpay_python.models import PaymentTransaction
from vnpay_python.templatetags.currency import vnd


class VNPayLibraryTests(TestCase):
    def setUp(self):
        self.client = VNPayClient('test-merchant', 'test-secret')

    def callback(self, **overrides):
        values = {
            'vnp_TmnCode': 'test-merchant',
            'vnp_TxnRef': 'order-1001',
            'vnp_Amount': '1250000',
            'vnp_ResponseCode': '00',
        }
        values.update(overrides)
        signed_data = '&'.join(
            '{}={}'.format(key, quote_plus(str(value)))
            for key, value in sorted(values.items())
        )
        values['vnp_SecureHash'] = hmac_sha512('test-secret', signed_data)
        return values

    def test_payment_url_converts_vnd_and_adds_valid_signature(self):
        url = self.client.create_payment_url(
            amount=10000,
            order_id='order-1001',
            order_info='Test order',
            return_url='https://shop.example/payment/return',
            ip_addr='127.0.0.1',
            created_at='20261007120000',
        )
        parameters = {
            key: values[0]
            for key, values in parse_qs(urlsplit(url).query).items()
        }

        self.assertIn('vnp_Amount=1000000', url)
        self.assertEqual(parameters['vnp_TmnCode'], 'test-merchant')
        self.assertTrue(verify_response_signature(parameters, 'test-secret'))

    def test_callback_verification_checks_signature_merchant_amount_and_order(self):
        callback = self.callback()
        original_callback = callback.copy()

        verified = self.client.verify_callback(
            callback,
            expected_amount=12500,
            expected_order_id='order-1001',
        )

        self.assertEqual(verified['vnp_ResponseCode'], '00')
        self.assertEqual(callback, original_callback)
        with self.assertRaises(VNPayCallbackError) as amount_error:
            self.client.verify_callback(callback, expected_amount=12501)
        self.assertEqual(amount_error.exception.code, 'invalid_amount')
        with self.assertRaises(VNPayCallbackError) as order_error:
            self.client.verify_callback(callback, expected_order_id='different-order')
        self.assertEqual(order_error.exception.code, 'unknown_order')

    def test_callback_with_invalid_signature_is_rejected(self):
        callback = self.callback()
        callback['vnp_SecureHash'] = 'invalid'

        with self.assertRaises(VNPayCallbackError) as error:
            self.client.verify_callback(callback)

        self.assertEqual(error.exception.code, 'invalid_signature')

    def test_api_request_signing_uses_explicit_pipe_delimited_field_order(self):
        request_data = {
            'vnp_RequestId': 'request-1',
            'vnp_Command': 'querydr',
            'vnp_TxnRef': 'order-1001',
        }

        signed_request = self.client.sign_api_request(
            request_data,
            ('vnp_RequestId', 'vnp_Command', 'vnp_TxnRef'),
        )

        self.assertEqual(
            signed_request['vnp_SecureHash'],
            hmac_sha512('test-secret', 'request-1|querydr|order-1001'),
        )
        self.assertNotIn('vnp_SecureHash', request_data)

    def test_query_and_refund_builders_use_their_vnpay_field_orders(self):
        query = self.client.build_query_request(
            request_id='request-1',
            order_id='order-1001',
            transaction_date='20261007120000',
            create_date='20261007120100',
            ip_addr='127.0.0.1',
        )
        refund = self.client.build_refund_request(
            request_id='request-2',
            transaction_type='03',
            order_id='order-1001',
            amount='500000',
            transaction_date='20261007120000',
            create_date='20261007120200',
            ip_addr='127.0.0.1',
            order_info='Partial refund',
        )

        self.assertEqual(query['vnp_Command'], 'querydr')
        self.assertEqual(
            query['vnp_SecureHash'],
            hmac_sha512(
                'test-secret',
                'request-1|2.1.0|querydr|test-merchant|order-1001|'
                '20261007120000|20261007120100|127.0.0.1|kiem tra gd',
            ),
        )
        self.assertEqual(refund['vnp_Command'], 'refund')
        self.assertEqual(
            refund['vnp_SecureHash'],
            hmac_sha512(
                'test-secret',
                'request-2|2.1.0|refund|test-merchant|03|order-1001|'
                '500000|0|20261007120000|user01|20261007120200|'
                '127.0.0.1|Partial refund',
            ),
        )


@override_settings(
    VNPAY_TMN_CODE='test-merchant',
    VNPAY_HASH_SECRET_KEY='test-secret',
)
class PaymentCallbackTests(TestCase):
    def setUp(self):
        self.payment_record = PaymentTransaction.objects.create(
            order_id='order-1001',
            amount=12500,
            order_type='other',
            order_desc='Test payment',
        )

    def signed_callback(self, **overrides):
        callback = {
            'vnp_TmnCode': 'test-merchant',
            'vnp_TxnRef': self.payment_record.order_id,
            'vnp_Amount': '1250000',
            'vnp_ResponseCode': '00',
            'vnp_TransactionNo': '987654321',
            'vnp_BankCode': 'NCB',
            'vnp_CardType': 'ATM',
            'vnp_PayDate': '20261002123000',
        }
        callback.update(overrides)
        signed_data = '&'.join(
            '{}={}'.format(key, quote_plus(str(value)))
            for key, value in sorted(callback.items())
        )
        callback['vnp_SecureHash'] = hmac.new(
            b'test-secret', signed_data.encode(), hashlib.sha512
        ).hexdigest()
        return callback

    def test_valid_ipn_persists_success_and_duplicate_is_idempotent(self):
        url = reverse('payment_ipn')
        callback_url = '{}?{}'.format(url, urlencode(self.signed_callback()))

        response = self.client.get(callback_url)
        self.payment_record.refresh_from_db()
        self.assertEqual(response.json()['RspCode'], '00')
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(self.payment_record.status, PaymentTransaction.Status.SUCCEEDED)
        self.assertEqual(self.payment_record.transaction_no, '987654321')

        duplicate_response = self.client.get(callback_url)
        self.assertEqual(duplicate_response.json()['RspCode'], '02')

    def test_return_only_reads_database_and_redirects_to_clean_order_url(self):
        callback = self.signed_callback()
        response = self.client.get(reverse('payment_return'), callback)

        self.payment_record.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertIn('order_id=order-1001', response['Location'])
        self.assertNotIn('vnp_SecureHash', response['Location'])
        self.assertEqual(self.payment_record.status, PaymentTransaction.Status.PENDING)

        response = self.client.get(response['Location'])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Đang chờ VNPay xác nhận')
        self.assertContains(response, 'setTimeout')
        self.assertContains(response, 'Về tổng quan')

    def test_return_displays_success_only_after_ipn_updates_database(self):
        callback = self.signed_callback()
        ipn_response = self.client.get(
            '{}?{}'.format(reverse('payment_ipn'), urlencode(callback))
        )
        self.assertEqual(ipn_response.json()['RspCode'], '00')

        response = self.client.get(reverse('payment_return'), callback)
        self.assertEqual(response.status_code, 302)
        response = self.client.get(response['Location'])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ĐÃ XÁC NHẬN QUA IPN')
        self.assertContains(response, 'Thanh toán thành công')
        self.assertNotContains(response, 'setTimeout')
        self.payment_record.refresh_from_db()
        self.assertEqual(self.payment_record.status, PaymentTransaction.Status.SUCCEEDED)

    def test_invalid_signature_does_not_change_pending_payment(self):
        callback = self.signed_callback()
        callback['vnp_SecureHash'] = 'invalid'
        response = self.client.get(
            '{}?{}'.format(reverse('payment_ipn'), urlencode(callback))
        )

        self.payment_record.refresh_from_db()
        self.assertEqual(response.json()['RspCode'], '97')
        self.assertEqual(self.payment_record.status, PaymentTransaction.Status.PENDING)

    def test_amount_mismatch_is_rejected(self):
        callback = self.signed_callback(vnp_Amount='1250001')
        response = self.client.get(
            '{}?{}'.format(reverse('payment_ipn'), urlencode(callback))
        )

        self.payment_record.refresh_from_db()
        self.assertEqual(response.json()['RspCode'], '04')
        self.assertEqual(self.payment_record.status, PaymentTransaction.Status.PENDING)

    def test_summary_reads_status_from_database(self):
        self.payment_record.status = PaymentTransaction.Status.SUCCEEDED
        self.payment_record.response_code = '00'
        self.payment_record.transaction_no = '987654321'
        self.payment_record.save()

        response = self.client.get(
            reverse('summary'), {'order_id': self.payment_record.order_id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Giao dịch thành công')
        self.assertContains(response, '12.500 VND')
        self.assertContains(response, '987654321')

    def test_payment_amount_is_formatted_with_dot_thousands_separator(self):
        response = self.client.get(reverse('payment_return'), {'order_id': self.payment_record.order_id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '12.500 ₫')


class TransactionHistoryTests(TestCase):
    def setUp(self):
        self.pending = PaymentTransaction.objects.create(
            order_id='order-pending',
            amount=10000,
            order_type='other',
            order_desc='Pending test order',
        )
        self.succeeded = PaymentTransaction.objects.create(
            order_id='order-success',
            amount=25000,
            order_type='other',
            order_desc='Successful test order',
            status=PaymentTransaction.Status.SUCCEEDED,
            transaction_no='txn-25000',
            bank_code='NCB',
        )

    def test_dashboard_shows_database_counts_and_recent_transaction(self):
        response = self.client.get(reverse('index'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'order-success')
        self.assertContains(response, 'Tổng giao dịch')
        self.assertContains(response, '25.000 ₫')
        self.assertEqual(response.context['transaction_count'], 2)
        self.assertEqual(response.context['pending_count'], 1)
        self.assertEqual(response.context['succeeded_count'], 1)
        self.assertEqual(response.context['succeeded_amount'], 25000)

    def test_transaction_list_filters_database_records_by_status_and_search(self):
        response = self.client.get(
            reverse('transactions'),
            {'status': PaymentTransaction.Status.SUCCEEDED, 'q': 'txn-25000'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'order-success')
        self.assertContains(response, '25.000 ₫')
        self.assertNotContains(response, 'order-pending')
        self.assertEqual(response.context['page'].paginator.count, 1)

    def test_transaction_list_handles_invalid_page_and_empty_search(self):
        response = self.client.get(reverse('transactions'), {'page': 'invalid', 'q': 'missing'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Không tìm thấy giao dịch')
        self.assertEqual(response.context['page'].paginator.count, 0)

    def test_payment_tools_render_without_refund_navigation(self):
        for page_name in ('payment', 'query'):
            with self.subTest(page=page_name):
                response = self.client.get(reverse(page_name))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'VNPay')
                self.assertContains(response, 'Giao dịch')
                self.assertNotContains(response, 'Hoàn tiền')

    def test_currency_uses_dots_as_thousands_separator(self):
        self.assertEqual(vnd(500000), '500.000')