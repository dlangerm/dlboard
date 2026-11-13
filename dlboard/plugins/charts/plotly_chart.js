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

        // The page's colors, as Plotly can read them (`plotly_colors.js`).
        const { resolveColor, resolve } = window.dlPlotlyColors;

        // The run's figure at the latest step it logged one at or before `step`; its first, if it has none yet. None if
        // the window of steps the chart has holds none of the run's (it is read as the slider gets there).
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

        // A trace of one run's among every run's: named and grouped by the run, so a legend click hides a run's traces
        // together, and in the run's color unless the trace has one of its own, as every other chart draws a run.
        const ofRun = (trace, run, runId) => ({
            ...trace,
            name: trace.name ? `${run.name} · ${trace.name}` : run.name,
            legendgroup: `run-${runId}`,
            line: { color: run.color, ...trace.line },
            marker: { color: run.color, ...trace.marker },
        });

        // Every run's traces in the first run's figure. Frames are left out: one run's animation is not the others'.
        const combined = (payload, loaded) => ({
            data: loaded.flatMap(({ run, figure }) =>
                (figure.data ?? []).map((trace) => ofRun(trace, payload.runs[run], run)),
            ),
            layout: loaded[0].figure.layout ?? {},
        });

        // `node` with the value at `path` (`["marker", "color"]`) set, copying only what is on the way to it.
        const withValue = (node, [name, ...rest], value) => ({
            ...node,
            [name]: rest.length ? withValue(node?.[name] ?? {}, rest, value) : value,
        });

        // The template's traces with one run's data in the attributes its bindings name: each record's column after
        // the one before, so a list of values grows by each step's and a 2-D column by its rows. A trace no binding
        // names is as it was logged.
        const filled = (template, records) => {
            const bindings = template[bindingsMember] ?? {};
            return (template.data ?? []).map((trace, index) =>
                Object.entries(bindings[index] ?? {}).reduce(
                    (bound, [path, column]) =>
                        withValue(
                            bound,
                            path.split("."),
                            records.flatMap((record) => record.columns?.[column] ?? []),
                        ),
                    trace,
                ),
            );
        };

        // A template drawn with one run's data, or every run's: each run's bound traces then, the rest once.
        const templated = async (runs, payload, step, combine) => {
            const template = await load(payload.template);
            const scrubbed = payload.data_steps === "scrub";
            const traces = await Promise.all(
                runs.map(async (run) => {
                    const urls = payload.runs[run].urls;
                    const records = (scrubbed ? [urlAt(urls, step)] : Object.values(urls)).filter(Boolean);
                    return { run, traces: filled(template, await Promise.all(records.map(load))) };
                }),
            );
            if (!combine) {
                return { ...template, data: traces[0].traces };
            }
            const bound = new Set(Object.keys(template[bindingsMember] ?? {}).map(Number));
            const { frames: _frames, ...still } = template;
            return {
                ...still,
                data: [
                    ...(template.data ?? []).filter((_, index) => !bound.has(index)),
                    ...traces.flatMap(({ run, traces: own }) =>
                        own
                            .filter((_, index) => bound.has(index))
                            .map((trace) => ofRun(trace, payload.runs[run], run)),
                    ),
                ],
            };
        };

        // Not a safeguard: the page's CSP refuses whatever a figure would load from another server (#622). While it
        // does, a map's own style, which names its tiles on one, would leave the map blank, so a map is drawn on the
        // deployment's tiles (`DLBOARD_PLOTLY_MAP_TILES`) instead, or on a plain background.
        const mapStyle = (tiles) =>
            tiles
                ? {
                      version: 8,
                      sources: { dlboard: { type: "raster", tiles: [tiles], tileSize: 256 } },
                      layers: [{ id: "dlboard", type: "raster", source: "dlboard" }],
                  }
                : "white-bg";
        const onAllowedMaps = (layout, remote) =>
            remote.remote_resources
                ? layout
                : Object.fromEntries(
                      Object.entries(layout).map(([name, value]) => [
                          name,
                          /^(map|mapbox)\d*$/.test(name)
                              ? { ...value, style: mapStyle(remote.map_tiles) }
                              : value,
                      ]),
                  );

        // The figure as its graph draws it, but for the theme: its own size gives way to the chart's, its view is kept
        // across steps unless it asks otherwise, and a map is drawn on what the page may load.
        const dressed = (figure, payload, step) => {
            const { [bindingsMember]: _bindings, ...plain } = figure;
            const { height: _height, width: _width, ...layout } = plain.layout ?? {};
            return {
                ...plain,
                layout: {
                    ...onAllowedMaps(layout, payload.remote),
                    autosize: true,
                    uirevision: payload.keep_view ? (layout.uirevision ?? "dlboard") : String(step),
                },
            };
        };
        // The run colors traces were given (`ofRun`) and the page's theme, in the colors the page has now.
        const resolvedColor = (holder) =>
            typeof holder?.color === "string" && holder.color.startsWith("var(")
                ? { ...holder, color: resolveColor(holder.color) }
                : holder;
        const themed = (figure, theme) => ({
            ...figure,
            data: (figure.data ?? []).map((trace) => ({
                ...trace,
                ...(trace.line ? { line: resolvedColor(trace.line) } : {}),
                ...(trace.marker ? { marker: resolvedColor(trace.marker) } : {}),
            })),
            layout: theme ? { ...figure.layout, template: resolve(theme) } : figure.layout,
        });

        // Each graph's figure as drawn, but for the theme, by its id: what is drawn again in a new color scheme.
        const drawn = new Map();
        new MutationObserver(() => {
            for (const [key, { id, figure, theme }] of drawn) {
                if (document.getElementById(key)) {
                    window.dash_clientside.set_props(id, { figure: themed(figure, theme) });
                } else {
                    drawn.delete(key);
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
                if (payload.template) {
                    figure = dressed(
                        await templated(runs, payload, step, id.run === combinedRuns),
                        payload,
                        step,
                    );
                } else {
                    const shown = runs.filter((run) => urlAt(payload.runs[run].urls, step));
                    const loaded = await Promise.all(
                        shown.map(async (run) => ({
                            run,
                            figure: await load(urlAt(payload.runs[run].urls, step)),
                        })),
                    );
                    figure = loaded.length
                        ? dressed(
                              id.run === combinedRuns ? combined(payload, loaded) : loaded[0].figure,
                              payload,
                              step,
                          )
                        : unavailable("Loading this step's figure…");
                }
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
                    const url = urlAt(payload.runs[run].urls, near);
                    if (url) {
                        load(url).catch(() => {});
                    }
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
