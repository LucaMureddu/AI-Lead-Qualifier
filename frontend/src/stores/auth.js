// src/stores/auth.js
// ------------------------------------------------------------------
// Alpine store per la gestione dell'autenticazione JWT.
//
// Il token è persisted in localStorage; lo state Alpine è completamente
// reattivo: ogni componente che legge isAuthenticated / tenantId si
// aggiorna automaticamente quando il token cambia.
//
// Strategia 401: l'evento DOM "auth:unauthorized" viene dispatch-ato da
// api.js quando il backend risponde 401; app.js lo ascolta e chiama
// App.logout() che svuota lo store (→ la login screen torna visibile).

const TOKEN_KEY = "jwt_token";

/**
 * Decodifica il payload di un JWT (base64url → JSON).
 * Non verifica la firma — usato solo per leggere il claim "sub" a scopo display.
 */
function decodePayload(token) {
  try {
    // base64url → base64 standard → UTF-8 string → JSON
    const b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(b64));
  } catch {
    return null;
  }
}

// Inizializza dal localStorage in modo sincrono (evita flash di login screen
// su pagine già autenticate).
const _storedToken = localStorage.getItem(TOKEN_KEY) || null;
const _storedPayload = _storedToken ? decodePayload(_storedToken) : null;

export default {
  /** Token JWT grezzo (o null se non autenticato). */
  token: _storedToken,

  /**
   * Claim "sub" del JWT — corrisponde all'username/tenant_id usato in login.
   * Solo per display: l'autenticazione reale avviene tramite l'header Bearer.
   */
  tenantId: _storedPayload?.sub || null,

  /** Messaggio di errore dell'ultimo tentativo di login (null se ok). */
  loginError: null,

  /** True mentre il POST /token è in volo. */
  isLoggingIn: false,

  /** True se c'è un token valido in memoria. */
  get isAuthenticated() {
    return !!this.token;
  },

  /**
   * Salva il token, decodifica il claim "sub" e aggiorna lo state reattivo.
   * Chiamato da App.login() dopo aver ricevuto il token dal backend.
   */
  setToken(token) {
    const payload = decodePayload(token);
    this.token = token;
    this.tenantId = payload?.sub || null;
    this.loginError = null;
    localStorage.setItem(TOKEN_KEY, token);
  },

  /**
   * Cancella il token da memoria e localStorage.
   * Chiamato da App.logout() e dall'handler di "auth:unauthorized".
   */
  clear() {
    this.token = null;
    this.tenantId = null;
    localStorage.removeItem(TOKEN_KEY);
  },
};
