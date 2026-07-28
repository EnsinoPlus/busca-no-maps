document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("search-form");
  const button = document.getElementById("search-button");
  const spinner = document.getElementById("search-spinner");
  const label = document.getElementById("search-button-label");

  if (!form || !button || !spinner || !label) return;

  form.addEventListener("submit", () => {
    spinner.hidden = false;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    label.textContent = "Buscando contatos com e-mail...";
  });
});