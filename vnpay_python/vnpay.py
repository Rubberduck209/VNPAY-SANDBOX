from vnpay_lib import build_payment_url, verify_response_signature


class vnpay:
    def __init__(self):
        self.requestData = {}
        self.responseData = {}

    def get_payment_url(self, vnpay_payment_url, secret_key):
        return build_payment_url(
            vnpay_payment_url, self.requestData, secret_key
        )

    def validate_response(self, secret_key):
        return verify_response_signature(self.responseData, secret_key)
