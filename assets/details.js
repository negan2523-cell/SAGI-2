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
