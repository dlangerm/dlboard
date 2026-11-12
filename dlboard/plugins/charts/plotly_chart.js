// A Plotly chart's figures (`plotly_chart.py`): each run's at the step on show, fetched from its artifact URL, dressed
// in the page's theme and handed to its graph. The theme's colors are CSS variables, which Plotly cannot read, so they
// are resolved here, and again whenever the color scheme changes.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function plotlyChartFigures(step, payload) {
    // On `window` because Dash takes this file as one function expression, with nothing outside it.
    window.dlPlotlyChart ??= (() => {
        const cachedFigures = 64;
        const combinedRuns = "combined";
        const bindingsMember = "dlboard_bindings";

        // Fetched figures by URL, the most recently used last: moving the slider back asks the server for nothing, and
        // the browser keeps the rest (an artifact's URL is cached for good).
        const cache = new Map();
        const load = (url) => {
            let figure = cache.get(url);
            if (figure) {
                cache.delete(url);
            } else {
                figure = fetch(url, { credentials: "same-origin" }).then((response) => {
                    if (!response.ok) {
                        throw new Error(`${response.status} fetching ${url}`);
                    }
                    return response.json();
                });
                figure.catch(() => cache.delete(url));
            }
            cache.set(url, figure);
            while (cache.size > cachedFigures) {
                cache.delete(cache.keys().next().value);
            }
            return figure;
        };

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
        // The page's own computed color is what a variable means to it, and one pixel of a canvas painted with it is
        // that color as the red, green and blue Plotly can parse (as `series_band_colors.js` does).
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

        // The run's figure at the latest step it logged one at or before `step`; its first, if it has none yet.
        const urlAt = (urls, step) => {
            const steps = Object.keys(urls)
                .map(Number)
                .sort((a, b) => a - b);
            return urls[steps.filter((logged) => logged <= step).pop() ?? steps[0]];
        };

        const unavailable = (text) => ({
            data: [],
            layout: {
                annotations: [{ text, showarrow: false, xref: "paper", yref: "paper", x: 0.5, y: 0.5 }],
                xaxis: { visible: false },
                yaxis: { visible: false },
            },
        });

        // Every run's traces in the first run's figure, each named and grouped by its run, so a legend click on one
        // hides a run's traces together. Frames are left out: one run's animation is not the others'.
        const combined = (payload, loaded) => ({
            data: loaded.flatMap(({ run, figure }) =>
                (figure.data ?? []).map((trace, index) => ({
                    ...trace,
                    name: `${payload.runs[run].name} · ${trace.name ?? `trace ${index}`}`,
                    legendgroup: `run-${run}`,
                })),
            ),
            layout: loaded[0].figure.layout ?? {},
        });

        // The figure as its graph draws it, but for the theme: its own size gives way to the chart's, and its view is
        // kept across steps unless it asks otherwise.
        const dressed = (figure, payload, step) => {
            const { [bindingsMember]: _bindings, ...plain } = figure;
            const { height: _height, width: _width, ...layout } = plain.layout ?? {};
            return {
                ...plain,
                layout: {
                    ...layout,
                    autosize: true,
                    uirevision: payload.keep_view ? (layout.uirevision ?? "dlboard") : String(step),
                },
            };
        };
        const themed = (figure, theme) =>
            theme ? { ...figure, layout: { ...figure.layout, template: resolve(theme) } } : figure;

        // Each graph's figure as drawn, but for the theme, by its id: what is drawn again in a new color scheme.
        const drawn = new Map();
        new MutationObserver(() => {
            for (const [key, { id, figure, theme }] of drawn) {
                if (!document.getElementById(key)) {
                    drawn.delete(key);
                } else if (theme) {
                    window.dash_clientside.set_props(id, { figure: themed(figure, theme) });
                }
            }
        }).observe(document.documentElement, {
            attributes: true,
            attributeFilter: ["data-mantine-color-scheme"],
        });

        const figureFor = async (id, payload, step) => {
            const runs = id.run === combinedRuns ? payload.order.map(String) : [id.run];
            let figure;
            try {
                const loaded = await Promise.all(
                    runs.map(async (run) => ({
                        run,
                        figure: await load(urlAt(payload.runs[run].urls, step)),
                    })),
                );
                figure = dressed(
                    id.run === combinedRuns ? combined(payload, loaded) : loaded[0].figure,
                    payload,
                    step,
                );
            } catch {
                figure = unavailable("This figure could not be loaded.");
            }
            // A pattern-matching id is the element's DOM id as JSON with its keys sorted.
            drawn.set(JSON.stringify(id, Object.keys(id).sort()), { id, figure, theme: payload.theme });
            return themed(figure, payload.theme);
        };

        // The steps either side of the one on show, fetched ahead, so the slider's next move draws at once.
        const prefetch = (payload, step) => {
            const at = payload.steps.filter((logged) => logged <= step).length - 1;
            for (const near of [payload.steps[at - 1], payload.steps[at + 1]]) {
                if (near === undefined) {
                    continue;
                }
                for (const run of payload.order) {
                    load(urlAt(payload.runs[run].urls, near)).catch(() => {});
                }
            }
        };

        return { figureFor, prefetch };
    })();
    // Dash takes the callback's context away as soon as this returns its promise, so it is read first.
    const outputs = window.dash_clientside.callback_context.outputs_list;
    if (!payload || step === null || step === undefined) {
        return window.dash_clientside.no_update;
    }
    window.dlPlotlyChart.prefetch(payload, step);
    return Promise.all(outputs.map((output) => window.dlPlotlyChart.figureFor(output.id, payload, step)));
}
