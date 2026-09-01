/* Service Worker anmelden – auf jeder Seite eingebunden.
 * Läuft nur im "sicheren Kontext" (HTTPS oder localhost); im LAN über http://
 * passiert schlicht nichts, die App funktioniert dort ganz normal weiter.
 */
'use strict';
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* egal, App läuft trotzdem */ });
  });
}
