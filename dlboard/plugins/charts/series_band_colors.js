// Plotly cannot read a CSS variable, so the band chart's figure names its colors as `var(--dl-series-3)` (or
// `var(--dl-series-3)/0.12`, the same at an opacity), and this turns them into the colors the page's theme gives
// them: when the figure arrives, and again whenever the color scheme changes. Every other chart draws a run in
// `series_color`'s variable, so a band's runs are the colors of theirs, in any theme.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function seriesBandFigure(tokens) {
    // On `window` because Dash takes this file as one function expression, with nothing outside it.
    window.dlSeriesBand ??= (() => {
        const colorKeys = new Set(["color", "fillcolor", "gridcolor", "linecolor", "zerolinecolor"]);
        const token = /(var\(--[\w-]+\))(?:\/([\d.]+))?/g;
        // The page's own computed color is what a variable means to it (a theme may give one in any color space),
        // and one pixel of a canvas painted with it is that color as the red, green and blue Plotly can parse.
        const probe = document.createElement("span");
        probe.hidden = true;
        document.body.append(probe);
        const canvas = document.createElement("canvas");
        canvas.width = 1;
        canvas.height = 1;
        const pixel = canvas.getContext("2d", { willReadFrequently: true });
        const rgb = (variable) => {
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
        // Each band chart's figure as the server sent it, by the DOM id of its graph, to draw again in a new scheme.
        const figures = new Map();
        const redraw = () => {
            for (const [domId, unresolved] of figures) {
                const graph = document.getElementById(domId)?.querySelector(".js-plotly-plot");
                if (!graph) {
                    figures.delete(domId);
                    continue;
                }
                const figure = resolve(unresolved);
                window.Plotly?.react(graph, figure.data, figure.layout);
            }
        };
        new MutationObserver(redraw).observe(document.documentElement, {
            attributes: true,
            attributeFilter: ["data-mantine-color-scheme"],
        });
        return { figures, resolve };
    })();
    // A pattern-matching id is the element's DOM id as JSON with its keys sorted.
    const id = window.dash_clientside.callback_context.outputs_list.id;
    window.dlSeriesBand.figures.set(JSON.stringify(id, Object.keys(id).sort()), tokens);
    return window.dlSeriesBand.resolve(tokens);
}
