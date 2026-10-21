// Draws the layers of an image chart over its picture, each at its own opacity: an overlay of shapes (a
// `dlboard.client.artifacts.overlay.Overlay`, logged as JSON, masks included) as an SVG, or an image as it is. Both are
// scaled `contain`, as the picture is, and the shapes are fractions of the picture's size that are scaled by its pixel
// size here, so the layers line up with it at any size. Every element is built with DOM calls, never by parsing
// markup, so a label is only text.
//
// The data was checked where it was made, so none of it is checked again here. What can still go wrong (a layer that
// will not load, a kind this script does not know, a shape that cannot be drawn) is logged with `console.error`, which
// the browser tests fail on, and never stops the rest of the page or the other layers from drawing.
//
// A plain script, not a clientside callback: the image-series scrub and the modal both draw through `show`.
window.dlImageSeriesOverlay = (() => {
    const SVG_NS = "http://www.w3.org/2000/svg";
    const MIN_LABEL_SIZE = 8;
    const LABEL_SIZE_DIVISOR = 32;
    // The mask defaults (`DEFAULT_CLASS_COLORS`, `DEFAULT_MASK_OPACITY` in `overlay.py`), which the server writes ahead of
    // this script, so a mask with no `colors` is drawn in the same palette as the Python that made it.
    const { DEFAULT_CLASS_COLORS, DEFAULT_MASK_OPACITY } = window.dlMaskDefaults;

    // The color class `id` of `mask` is drawn in, or nothing if it is left see-through (`ImageMask.class_color`).
    const classColor = (mask, id) => {
        if (mask.colors) {
            // The colors are keyed by ID, which JSON keeps as a string key: indexing by the number finds it the same.
            return mask.colors[id];
        }
        return id > 0 ? DEFAULT_CLASS_COLORS[(id - 1) % DEFAULT_CLASS_COLORS.length] : undefined;
    };

    const element = (name, attributes) => {
        const el = document.createElementNS(SVG_NS, name);
        for (const [key, value] of Object.entries(attributes)) {
            el.setAttribute(key, String(value));
        }
        return el;
    };

    // `points` (fractions) as pixels in a picture `width` by `height`.
    const toPixels = (points, { width, height }) => points.map(([x, y]) => [x * width, y * height]);

    // A line through `points`, closed or not: an SVG `name` (`polyline`, `polygon`), labelled at its first point.
    const throughPoints = (name) => (shape, size, style) => {
        const points = toPixels(shape.points, size);
        const outline = element(name, { points: points.map(([x, y]) => `${x},${y}`).join(" "), ...style });
        return { outline, anchor: points[0] };
    };

    // A mask (`ImageMask`, `rle`) as a PNG data URL the size of the picture: alternating `N, I` pairs, `N` pixels of
    // class `I` each, in row-major order over `shape`, whose last two dimensions are the picture's height and width.
    // A class without a color is left transparent, and the rest are drawn at the mask's opacity.
    const maskPicture = (mask, { width, height }) => {
        const [rows, cols] = mask.shape.slice(-2);
        const elements = mask.shape.reduce((product, n) => product * n, 1);
        if (rows !== height || cols !== width || elements !== width * height) {
            throw new Error(`a mask of shape ${mask.shape} does not cover a ${width} by ${height} picture`);
        }
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d");
        const picture = context.createImageData(width, height);
        const bytes = picture.data;
        const alpha = Math.round((mask.opacity ?? DEFAULT_MASK_OPACITY) * 255);
        let pixel = 0;
        for (let i = 0; i < mask.data.length; i += 2) {
            const run = mask.data[i];
            const color = classColor(mask, mask.data[i + 1]);
            if (color) {
                const [r, g, b] = [1, 3, 5].map((offset) => parseInt(color.slice(offset, offset + 2), 16));
                // Written byte by byte: a small array per pixel was the cost of drawing a mask on a large picture.
                for (let at = pixel * 4; at < (pixel + run) * 4; at += 4) {
                    bytes[at] = r;
                    bytes[at + 1] = g;
                    bytes[at + 2] = b;
                    bytes[at + 3] = alpha;
                }
            }
            pixel += run;
        }
        context.putImageData(picture, 0, 0);
        return canvas.toDataURL("image/png");
    };

    // How each kind of shape is outlined: its SVG element, and the point its label goes at.
    const SHAPE_OUTLINES = {
        box: (shape, { width, height }, style) => ({
            outline: element("rect", {
                x: shape.x * width,
                y: shape.y * height,
                width: shape.width * width,
                height: shape.height * height,
                ...style,
            }),
            anchor: [shape.x * width, shape.y * height],
        }),
        polyline: throughPoints("polyline"),
        polygon: throughPoints("polygon"),
        mask: (shape, size) => ({
            outline: element("image", {
                href: maskPicture(shape, size),
                x: 0,
                y: 0,
                width: size.width,
                height: size.height,
                preserveAspectRatio: "none",
            }),
            anchor: [0, 0],
        }),
    };

    const shapeGroup = (shape, size, labelSize) => {
        const outlineOf = SHAPE_OUTLINES[shape.kind];
        if (!outlineOf) {
            throw new Error(`no outline for a shape of kind "${shape.kind}"`);
        }
        const style = {
            fill: "none",
            stroke: shape.color,
            "stroke-width": 2,
            "vector-effect": "non-scaling-stroke",
        };
        const { outline, anchor } = outlineOf(shape, size, style);
        const group = element("g", {});
        group.append(outline);
        if (shape.label) {
            const label = element("text", {
                x: anchor[0] + 2,
                y: anchor[1] + labelSize,
                "font-size": labelSize,
                fill: shape.color,
                stroke: "#000000",
                "stroke-width": labelSize / 8,
                "paint-order": "stroke",
            });
            label.textContent = shape.label;
            group.append(label);
        }
        return group;
    };

    const overlaySvg = (overlay, size) => {
        const labelSize = Math.max(MIN_LABEL_SIZE, size.height / LABEL_SIZE_DIVISOR);
        const svg = element("svg", {
            viewBox: `0 0 ${size.width} ${size.height}`,
            preserveAspectRatio: "xMidYMid meet",
        });
        for (const shape of overlay.shapes) {
            try {
                svg.append(shapeGroup(shape, size, labelSize));
            } catch (error) {
                // One shape that cannot be drawn does not hide the rest.
                console.error("image overlay: a shape was not drawn:", shape, error);
            }
        }
        return svg;
    };

    // How each kind of layer is drawn into its `holder`, given the layer (`{url, kind, layer}`) and what the drawing
    // needs of the picture it goes over (`context`: its element and URL, and whether this request is still the latest).
    const LAYER_DRAWERS = {
        picture: (holder, { url }) => {
            const img = document.createElement("img");
            img.alt = "";
            img.src = url;
            img.onerror = () => console.error("image overlay: a picture layer did not load:", url);
            holder.append(img);
        },
        shapes: (holder, { url }, { picture, pictureUrl, isCurrent }) => {
            const isShown = () =>
                picture.getAttribute("src") === pictureUrl && picture.complete && picture.naturalWidth > 0;
            const size = () => ({ width: picture.naturalWidth, height: picture.naturalHeight });
            // Draw once the picture is on screen, unless the request is overtaken first.
            const draw = (overlay) => {
                if (isShown()) {
                    holder.replaceChildren(overlaySvg(overlay, size()));
                    return;
                }
                const onLoad = () => {
                    if (!isCurrent()) {
                        picture.removeEventListener("load", onLoad);
                    } else if (isShown()) {
                        picture.removeEventListener("load", onLoad);
                        holder.replaceChildren(overlaySvg(overlay, size()));
                    }
                };
                picture.addEventListener("load", onLoad);
            };
            fetch(url)
                .then((response) => {
                    if (!response.ok) {
                        throw new Error(`answered ${response.status}`);
                    }
                    return response.json();
                })
                .then((overlay) => {
                    if (isCurrent()) {
                        holder.dlClasses = classesOf(overlay);
                        draw(overlay);
                        updateLegend(holder.parentElement);
                    }
                })
                .catch((error) => console.error("image overlay: a shapes layer was not drawn:", url, error));
        },
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

    // What the layers in a file hold, for the legend: each class of a mask (named if it was, else "class N") and each
    // labelled shape, in its color. Kept apart by layer: a class of the same name in two layers is two classes.
    const classesOf = ({ shapes }) => {
        const found = new Map();
        for (const shape of shapes) {
            if (shape.kind === "mask") {
                const ids = new Set(shape.data.filter((_, i) => i % 2 === 1));
                for (const id of [...ids].sort((a, b) => a - b)) {
                    const color = classColor(shape, id);
                    if (color) {
                        found.set(shape.classnames?.[id] ?? `class ${id}`, color);
                    }
                }
            } else if (shape.label) {
                found.set(shape.label, shape.color);
            }
        }
        return [...found].map(([label, color]) => ({ label, color }));
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

    // Shows or hides a layer's holder and sets its opacity. A layer that has never been shown is drawn when it first is, so
    // a hidden layer costs nothing until someone wants it.
    const applyStyle = (holder, style) => {
        holder.hidden = style.hidden;
        holder.style.opacity = String(style.opacity);
        if (!style.hidden && holder.dlDraw) {
            const draw = holder.dlDraw;
            holder.dlDraw = null;
            draw();
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
            applyStyle(holder, effectiveStyle(root, holder.dataset.dlLayer));
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

    // Draws `layers` (bottom first) over `picture`, which is showing `pictureUrl`, into `layer`. No layers clears it. A
    // layer element remembers the request it was last given, so a request that the slider (or another modal open) has
    // overtaken draws nothing, and the same one is not drawn twice. How each is drawn (hidden, opacity) is not part of
    // the request: it is applied after, and changing it never draws anything again.
    const show = (layer, picture, pictureUrl, layers) => {
        const request = `${pictureUrl} ${JSON.stringify(layers)}`;
        if (layer.dataset.request === request) {
            return;
        }
        layer.dataset.request = request;
        layer.replaceChildren();
        const context = { picture, pictureUrl, isCurrent: () => layer.dataset.request === request };
        for (const spec of layers) {
            // Made in order, before anything has loaded, so the layers stack as they are listed.
            const holder = document.createElement("div");
            holder.className = "dl-image-series-layer";
            holder.dataset.dlLayer = spec.layer;
            layer.append(holder);
            const draw = LAYER_DRAWERS[spec.kind];
            if (!draw) {
                console.error(`image overlay: no way to draw a layer of kind "${spec.kind}":`, spec.url);
                continue;
            }
            holder.dlDraw = () => {
                try {
                    draw(holder, spec, context);
                } catch (error) {
                    console.error("image overlay: a layer was not drawn:", spec.url, error);
                }
            };
        }
        restyleOverlay(layer);
        // A picture's size is known once it has loaded, and the legend is anchored to it.
        picture.addEventListener("load", () => updateLegend(layer), { once: true });
    };

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
