from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from vnpay_lib import VNPayClient, VNPayCallbackError

# Khởi tạo Router đóng vai trò như một Controller độc lập
vnpay_router = APIRouter(prefix="/api/vnpay", tags=["VNPay Payment Gateway"])

# Cấu hình VNPay (Trong thực tế nên load từ biến môi trường/Config service)
VNPAY_TMN_CODE = "YOUR_TMN_CODE"
VNPAY_HASH_SECRET = "YOUR_HASH_SECRET"
VNPAY_PAYMENT_URL = "https://sandbox.vnpayment.vn/paymentv2/vpcpay.html"

vnpay = VNPayClient(
    tmn_code=VNPAY_TMN_CODE,
    hash_secret_key=VNPAY_HASH_SECRET,
    payment_url=VNPAY_PAYMENT_URL
)

# Giả lập Database để tracking trạng thái
fake_database = {}

# Định nghĩa DTO cho đầu vào
class PaymentRequestDTO(BaseModel):
    order_id: str
    amount: int
    order_info: str

@vnpay_router.post("/create-payment")
async def create_payment(request: Request, payload: PaymentRequestDTO):
    # 1. Lưu thông tin khởi tạo vào DB
    fake_database[payload.order_id] = {"amount": payload.amount, "status": "pending"}
    
    # 2. Lấy IP khách hàng (Xử lý trường hợp chạy sau API Gateway, Docker, Nginx, hoặc Load Balancer)
    client_ip = request.headers.get("X-Forwarded-For", request.client.host)
    
    # 3. Linh hoạt lấy Base URL hiện tại làm Return URL
    return_url = f"{request.base_url}api/vnpay/return"

    # 4. Tạo URL thanh toán
    payment_url = vnpay.create_payment_url(
        amount=payload.amount,
        order_id=payload.order_id,
        order_info=payload.order_info,
        return_url=return_url,
        ip_addr=client_ip
    )
    return {"payment_url": payment_url}

@vnpay_router.get("/return")
async def vnpay_return(request: Request):
    """Điểm đích khi trình duyệt khách hàng bị redirect về từ VNPAY."""
    parameters = dict(request.query_params)
    try:
        verified_data = vnpay.verify_callback(parameters)
        if verified_data.get("vnp_ResponseCode") == "00":
            return {"status": "success", "message": "Giao dịch thành công", "data": verified_data}
        return {"status": "failed", "message": "Giao dịch thất bại", "data": verified_data}
    except VNPayCallbackError as e:
        raise HTTPException(status_code=400, detail=f"Dữ liệu không hợp lệ: {str(e)}")

@vnpay_router.get("/ipn")
async def vnpay_ipn(request: Request):
    """Webhook Server-to-Server dành riêng cho VNPAY gọi để cập nhật DB."""
    parameters = dict(request.query_params)
    try:
        order_id = parameters.get("vnp_TxnRef")
        if order_id not in fake_database:
            return {"RspCode": "01", "Message": "Order not found"}
            
        # Lấy số tiền từ DB để đối chiếu
        expected_amount = fake_database[order_id]["amount"]
        verified_data = vnpay.verify_callback(
            parameters, 
            expected_amount=expected_amount, 
            expected_order_id=order_id
        )

        # Kiểm tra Idempotency: Không cập nhật lại nếu đơn đã hoàn tất
        if fake_database[order_id]["status"] == "completed":
             return {"RspCode": "02", "Message": "Order already confirmed"}

        # Cập nhật trạng thái vào hệ quản trị CSDL (ví dụ: SQL Server, MySQL, MongoDB...)
        if verified_data["vnp_ResponseCode"] == "00":
            fake_database[order_id]["status"] = "completed"
        else:
            fake_database[order_id]["status"] = "failed"

        return {"RspCode": "00", "Message": "Confirm Success"}

    except VNPayCallbackError as e:
        error_map = {'invalid_signature': '97', 'invalid_amount': '04'}
        code = error_map.get(e.code, '99')
        return {"RspCode": code, "Message": e.message}