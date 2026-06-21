// src/stores/toast.js
// ------------------------------------------------------------------
// Store Alpine: SISTEMA DI NOTIFICHE TOAST GLOBALE.
//
// Bus di notifiche leggero e disaccoppiato. Qualunque store/azione può
// emettere un feedback operativo senza toccare il DOM:
//
//   Alpine.store("toast").success("Servizio aggiornato.");
//   Alpine.store("toast").error("Vincolo DB violato (422).");
//
// Caratteristiche:
//  - TTL: ogni toast si autochiude dopo `ttl` ms (default per tipo).
//  - Impilabili: `items` è un array; il container li renderizza in colonna.
//  - Chiusura manuale: remove(id) per il bottone "×".
//  - Nessuna dipendenza dal DOM: la UI è puramente reattiva a `items`.

export default {
  /** @type {Array<{ id: number, type: 'success'|'error'|'info', message: string }>} */
  items: [],

  /** Contatore monotono per ID stabili (key del x-for). */
  _nextId: 0,

  /**
   * Accoda un toast e ne programma l'autochiusura.
   * @param {'success'|'error'|'info'} type
   * @param {string} message
   * @param {number} [ttl=5000]  Millisecondi prima dell'autochiusura.
   * @returns {number} id del toast (per remove anticipata).
   */
  push(type, message, ttl = 5000) {
    const id = ++this._nextId;
    this.items.push({ id, type, message });
    if (ttl > 0) {
      setTimeout(() => this.remove(id), ttl);
    }
    return id;
  },

  /** Toast di successo (verde) — TTL breve. */
  success(message, ttl = 4000) {
    return this.push("success", message, ttl);
  },

  /** Toast di errore (rosso) — TTL più lungo, l'utente deve leggerlo. */
  error(message, ttl = 7000) {
    return this.push("error", message, ttl);
  },

  /** Toast informativo (neutro). */
  info(message, ttl = 5000) {
    return this.push("info", message, ttl);
  },

  /** Rimuove un toast per id (autochiusura o click su "×"). */
  remove(id) {
    this.items = this.items.filter((t) => t.id !== id);
  },

  /** Svuota tutti i toast (es. al logout / cambio sessione). */
  clear() {
    this.items = [];
  },
};
