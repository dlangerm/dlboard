// Plotly cannot read a CSS variable, so the band chart's figure names its colors as `var(--dl-series-3)` (or
// `var(--dl-series-3)/0.12`, the same at an opacity), and this turns them into the colors the page's theme gives
// them (`plotly_colors.js`): when the figure arrives, and again whenever the color scheme changes. Every other chart
// draws a run in `series_color`'s variable, so a band's runs are the colors of theirs, in any theme.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function seriesBandFigure(tokens) {
    const { resolve } = window.dlPlotlyColors;
    // On `window` because Dash takes this file as one function expression, with nothing outside it.
    window.dlSeriesBand ??= (() => {
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
        return { figures };
    })();
    // A pattern-matching id is the element's DOM id as JSON with its keys sorted.
    const id = window.dash_clientside.callback_context.outputs_list.id;
    window.dlSeriesBand.figures.set(JSON.stringify(id, Object.keys(id).sort()), tokens);
    return resolve(tokens);
}
