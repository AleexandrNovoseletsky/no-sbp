"""Сквозные тесты панели: CRUD заказчиков, организаций, баланса и ключей."""

from sqlalchemy import select

from nosbp.db.models import Account, ApiToken, LedgerEntry, Organization
from tests.factories import (
    ADMIN_PREFIX,
    BIC,
    CORRESP_ACC,
    INN_COMPANY,
    PERSONAL_ACC,
)

ADMIN = ADMIN_PREFIX


def org_form(csrf: str, **overrides: object) -> dict[str, object]:
    """Заполненная форма организации с корректными реквизитами."""
    return {
        "csrf_token": csrf,
        "alias": "main",
        "name": "ООО Ромашка",
        "personal_acc": PERSONAL_ACC,
        "bank_name": "ПАО Сбербанк",
        "bic": BIC,
        "corresp_acc": CORRESP_ACC,
        "payee_inn": INN_COMPANY,
        "kpp": "773601001",
        "qr_color": "#0e6b62",
        "fee_percent": "0,7",
        "average_check": "",
        **overrides,
    }


# ---------------------------------------------------------------------------
# Список и создание заказчиков
# ---------------------------------------------------------------------------


async def test_index_opens(logged_in):
    panel, _, _ = logged_in
    response = await panel.get(f"{ADMIN}/")
    assert response.status_code == 200
    assert "Заказчики" in response.text


async def test_create_account(logged_in, session):
    panel, _, csrf = logged_in
    response = await panel.post(
        f"{ADMIN}/accounts/new",
        data={
            "csrf_token": csrf,
            "email": "shop@example.com",
            "display_name": "ООО Ромашка",
        },
    )
    assert response.status_code == 303

    account = (
        await session.execute(
            select(Account).where(Account.email == "shop@example.com")
        )
    ).scalar_one()
    assert account.display_name == "ООО Ромашка"
    assert account.is_active
    assert not account.is_unlimited


async def test_duplicate_email_is_refused(logged_in, session, make_account):
    panel, _, csrf = logged_in
    existing = await make_account(email="taken@example.com")

    response = await panel.post(
        f"{ADMIN}/accounts/new",
        data={"csrf_token": csrf, "email": existing.email, "display_name": "Второй"},
    )
    assert response.status_code == 200
    assert "уже заведён" in response.text


async def test_create_unlimited_account(logged_in, session):
    panel, _, csrf = logged_in
    await panel.post(
        f"{ADMIN}/accounts/new",
        data={
            "csrf_token": csrf,
            "email": "friend@example.com",
            "display_name": "Друг",
            "is_unlimited": "1",
        },
    )
    account = (
        await session.execute(
            select(Account).where(Account.email == "friend@example.com")
        )
    ).scalar_one()
    assert account.is_unlimited


async def test_account_search(logged_in, make_account):
    panel, _, _ = logged_in
    await make_account(email="findme@example.com")
    await make_account(email="other@example.com")

    response = await panel.get(f"{ADMIN}/", params={"q": "findme"})
    assert "findme@example.com" in response.text
    assert "other@example.com" not in response.text


# ---------------------------------------------------------------------------
# Карточка заказчика
# ---------------------------------------------------------------------------


async def test_account_page_opens(logged_in, make_account, make_organization):
    panel, _, _ = logged_in
    account = await make_account()
    await make_organization(account)

    response = await panel.get(f"{ADMIN}/accounts/{account.id}")
    assert response.status_code == 200
    assert account.email in response.text
    assert "уникальных операций" in response.text


async def test_edit_account(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/edit",
        data={
            "csrf_token": csrf,
            "display_name": "Новое название",
            "is_active": "1",
            "is_unlimited": "1",
            "daily_limit": "250",
        },
    )
    assert response.status_code == 303

    await session.refresh(account)
    assert account.display_name == "Новое название"
    assert account.is_unlimited
    assert account.daily_charge_limit_kopecks == 25_000


async def test_clearing_daily_limit_means_unlimited(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account(daily_limit_rubles=100)

    await panel.post(
        f"{ADMIN}/accounts/{account.id}/edit",
        data={
            "csrf_token": csrf,
            "display_name": account.display_name,
            "is_active": "1",
            "daily_limit": "",
        },
    )
    await session.refresh(account)
    assert account.daily_charge_limit_kopecks is None


async def test_unchecked_box_disables_account(logged_in, session, make_account):
    """Снятая галочка не приходит в запросе вовсе — это надо учитывать."""
    panel, _, csrf = logged_in
    account = await make_account()

    await panel.post(
        f"{ADMIN}/accounts/{account.id}/edit",
        data={"csrf_token": csrf, "display_name": account.display_name},
    )
    await session.refresh(account)
    assert not account.is_active


async def test_delete_account_removes_everything(
    logged_in, session, make_account, make_organization
):
    panel, _, csrf = logged_in
    account = await make_account()
    await make_organization(account)

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/delete", data={"csrf_token": csrf}
    )
    assert response.status_code == 303

    # Панель работает в своей сессии; тестовую надо заставить сходить в базу,
    # иначе она отдаст объект из кэша, которого в базе уже нет.
    session.expunge_all()
    assert await session.get(Account, account.id) is None
    left = await session.execute(
        select(Organization).where(Organization.account_id == account.id)
    )
    assert left.scalars().first() is None


# ---------------------------------------------------------------------------
# Организации
# ---------------------------------------------------------------------------


async def test_create_organization(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf, is_default="1"),
    )
    assert response.status_code == 303, response.text

    organization = (
        await session.execute(
            select(Organization).where(Organization.account_id == account.id)
        )
    ).scalar_one()
    assert organization.alias == "main"
    assert organization.acquiring_fee_bps == 70
    assert organization.qr_color == "#0e6b62"
    assert organization.is_default


async def test_organization_with_typo_in_account_is_refused(
    logged_in, session, make_account
):
    """Контрольный разряд ловит опечатку до того, как деньги уйдут не туда."""
    panel, _, csrf = logged_in
    account = await make_account()
    broken = PERSONAL_ACC[:-1] + "8"

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf, personal_acc=broken),
        follow_redirects=True,
    )
    assert "контрольного разряда" in response.text

    left = await session.execute(
        select(Organization).where(Organization.account_id == account.id)
    )
    assert left.scalars().first() is None


async def test_light_qr_colour_is_refused(logged_in, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf, qr_color="#f5f5f5"),
        follow_redirects=True,
    )
    assert "слишком светлый" in response.text


async def test_duplicate_alias_is_refused(logged_in, make_account, make_organization):
    panel, _, csrf = logged_in
    account = await make_account()
    await make_organization(account, alias="main")

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf, alias="main"),
        follow_redirects=True,
    )
    assert "уже есть организация" in response.text


async def test_edit_organization(logged_in, session, make_account, make_organization):
    panel, _, csrf = logged_in
    account = await make_account()
    organization = await make_organization(account)

    response = await panel.post(
        f"{ADMIN}/organizations/{organization.id}/edit",
        data=org_form(
            csrf,
            alias="second",
            name="ИП Иванов",
            fee_percent="2,5",
            average_check="47000",
        ),
    )
    assert response.status_code == 303

    await session.refresh(organization)
    assert organization.alias == "second"
    assert organization.acquiring_fee_bps == 250
    assert organization.average_check_kopecks == 4_700_000


async def test_second_default_organization_replaces_the_first(
    logged_in, session, make_account, make_organization
):
    """Организация по умолчанию может быть только одна."""
    panel, _, csrf = logged_in
    account = await make_account()
    first = await make_organization(account, alias="first")

    await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf, alias="second", is_default="1"),
    )

    await session.refresh(first)
    assert not first.is_default


async def test_delete_organization(logged_in, session, make_account, make_organization):
    panel, _, csrf = logged_in
    account = await make_account()
    organization = await make_organization(account)

    response = await panel.post(
        f"{ADMIN}/organizations/{organization.id}/delete", data={"csrf_token": csrf}
    )
    assert response.status_code == 303

    session.expunge_all()
    assert await session.get(Organization, organization.id) is None


# ---------------------------------------------------------------------------
# Баланс
# ---------------------------------------------------------------------------


async def test_topup_from_panel(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account(balance_rubles=0)

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/balance",
        data={
            "csrf_token": csrf,
            "operation": "topup",
            "amount": "1000",
            "comment": "по счёту 42",
        },
    )
    assert response.status_code == 303

    await session.refresh(account)
    assert account.balance_kopecks == 100_000

    entry = (
        await session.execute(
            select(LedgerEntry).where(LedgerEntry.account_id == account.id)
        )
    ).scalar_one()
    assert entry.comment == "по счёту 42"


async def test_topup_below_minimum_is_refused(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account(balance_rubles=0)

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/balance",
        data={"csrf_token": csrf, "operation": "topup", "amount": "10"},
        follow_redirects=True,
    )
    assert "Минимальная сумма пополнения" in response.text

    await session.refresh(account)
    assert account.balance_kopecks == 0


async def test_negative_adjustment(logged_in, session, make_account):
    """Корректировкой можно и списать — например, исправить свою же ошибку."""
    panel, _, csrf = logged_in
    account = await make_account(balance_rubles=100)

    await panel.post(
        f"{ADMIN}/accounts/{account.id}/balance",
        data={
            "csrf_token": csrf,
            "operation": "adjust",
            "amount": "-50",
            "comment": "исправление",
        },
    )
    await session.refresh(account)
    assert account.balance_kopecks == 5_000


async def test_comma_in_amount_is_accepted(logged_in, session, make_account):
    """На цифровом блоке русской раскладки запятая, а не точка."""
    panel, _, csrf = logged_in
    account = await make_account(balance_rubles=0)

    await panel.post(
        f"{ADMIN}/accounts/{account.id}/balance",
        data={"csrf_token": csrf, "operation": "topup", "amount": "1000,50"},
    )
    await session.refresh(account)
    assert account.balance_kopecks == 100_050


async def test_garbage_amount_is_refused(logged_in, make_account):
    panel, _, csrf = logged_in
    account = await make_account(balance_rubles=0)

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/balance",
        data={"csrf_token": csrf, "operation": "topup", "amount": "тысяча"},
        follow_redirects=True,
    )
    assert "не похоже на сумму" in response.text


# ---------------------------------------------------------------------------
# Ключи
# ---------------------------------------------------------------------------


async def test_issue_and_revoke_token(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    issued = await panel.post(
        f"{ADMIN}/accounts/{account.id}/tokens",
        data={"csrf_token": csrf, "label": "RetailCRM"},
    )
    assert issued.status_code == 303
    assert "token=" in issued.headers["location"]

    api_token = (
        await session.execute(select(ApiToken).where(ApiToken.account_id == account.id))
    ).scalar_one()
    assert api_token.label == "RetailCRM"
    assert api_token.revoked_at is None

    revoked = await panel.post(
        f"{ADMIN}/accounts/{account.id}/tokens/{api_token.id}/revoke",
        data={"csrf_token": csrf},
    )
    assert revoked.status_code == 303

    await session.refresh(api_token)
    assert api_token.revoked_at is not None


# ---------------------------------------------------------------------------
# Защита форм
# ---------------------------------------------------------------------------


async def test_form_without_csrf_is_refused(logged_in, session, make_account):
    """Без токена чужой сайт мог бы обнулить баланс от имени открытой сессии."""
    panel, _, _ = logged_in
    account = await make_account(balance_rubles=0)

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/balance",
        data={"operation": "topup", "amount": "1000"},
    )
    assert response.status_code == 303
    assert "/login" in response.headers["location"]

    await session.refresh(account)
    assert account.balance_kopecks == 0


async def test_form_with_wrong_csrf_is_refused(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account(balance_rubles=0)

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/balance",
        data={
            "csrf_token": csrf[:-1],
            "operation": "topup",
            "amount": "1000",
        },
    )
    assert response.status_code == 303
    assert "/login" in response.headers["location"]

    await session.refresh(account)
    assert account.balance_kopecks == 0


# ---------------------------------------------------------------------------
# Сохранение введённого при ошибке
# ---------------------------------------------------------------------------


async def test_organization_form_keeps_input_after_error(logged_in, make_account):
    """Ошибка валидации не должна очищать остальные поля формы."""
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(
            csrf,
            alias="romashka",
            name="ООО Ромашка",
            qr_color="#ffa500",
            fee_percent="1,25",
            average_check="47000",
            is_default="1",
        ),
    )

    assert response.status_code == 200
    assert "слишком светлый" in response.text

    page = response.text
    assert 'value="romashka"' in page
    assert 'value="ООО Ромашка"' in page
    assert f'value="{PERSONAL_ACC}"' in page
    assert f'value="{CORRESP_ACC}"' in page
    assert f'value="{INN_COMPANY}"' in page
    assert f'value="{BIC}"' in page
    assert 'value="773601001"' in page
    assert 'value="#ffa500"' in page
    assert 'value="1,25"' in page
    assert 'value="47000"' in page
    assert "checked" in page


async def test_organization_form_keeps_input_after_checksum_error(
    logged_in, make_account
):
    panel, _, csrf = logged_in
    account = await make_account()
    broken = PERSONAL_ACC[:-1] + "8"

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf, personal_acc=broken, name="ООО Ошибка"),
    )
    assert response.status_code == 200
    assert "контрольного разряда" in response.text
    assert f'value="{broken}"' in response.text
    assert 'value="ООО Ошибка"' in response.text


async def test_organization_edit_keeps_input_after_error(
    logged_in, make_account, make_organization
):
    panel, _, csrf = logged_in
    account = await make_account()
    organization = await make_organization(account, alias="first")

    response = await panel.post(
        f"{ADMIN}/organizations/{organization.id}/edit",
        data=org_form(csrf, alias="renamed", qr_color="#eeeeee"),
    )
    assert response.status_code == 200
    assert 'value="renamed"' in response.text
    assert 'value="#eeeeee"' in response.text


async def test_account_form_keeps_input_after_error(logged_in, make_account):
    panel, _, csrf = logged_in
    existing = await make_account(email="taken@example.com")

    response = await panel.post(
        f"{ADMIN}/accounts/new",
        data={
            "csrf_token": csrf,
            "email": existing.email,
            "display_name": "ООО Второй",
            "daily_limit": "250",
            "is_unlimited": "1",
        },
    )
    assert response.status_code == 200
    assert "уже заведён" in response.text
    assert 'value="taken@example.com"' in response.text
    assert 'value="ООО Второй"' in response.text
    assert 'value="250"' in response.text
    assert "checked" in response.text


async def test_balance_form_keeps_input_after_error(logged_in, make_account):
    panel, _, csrf = logged_in
    account = await make_account(balance_rubles=0)

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/balance",
        data={
            "csrf_token": csrf,
            "operation": "topup",
            "amount": "10",
            "comment": "по счёту 42",
        },
    )
    assert response.status_code == 200
    assert "Минимальная сумма пополнения" in response.text
    assert 'value="10"' in response.text
    assert 'value="по счёту 42"' in response.text


async def test_account_edit_keeps_input_after_error(logged_in, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/edit",
        data={
            "csrf_token": csrf,
            "display_name": "ООО Новое",
            "daily_limit": "не число",
            "is_active": "1",
        },
    )
    assert response.status_code == 200
    assert "не похоже на сумму" in response.text
    assert 'value="ООО Новое"' in response.text
    assert 'value="не число"' in response.text


# ---------------------------------------------------------------------------
# Логотип
# ---------------------------------------------------------------------------


def png_bytes(size: int = 120) -> bytes:
    """Маленький настоящий PNG."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGBA", (size, size), (14, 107, 98, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_logo_upload_is_stored(logged_in, session, storage, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf),
        files={"logo": ("logo.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 303

    organization = (
        await session.execute(
            select(Organization).where(Organization.account_id == account.id)
        )
    ).scalar_one()
    assert organization.logo_key is not None
    assert await storage.get(organization.logo_key) == png_bytes()


async def test_empty_file_field_does_not_wipe_existing_logo(
    logged_in, session, make_account, make_organization
):
    """Браузер присылает поле файла всегда — даже когда ничего не выбрано."""
    panel, _, csrf = logged_in
    account = await make_account()
    organization = await make_organization(account, logo_key="logos/keep.png")

    response = await panel.post(
        f"{ADMIN}/organizations/{organization.id}/edit",
        data=org_form(csrf, name="Переименовано"),
        files={"logo": ("", b"", "application/octet-stream")},
    )
    assert response.status_code == 303

    await session.refresh(organization)
    assert organization.logo_key == "logos/keep.png"


async def test_not_an_image_is_refused(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf),
        files={"logo": ("logo.png", b"\x00\x01 not an image", "image/png")},
    )
    assert response.status_code == 200
    assert "не удалось прочитать как изображение" in response.text

    left = await session.execute(
        select(Organization).where(Organization.account_id == account.id)
    )
    assert left.scalars().first() is None


async def test_wrong_extension_is_refused(logged_in, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf),
        files={"logo": ("logo.gif", png_bytes(), "image/gif")},
    )
    assert response.status_code == 200
    assert "в одном из форматов" in response.text


async def test_oversized_logo_is_refused(logged_in, make_account, settings):
    panel, _, csrf = logged_in
    account = await make_account()
    huge = b"\x89PNG\r\n\x1a\n" + b"\x00" * settings.logo_max_bytes

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/organizations/new",
        data=org_form(csrf),
        files={"logo": ("logo.png", huge, "image/png")},
    )
    assert response.status_code == 200
    assert "КБ" in response.text


async def test_replacing_logo_drops_it_from_cache(
    logged_in, session, storage, make_account, make_organization, panel
):
    """Иначе организация несколько минут показывала бы старую картинку."""
    from nosbp.storage.logo_cache import LogoCache

    panel_client, _, csrf = logged_in
    account = await make_account()
    organization = await make_organization(account, logo_key="logos/old.png")
    await storage.put("logos/old.png", png_bytes(), content_type="image/png")

    cache: LogoCache = panel_client._transport.app.state.logo_cache  # type: ignore[attr-defined]
    await cache.get("logos/old.png")
    assert len(cache) == 1

    response = await panel_client.post(
        f"{ADMIN}/organizations/{organization.id}/edit",
        data=org_form(csrf),
        files={"logo": ("new.png", png_bytes(200), "image/png")},
    )
    assert response.status_code == 303
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# Поиск
# ---------------------------------------------------------------------------


async def test_search_wildcards_are_not_special(logged_in, make_account):
    """Знак процента в строке поиска — это символ, а не «что угодно»."""
    panel, _, _ = logged_in
    await make_account(email="plain@example.com")

    response = await panel.get(f"{ADMIN}/", params={"q": "%"})
    assert "plain@example.com" not in response.text


async def test_underscore_is_not_special(logged_in, make_account):
    panel, _, _ = logged_in
    await make_account(email="abc@example.com")

    response = await panel.get(f"{ADMIN}/", params={"q": "a_c"})
    assert "abc@example.com" not in response.text


async def test_search_finds_by_substring(logged_in, make_account):
    panel, _, _ = logged_in
    await make_account(email="findme@example.com")

    response = await panel.get(f"{ADMIN}/", params={"q": "INDM"})
    assert "findme@example.com" in response.text


# ---------------------------------------------------------------------------
# Проверка ввода
# ---------------------------------------------------------------------------


async def test_negative_daily_limit_is_refused(logged_in, session, make_account):
    panel, _, csrf = logged_in
    account = await make_account()

    response = await panel.post(
        f"{ADMIN}/accounts/{account.id}/edit",
        data={
            "csrf_token": csrf,
            "display_name": account.display_name,
            "is_active": "1",
            "daily_limit": "-100",
        },
    )
    assert response.status_code == 200
    assert "не может быть отрицательным" in response.text

    await session.refresh(account)
    assert account.daily_charge_limit_kopecks != -10_000
