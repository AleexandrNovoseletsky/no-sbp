"use strict";

/**
 * Клиентская часть панели управления.
 *
 * Скрипт вынесен в отдельный файл, чтобы политика безопасности контента
 * могла запрещать встроенные обработчики и стили.
 */
(function () {
  var MIN_CONTRAST_RATIO = 3.0;

  /** Снимает гамма-коррекцию sRGB с одного канала. */
  function toLinear(channel) {
    var srgb = channel / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : Math.pow((srgb + 0.055) / 1.055, 2.4);
  }

  /**
   * Контраст цвета к белому фону по формуле WCAG 2.1.
   *
   * Повторяет проверку, которую выполняет сервер: результат нужен, чтобы
   * предупредить о нечитаемом цвете до отправки формы.
   */
  function contrastWithWhite(hex) {
    var value = parseInt(hex.slice(1), 16);
    var luminance =
      0.2126 * toLinear((value >> 16) & 255) +
      0.7152 * toLinear((value >> 8) & 255) +
      0.0722 * toLinear(value & 255);
    return 1.05 / (luminance + 0.05);
  }

  /** Подсказка о пригодности выбранного цвета QR-кода. */
  function bindColorHint() {
    var input = document.getElementById("qr_color");
    var hint = document.getElementById("qr_color_hint");
    if (!input || !hint) {
      return;
    }

    function update() {
      var ratio = contrastWithWhite(input.value);
      if (ratio < MIN_CONTRAST_RATIO) {
        hint.textContent = "Слишком светлый — код не отсканируется";
        hint.className = "warn-text";
      } else {
        hint.textContent = "Контраст " + ratio.toFixed(1) + " — годится";
        hint.className = "muted hint";
      }
    }

    input.addEventListener("input", update);
    update();
  }

  /** Подтверждение необратимых действий. */
  function bindConfirmations() {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.dataset.confirm)) {
          event.preventDefault();
        }
      });
    });
  }

  /** Заливка образцов цвета: значение приходит из данных, а не из стиля. */
  function paintSwatches() {
    document.querySelectorAll("[data-color]").forEach(function (element) {
      element.style.background = element.dataset.color;
    });
  }

  bindColorHint();
  bindConfirmations();
  paintSwatches();
})();
