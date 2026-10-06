## Yêu cầu
- Vào website https://sandbox.vnpayment.vn/devreg/ để đăng ký merchant môi trường test (URL đặt sao cho không báo lỗi URL đã tồn tại là được, khuyến khích để ký tự đặt biệt trong password)

- Cài Docker Desktop và đảm bảo Docker Engine đang chạy.
- Mở PowerShell tại thư mục chứa `docker-compose.yml` và `manage.py`.

## Set up file `.env`

Tạo file `.env` từ file `.env.example`.


Điền các giá trị sau đây trong file `.env`:

- `DJANGO_SECRET_KEY`: dán các ký tự được sinh ra từ câu lệnh sau

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(50))"
   ```

- `DB_PASSWORD` va `DB_ROOT_PASSWORD`: Đặt 2 mật khẩu bất kỳ

- `VNPAY_TMN_CODE` va `VNPAY_HASH_SECRET_KEY`: nhập từ email trả về sau khi đăng ký thành công môi trường test



## Khởi động

Khởi động docker

```powershell
docker compose up --build
```

Khi thấy dòng `Starting development server at http://0.0.0.0:8000`, mở:

<http://localhost:8000>



## Set up cấu hình IPN:
Mở powershell trong thư mục chứa `docker-compose.yml` và `manage.py`.
Sau đó khởi động cloudflare tunnel:

   ```powershell
   docker compose --profile tunnel up --build -d
   ```
Lấy hostname do tunnel cung cấp:

   ```powershell
   docker compose logs -f cloudflared
   ```
   Rồi tìm dòng log có URL HTTPS có dạng `https://ten-ngau-nhien.trycloudflare.com`.

Sau đó đăng nhập trên cổng [VNPAYGW SIT Testing](https://sandbox.vnpayment.vn/vnpaygw-sit-testing/user/login) bằng tài khoản merchant đã tạo và vào mục **Cấu hình IPN URL** và nhập:
   `https://ten-ngau-nhien.trycloudflare.com/payment_ipn`






Các công cụ hỗ trợ:

Xem trạng thái container:

```powershell
docker compose ps
```

Xem logs:

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



Chỉ dừng riêng tunnel:

```powershell
docker compose stop cloudflared
```
