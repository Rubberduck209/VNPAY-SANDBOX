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
        self.assertContains(response, '987654321')


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
        self.assertNotContains(response, 'order-pending')
        self.assertEqual(response.context['page'].paginator.count, 1)

    def test_transaction_list_handles_invalid_page_and_empty_search(self):
        response = self.client.get(reverse('transactions'), {'page': 'invalid', 'q': 'missing'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Không tìm thấy giao dịch')
        self.assertEqual(response.context['page'].paginator.count, 0)

    def test_payment_tools_render_with_shared_navigation(self):
        for page_name in ('payment', 'query', 'refund'):
            with self.subTest(page=page_name):
                response = self.client.get(reverse(page_name))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'VNPay')
                self.assertContains(response, 'Giao dịch')