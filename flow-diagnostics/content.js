(() => {
  "use strict";

  const BADGE_ID = "ferixdi-flow-diagnostics";
  const patterns = [
    { type: "warning", re: /suspicious activity|подозрительн\w* активност\w*/i, label: "Flow отклонил действие", detail: "Проверка отображается на самой странице Flow. Расширение ничего не скрывает и не пытается обходить её." },
    { type: "auth", re: /sign in|войдите в аккаунт|нужно войти в аккаунт/i, label: "Требуется авторизация", detail: "Проверьте, что в этом браузерном профиле выполнен вход в Google Flow." },
    { type: "error", re: /something went wrong|что-то пошло не так|не удалось выполнить/i, label: "Ошибка страницы Flow", detail: "Сохраните текст ошибки и проверьте логи автоматизации." }
  ];

  function getVisibleText() {
    return (document.body?.innerText || "").slice(0, 60000);
  }

  function render() {
    const bodyText = getVisibleText();
    const found = patterns.find(item => item.re.test(bodyText));
    let badge = document.getElementById(BADGE_ID);

    if (!found) {
      badge?.remove();
      return;
    }

    if (!badge) {
      badge = document.createElement("aside");
      badge.id = BADGE_ID;
      badge.setAttribute("role", "status");
      Object.assign(badge.style, {
        position: "fixed",
        right: "16px",
        bottom: "16px",
        zIndex: "2147483647",
        maxWidth: "340px",
        padding: "12px 14px",
        border: "1px solid #777",
        borderRadius: "10px",
        background: "#111",
        color: "#fff",
        boxShadow: "0 4px 18px rgba(0,0,0,.25)",
        font: "13px/1.45 system-ui, sans-serif"
      });
      document.documentElement.appendChild(badge);
    }

    if (badge.dataset.status === found.label) return;
    badge.dataset.status = found.label;
    badge.replaceChildren();
    const heading = document.createElement("strong");
    heading.textContent = "Flow Diagnostics: " + found.label;
    const detail = document.createElement("div");
    detail.textContent = found.detail;
    detail.style.marginTop = "5px";
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "Закрыть";
    Object.assign(close.style, {
      marginTop: "8px",
      padding: "4px 8px",
      borderRadius: "6px",
      border: "1px solid #777",
      background: "#222",
      color: "#fff",
      cursor: "pointer"
    });
    close.addEventListener("click", () => badge.remove(), { once: true });
    badge.append(heading, detail, close);
  }

  render();
  const observer = new MutationObserver(() => {
    if (!document.getElementById(BADGE_ID)) render();
    else render();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
})();
