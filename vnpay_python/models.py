from django.db import models


class PaymentTransaction(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Đang chờ thanh toán'
        SUCCEEDED = 'succeeded', 'Thành công'
        FAILED = 'failed', 'Thất bại'

    order_id = models.CharField(max_length=250, unique=True)
    amount = models.PositiveBigIntegerField()
    order_type = models.CharField(max_length=20)
    order_desc = models.CharField(max_length=100)
    requested_bank_code = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    response_code = models.CharField(max_length=2, blank=True)
    transaction_no = models.CharField(max_length=50, blank=True)
    bank_code = models.CharField(max_length=20, blank=True)
    card_type = models.CharField(max_length=20, blank=True)
    pay_date = models.CharField(max_length=14, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return '{} ({})'.format(self.order_id, self.status)