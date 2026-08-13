(() => {
  let currentUser = null;
  let resolveReady;
  const ready = new Promise((resolve) => { resolveReady = resolve; });

  window.erpAuth = {
    ready,
    async getToken() {
      return currentUser ? currentUser.getIdToken() : null;
    },
  };

  document.addEventListener("DOMContentLoaded", initialize);

  async function initialize() {
    const gate = document.getElementById("auth-gate");
    const form = document.getElementById("auth-form");
    const status = document.getElementById("auth-status");
    try {
      const response = await fetch("/api/firebase-client-config", { cache: "no-store" });
      if (!response.ok) throw new Error("Nao foi possivel iniciar a sessao.");
      const config = await response.json();
      if (!config.auth_required) {
        resolveReady();
        return;
      }

      gate.hidden = false;
      if (!config.enabled) {
        setStatus(status, "A configuracao de acesso ainda nao foi concluida.");
        return;
      }

      const [{ initializeApp }, authSdk] = await Promise.all([
        import("https://www.gstatic.com/firebasejs/12.17.1/firebase-app.js"),
        import("https://www.gstatic.com/firebasejs/12.17.1/firebase-auth.js"),
      ]);
      const { getAuth, onAuthStateChanged, signInWithEmailAndPassword, signOut } = authSdk;
      const auth = getAuth(initializeApp(config));
      let deniedMessage = "";

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        setStatus(status, "Entrando...");
        try {
          await signInWithEmailAndPassword(auth, form.email.value.trim(), form.password.value);
        } catch (_) {
          setStatus(status, "Email ou senha nao conferem. Tente novamente.");
        } finally {
          button.disabled = false;
        }
      });

      onAuthStateChanged(auth, async (user) => {
        if (!user) {
          currentUser = null;
          setStatus(status, deniedMessage || "Entre com a conta autorizada da loja.");
          return;
        }
        try {
          const token = await user.getIdTokenResult(true);
          if (token.claims.erp_admin !== true) {
            deniedMessage = "Esta conta ainda nao recebeu acesso a loja.";
            await signOut(auth);
            return;
          }
          currentUser = user;
          gate.hidden = true;
          resolveReady();
        } catch (_) {
          setStatus(status, "Nao foi possivel validar o acesso. Tente novamente.");
        }
      });
    } catch (_) {
      gate.hidden = false;
      setStatus(status, "Nao foi possivel iniciar o acesso. Verifique a conexao.");
    }
  }

  function setStatus(element, message) {
    element.textContent = message;
  }
})();
