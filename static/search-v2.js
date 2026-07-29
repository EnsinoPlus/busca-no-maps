(() => {
  "use strict";
  const form = document.getElementById("search-form");
  if (!form) return;
  const panel = document.getElementById("search-panel");
  const button = document.getElementById("search-button");
  const cancel = document.getElementById("search-cancel");
  const phase = document.getElementById("search-phase");
  const progress = document.getElementById("search-progress");
  const scanned = document.getElementById("count-scanned");
  const found = document.getElementById("count-found");
  const api = document.getElementById("count-api");
  const log = document.getElementById("search-log");
  let controller = null;

  function render(event) {
    phase.textContent = event.phase === "concluida" ? "Varredura concluída" : event.message;
    progress.value = Number(event.progress || 0);
    scanned.textContent = String(event.scanned || 0);
    found.textContent = String(event.new_email_leads || 0);
    api.textContent = String(event.api_calls || 0);
    log.textContent += `${event.message}\n`;
    log.scrollTop = log.scrollHeight;
    if (["concluida", "limite_api"].includes(event.phase)) {
      cancel.disabled = true;
      panel.classList.add("is-complete");
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    controller = new AbortController();
    panel.hidden = false;
    panel.classList.remove("is-complete");
    cancel.disabled = false;
    button.disabled = true;
    log.textContent = "";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: {"Accept": "application/x-ndjson"},
        credentials: "same-origin",
        signal: controller.signal
      });
      if (!response.ok || !response.body) throw new Error(`Falha na busca (${response.status})`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value || new Uint8Array(), {stream: !chunk.done});
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) if (line.trim()) render(JSON.parse(line));
        if (chunk.done) break;
      }
      if (buffer.trim()) render(JSON.parse(buffer));
    } catch (error) {
      phase.textContent = error.name === "AbortError" ? "Busca cancelada" : "Erro na busca";
      log.textContent += `${error.message}\n`;
    } finally {
      button.disabled = false;
      cancel.disabled = true;
      panel.classList.add("is-complete");
      controller = null;
    }
  });

  cancel.addEventListener("click", () => {
    if (controller) controller.abort();
  });
})();
