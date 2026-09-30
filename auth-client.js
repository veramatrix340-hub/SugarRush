/* Pega esto en index.html dentro del <script>, y en el <head> añade:
   <script src="https://telegram.org/js/telegram-web-app.js"></script>
   (funciona en tu sitio de GitHub Pages; no en la vista previa de Claude) */

const API = 'https://TU-API.com';   // la URL donde despliegues api.py (https)
let JWT = null;                     // el token vive solo en memoria

async function api(path, body) {
  const r = await fetch(API + path, {
    method: body ? 'POST' : 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(JWT ? { Authorization: 'Bearer ' + JWT } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 && path !== '/auth') { await login(); return api(path, body); }
  if (!r.ok) throw new Error(path + ' ' + r.status);
  return r.json();
}

async function login() {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (!tg || !tg.initData) return;             // abierto fuera de Telegram
  tg.ready(); tg.expand();
  const d = await fetch(API + '/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ init_data: tg.initData }),
  }).then(r => r.json());
  JWT = d.token;
  const me = await api('/me');
  coins = me.coins; friends = me.friends; ui();
}

/* Al terminar una partida, informa al servidor:  api('/game-over', { coins: gc }) */
login().catch(console.error);
