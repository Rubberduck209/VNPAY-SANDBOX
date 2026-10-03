## Yeu cau
- Vào website https://sandbox.vnpayment.vn/devreg/ để đăng ký merchant môi trường test (URL đặt sao cho không báo lỗi URL đã tồn tại là được)

- Cài Docker Desktop và đảm bảo Docker Engine đang chạy.
- Mở PowerShell tại thư mục chứa `docker-compose.yml` và `manage.py`.

## Chuan bi `.env`

Tạo file `.env` từ file mẫu.


Điền các giá trị sau đây trong file `.env`:

- `DJANGO_SECRET_KEY`: dán các ký tự được sinh ra từ câu lệnh sau

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(50))"
   ```

- `DB_PASSWORD` va `DB_ROOT_PASSWORD`: Đặt 2 mật khẩu bất kỳ

- `VNPAY_TMN_CODE` va `VNPAY_HASH_SECRET_KEY`: nhập từ email trả về sau khi đăng ký thành công môi trường test




## Khoi dong

Khởi động docker

```powershell
docker compose up --build
```

Khi thấy dòng `Starting development server at http://0.0.0.0:8000`, mở:

<http://localhost:8000>

Xem trạng thái container:

```powershell
docker compose ps
```

Để xem logs nếu app không lên:

```powershell
docker compose logs -f web
docker compose logs -f db
```



## Xem database MySQL

Chạy các lệnh sau trong powershell thư mục có `docker-compose.yml` :

```powershell
docker compose exec db mysql -u vnpay -p
```

Khi MySQL hiện `Enter password:`, nhập mật khẩu `DB_PASSWORD` trong `.env`. Sau đó, chạy:

```sql
USE vnpay;
SHOW TABLES;
SELECT order_id, amount, status, response_code, transaction_no
FROM vnpay_python_paymenttransaction
ORDER BY created_at DESC;
```

## Cau hinh IPN tren cong VNPAY Sandbox



1. Khoi dong ung dung va Cloudflare Tunnel bang Docker Compose:

   ```powershell
   docker compose --profile tunnel up --build -d
   ```

2. Lay hostname public do tunnel cap:

   ```powershell
   docker compose logs -f cloudflared
   ```

   Cho den khi log hien URL HTTPS dang `https://ten-ngau-nhien.trycloudflare.com`. Giu container tunnel chay.



3. Tren cong VNPAY Sandbox, vao **Cau hinh IPN URL** va nhap URL:

   vào https://sandbox.vnpayment.vn/vnpaygw-sit-testing/user/login đăng nhập bằng tài khoản merchant đã tạo sau đó vào mục IPN URL nhập:

   ```text
   https://ten-ngau-nhien.trycloudflare.com/payment_ipn
   ```

   

dừng tunnel:

```powershell
docker compose stop cloudflared
```
