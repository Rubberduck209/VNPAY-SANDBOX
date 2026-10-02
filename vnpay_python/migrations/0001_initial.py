from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='PaymentTransaction',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order_id', models.CharField(max_length=250, unique=True)),
                ('amount', models.PositiveBigIntegerField()),
                ('order_type', models.CharField(max_length=20)),
                ('order_desc', models.CharField(max_length=100)),
                ('requested_bank_code', models.CharField(blank=True, max_length=20)),
                ('status', models.CharField(choices=[('pending', 'Đang chờ thanh toán'), ('succeeded', 'Thành công'), ('failed', 'Thất bại')], default='pending', max_length=12)),
                ('response_code', models.CharField(blank=True, max_length=2)),
                ('transaction_no', models.CharField(blank=True, max_length=50)),
                ('bank_code', models.CharField(blank=True, max_length=20)),
                ('card_type', models.CharField(blank=True, max_length=20)),
                ('pay_date', models.CharField(blank=True, max_length=14)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ('-created_at',)},
        ),
    ]