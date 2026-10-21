// Draws the layers over a picture, each at its own opacity: an overlay of shapes (a `dlboard.client.artifacts.overlay.Overlay`,
// logged as JSON, masks included) as an SVG, or an image as it is. Both are scaled `contain`, as the picture is, and the
// shapes are fractions of the picture's size that are scaled by its pixel size here, so the layers line up with it at any
// size. Every element is built with DOM calls, never by parsing markup, so a label is only text.
//
// Shared by every chart that shows a picture with its layers (an image chart's thumbnails and enlarged view, a table's
// cells): it knows nothing of the chart around it, which says how each layer is drawn (`styleOf`) and hears of a layer that
// has been drawn through the `dl-layer-drawn` event.
//
// The data was checked where it was made, so none of it is checked again here. What can still go wrong (a layer that
// will not load, a kind this script does not know, a shape that cannot be drawn) is logged with `console.error`, which
// the browser tests fail on, and never stops the rest of the page or the other layers from drawing.
window.dlLayeredPicture = (() => {
    const SVG_NS = "http://www.w3.org/2000/svg";
    const MIN_LABEL_SIZE = 8;
    const LABEL_SIZE_DIVISOR = 32;
    // The mask defaults (`DEFAULT_CLASS_COLORS`, `DEFAULT_MASK_OPACITY` in `overlay.py`), which the server writes ahead of
    // this script, so a mask with no `colors` is drawn in the same palette as the Python that made it.
    const { DEFAULT_CLASS_COLORS, DEFAULT_MASK_OPACITY } = window.dlMaskDefaults;

    // The color class `id` of `mask` is drawn in, or nothing if it is left see-through (`colors`, else the default palette of `overlay.py`).
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

    // A mask (`ImageMask`, `rle`) as a picture the size of the picture it is over, shown by `image` (an SVG `<image>`) once
    // the browser has encoded it: alternating `N, I` pairs, `N` pixels of class `I` each, in row-major order over the
    // picture's `[height, width]`. A class without a color is left transparent, and the rest are drawn at the mask's
    // opacity. The encoding is done off the main thread, so a page of masks never stops it, and the picture is a blob that
    // lives as long as its layer does (`revokeMasks`).
    const maskPicture = (mask, { width, height }, image) => {
        const [rows, cols] = mask.shape;
        if (rows !== height || cols !== width) {
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
        canvas.toBlob((blob) => {
            if (!blob) {
                console.error("image overlay: a mask could not be encoded as a picture");
            } else if (image.isConnected) {
                image.dlObjectUrl = URL.createObjectURL(blob);
                image.setAttribute("href", image.dlObjectUrl);
            }
            // Otherwise its layer was drawn again while this was being encoded, and nothing wants it.
        });
    };

    // Frees the picture of every mask drawn under `root`, which is about to be drawn again.
    const revokeMasks = (root) => {
        for (const image of root.querySelectorAll("image")) {
            if (image.dlObjectUrl) {
                URL.revokeObjectURL(image.dlObjectUrl);
            }
        }
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
        mask: (shape, size) => {
            const outline = element("image", {
                x: 0,
                y: 0,
                width: size.width,
                height: size.height,
                preserveAspectRatio: "none",
            });
            maskPicture(shape, size, outline);
            return { outline, anchor: [0, 0] };
        },
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
                        holder.dispatchEvent(new CustomEvent("dl-layer-drawn", { bubbles: true }));
                    }
                })
                .catch((error) => console.error("image overlay: a shapes layer was not drawn:", url, error));
        },
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

    // Draws `layers` (bottom first) over `picture`, which is showing `pictureUrl`, into `layer`. No layers clears it. A
    // layer element remembers the request it was last given, so a request that the slider (or another modal open) has
    // overtaken draws nothing, and the same one is not drawn twice. How each is drawn (`styleOf(name)`: hidden, opacity)
    // is not part of the request: it is applied after, and changing it never draws anything again (`applyStyle`).
    const show = (layer, picture, pictureUrl, layers, styleOf) => {
        const request = `${pictureUrl} ${JSON.stringify(layers)}`;
        if (layer.dataset.request === request) {
            return;
        }
        layer.dataset.request = request;
        revokeMasks(layer);
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
            applyStyle(holder, styleOf(spec.layer));
        }
    };

    return { show, applyStyle };
})();
