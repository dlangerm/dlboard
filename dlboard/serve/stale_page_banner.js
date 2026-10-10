// Shows one "reload" prompt when the server says this page is from an older version of the app
// (a 409 on a callback request -- see `_stale_page.py`), instead of every poll failing quietly.
//
// A prompt rather than an automatic reload: whatever someone has half-typed in this page survives
// until they choose to reload, and a server that keeps answering 409 can't put the page in a loop.
(() => {
    const BANNER_ID = "dl-stale-page";
    const CONFLICT = 409;
    const originalFetch = window.fetch.bind(window);

    function showBanner() {
        if (document.getElementById(BANNER_ID)) {
            return;
        }
        const banner = document.createElement("div");
        banner.id = BANNER_ID;
        banner.setAttribute("role", "alert");
        banner.style.cssText = [
            "position: fixed",
            "top: 0.75rem",
            "left: 50%",
            "transform: translateX(-50%)",
            "z-index: 10000",
            "display: flex",
            "align-items: center",
            "gap: 0.75rem",
            "padding: 0.5rem 0.75rem",
            "background: var(--mantine-color-body)",
            "color: var(--mantine-color-text)",
            "border: 1px solid var(--mantine-color-default-border)",
            "border-radius: var(--mantine-radius-md)",
            "box-shadow: var(--mantine-shadow-md)",
            "font-size: var(--mantine-font-size-sm)",
        ].join("; ");
        const message = document.createElement("span");
        message.textContent = "dlboard was updated since this page loaded.";
        const reload = document.createElement("button");
        reload.type = "button";
        reload.textContent = "Reload";
        reload.addEventListener("click", () => window.location.reload());
        banner.append(message, reload);
        document.body.append(banner);
    }

    window.fetch = async (input, init) => {
        const response = await originalFetch(input, init);
        const url = typeof input === "string" ? input : input.url;
        if (response.status === CONFLICT && url.includes("_dash-update-component")) {
            showBanner();
        }
        return response;
    };
})();
