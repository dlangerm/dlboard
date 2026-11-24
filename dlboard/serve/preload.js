// Inlined at the top of <head> (see `app.py`), so it runs before any stylesheet or the Dash bundle
// loads: stamps the color scheme Mantine will pick and the density (`_density.py`) onto <html> up
// front, which lets the page paint themed and at its density immediately instead of flashing white,
// or shifting, until React mounts. Reads the same localStorage keys Mantine's own color-scheme
// manager and the density's `dcc.Store` (JSON under its id) write, falling back to the defaults.
(() => {
    const script = document.currentScript;
    let scheme = script.dataset.defaultScheme;
    let density = script.dataset.defaultDensity;
    try {
        scheme = localStorage.getItem("mantine-color-scheme-value") || scheme;
        density = JSON.parse(localStorage.getItem(script.dataset.densityKey)) || density;
    } catch (_) {
        // Storage blocked (private mode, sandboxed frame), or a value that isn't JSON: the defaults are fine.
    }
    if (scheme === "auto") {
        scheme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    document.documentElement.setAttribute("data-mantine-color-scheme", scheme);
    document.documentElement.setAttribute("data-dl-density", density);
})();
