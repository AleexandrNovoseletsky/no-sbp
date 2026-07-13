from nosbp.payments.schemas import PaymentDetails


def build_gost_payload(details: PaymentDetails) -> str:
    """Формирует строку для оплаты по ГОСТ Р 56042-2014."""

    fields = {
        "Name": details.name,
        "PersonalAcc": details.personal_acc,
        "BankName": details.bank_name,
        "BIC": details.bic,
        "PayeeINN": details.payee_inn,
        "CorrespAcc": details.corresp_acc,
        "Sum": details.payment_sum,
        "Purpose": details.purpose,
        "LastName": details.last_name,
        "FirstName": details.first_name,
        "MiddleName": details.middle_name,
        "Phone": details.phone,
    }

    parts = [
        f"{key}={value}" for key, value in fields.items() if value is not None
    ]
    return "ST00012|" + "|".join(parts)
