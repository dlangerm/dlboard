// Plotly cannot read a CSS variable, so a figure dlboard draws names its colors as `var(--dl-series-3)` (or
// `var(--dl-series-3)/0.12`, the same at an opacity), and this turns them into the colors the page's theme gives them.
// Served once per app (`_plotly_colors.py`) for every chart that draws such a figure: the band chart
// (`series_band_colors.js`) and the Plotly chart (`plotly_chart.js`).
window.dlPlotlyColors = (() => {
    const colorKeys = new Set([
        "color",
        "colorway",
        "bgcolor",
        "fillcolor",
        "gridcolor",
        "linecolor",
        "zerolinecolor",
    ]);
    const token = /(var\(--[\w-]+\))(?:\/([\d.]+))?/g;
    // The page's own computed color is what a variable means to it (a theme may give one in any color space), and one
    // pixel of a canvas painted with it is that color as the red, green and blue Plotly can parse. Made at first use,
    // so the page's body is there to hold the probe.
    let probe;
    let pixel;
    const rgb = (variable) => {
        if (!probe) {
            probe = document.createElement("span");
            probe.hidden = true;
            document.body.append(probe);
            const canvas = document.createElement("canvas");
            canvas.width = 1;
            canvas.height = 1;
            pixel = canvas.getContext("2d", { willReadFrequently: true });
        }
        probe.style.color = "";
        probe.style.color = variable;
        pixel.clearRect(0, 0, 1, 1);
        pixel.fillStyle = getComputedStyle(probe).color;
        pixel.fillRect(0, 0, 1, 1);
        return pixel.getImageData(0, 0, 1, 1).data.slice(0, 3).join(", ");
    };
    const resolveColor = (text) =>
        text.replace(token, (_, variable, opacity) =>
            opacity === undefined ? `rgb(${rgb(variable)})` : `rgba(${rgb(variable)}, ${opacity})`,
        );
    // Only the keys that hold a color: a metric named `var(--x)` is in a hover text, and stays as it is.
    const resolve = (node, key) => {
        if (Array.isArray(node)) {
            return node.map((item) => resolve(item, key));
        }
        if (node && typeof node === "object") {
            return Object.fromEntries(
                Object.entries(node).map(([name, value]) => [name, resolve(value, name)]),
            );
        }
        return typeof node === "string" && colorKeys.has(key) ? resolveColor(node) : node;
    };
    return { resolveColor, resolve };
})();
