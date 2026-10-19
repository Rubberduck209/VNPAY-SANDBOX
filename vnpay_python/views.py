import json
import urllib
import urllib.parse
import urllib.request
import random
import requests
from datetime import datetime
from django.conf import settings
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction as db_transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.utils.http import urlquote
from django.urls import reverse

from vnpay_python.forms import PaymentForm
from vnpay_python.models import PaymentTransaction
from vnpay_lib import VNPayCallbackError, VNPayClient, VNPayError


def index(request):
    transactions = PaymentTransaction.objects.all()
    succeeded = transactions.filter(status=PaymentTransaction.Status.SUCCEEDED)
    context = {
        'title': 'Tổng quan thanh toán',
        'transaction_count': transactions.count(),
        'pending_count': transactions.filter(
            status=PaymentTransaction.Status.PENDING
        ).count(),
        'succeeded_count': succeeded.count(),
        'failed_count': transactions.filter(
            status=PaymentTransaction.Status.FAILED
        ).count(),
        'succeeded_amount': succeeded.aggregate(total=Sum('amount'))['total'] or 0,
        'recent_transactions': transactions[:5],
    }
    return render(request, "index.html", context)


def transaction_history(request):
    query = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    transactions = PaymentTransaction.objects.all()

    if query:
        transactions = transactions.filter(
            Q(order_id__icontains=query)
            | Q(order_desc__icontains=query)
            | Q(transaction_no__icontains=query)
        )
    if status in dict(PaymentTransaction.Status.choices):
        transactions = transactions.filter(status=status)
    else:
        status = ''

    page = Paginator(transactions, 10).get_page(request.GET.get('page'))
    return render(request, 'transactions.html', {
        'title': 'Lịch sử giao dịch',
        'page': page,
        'query': query,
        'selected_status': status,
        'status_choices': PaymentTransaction.Status.choices,
    })


def _duplicate_order_message(payment_record):
    """Thông báo khi mã hóa đơn đã tồn tại. Mỗi lần thử thanh toán cần một mã mới."""
    if payment_record.status == PaymentTransaction.Status.SUCCEEDED:
        return 'Mã hóa đơn này đã được thanh toán.'
    if payment_record.status == PaymentTransaction.Status.FAILED:
        return (
            'Mã hóa đơn này đã thanh toán không thành công. '
            'Vui lòng dùng mã hóa đơn mới để thử lại.'
        )
    return (
        'Mã hóa đơn này đang chờ kết quả thanh toán. '
        'Vui lòng dùng mã hóa đơn mới hoặc chờ kết quả.'
    )


def payment(request):
    if request.method == 'POST':
        form = PaymentForm(request.POST)
        if form.is_valid():
            order_type = form.cleaned_data['order_type']
            order_id = form.cleaned_data['order_id']
            amount = form.cleaned_data['amount']
            order_desc = form.cleaned_data['order_desc']
            bank_code = form.cleaned_data['bank_code']
            language = form.cleaned_data['language']
            ipaddr = get_client_ip(request)
            existing_record = PaymentTransaction.objects.filter(order_id=order_id).first()
            if existing_record is not None:
                form.add_error('order_id', _duplicate_order_message(existing_record))
                return render(request, 'payment.html', {'title': 'Thanh toán', 'form': form})

            # Lưu đơn và tạo link trong cùng một transaction: nếu tạo link lỗi
            # (số tiền sai, cấu hình thiếu...) thì đơn pending cũng được rollback,
            # không để lại đơn "mồ côi" trong DB.
            try:
                with db_transaction.atomic():
                    PaymentTransaction.objects.create(
                        order_id=order_id,
                        amount=amount,
                        order_type=order_type,
                        order_desc=order_desc,
                        requested_bank_code=bank_code,
                    )
                    vnpay_client = VNPayClient(
                        settings.VNPAY_TMN_CODE,
                        settings.VNPAY_HASH_SECRET_KEY,
                        settings.VNPAY_PAYMENT_URL,
                    )
                    vnpay_payment_url = vnpay_client.create_payment_url(
                        amount=amount,
                        order_id=order_id,
                        order_info=order_desc,
                        return_url=settings.VNPAY_RETURN_URL,
                        ip_addr=ipaddr,
                        order_type=order_type,
                        locale=language or 'vn',
                        bank_code=bank_code,
                        created_at=datetime.now(),
                    )
            except IntegrityError:
                # Hai request cùng order_id chạy đồng thời: request đến sau thua ở ràng buộc unique.
                form.add_error('order_id', 'Mã đơn hàng này đã được sử dụng.')
                return render(request, 'payment.html', {'title': 'Thanh toán', 'form': form})
            except VNPayError as error:
                form.add_error(None, 'Không thể tạo yêu cầu thanh toán: {}'.format(error))
                return render(request, 'payment.html', {'title': 'Thanh toán', 'form': form})
            return redirect(vnpay_payment_url)
        return render(request, 'payment.html', {'title': 'Thanh toán', 'form': form})
    return render(request, 'payment.html', {'title': 'Thanh toán', 'form': PaymentForm()})


def _process_vnpay_callback(input_data):
    try:
        vnpay_client = VNPayClient(
            settings.VNPAY_TMN_CODE,
            settings.VNPAY_HASH_SECRET_KEY,
            settings.VNPAY_PAYMENT_URL,
        )
        callback_data = vnpay_client.verify_callback(input_data)
    except VNPayCallbackError as error:
        return None, error.code

    with db_transaction.atomic():
        payment_record = PaymentTransaction.objects.select_for_update().filter(
            order_id=callback_data['vnp_TxnRef']
        ).first()
        if payment_record is None:
            return None, 'unknown_order'
        callback_amount = int(callback_data['vnp_Amount'])
        if callback_amount != payment_record.amount * 100:
            return None, 'invalid_amount'
        if payment_record.status != PaymentTransaction.Status.PENDING:
            return payment_record, 'already_processed'

        response_code = callback_data['vnp_ResponseCode']
        # VNPay khuyến nghị coi giao dịch là thành công chỉ khi CẢ vnp_ResponseCode
        # và vnp_TransactionStatus đều là '00'. Nếu VNPay không gửi
        # vnp_TransactionStatus thì chỉ xét vnp_ResponseCode.
        transaction_status = callback_data.get('vnp_TransactionStatus', '00')
        is_paid = response_code == '00' and transaction_status == '00'
        payment_record.response_code = response_code
        payment_record.status = (
            PaymentTransaction.Status.SUCCEEDED
            if is_paid
            else PaymentTransaction.Status.FAILED
        )
        payment_record.transaction_no = callback_data.get('vnp_TransactionNo', '')
        payment_record.bank_code = callback_data.get('vnp_BankCode', '')
        payment_record.card_type = callback_data.get('vnp_CardType', '')
        payment_record.pay_date = callback_data.get('vnp_PayDate', '')
        payment_record.save()

    return payment_record, None


def payment_ipn(request):
    payment_record, error = _process_vnpay_callback(request.GET)
    error_responses = {
        'invalid_request': ('99', 'Invalid request'),
        'invalid_signature': ('97', 'Invalid signature'),
        'wrong_merchant': ('97', 'Invalid merchant'),
        'unknown_order': ('01', 'Order not found'),
        'invalid_amount': ('04', 'Invalid amount'),
        'already_processed': ('02', 'Order already confirmed'),
    }
    if error:
        response_code, message = error_responses[error]
        return JsonResponse({'RspCode': response_code, 'Message': message})
    return JsonResponse({'RspCode': '00', 'Message': 'Confirm Success'})


def payment_return(request):
    order_id = request.GET.get('vnp_TxnRef') or request.GET.get('order_id', '')
    if request.GET.get('vnp_TxnRef'):
        return redirect('{}?{}'.format(
            reverse('payment_return'),
            urllib.parse.urlencode({'order_id': order_id}),
        ))

    payment_record = get_object_or_404(PaymentTransaction, order_id=order_id)
    is_success = payment_record.status == PaymentTransaction.Status.SUCCEEDED
    is_pending = payment_record.status == PaymentTransaction.Status.PENDING
    is_failed = payment_record.status == PaymentTransaction.Status.FAILED
    return render(request, 'payment_return.html', {
        'title': (
            'Đang chờ xác nhận'
            if is_pending
            else 'Thanh toán thành công' if is_success else 'Thanh toán chưa thành công'
        ),
        'result': payment_record.get_status_display(),
        'order_id': payment_record.order_id,
        'amount': payment_record.amount,
        'order_desc': payment_record.order_desc,
        'vnp_TransactionNo': payment_record.transaction_no,
        'vnp_ResponseCode': payment_record.response_code,
        'is_success': is_success,
        'is_pending': is_pending,
        'is_failed': is_failed,
        'refresh_url': '{}?{}'.format(
            reverse('payment_return'),
            urllib.parse.urlencode({'order_id': payment_record.order_id}),
        ),
    })


def payment_status_label(response_code):
    status_map = {
        "00": "Thành công",
        "07": "Giao dịch bị nghi ngờ (cần đối soát với VNPay)",
        "09": "Thẻ/Tài khoản chưa đăng ký dịch vụ Internet Banking",
        "10": "Xác thực thông tin thẻ/tài khoản sai quá số lần quy định",
        "11": "Đã hết hạn chờ thanh toán",
        "12": "Thẻ/Tài khoản bị khóa",
        "13": "Nhập sai mật khẩu xác thực giao dịch (OTP)",
        "24": "Khách hàng hủy giao dịch",
        "51": "Tài khoản không đủ số dư",
        "65": "Tài khoản vượt quá hạn mức giao dịch trong ngày",
        "75": "Ngân hàng thanh toán đang bảo trì",
        "79": "Nhập sai mật khẩu thanh toán quá số lần quy định",
        "99": "Lỗi khác",
    }
    if not response_code:
        return 'Đang chờ kết quả thanh toán'
    return status_map.get(str(response_code), "Thanh toán không thành công")


def summary(request):
    payment_record = get_object_or_404(
        PaymentTransaction, order_id=request.GET.get('order_id', '')
    )
    is_success = payment_record.status == PaymentTransaction.Status.SUCCEEDED
    context = {
        'title': 'Giao dịch thành công' if is_success else 'Giao dịch chưa thành công',
        'order_id': payment_record.order_id,
        'amount': payment_record.amount,
        'order_desc': payment_record.order_desc,
        'vnp_TransactionNo': payment_record.transaction_no,
        'vnp_ResponseCode': payment_record.response_code,
        'payment_status': (
            'Thanh toán không thành công'
            if payment_record.status == PaymentTransaction.Status.FAILED
            and payment_record.response_code == '00'
            else payment_status_label(payment_record.response_code)
        ),
        'is_success': is_success,
    }
    return render(request, "summary.html", context)


def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip

n = random.randint(10**11, 10**12 - 1)
n_str = str(n)
while len(n_str) < 12:
    n_str = '0' + n_str


def query(request):
    if request.method == 'GET':
        return render(request, "query.html", {"title": "Kiểm tra kết quả giao dịch"})

    url = settings.VNPAY_API_URL
    secret_key = settings.VNPAY_HASH_SECRET_KEY
    vnp_TmnCode = settings.VNPAY_TMN_CODE
    vnp_CreateDate = datetime.now().strftime('%Y%m%d%H%M%S')
    vnp_IpAddr = get_client_ip(request)

    vnpay_client = VNPayClient(vnp_TmnCode, secret_key, settings.VNPAY_PAYMENT_URL)
    data = vnpay_client.build_query_request(
        request_id=n_str,
        order_id=request.POST['order_id'],
        transaction_date=request.POST['trans_date'],
        create_date=vnp_CreateDate,
        ip_addr=vnp_IpAddr,
    )

    headers = {"Content-Type": "application/json"}

    response = requests.post(url, headers=headers, data=json.dumps(data))

    if response.status_code == 200:
        response_json = json.loads(response.text)
    else:
        response_json = {"error": f"Request failed with status code: {response.status_code}"}

    return render(request, "query.html", {"title": "Kiểm tra kết quả giao dịch", "response_json": response_json})

def refund(request):
    if request.method == 'GET':
        return render(request, "refund.html", {"title": "Hoàn tiền giao dịch"})

    url = settings.VNPAY_API_URL
    secret_key = settings.VNPAY_HASH_SECRET_KEY
    vnp_TmnCode = settings.VNPAY_TMN_CODE
    vnp_CreateDate = datetime.now().strftime('%Y%m%d%H%M%S')
    vnp_IpAddr = get_client_ip(request)

    vnpay_client = VNPayClient(vnp_TmnCode, secret_key, settings.VNPAY_PAYMENT_URL)
    data = vnpay_client.build_refund_request(
        request_id=n_str,
        transaction_type=request.POST['TransactionType'],
        order_id=request.POST['order_id'],
        amount=request.POST['amount'],
        transaction_date=request.POST['trans_date'],
        create_date=vnp_CreateDate,
        ip_addr=vnp_IpAddr,
        order_info=request.POST['order_desc'],
    )

    headers = {"Content-Type": "application/json"}

    response = requests.post(url, headers=headers, data=json.dumps(data))

    if response.status_code == 200:
        response_json = json.loads(response.text)
    else:
        response_json = {"error": f"Request failed with status code: {response.status_code}"}

    return render(request, "refund.html", {"title": "Kết quả hoàn tiền giao dịch", "response_json": response_json})