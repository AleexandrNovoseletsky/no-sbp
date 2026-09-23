"""Работа с формой организации-получателя.

Форма одинакова в панели управления и в личном кабинете: те же поля, те
же проверки, та же загрузка логотипа. Различаются только адреса, по
которым она отправляется.
"""

from fastapi import UploadFile

from nosbp.admin import crud
from nosbp.admin.logos import store_logo
from nosbp.admin.stats import format_fee_percent, parse_fee_percent
from nosbp.core.config import Settings
from nosbp.core.constants import DEFAULT_QR_COLOR
from nosbp.core.money import parse_roubles, roubles_input
from nosbp.db.models import Organization
from nosbp.storage.logo_cache import LogoCache
from nosbp.web.responses import FormValues, checkbox


def fee_input(bps: int) -> str:
    """Ставка для поля ввода: «0,7» без знака процента."""
    return format_fee_percent(bps).replace(" %", "")


def values_of(organization: Organization | None, settings: Settings) -> FormValues:
    """Готовит значения формы: из модели или пустые."""
    if organization is None:
        return {
            "alias": "",
            "name": "",
            "personal_acc": "",
            "bank_name": "",
            "bic": "",
            "corresp_acc": "",
            "payee_inn": "",
            "kpp": "",
            "qr_color": DEFAULT_QR_COLOR,
            "fee_percent": fee_input(settings.default_acquiring_fee_bps),
            "average_check": "",
            "is_default": False,
        }
    return {
        "alias": organization.alias,
        "name": organization.name,
        "personal_acc": organization.personal_acc,
        "bank_name": organization.bank_name,
        "bic": organization.bic,
        "corresp_acc": organization.corresp_acc,
        "payee_inn": organization.payee_inn,
        "kpp": organization.kpp or "",
        "qr_color": organization.qr_color,
        "fee_percent": fee_input(organization.acquiring_fee_bps),
        "average_check": roubles_input(organization.average_check_kopecks),
        "is_default": organization.is_default,
    }


def submitted_values(
    *,
    alias: str,
    name: str,
    personal_acc: str,
    bank_name: str,
    bic: str,
    corresp_acc: str,
    payee_inn: str,
    kpp: str,
    qr_color: str,
    fee_percent: str,
    average_check: str,
    is_default: str | None,
) -> FormValues:
    """Собирает то, что прислал браузер, — для повторной отрисовки формы."""
    return {
        "alias": alias,
        "name": name,
        "personal_acc": personal_acc,
        "bank_name": bank_name,
        "bic": bic,
        "corresp_acc": corresp_acc,
        "payee_inn": payee_inn,
        "kpp": kpp,
        "qr_color": qr_color,
        "fee_percent": fee_percent,
        "average_check": average_check,
        "is_default": checkbox(is_default),
    }


def to_requisites(values: FormValues, default_fee_bps: int) -> crud.RequisitesInput:
    """Переводит значения формы в проверяемый набор реквизитов.

    Ставка и средний чек разбираются здесь, а не схемой FastAPI: ошибка
    ввода должна превращаться в понятный текст, а не в стандартный ответ
    о непройденной валидации.

    :raises ValueError: если ставка или средний чек введены неверно.
    """
    fee_percent = str(values["fee_percent"]).strip()
    average_check = str(values["average_check"]).strip()

    return crud.RequisitesInput(
        alias=str(values["alias"]),
        name=str(values["name"]),
        personal_acc=str(values["personal_acc"]),
        bank_name=str(values["bank_name"]),
        bic=str(values["bic"]),
        corresp_acc=str(values["corresp_acc"]),
        payee_inn=str(values["payee_inn"]),
        kpp=str(values["kpp"]) or None,
        qr_color=str(values["qr_color"]),
        acquiring_fee_bps=(
            parse_fee_percent(fee_percent) if fee_percent else default_fee_bps
        ),
        average_check_kopecks=(parse_roubles(average_check) if average_check else None),
        is_default=bool(values["is_default"]),
    )


async def attach_logo(
    organization: Organization,
    upload: UploadFile | None,
    settings: Settings,
    logo_cache: LogoCache,
) -> None:
    """Сохраняет присланный логотип и привязывает его к организации.

    Пустое поле файла браузер присылает всегда — с пустым именем; такой
    файл игнорируется, иначе сохранение формы без выбора файла стирало бы
    прежний логотип.

    :raises ValidationError: формат не поддерживается, файл слишком велик
        или не читается как изображение.
    """
    if upload is None or not upload.filename:
        return

    data = await upload.read()
    key = await store_logo(
        storage=logo_cache.storage,
        account_id=organization.account_id,
        filename=upload.filename,
        data=data,
        max_bytes=settings.logo_max_bytes,
    )

    previous = organization.logo_key
    organization.logo_key = key

    # Кэш держит логотип в памяти процесса; без сброса организация
    # ещё несколько минут показывала бы старую картинку.
    if previous is not None:
        logo_cache.invalidate(previous)
