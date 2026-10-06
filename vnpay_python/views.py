import hashlib
import hmac
import json
import urllib
import urllib.parse
import urllib.request
import random
import requests
from datetime import datetime
from django.conf import settings
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.utils.http import urlquote
from django.urls import reverse

from vnpay_python.forms import PaymentForm
from vnpay_python.models import PaymentTransaction
from vnpay_python.vnpay import vnpay


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


def hmacsha512(key, data):
    byteKey = key.encode('utf-8')
    byteData = data.encode('utf-8')
    return hmac.new(byteKey, byteData, hashlib.sha512).hexdigest()


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
            if PaymentTransaction.objects.filter(order_id=order_id).exists():
                form.add_error('order_id', 'Mã đơn hàng này đã được sử dụng.')
                return render(request, 'payment.html', {'title': 'Thanh toán', 'form': form})

            PaymentTransaction.objects.create(
                order_id=order_id,
                amount=amount,
                order_type=order_type,
                order_desc=order_desc,
                requested_bank_code=bank_code,
            )

            vnp = vnpay()
            vnp.requestData['vnp_Version'] = '2.1.0'
            vnp.requestData['vnp_Command'] = 'pay'
            vnp.requestData['vnp_TmnCode'] = settings.VNPAY_TMN_CODE
            vnp.requestData['vnp_Amount'] = amount * 100
            vnp.requestData['vnp_CurrCode'] = 'VND'
            vnp.requestData['vnp_TxnRef'] = order_id
            vnp.requestData['vnp_OrderInfo'] = order_desc
            vnp.requestData['vnp_OrderType'] = order_type
            # Check language, default: vn
            if language and language != '':
                vnp.requestData['vnp_Locale'] = language
            else:
                vnp.requestData['vnp_Locale'] = 'vn'
                # Check bank_code, if bank_code is empty, customer will be selected bank on VNPAY
            if bank_code and bank_code != "":
                vnp.requestData['vnp_BankCode'] = bank_code

            vnp.requestData['vnp_CreateDate'] = datetime.now().strftime('%Y%m%d%H%M%S')  # 20150410063022
            vnp.requestData['vnp_IpAddr'] = ipaddr
            vnp.requestData['vnp_ReturnUrl'] = settings.VNPAY_RETURN_URL
            vnpay_payment_url = vnp.get_payment_url(settings.VNPAY_PAYMENT_URL, settings.VNPAY_HASH_SECRET_KEY)
            return redirect(vnpay_payment_url)
        return render(request, 'payment.html', {'title': 'Thanh toán', 'form': form})
    return render(request, 'payment.html', {'title': 'Thanh toán', 'form': PaymentForm()})


def _process_vnpay_callback(input_data):
    required_fields = (
        'vnp_SecureHash', 'vnp_TxnRef', 'vnp_Amount',
        'vnp_ResponseCode', 'vnp_TmnCode',
    )
    if any(field not in input_data for field in required_fields):
        return None, 'invalid_request'

    callback_data = input_data.dict()
    vnp = vnpay()
    vnp.responseData = callback_data.copy()
    if not vnp.validate_response(settings.VNPAY_HASH_SECRET_KEY):
        return None, 'invalid_signature'
    if callback_data['vnp_TmnCode'] != settings.VNPAY_TMN_CODE:
        return None, 'wrong_merchant'

    try:
        callback_amount = int(callback_data['vnp_Amount'])
    except (TypeError, ValueError):
        return None, 'invalid_amount'

    with db_transaction.atomic():
        payment_record = PaymentTransaction.objects.select_for_update().filter(
            order_id=callback_data['vnp_TxnRef']
        ).first()
        if payment_record is None:
            return None, 'unknown_order'
        if callback_amount != payment_record.amount * 100:
            return None, 'invalid_amount'
        if payment_record.status != PaymentTransaction.Status.PENDING:
            return payment_record, 'already_processed'

        response_code = callback_data['vnp_ResponseCode']
        payment_record.response_code = response_code
        payment_record.status = (
            PaymentTransaction.Status.SUCCEEDED
            if response_code == '00'
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
        "24": "Khách hàng hủy thanh toán",
        "04": "Giao dịch quá hạn hoặc không thành công",
        "05": "Giao dịch không thành công do xác thực",
        "09": "Thẻ/Tài khoản chưa đăng ký dịch vụ",
        "10": "Xác thực OTP thất bại",
        "11": "Giao dịch đã bị hủy",
        "12": "Thẻ/Tài khoản không đủ số dư",
        "13": "Khách hàng nhập sai thông tin",
        "15": "Khách hàng chưa đăng nhập",
        "47": "Tài khoản đã hết hạn",
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
        'payment_status': payment_status_label(payment_record.response_code),
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
    vnp_Version = '2.1.0'

    vnp_RequestId = n_str
    vnp_Command = 'querydr'
    vnp_TxnRef = request.POST['order_id']
    vnp_OrderInfo = 'kiem tra gd'
    vnp_TransactionDate = request.POST['trans_date']
    vnp_CreateDate = datetime.now().strftime('%Y%m%d%H%M%S')
    vnp_IpAddr = get_client_ip(request)

    hash_data = "|".join([
        vnp_RequestId, vnp_Version, vnp_Command, vnp_TmnCode,
        vnp_TxnRef, vnp_TransactionDate, vnp_CreateDate,
        vnp_IpAddr, vnp_OrderInfo
    ])

    secure_hash = hmac.new(secret_key.encode(), hash_data.encode(), hashlib.sha512).hexdigest()

    data = {
        "vnp_RequestId": vnp_RequestId,
        "vnp_TmnCode": vnp_TmnCode,
        "vnp_Command": vnp_Command,
        "vnp_TxnRef": vnp_TxnRef,
        "vnp_OrderInfo": vnp_OrderInfo,
        "vnp_TransactionDate": vnp_TransactionDate,
        "vnp_CreateDate": vnp_CreateDate,
        "vnp_IpAddr": vnp_IpAddr,
        "vnp_Version": vnp_Version,
        "vnp_SecureHash": secure_hash
    }

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
    vnp_RequestId = n_str
    vnp_Version = '2.1.0'
    vnp_Command = 'refund'
    vnp_TransactionType = request.POST['TransactionType']
    vnp_TxnRef = request.POST['order_id']
    vnp_Amount = request.POST['amount']
    vnp_OrderInfo = request.POST['order_desc']
    vnp_TransactionNo = '0'
    vnp_TransactionDate = request.POST['trans_date']
    vnp_CreateDate = datetime.now().strftime('%Y%m%d%H%M%S')
    vnp_CreateBy = 'user01'
    vnp_IpAddr = get_client_ip(request)

    hash_data = "|".join([
        vnp_RequestId, vnp_Version, vnp_Command, vnp_TmnCode, vnp_TransactionType, vnp_TxnRef,
        vnp_Amount, vnp_TransactionNo, vnp_TransactionDate, vnp_CreateBy, vnp_CreateDate,
        vnp_IpAddr, vnp_OrderInfo
    ])

    secure_hash = hmac.new(secret_key.encode(), hash_data.encode(), hashlib.sha512).hexdigest()

    data = {
        "vnp_RequestId": vnp_RequestId,
        "vnp_TmnCode": vnp_TmnCode,
        "vnp_Command": vnp_Command,
        "vnp_TxnRef": vnp_TxnRef,
        "vnp_Amount": vnp_Amount,
        "vnp_OrderInfo": vnp_OrderInfo,
        "vnp_TransactionDate": vnp_TransactionDate,
        "vnp_CreateDate": vnp_CreateDate,
        "vnp_IpAddr": vnp_IpAddr,
        "vnp_TransactionType": vnp_TransactionType,
        "vnp_TransactionNo": vnp_TransactionNo,
        "vnp_CreateBy": vnp_CreateBy,
        "vnp_Version": vnp_Version,
        "vnp_SecureHash": secure_hash
    }

    headers = {"Content-Type": "application/json"}

    response = requests.post(url, headers=headers, data=json.dumps(data))

    if response.status_code == 200:
        response_json = json.loads(response.text)
    else:
        response_json = {"error": f"Request failed with status code: {response.status_code}"}

    return render(request, "refund.html", {"title": "Kết quả hoàn tiền giao dịch", "response_json": response_json})