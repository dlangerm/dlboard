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
    const DEFAULT_MASK_OPACITY = 0.5;
    // What a mask with no `colors` draws its classes in: class 1 the first, class 2 the second, and so on, around.
    // `overlay.DEFAULT_CLASS_COLORS`, which a test keeps equal to this.
    const DEFAULT_CLASS_COLORS = [
        "#e6194b",
        "#3cb44b",
        "#4363d8",
        "#f58231",
        "#911eb4",
        "#42d4f4",
        "#f032e6",
        "#bfef45",
        "#fabed4",
        "#469990",
    ];

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
        const alpha = Math.round((mask.opacity ?? DEFAULT_MASK_OPACITY) * 255);
        let pixel = 0;
        for (let i = 0; i < mask.data.length; i += 2) {
            const run = mask.data[i];
            const color = classColor(mask, mask.data[i + 1]);
            if (color) {
                const [r, g, b] = [1, 3, 5].map((offset) => parseInt(color.slice(offset, offset + 2), 16));
                for (let p = pixel; p < pixel + run; p++) {
                    picture.data.set([r, g, b, alpha], p * 4);
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
                        const root = chartRoot(holder.parentElement.dataset.dlInstance);
                        if (root) {
                            refreshClasses(root);
                        }
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

    const viewerState = (root) => window.dlChartViewState?.read(viewKey(root)) ?? {};

    // Changes what a chart's viewer has changed, and draws it.
    const changeViewerState = (root, change) => {
        const state = { layers: {}, ...viewerState(root) };
        change(state);
        window.dlChartViewState.write(viewKey(root), state);
        restyle(root.dataset.dlInstance);
    };

    // How layer `name` of the chart `root` is by itself: as the chart has it, over which the viewer's change.
    const layerStyle = (root, name) => {
        const defaults = JSON.parse(root.dataset.dlDefaults || "{}")[name] ?? { hidden: false, opacity: 1 };
        return { ...defaults, ...viewerState(root).layers?.[name] };
    };

    // How it is drawn now: the same, but hidden if the viewer has hidden every overlay.
    const effectiveStyle = (root, name) => {
        const style = layerStyle(root, name);
        return { ...style, hidden: style.hidden || Boolean(viewerState(root).overlays_hidden) };
    };

    // What the layers in a file hold, for the legend: each class of a mask (named if it was, else "class N") and each
    // labelled shape, in its color.
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

    // Lists, under the layer chips, the classes and labels of the layers that are shown on the thumbnails.
    const refreshClasses = (root) => {
        const target = root.querySelector(".dl-class-legend");
        if (!target) {
            return;
        }
        const found = new Map();
        for (const holder of root.querySelectorAll(
            ".dl-image-series-thumb .dl-image-series-layer:not([hidden])",
        )) {
            for (const { label, color } of holder.dlClasses ?? []) {
                found.set(label, color);
            }
        }
        target.replaceChildren(
            ...[...found].map(([label, color]) => {
                const chip = document.createElement("span");
                chip.className = "dl-class-chip";
                const swatch = document.createElement("span");
                swatch.className = "dl-class-swatch";
                swatch.style.background = color;
                chip.append(swatch, label);
                return chip;
            }),
        );
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

    // Applies the chart's styles to every layer of one overlay element (a thumbnail's, or the enlarged view's).
    const restyleOverlay = (overlay) => {
        const root = chartRoot(overlay.dataset.dlInstance);
        if (!root) {
            return;
        }
        for (const holder of overlay.querySelectorAll(":scope > .dl-image-series-layer")) {
            applyStyle(holder, effectiveStyle(root, holder.dataset.dlLayer));
        }
    };

    // Applies them to a whole chart: its thumbnails, its enlarged view, and the legend that says what each layer is doing,
    // which of them the pictures on show have nothing for, and what they hold.
    const restyle = (instance) => {
        const root = chartRoot(instance);
        if (!root) {
            return;
        }
        for (const overlay of document.querySelectorAll(
            `.dl-image-series-overlay[data-dl-instance="${CSS.escape(instance)}"]`,
        )) {
            restyleOverlay(overlay);
        }
        const onShow = new Set(
            [...root.querySelectorAll(".dl-image-series-thumb .dl-image-series-layer")].map(
                (h) => h.dataset.dlLayer,
            ),
        );
        root.querySelector(".dl-layer-master")?.setAttribute(
            "aria-pressed",
            String(!viewerState(root).overlays_hidden),
        );
        for (const chip of root.querySelectorAll(".dl-layer-chip[data-dl-layer]")) {
            const name = chip.dataset.dlLayer;
            chip.setAttribute("aria-pressed", String(!layerStyle(root, name).hidden));
            chip.toggleAttribute("data-dl-absent", !onShow.has(name));
        }
        for (const slider of root.querySelectorAll(".dl-layer-opacity input")) {
            const opacity = String(layerStyle(root, slider.name).opacity);
            if (slider.value !== opacity && document.activeElement !== slider) {
                slider.value = opacity;
            }
        }
        refreshClasses(root);
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
    };

    // The legend. A click on a layer's chip shows or hides that layer, for this viewer: kept in the browser, and saved as
    // the chart's own only if the viewer asks to. The "overlays" chip hides every layer (or shows them again), "opacity"
    // opens a slider for each.
    document.addEventListener("click", (event) => {
        const chip = event.target.closest(".dl-layer-chip");
        const root = chip?.closest(".dl-image-series");
        if (!root) {
            return;
        }
        if (chip.classList.contains("dl-layer-master")) {
            changeViewerState(root, (state) => {
                state.overlays_hidden = !state.overlays_hidden;
            });
        } else if (chip.classList.contains("dl-layer-opacity-toggle")) {
            const sliders = root.querySelector(".dl-layer-opacities");
            sliders.hidden = !sliders.hidden;
            chip.setAttribute("aria-pressed", String(!sliders.hidden));
        } else {
            const name = chip.dataset.dlLayer;
            const hidden = !layerStyle(root, name).hidden;
            changeViewerState(root, (state) => {
                state.layers[name] = { ...state.layers[name], hidden };
            });
        }
    });

    document.addEventListener("input", (event) => {
        const slider = event.target.matches?.(".dl-layer-opacity input") ? event.target : null;
        const root = slider?.closest(".dl-image-series");
        if (root) {
            changeViewerState(root, (state) => {
                state.layers[slider.name] = {
                    ...state.layers[slider.name],
                    opacity: Number(slider.value),
                };
            });
        }
    });

    // Peek: holding a picture (or the "overlays" chip) for a moment shows it without its layers until it is let go, to see
    // what is under them. The click that ends a hold is not a click (it would open the picture, or toggle the chip).
    const PEEK_MS = 250;
    const SWALLOW_MS = 400;
    let peek = null;
    let swallowing = false;
    document.addEventListener("pointerdown", (event) => {
        const held = event.target.closest(".dl-image-series-thumb, .dl-layer-master");
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
    document.addEventListener(
        "click",
        (event) => {
            if (swallowing && event.target.closest(".dl-image-series-thumb, .dl-layer-master")) {
                event.stopPropagation();
                event.preventDefault();
                swallowing = false;
            }
        },
        true,
    );
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
