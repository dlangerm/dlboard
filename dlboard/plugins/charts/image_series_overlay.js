// Draws the layers of an image chart over its picture: an overlay of shapes (a `dlboard.client.artifacts.overlay.Overlay`,
// logged as JSON) as an SVG, or an image (a mask, say) as it is, each at its own opacity. Both are scaled `contain`, as the
// picture is, and an SVG's viewBox is the picture's own pixel size, so the layers line up with it at any size. Every
// element is built with DOM calls, never by parsing markup, so a label is only text.
//
// A plain script, not a clientside callback: the image-series scrub and the modal both draw through `show`.
window.dlImageSeriesOverlay = (() => {
    const SVG_NS = "http://www.w3.org/2000/svg";
    const HEX_COLOR = /^#[0-9a-fA-F]{6}$/;
    const MIN_LABEL_SIZE = 8;
    const LABEL_SIZE_DIVISOR = 32;

    const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
    // A shape's coordinates are fractions of the picture's size, from 0 (its left or top edge) to 1.
    const isFraction = (value) => isNumber(value) && value >= 0 && value <= 1;
    const element = (name, attributes) => {
        const el = document.createElementNS(SVG_NS, name);
        for (const [key, value] of Object.entries(attributes)) {
            el.setAttribute(key, String(value));
        }
        return el;
    };

    // One shape as an SVG group in a picture `width` by `height` pixels, or null for a shape that is not well formed (so
    // one bad shape does not hide the rest).
    const shapeGroup = (shape, width, height, labelSize) => {
        if (!HEX_COLOR.test(shape?.color)) {
            return null;
        }
        const style = {
            fill: "none",
            stroke: shape.color,
            "stroke-width": 2,
            "vector-effect": "non-scaling-stroke",
        };
        let outline;
        let anchor;
        if (shape.kind === "box" && [shape.x, shape.y, shape.width, shape.height].every(isFraction)) {
            outline = element("rect", {
                x: shape.x * width,
                y: shape.y * height,
                width: shape.width * width,
                height: shape.height * height,
                ...style,
            });
            anchor = [shape.x * width, shape.y * height];
        } else if (
            shape.kind === "polygon" &&
            Array.isArray(shape.points) &&
            shape.points.length >= 3 &&
            shape.points.every(
                (point) => Array.isArray(point) && point.length === 2 && point.every(isFraction),
            )
        ) {
            const points = shape.points.map(([x, y]) => [x * width, y * height]);
            outline = element("polygon", { points: points.map(([x, y]) => `${x},${y}`).join(" "), ...style });
            anchor = points[0];
        } else {
            return null;
        }
        const group = element("g", {});
        group.append(outline);
        if (typeof shape.label === "string" && shape.label !== "") {
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

    const overlaySvg = (overlay, width, height) => {
        const labelSize = Math.max(MIN_LABEL_SIZE, height / LABEL_SIZE_DIVISOR);
        const svg = element("svg", {
            viewBox: `0 0 ${width} ${height}`,
            preserveAspectRatio: "xMidYMid meet",
        });
        for (const shape of Array.isArray(overlay.shapes) ? overlay.shapes : []) {
            const group = shapeGroup(shape, width, height, labelSize);
            if (group) {
                svg.append(group);
            }
        }
        return svg;
    };

    // Draws a layer of shapes: fetches its JSON, then draws it over `picture` (showing `pictureUrl`) once the picture is
    // on screen, into `holder`, unless `isCurrent` says the request has been overtaken by then.
    const drawShapes = (holder, picture, pictureUrl, url, isCurrent) => {
        const isShown = () =>
            picture.getAttribute("src") === pictureUrl && picture.complete && picture.naturalWidth > 0;
        const draw = (overlay) => {
            const render = () =>
                holder.replaceChildren(overlaySvg(overlay, picture.naturalWidth, picture.naturalHeight));
            if (isShown()) {
                render();
                return;
            }
            // The picture is not on screen yet: draw once it is, unless the request is overtaken first.
            const onLoad = () => {
                if (!isCurrent()) {
                    picture.removeEventListener("load", onLoad);
                } else if (isShown()) {
                    picture.removeEventListener("load", onLoad);
                    render();
                }
            };
            picture.addEventListener("load", onLoad);
        };
        fetch(url)
            .then((response) => {
                if (!response.ok) {
                    throw new Error(`overlay ${url} answered ${response.status}`);
                }
                return response.json();
            })
            .then((overlay) => {
                if (isCurrent()) {
                    draw(overlay);
                }
            })
            .catch((error) => console.warn("image overlay not drawn:", error));
    };

    // Draws `layers` (each `{url, kind, opacity}`, bottom first) over `picture`, which is showing `pictureUrl`, into
    // `layer`. No layers clears it. A layer element remembers the request it was last given, so a request that the
    // slider (or another modal open) has overtaken draws nothing.
    const show = (layer, picture, pictureUrl, layers) => {
        const request = `${pictureUrl} ${JSON.stringify(layers)}`;
        if (layer.dataset.request === request) {
            return;
        }
        layer.dataset.request = request;
        layer.replaceChildren();
        const isCurrent = () => layer.dataset.request === request;
        for (const { url, kind, opacity } of layers) {
            // Made in order, before anything has loaded, so the layers stack as they are listed.
            const holder = document.createElement("div");
            holder.className = "dl-image-series-layer";
            holder.style.opacity = String(isNumber(opacity) ? Math.min(1, Math.max(0, opacity)) : 1);
            layer.append(holder);
            if (kind === "picture") {
                const img = document.createElement("img");
                img.alt = "";
                img.src = url;
                holder.append(img);
            } else if (kind === "shapes") {
                drawShapes(holder, picture, pictureUrl, url, isCurrent);
            }
        }
    };

    return { show };
})();
