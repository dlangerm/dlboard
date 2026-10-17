// Draws the layers of an image chart over its picture: each an image (a segmentation mask, say) at its own opacity,
// scaled `contain` as the picture is, so a layer lines up with it at any size.
//
// A plain script, not a clientside callback: the image-series scrub and the modal both draw through `show`.
window.dlImageSeriesOverlay = (() => {
    // Draws `layers` (each `{url, opacity}`, bottom first) into `layer`. No layers clears it. A layer element
    // remembers what it was last given, so the same layers are not drawn again.
    const show = (layer, layers) => {
        const request = JSON.stringify(layers);
        if (layer.dataset.request === request) {
            return;
        }
        layer.dataset.request = request;
        layer.replaceChildren();
        for (const { url, opacity } of layers) {
            const holder = document.createElement("div");
            holder.className = "dl-image-series-layer";
            holder.style.opacity = String(Number.isFinite(opacity) ? Math.min(1, Math.max(0, opacity)) : 1);
            const img = document.createElement("img");
            img.alt = "";
            img.src = url;
            holder.append(img);
            layer.append(holder);
        }
    };

    return { show };
})();
