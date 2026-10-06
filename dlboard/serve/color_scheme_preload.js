// Inlined at the top of <head> (see `app.py`), so it runs before any stylesheet or the Dash bundle
// loads: stamps the color scheme Mantine will pick onto <html> up front, which lets the page paint
// its themed background immediately instead of flashing white until React mounts. Reads the same
// localStorage key Mantine's own color-scheme manager writes, falling back to the theme's default.
(() => {
    const script = document.currentScript;
    let scheme = script.dataset.defaultScheme;
    try {
        scheme = localStorage.getItem("mantine-color-scheme-value") || scheme;
    } catch (_) {
        // Storage blocked (private mode, sandboxed frame): the theme default is fine.
    }
    if (scheme === "auto") {
        scheme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    document.documentElement.setAttribute("data-mantine-color-scheme", scheme);
})();
