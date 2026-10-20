// html.Details no sincroniza 'open' con Dash cuando el usuario lo despliega.
document.addEventListener(
  "toggle",
  (e) => {
    const el = e.target;
    if (!(el instanceof HTMLDetailsElement) || !el.id.startsWith("{")) return;
    if (!window.dash_clientside || !window.dash_clientside.set_props) return;
    window.dash_clientside.set_props(JSON.parse(el.id), { open: el.open });
  },
  true
);

// El contenido de Dash se limpia en el servidor, pero el input nativo conserva
// el archivo anterior. Vaciarlo permite elegir de nuevo el mismo CSV.
document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element) || !event.target.closest("#nuevo-escenario")) return;
  const input = document.querySelector('#upload-data input[type="file"]');
  if (input) input.value = "";
});
