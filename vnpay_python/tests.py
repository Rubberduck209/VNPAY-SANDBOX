import hashlib
import hmac
from urllib.parse import quote_plus, urlencode

from django.test import TestCase, override_settings
from django.urls import reverse

from vnpay_python.models import PaymentTransaction


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

    def test_successful_return_shows_home_button_without_summary_redirect(self):
        response = self.client.get(
            reverse('payment_return'), self.signed_callback()
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Về trang chủ')
        self.assertNotContains(response, 'Đến trang tổng')
        self.assertNotContains(response, 'setTimeout')

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
        self.assertContains(response, '987654321')