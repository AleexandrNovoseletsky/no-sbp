"""Превращение письма в текст и в HTML.

Оба варианта собираются из одного и того же :class:`~nosbp.mail.base.Letter`:
почтовый клиент сам выбирает, что показать, а расхождений между версиями
не возникает, потому что источник один.
"""

from functools import lru_cache
from pathlib import Path
from typing import Final

from jinja2 import Environment, FileSystemLoader, select_autoescape

from nosbp.mail.base import Letter

TEMPLATES_DIRECTORY: Final[Path] = Path(__file__).parent / "templates"
LAYOUT_TEMPLATE: Final[str] = "letter.html"


@lru_cache
def _environment() -> Environment:
    """Движок шаблонов писем.

    Отдельный от веб-интерфейсов: у писем своя вёрстка и свой каталог,
    а автоэкранирование обязательно — в текст подставляются данные
    заказчика.
    """
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIRECTORY),
        autoescape=select_autoescape(("html",)),
    )


def render_html(letter: Letter, *, service_name: str, base_url: str) -> str:
    """Собирает HTML-версию письма."""
    template = _environment().get_template(LAYOUT_TEMPLATE)
    return template.render(letter=letter, service_name=service_name, base_url=base_url)


def render_text(letter: Letter, *, service_name: str, base_url: str) -> str:
    """Собирает текстовую версию письма.

    Нужна не только ради старых клиентов: письмо без текстовой части
    почтовые фильтры считают подозрительным и охотнее уводят в спам.
    """
    blocks: list[str] = [letter.heading, ""]
    blocks.extend(letter.paragraphs)

    if letter.action_url:
        blocks.extend(("", f"{letter.action_label}: {letter.action_url}"))

    if letter.footer:
        blocks.append("")
        blocks.extend(letter.footer)

    blocks.extend(("", "—", f"{service_name} — {base_url}"))
    return "\n".join(blocks)
