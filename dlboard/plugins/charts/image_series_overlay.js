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

    // How each kind of layer is drawn into its `holder`, given the layer (`{url, kind, opacity}`) and what the drawing
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
                        draw(overlay);
                    }
                })
                .catch((error) => console.error("image overlay: a shapes layer was not drawn:", url, error));
        },
    };

    // Draws `layers` (bottom first) over `picture`, which is showing `pictureUrl`, into `layer`. No layers clears it. A
    // layer element remembers the request it was last given, so a request that the slider (or another modal open) has
    // overtaken draws nothing, and the same one is not drawn twice.
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
            holder.style.opacity = String(spec.opacity);
            layer.append(holder);
            const draw = LAYER_DRAWERS[spec.kind];
            if (!draw) {
                console.error(`image overlay: no way to draw a layer of kind "${spec.kind}":`, spec.url);
                continue;
            }
            try {
                draw(holder, spec, context);
            } catch (error) {
                console.error("image overlay: a layer was not drawn:", spec.url, error);
            }
        }
    };

    return { show };
})();
