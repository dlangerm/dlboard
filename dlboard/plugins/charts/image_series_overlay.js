// The legend and the viewer's changes over an image chart's pictures: which layers are shown, hidden or soloed from a legend
// on each picture, what each layer holds, and holding a picture to peek at it without them. The layers themselves are
// drawn by `_layered_picture.js` (`window.dlLayeredPicture`), which this styles from the chart's defaults and what the
// viewer has changed (`window.dlChartViewState`).
//
// A plain script, not a clientside callback: the image-series scrub and the modal both draw through `show`.
window.dlImageSeriesOverlay = (() => {
    const SVG_NS = "http://www.w3.org/2000/svg";

    const element = (name, attributes) => {
        const el = document.createElementNS(SVG_NS, name);
        for (const [key, value] of Object.entries(attributes)) {
            el.setAttribute(key, String(value));
        }
        return el;
    };

    // A chart's own element, which holds what its layers are by default (`data-dl-defaults`, by the layer's name) and the
    // key its viewer's changes are kept under (`data-dl-view-key`). Found by the chart's instance, since its enlarged view
    // is not inside it.
    const chartRoot = (instance) =>
        document.querySelector(`.dl-image-series[data-dl-instance="${CSS.escape(instance)}"]`);

    // What a chart's viewer changes are kept under: its card's key, or for a chart with none (a preview) this one drawing.
    const viewKey = (root) => root.dataset.dlViewKey || `instance:${root.dataset.dlInstance}`;

    const viewerState = (root) => window.dlChartViewState.read(viewKey(root)) ?? {};

    // How the viewer has chosen to look at a chart, which is no change to it: it never says "View changed", and it is not
    // saved with "Save as default". Whether every overlay is hidden (or peeked past), and whether the legend lists its
    // layers: it starts folded to its title, to leave the picture clear.
    const looking = (root) => window.dlChartViewState.local(viewKey(root));
    const overlaysHidden = (root) => looking(root).overlaysHidden === true;
    const setOverlaysHidden = (root, hidden) =>
        window.dlChartViewState.setLocal(viewKey(root), { overlaysHidden: hidden });
    const legendOpen = (root) => looking(root).legendOpen === true;
    const toggleLegend = (root) => {
        window.dlChartViewState.setLocal(viewKey(root), { legendOpen: !legendOpen(root) });
        restyle(root.dataset.dlInstance);
    };

    // What a chart's layers are by default, parsed once for as long as the chart keeps the same `data-dl-defaults`.
    const parsedDefaults = new WeakMap();
    const layerDefaults = (root) => {
        const raw = root.dataset.dlDefaults || "{}";
        if (parsedDefaults.get(root)?.raw !== raw) {
            parsedDefaults.set(root, { raw, parsed: JSON.parse(raw) });
        }
        return parsedDefaults.get(root).parsed;
    };

    // The keys of a chart's layers, bottom first: what their styles, and a viewer's changes to them, are kept under.
    const layerNames = (root) => Object.keys(layerDefaults(root));
    // What a layer is called in the legend.
    const layerLabel = (root, key) => layerDefaults(root)[key]?.label ?? key;

    // Changes what a chart's viewer has changed, and draws it.
    const changeViewerState = (root, change) => {
        const state = { layers: {}, ...viewerState(root) };
        change(state);
        window.dlChartViewState.write(viewKey(root), state);
        restyle(root.dataset.dlInstance);
    };

    // How layer `name` of the chart `root` is by itself: as the chart has it, over which the viewer's change.
    const layerStyle = (root, name) => {
        const defaults = layerDefaults(root)[name] ?? { hidden: false, opacity: 1 };
        return { ...defaults, ...viewerState(root).layers?.[name] };
    };

    // How it is drawn now: the same, but hidden if the viewer has hidden every overlay.
    const effectiveStyle = (root, name) => {
        const style = layerStyle(root, name);
        return { ...style, hidden: style.hidden || overlaysHidden(root) };
    };

    // The icons are drawn with DOM calls like everything else here, never by parsing markup.
    const icon = (paths) => {
        const svg = element("svg", {
            viewBox: "0 0 24 24",
            width: 15,
            height: 15,
            fill: "none",
            stroke: "currentColor",
            "stroke-width": 2,
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
            "aria-hidden": "true",
        });
        for (const d of paths) {
            svg.append(element("path", { d }));
        }
        return svg;
    };
    const EYE = [
        "M10 12a2 2 0 1 0 4 0a2 2 0 0 0 -4 0",
        "M21 12c-2.4 4 -5.4 6 -9 6c-3.6 0 -6.6 -2 -9 -6c2.4 -4 5.4 -6 9 -6c3.6 0 6.6 2 9 6",
    ];
    const EYE_OFF = [
        "M10.585 10.587a2 2 0 0 0 2.829 2.828",
        "M16.681 16.673a8.717 8.717 0 0 1 -4.681 1.327c-3.6 0 -6.6 -2 -9 -6c1.272 -2.12 2.712 -3.678 4.32 -4.674m2.86 -1.146a9.055 9.055 0 0 1 1.82 -.18c3.6 0 6.6 2 9 6c-.666 1.11 -1.379 2.067 -2.138 2.87",
        "M3 3l18 18",
    ];

    const make = (tag, className, text) => {
        const el = document.createElement(tag);
        el.className = className;
        if (text !== undefined) {
            el.textContent = text;
        }
        return el;
    };

    // The legend of one picture, built once for its layers: the eye (every overlay, and peek), then a row for each layer with
    // a toggle and, beneath, the classes it holds. After that it is only updated, so a click is never on an element that
    // has been replaced since the press.
    const buildLegend = (legend, root, keys) => {
        const eye = make("button", "dl-layer-eye");
        eye.type = "button";
        eye.title = "Hide or show every overlay. Hold to peek at the picture without them.";
        eye.setAttribute("aria-label", "Overlays");
        const shown = icon(EYE);
        shown.classList.add("dl-eye-shown");
        const hidden = icon(EYE_OFF);
        hidden.classList.add("dl-eye-hidden");
        eye.append(shown, hidden);
        const rows = make("div", "dl-layer-rows");
        for (const key of keys) {
            const label = layerLabel(root, key);
            const row = make("div", "dl-layer-row");
            row.dataset.dlLayer = key;
            const toggle = make("button", "dl-layer-toggle");
            toggle.type = "button";
            toggle.title = `Show or hide ${label}. Shift-click to show only ${label}.`;
            toggle.append(make("span", "dl-layer-box"), make("span", "dl-layer-name", label));
            row.append(toggle, make("span", "dl-layer-classes"));
            rows.append(row);
        }
        const title = make("button", "dl-layer-title", "Layers");
        title.type = "button";
        title.title = "Show or hide the list of layers";
        legend.replaceChildren(make("div", "dl-layer-head"), rows);
        legend.firstChild.append(eye, title);
    };

    // Anchors the legend to the top-left of the picture itself, which is letterboxed (`contain`) inside its box, and keeps it
    // inside the picture. On a picture too small for a list it shrinks to the eye alone.
    const LEGEND_INSET = 6;
    const COMPACT_BELOW = 152;
    const fitLegend = (overlay, legend) => {
        const picture = overlay.parentElement?.querySelector("img");
        if (!picture?.naturalWidth) {
            return;
        }
        const thumb = overlay.parentElement.getBoundingClientRect();
        const box = picture.getBoundingClientRect();
        const scale = Math.min(box.width / picture.naturalWidth, box.height / picture.naturalHeight);
        const [width, height] = [picture.naturalWidth * scale, picture.naturalHeight * scale];
        legend.style.left = `${box.left - thumb.left + (box.width - width) / 2 + LEGEND_INSET}px`;
        legend.style.top = `${box.top - thumb.top + (box.height - height) / 2 + LEGEND_INSET}px`;
        legend.style.maxWidth = `${Math.max(0, width - 2 * LEGEND_INSET)}px`;
        legend.style.maxHeight = `${Math.max(0, height - 2 * LEGEND_INSET)}px`;
        legend.toggleAttribute("data-dl-compact", width < COMPACT_BELOW);
    };

    // Brings the legend over one picture up to date: what is shown, what this picture has nothing for, what each layer holds.
    const updateLegend = (overlay) => {
        const legend = overlay.parentElement?.querySelector(":scope > .dl-layer-legend");
        const root = chartRoot(overlay.dataset.dlInstance);
        if (!legend || !root) {
            return;
        }
        const names = layerNames(root);
        if (legend.dataset.layers !== names.join("\n")) {
            buildLegend(legend, root, names);
            legend.dataset.layers = names.join("\n");
            // The picture's box changes with the window and with the panel's columns.
            if (!legend.dlResize) {
                legend.dlResize = new ResizeObserver(() => updateLegend(overlay));
                legend.dlResize.observe(overlay.parentElement);
            }
        }
        legend.querySelector(".dl-layer-eye").setAttribute("aria-pressed", String(!overlaysHidden(root)));
        legend.toggleAttribute("data-dl-open", legendOpen(root));
        fitLegend(overlay, legend);
        const holders = new Map(
            [...overlay.querySelectorAll(":scope > .dl-image-series-layer")].map((h) => [
                h.dataset.dlLayer,
                h,
            ]),
        );
        for (const row of legend.querySelectorAll(".dl-layer-row")) {
            const name = row.dataset.dlLayer;
            const holder = holders.get(name);
            row.querySelector(".dl-layer-toggle").setAttribute(
                "aria-pressed",
                String(!layerStyle(root, name).hidden),
            );
            row.toggleAttribute("data-dl-absent", !holder);
            // The classes of a layer that is not shown are not on the picture, so they are not listed.
            const classes = holder && !holder.hidden ? (holder.dlClasses ?? []) : [];
            const target = row.querySelector(".dl-layer-classes");
            const signature = JSON.stringify(classes);
            if (target.dataset.classes !== signature) {
                target.dataset.classes = signature;
                target.replaceChildren(
                    ...classes.map(({ label, color }) => {
                        const entry = make("span", "dl-class");
                        const swatch = make("span", "dl-class-swatch");
                        swatch.style.background = color;
                        entry.append(swatch, label);
                        return entry;
                    }),
                );
            }
        }
    };

    // Applies the chart's styles to every layer of one overlay element (a thumbnail's, or the enlarged view's), and brings
    // the legend over it up to date.
    const restyleOverlay = (overlay) => {
        const root = chartRoot(overlay.dataset.dlInstance);
        if (!root) {
            return;
        }
        for (const holder of overlay.querySelectorAll(":scope > .dl-image-series-layer")) {
            window.dlLayeredPicture.applyStyle(holder, effectiveStyle(root, holder.dataset.dlLayer));
        }
        updateLegend(overlay);
    };

    // Applies them to a whole chart: its thumbnails and its enlarged view. A chart that draws marks its card, which shows its
    // "View changed" bar if the viewer has changed it.
    const restyle = (instance) => {
        const root = chartRoot(instance);
        if (root?.dataset.dlViewKey) {
            window.dlChartViewState.mark(root.dataset.dlViewKey);
        }
        for (const overlay of document.querySelectorAll(
            `.dl-image-series-overlay[data-dl-instance="${CSS.escape(instance)}"]`,
        )) {
            restyleOverlay(overlay);
        }
    };

    // Draws `layers` (bottom first) over `picture`, which is showing `pictureUrl`, into `layer` (see
    // `window.dlLayeredPicture.show`), each as this chart's defaults and its viewer's changes have it, and brings the legend
    // up to date.
    const show = (layer, picture, pictureUrl, layers) => {
        const root = chartRoot(layer.dataset.dlInstance);
        window.dlLayeredPicture.show(layer, picture, pictureUrl, layers, (name) =>
            root ? effectiveStyle(root, name) : { hidden: false, opacity: 1 },
        );
        updateLegend(layer);
        // A picture's size is known once it has loaded, and the legend is anchored to it.
        picture.addEventListener("load", () => updateLegend(layer), { once: true });
    };

    // A shapes layer has been drawn: what it holds is now known to the legend.
    document.addEventListener("dl-layer-drawn", ({ target }) => {
        const overlay = target.closest?.(".dl-image-series-overlay");
        if (overlay) {
            updateLegend(overlay);
        }
    });

    // Shows `name` alone, or if it is the only one shown already, every layer again: for comparing one layer with the rest.
    const solo = (root, name) => {
        const names = layerNames(root);
        const alone = names.every((other) => (other === name) === !layerStyle(root, other).hidden);
        setOverlaysHidden(root, false);
        changeViewerState(root, (state) => {
            for (const other of names) {
                state.layers[other] = { hidden: alone ? false : other !== name };
            }
        });
    };

    // Peek: holding a picture (or the eye) for a moment shows the picture without its layers until it is let go, to see
    // what is under them. The click that ends a hold is not a click: it would open the picture, or toggle the eye.
    const PEEK_MS = 250;
    const SWALLOW_MS = 400;
    let peek = null;
    let swallowing = false;

    // The legend. A click on a layer's row shows or hides that layer, for this viewer: kept in the browser, and saved as the
    // chart's own only if the viewer asks to. The eye hides every layer, or shows them again. It is handled before the
    // picture sees it, as a click on the legend is not a click on the picture (which opens it).
    document.addEventListener(
        "click",
        (event) => {
            const held = event.target.closest?.(".dl-layer-eye, .dl-image-series-thumb");
            if (swallowing && held) {
                swallowing = false;
                event.stopPropagation();
                event.preventDefault();
                return;
            }
            const button = event.target.closest?.(".dl-layer-legend button");
            const root = button?.closest(".dl-image-series");
            if (!root) {
                return;
            }
            event.stopPropagation();
            if (button.classList.contains("dl-layer-eye")) {
                setOverlaysHidden(root, !overlaysHidden(root));
                restyle(root.dataset.dlInstance);
                return;
            }
            if (button.classList.contains("dl-layer-title")) {
                toggleLegend(root);
                return;
            }
            const name = button.closest(".dl-layer-row").dataset.dlLayer;
            if (event.shiftKey) {
                solo(root, name);
                return;
            }
            const hidden = !layerStyle(root, name).hidden;
            if (!hidden) {
                setOverlaysHidden(root, false); // showing a layer means seeing it
            }
            changeViewerState(root, (state) => {
                state.layers[name] = { hidden };
            });
        },
        true,
    );

    document.addEventListener("pointerdown", (event) => {
        const eye = event.target.closest?.(".dl-layer-eye");
        if (!eye && event.target.closest?.(".dl-layer-legend")) {
            return; // pressing a layer's row is not holding the picture
        }
        const held = eye ?? event.target.closest?.(".dl-image-series-thumb");
        const root = held?.closest(".dl-image-series");
        if (!root) {
            return;
        }
        peek = { root, active: false };
        peek.timer = setTimeout(() => {
            peek.active = true;
            root.setAttribute("data-dl-peek", "");
        }, PEEK_MS);
    });
    const endPeek = () => {
        if (!peek) {
            return;
        }
        clearTimeout(peek.timer);
        if (peek.active) {
            peek.root.removeAttribute("data-dl-peek");
            swallowing = true;
            setTimeout(() => {
                swallowing = false;
            }, SWALLOW_MS);
        }
        peek = null;
    };
    document.addEventListener("pointerup", endPeek);
    document.addEventListener("pointercancel", endPeek);
    // A long press on a touch screen opens a menu, which a peek must not.
    document.addEventListener("contextmenu", (event) => {
        if (peek?.active) {
            event.preventDefault();
        }
    });

    // The viewer's changes were dropped (Reset) or saved: every chart of that key draws what it has now.
    document.addEventListener("dl-chart-view-state", ({ detail: { key } }) => {
        for (const root of document.querySelectorAll(".dl-image-series")) {
            if (viewKey(root) === key) {
                restyle(root.dataset.dlInstance);
            }
        }
    });

    return { show, restyle };
})();
