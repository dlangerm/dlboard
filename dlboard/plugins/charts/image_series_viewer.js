// The enlarged view of an image chart's picture: zoomed and panned, with its layers staying registered to it, and moved
// through the chart's steps and runs from the keyboard.
//
// The picture and its layers are one box (`.dl-zoom-content`) that is moved and scaled as a whole by a CSS transform, so
// neither is resized and the layers line up at any zoom. The legend and the zoom controls are outside it, and stay put.
//
// A plain script, not a clientside callback: the callbacks of `image_series.py` call `open`, `close` and `shown`, and the
// events of the view are handled here, on the document, as the legend's are (`image_series_overlay.js`).
window.dlImageSeriesViewer = (() => {
    // The most a picture can be magnified, in screen pixels to a picture pixel; and the least that a picture already shown
    // larger than it is can still be zoomed into, as a factor of how it fits.
    const MAX_PIXELS_PER_PIXEL = 64;
    const MIN_ZOOM_RANGE = 8;
    // How far a turn of the wheel (or a pinch on a trackpad, which the browser sends as a Ctrl-wheel) zooms.
    const WHEEL_ZOOM = 0.002;
    const PINCH_ZOOM = 0.01;
    // The zoom slider is logarithmic, so each part of it is the same factor of zoom.
    const SLIDER_STEPS = 1000;
    // A press that moves less than this many pixels is a click, not the start of a pan.
    const DRAG_PX = 3;

    const VIEWER = ".dl-image-series-viewer";
    const SHELF = ".dl-layer-legend, .dl-zoom-controls, .dl-view-nav";

    const clamp = (value, low, high) => Math.min(Math.max(value, low), high);

    const viewerOf = (instance) =>
        document.querySelector(`${VIEWER}[data-dl-instance="${CSS.escape(instance)}"]`);

    // Where each viewer's picture is, by viewer: `scale` times its fitted size, moved by (`x`, `y`) from the box's corner.
    const views = new WeakMap();
    const viewOf = (viewer) => {
        if (!views.has(viewer)) {
            views.set(viewer, { scale: 1, x: 0, y: 0 });
        }
        return views.get(viewer);
    };

    // The picture a viewer shows is its first `<img>`: the layers' pictures come after it.
    const pictureOf = (viewer) => viewer.querySelector("img");

    // How the picture sits in the viewer at no zoom (`scale` 1): `base` is screen pixels to a picture pixel when it is
    // letterboxed to the view. A picture that fit makes larger than it is can be zoomed out as far as its own size (`minScale`).
    // Nothing until the picture has loaded.
    const geometry = (viewer) => {
        const picture = pictureOf(viewer);
        const [width, height] = [viewer.clientWidth, viewer.clientHeight];
        if (!picture?.naturalWidth || !width || !height) {
            return null;
        }
        const base = Math.min(width / picture.naturalWidth, height / picture.naturalHeight);
        return {
            width,
            height,
            base,
            minScale: Math.min(1, 1 / base),
            maxScale: Math.max(MIN_ZOOM_RANGE, MAX_PIXELS_PER_PIXEL / base),
        };
    };

    // Where a picture `scale` times the view can sit along one axis of `extent`: anywhere that keeps it covering the view, or
    // in the middle of it if it is smaller.
    const placed = (offset, extent, scale) =>
        scale < 1 ? (extent * (1 - scale)) / 2 : clamp(offset, extent * (1 - scale), 0);

    const button = (className, text, title) => {
        const el = document.createElement("button");
        el.type = "button";
        el.className = className;
        el.textContent = text;
        el.title = title;
        return el;
    };

    // The zoom controls: fit, a slider, the level, and one picture pixel to a screen pixel. Built once, when first needed.
    const buildControls = (bar) => {
        const slider = document.createElement("input");
        slider.type = "range";
        slider.className = "dl-zoom-slider";
        slider.min = "0";
        slider.max = String(SLIDER_STEPS);
        slider.value = "0";
        slider.setAttribute("aria-label", "Zoom");
        const level = document.createElement("span");
        level.className = "dl-zoom-level";
        bar.append(
            button(
                "dl-zoom-fit",
                "Fit",
                "Fit the whole picture in the view. Double-click the picture does the same.",
            ),
            slider,
            level,
            button(
                "dl-zoom-native",
                "100%",
                "Show the picture at its own size: a pixel of it to a pixel of the screen.",
            ),
        );
    };

    // Draws the view: the transform, and the controls. (The legend is not part of it: it stays in the view's corner.)
    const apply = (viewer) => {
        const view = viewOf(viewer);
        const bar = viewer.querySelector(".dl-zoom-controls");
        if (bar && !bar.firstChild) {
            buildControls(bar);
        }
        const geo = geometry(viewer);
        if (geo) {
            view.scale = clamp(view.scale, geo.minScale, geo.maxScale);
            view.x = placed(view.x, geo.width, view.scale);
            view.y = placed(view.y, geo.height, view.scale);
        }
        viewer.querySelector(".dl-zoom-content").style.transform =
            `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
        viewer.toggleAttribute("data-dl-zoomed", view.scale > 1);
        if (geo && bar) {
            bar.querySelector(".dl-zoom-slider").value = String(
                Math.round(
                    (Math.log(view.scale / geo.minScale) / Math.log(geo.maxScale / geo.minScale)) *
                        SLIDER_STEPS,
                ),
            );
            bar.querySelector(".dl-zoom-level").textContent = `${Math.round(view.scale * geo.base * 100)}%`;
        }
    };

    // Zooms to `scale`, keeping the point (`x`, `y`) of the view (the centre, if none) where it is.
    const zoomTo = (viewer, scale, x = viewer.clientWidth / 2, y = viewer.clientHeight / 2) => {
        const geo = geometry(viewer);
        const view = viewOf(viewer);
        if (!geo) {
            return;
        }
        const next = clamp(scale, geo.minScale, geo.maxScale);
        view.x = x - ((x - view.x) * next) / view.scale;
        view.y = y - ((y - view.y) * next) / view.scale;
        view.scale = next;
        apply(viewer);
    };

    const fit = (viewer) => {
        Object.assign(viewOf(viewer), { scale: 1, x: 0, y: 0 });
        apply(viewer);
    };

    // The step a run shows when the slider is at `step`: the latest it has at or before it, else its first.
    const stepOf = (data, run, step) => {
        const steps = data.per_run_steps[run];
        return steps.filter((at) => at <= step).pop() ?? steps[0];
    };

    // What the enlarged view is showing, by chart instance: only while it is open, and what the keyboard moves from.
    const opened = new Map();

    // Every step any run has, in order: what the chart's slider stops at, and what the timeline is a scale of.
    const allSteps = new WeakMap();
    const stepsOf = (data) => {
        if (!allSteps.has(data)) {
            allSteps.set(
                data,
                [...new Set(Object.values(data.per_run_steps).flat())].sort((a, b) => a - b),
            );
        }
        return allSteps.get(data);
    };

    const make = (tag, className, text) => {
        const el = document.createElement(tag);
        el.className = className;
        if (text !== undefined) {
            el.textContent = text;
        }
        return el;
    };

    // The tags of the picture on show as a table, a row each; nothing, and so no room taken, for a picture with none.
    const showTags = (panel, tags) => {
        const rows = Object.entries(tags).map(([name, value]) => {
            const row = document.createElement("tr");
            row.append(make("th", "dl-view-tag-name", name), make("td", "dl-view-tag-value", value));
            return row;
        });
        const table = make("table", "dl-view-tag-table");
        table.append(...rows);
        panel.replaceChildren(...(rows.length ? [make("div", "dl-view-tags-title", "Tags"), table] : []));
    };

    // The timeline under the picture: a scale of the chart's steps to scrub along, as the chart's own slider is. Built once.
    const showTimeline = (bar, steps, step) => {
        if (!bar.firstChild) {
            const range = make("input", "dl-timeline-slider");
            range.type = "range";
            range.min = "0";
            range.step = "1";
            range.setAttribute("aria-label", "Step");
            bar.append(range, make("span", "dl-timeline-label"));
        }
        const range = bar.querySelector(".dl-timeline-slider");
        range.max = String(steps.length - 1);
        // Not while it is being dragged: the chart answers a step behind the hand, and would pull it back.
        if (!range.matches(":active")) {
            range.value = String(steps.indexOf(step));
        }
    };

    // Draws everything about the picture on show that is not the picture: title, tags, timeline, and where the buttons can go.
    const showDetails = (instance, data, run, step, shown) => {
        const part = (className) =>
            document.querySelector(`.${className}[data-dl-instance="${CSS.escape(instance)}"]`);
        const axis = data.x_axis;
        const title = part("dl-view-title");
        const runName = title.querySelector(".dl-view-run");
        runName.textContent = data.per_run_names?.[run] ?? `Run ${run}`;
        runName.className = `dl-view-run ${data.per_run_swatches?.[run] ?? ""}`;
        title.querySelector(".dl-view-step").textContent = `${axis} ${shown}`;
        showTags(part("dl-view-tags"), data.per_run_tags?.[run]?.[String(shown)] ?? {});
        const steps = stepsOf(data);
        const bar = part("dl-view-timeline");
        showTimeline(bar, steps, step);
        bar.querySelector(".dl-timeline-label").textContent = `${axis} ${step}`;
        const at = steps.indexOf(step);
        viewerOf(instance).querySelector(".dl-view-prev").disabled = at <= 0;
        viewerOf(instance).querySelector(".dl-view-next").disabled = at >= steps.length - 1;
    };

    // How long a picture is waited for before the view says so with a spinner: a picture that is there at once, or nearly, is
    // no cause to flash one at every step scrubbed through.
    const LOADER_DELAY_MS = 150;
    // The most often the chart's slider is moved while the timeline or the keys drive it: each move draws every thumbnail.
    const STEP_INTERVAL_MS = 80;

    // The picture `run` shows at the slider's `step`, which the enlarged view is now showing, with its title, tags and
    // timeline. Opened afresh, it fits the picture to the view; moved along the steps or runs it keeps its zoom, to compare
    // the same place. The picture itself is `showPicture`'s: nothing is returned for the callback to set.
    const open = (instance, data, run, step) => {
        const viewer = viewerOf(instance);
        if (viewer && !opened.has(instance)) {
            fit(viewer);
        }
        opened.set(instance, { data, run, step });
        const shown = stepOf(data, run, step);
        showDetails(instance, data, run, step, shown);
        if (viewer) {
            showPicture(instance, viewer, data.per_run_urls[run][String(shown)]);
            prefetchAround(data, run, step);
        }
    };

    // The picture most recently asked for, by instance: one that is overtaken while it loads is dropped.
    const latestPicture = new Map();

    // Shows the picture at `url`. It is loaded and decoded off screen first and put in place only then, so the one on show stays
    // until the next can replace it, with no blank or half-drawn picture between, and a step that is overtaken by the next
    // while scrubbing costs nothing more than its download. If it takes a while, the one on show is dimmed with a spinner over
    // it (as a thumbnail has) until the new one is in.
    const showPicture = (instance, viewer, url) => {
        const picture = pictureOf(viewer);
        latestPicture.set(instance, url);
        clearTimeout(viewer.dlLoaderTimer);
        if (picture.getAttribute("src") === url) {
            viewer.removeAttribute("data-dl-loading");
            return;
        }
        viewer.dlLoaderTimer = setTimeout(() => viewer.setAttribute("data-dl-loading", ""), LOADER_DELAY_MS);
        const settle = () => {
            if (picture.getAttribute("src") === url) {
                clearTimeout(viewer.dlLoaderTimer);
                viewer.removeAttribute("data-dl-loading");
            }
        };
        picture.onload = settle;
        picture.onerror = settle;
        const incoming = new Image();
        incoming.src = url;
        // Decoded, or failed: either way the picture is put in place, to show it or to say it did not load.
        incoming
            .decode()
            .catch(() => undefined)
            .then(() => {
                if (latestPicture.get(instance) === url) {
                    window.dash_clientside.set_props(
                        { type: "image-series-modal-img", instance },
                        { src: url },
                    );
                }
            });
    };

    // The pictures a step either side of this one are fetched ahead of the move, so stepping on is not a wait.
    const prefetchAround = (data, run, step) => {
        const steps = stepsOf(data);
        const at = steps.indexOf(step);
        for (const near of [steps[at - 1], steps[at + 1]]) {
            if (near !== undefined) {
                new Image().src = data.per_run_urls[run][String(stepOf(data, run, near))];
            }
        }
    };

    // Forgets what was open, which is what stops the keyboard moving it. It is left as it is while it fades out.
    const close = (instance) => {
        opened.delete(instance);
        asked.delete(instance);
        latestPicture.delete(instance);
    };

    // The picture of the view has changed: the controls show where it is, and are told again when it has loaded.
    const shown = (instance) => {
        const viewer = viewerOf(instance);
        if (viewer) {
            apply(viewer);
            pictureOf(viewer).addEventListener("load", () => apply(viewer), { once: true });
        }
    };

    // The step the view has asked the chart's slider for, by instance, and whether a move of it is cooling down. A move goes at
    // once, so a click is not kept waiting; those that come while it cools (a drag along the timeline, a key held down) are
    // not sent each, only the latest, when it has: the slider is the whole chart's, and every move of it draws every thumbnail.
    const asked = new Map();
    const askForStep = (instance, step) => {
        const ask = asked.get(instance) ?? { sent: null, timer: null };
        asked.set(instance, ask);
        ask.step = step;
        if (ask.timer) {
            return;
        }
        const send = () => {
            ask.timer = null;
            if (ask.step !== ask.sent) {
                ask.sent = ask.step;
                window.dash_clientside.set_props(
                    { type: "image-series-slider", instance },
                    { value: ask.step },
                );
                ask.timer = setTimeout(send, STEP_INTERVAL_MS);
            }
        };
        send();
    };

    // Moves the open view along the chart's steps or runs the way its slider and pager do: by setting them, so the thumbnails
    // follow and the view answers to them like anything else that moves them.
    const moveAlong = (instance, along, by) => {
        const now = opened.get(instance);
        if (!now) {
            return;
        }
        if (along === "runs") {
            const runs = Object.keys(now.data.per_run_steps);
            const next = runs[clamp(runs.indexOf(now.run) + by, 0, runs.length - 1)];
            window.dash_clientside.set_props({ type: "image-series-modal-run", instance }, { data: next });
        } else {
            // From the step last asked for, which the slider may not have reached yet: a key held down, or a button clicked
            // quickly, moves on from where it is going, not from where it was.
            const steps = stepsOf(now.data);
            const from = asked.get(instance)?.step ?? now.step;
            askForStep(instance, steps[clamp(steps.indexOf(from) + by, 0, steps.length - 1)]);
        }
    };

    const MOVES = {
        ArrowLeft: { along: "steps", by: -1 },
        ArrowRight: { along: "steps", by: 1 },
        ArrowUp: { along: "runs", by: -1 },
        ArrowDown: { along: "runs", by: 1 },
    };
    document.addEventListener("keydown", (event) => {
        const move = MOVES[event.key];
        const instance = [...opened.keys()].pop();
        if (
            !move ||
            !instance ||
            event.altKey ||
            event.ctrlKey ||
            event.metaKey ||
            event.target.closest?.("input")
        ) {
            return;
        }
        event.preventDefault();
        moveAlong(instance, move.along, move.by);
    });

    // The buttons on either side of the picture step it back and forward.
    document.addEventListener("click", (event) => {
        const button = event.target.closest?.(".dl-view-nav");
        if (button) {
            moveAlong(
                button.closest(VIEWER).dataset.dlInstance,
                "steps",
                button.classList.contains("dl-view-prev") ? -1 : 1,
            );
        }
    });

    // Dragging the timeline goes to the step under the hand.
    document.addEventListener("input", (event) => {
        const range = event.target.closest?.(".dl-timeline-slider");
        const instance = range?.closest(".dl-view-timeline").dataset.dlInstance;
        const now = opened.get(instance);
        if (now) {
            const step = stepsOf(now.data)[Number(range.value)];
            // The label follows the hand at once; the picture follows as the chart does.
            range
                .closest(".dl-view-timeline")
                .querySelector(".dl-timeline-label").textContent = `${now.data.x_axis} ${step}`;
            askForStep(instance, step);
        }
    });

    // The pointer's place in a viewer, in the viewer's own pixels.
    const within = (viewer, { clientX, clientY }) => {
        const box = viewer.getBoundingClientRect();
        return [clientX - box.left, clientY - box.top];
    };

    // Scroll to zoom towards the pointer. A Ctrl-wheel is a pinch on a trackpad, which would zoom the whole page.
    document.addEventListener(
        "wheel",
        (event) => {
            const viewer = event.target.closest?.(VIEWER);
            if (!viewer || event.target.closest(SHELF)) {
                return;
            }
            event.preventDefault();
            const lines = event.deltaMode === 1 ? 16 : 1;
            const rate = event.ctrlKey ? PINCH_ZOOM : WHEEL_ZOOM;
            zoomTo(
                viewer,
                viewOf(viewer).scale * Math.exp(-event.deltaY * lines * rate),
                ...within(viewer, event),
            );
        },
        { passive: false },
    );

    // Drag to pan.
    let drag = null;
    document.addEventListener("pointerdown", (event) => {
        const viewer = event.target.closest?.(VIEWER);
        if (!viewer || event.button !== 0 || event.target.closest(SHELF)) {
            return;
        }
        const { x, y } = viewOf(viewer);
        drag = { viewer, id: event.pointerId, from: [event.clientX, event.clientY], x, y, moved: false };
        viewer.setPointerCapture(event.pointerId);
    });
    document.addEventListener("pointermove", (event) => {
        if (drag?.id !== event.pointerId) {
            return;
        }
        const [dx, dy] = [event.clientX - drag.from[0], event.clientY - drag.from[1]];
        if (!drag.moved && Math.hypot(dx, dy) < DRAG_PX) {
            return;
        }
        drag.moved = true;
        drag.viewer.setAttribute("data-dl-panning", "");
        Object.assign(viewOf(drag.viewer), { x: drag.x + dx, y: drag.y + dy });
        apply(drag.viewer);
    });
    const endDrag = (event) => {
        if (drag?.id !== event.pointerId) {
            return;
        }
        drag.viewer.removeAttribute("data-dl-panning");
        if (drag.moved) {
            // Letting go of a pan is not a click: it must not reach the modal, which closes on one outside its content.
            const swallow = (click) => click.stopPropagation();
            window.addEventListener("click", swallow, { capture: true, once: true });
            setTimeout(() => window.removeEventListener("click", swallow, { capture: true }), 0);
        }
        drag = null;
    };
    document.addEventListener("pointerup", endDrag);
    document.addEventListener("pointercancel", endDrag);

    // The picture is not dragged as a file would be.
    document.addEventListener("dragstart", (event) => {
        if (event.target.closest?.(VIEWER)) {
            event.preventDefault();
        }
    });

    document.addEventListener("dblclick", (event) => {
        const viewer = event.target.closest?.(VIEWER);
        if (viewer && !event.target.closest(SHELF)) {
            fit(viewer);
        }
    });

    // The controls.
    document.addEventListener("input", (event) => {
        const viewer = event.target.closest?.(`${VIEWER} .dl-zoom-slider`)?.closest(VIEWER);
        const geo = viewer && geometry(viewer);
        if (geo) {
            zoomTo(
                viewer,
                geo.minScale * (geo.maxScale / geo.minScale) ** (Number(event.target.value) / SLIDER_STEPS),
            );
        }
    });
    document.addEventListener("click", (event) => {
        const target = event.target.closest?.(".dl-zoom-controls button");
        const viewer = target?.closest(VIEWER);
        if (!viewer) {
            return;
        }
        if (target.classList.contains("dl-zoom-fit")) {
            fit(viewer);
        } else {
            zoomTo(viewer, 1 / (geometry(viewer)?.base ?? 1));
        }
    });
    // A slider that keeps the focus after a drag would take the arrow keys that move through the steps.
    document.addEventListener("pointerup", (event) =>
        event.target.closest?.(".dl-zoom-slider, .dl-timeline-slider")?.blur(),
    );

    // Downloading the picture on show as a PNG, drawn onto a canvas: the picture, each layer that is shown over it as it is
    // on screen, and the legend of what they hold. The viewer chooses how big: the picture's own size, or magnified by 2, 4
    // or 8 (a whole number, so its pixels stay as sharp as on screen and a label or an outline can be read), or the view
    // itself: the area that is zoomed and panned to, at the screen's resolution.
    const SVG_NS = "http://www.w3.org/2000/svg";
    // The largest a canvas is made, along a side and in pixels: browsers stop drawing on one beyond that.
    const MAX_CANVAS_SIDE = 16384;
    const MAX_CANVAS_PIXELS = 1e8;
    const VIEW_CHOICE = "view";

    const loaded = (src) =>
        new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve(img);
            img.onerror = () => reject(new Error(`could not load ${src.slice(0, 80)}`));
            img.src = src;
        });

    // A blob (a mask's picture) as a data URL: a drawing loaded as an image cannot reach out to the page's blobs.
    const asDataUrl = async (blobUrl) => {
        const blob = await (await fetch(blobUrl)).blob();
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(blob);
        });
    };

    // The shapes of a layer over `area` of the picture ([left, top, width, height], in the picture's pixels), drawn onto the
    // canvas at `at`, `extent` pixels across. They are scaled with the picture, but an outline is a fixed number of screen
    // pixels wide (`non-scaling-stroke`): it is widened by `outline`, the canvas pixels to a screen pixel.
    const drawShapes = async (context, svg, { area, at, extent, outline }) => {
        const copy = svg.cloneNode(true);
        copy.setAttribute("xmlns", SVG_NS);
        copy.setAttribute("viewBox", area.join(" "));
        copy.setAttribute("width", String(extent[0]));
        copy.setAttribute("height", String(extent[1]));
        copy.setAttribute("font-family", "sans-serif");
        for (const shape of copy.querySelectorAll("[vector-effect]")) {
            shape.setAttribute("stroke-width", String(Number(shape.getAttribute("stroke-width")) * outline));
        }
        for (const image of copy.querySelectorAll("image")) {
            // A mask whose picture has not been encoded yet is left out.
            const href = image.getAttribute("href");
            if (href) {
                image.setAttribute("href", await asDataUrl(href));
                image.style.imageRendering = "pixelated"; // magnified in blocks, as on screen
            } else {
                image.remove();
            }
        }
        const markup = new XMLSerializer().serializeToString(copy);
        const url = URL.createObjectURL(new Blob([markup], { type: "image/svg+xml" }));
        try {
            context.drawImage(await loaded(url), at[0], at[1], extent[0], extent[1]);
        } finally {
            URL.revokeObjectURL(url);
        }
    };

    // What the legend says, for the layers that are shown: each one's name and what it holds, in their colors.
    const legendOf = (viewer) =>
        [...viewer.querySelectorAll(".dl-layer-row")].flatMap((row) => {
            const key = CSS.escape(row.dataset.dlLayer);
            const holder = viewer.querySelector(`.dl-image-series-layer[data-dl-layer="${key}"]`);
            if (!holder || holder.hidden) {
                return [];
            }
            const classes = [...row.querySelectorAll(".dl-class")].map((entry) => ({
                label: entry.textContent,
                color: entry.querySelector(".dl-class-swatch").style.background,
            }));
            return [{ name: row.querySelector(".dl-layer-name").textContent, classes }];
        });

    // Draws the legend in the canvas's top-left corner, as it is over the picture: a panel with a line for each layer, its name
    // and then what it holds, wrapping when a line is full. Sized by the canvas so it reads at any size, but never so big
    // that it hides much of the picture: it shrinks until it takes no more than this much of its width and height.
    const LEGEND_SHARE = [0.4, 0.5];
    const laidOut = (context, layers, unit, room) => {
        const [pad, gap, swatch] = [unit * 0.7, unit * 0.9, unit * 0.8];
        const measure = (text, weight) => {
            context.font = `${weight} ${unit}px sans-serif`;
            return context.measureText(text).width;
        };
        // The entries as lines of {x, text, weight, color} (a color for an entry that has a swatch).
        const lines = [];
        for (const { name, classes } of layers) {
            let line = [{ x: 0, text: name, weight: 700 }];
            let x = measure(name, 700) + gap;
            for (const { label, color } of classes) {
                const wide = swatch + unit * 0.4 + measure(label, 400);
                if (x > 0 && x + wide > room - 2 * pad) {
                    lines.push(line);
                    [line, x] = [[], 0];
                }
                line.push({ x, text: label, weight: 400, color });
                x += wide + gap;
            }
            lines.push(line);
        }
        const rowHeight = unit * 1.45;
        const across = Math.max(
            ...lines.flat().map((e) => e.x + measure(e.text, e.weight) + (e.color ? swatch + unit * 0.4 : 0)),
        );
        return {
            lines,
            pad,
            swatch,
            rowHeight,
            w: across + 2 * pad,
            h: lines.length * rowHeight + 2 * pad - (rowHeight - unit),
        };
    };

    const drawLegend = (context, layers, [width, height]) => {
        if (!layers.length) {
            return;
        }
        let unit = Math.max(4, Math.round(Math.min(width, height) / 50));
        let legend = laidOut(context, layers, unit, width * LEGEND_SHARE[0]);
        while (unit > 4 && (legend.w > width * LEGEND_SHARE[0] || legend.h > height * LEGEND_SHARE[1])) {
            unit = Math.max(4, Math.floor(unit * 0.85));
            legend = laidOut(context, layers, unit, width * LEGEND_SHARE[0]);
        }
        const [x0, y0] = [unit * 0.6, unit * 0.6];
        context.globalAlpha = 1;
        context.fillStyle = "rgb(18 18 24 / 0.78)";
        context.strokeStyle = "rgb(255 255 255 / 0.25)";
        context.lineWidth = Math.max(1, unit / 12);
        context.beginPath();
        context.roundRect(x0, y0, legend.w, legend.h, unit * 0.6);
        context.fill();
        context.stroke();
        context.textBaseline = "middle";
        legend.lines.forEach((line, row) => {
            const middle = y0 + legend.pad + row * legend.rowHeight + unit / 2;
            for (const { x, text, weight, color } of line) {
                let at = x0 + legend.pad + x;
                if (color) {
                    context.fillStyle = color;
                    context.fillRect(at, middle - legend.swatch / 2, legend.swatch, legend.swatch);
                    context.strokeRect(at, middle - legend.swatch / 2, legend.swatch, legend.swatch);
                    at += legend.swatch + unit * 0.4;
                }
                context.font = `${weight} ${unit}px sans-serif`;
                context.fillStyle = "#f2f2f5";
                context.fillText(text, at, middle);
            }
        });
    };

    // `choice` is "view", or the number of times the picture's own size.
    const download = async (instance, choice) => {
        const viewer = viewerOf(instance);
        const picture = viewer && pictureOf(viewer);
        const geo = viewer && geometry(viewer);
        if (!geo) {
            return;
        }
        const [natural, view] = [[picture.naturalWidth, picture.naturalHeight], viewOf(viewer)];
        // Where the picture goes on the canvas: a picture pixel (x, y) at `origin + k * (x, y)`.
        const boxed = [(geo.width - natural[0] * geo.base) / 2, (geo.height - natural[1] * geo.base) / 2];
        let [k, origin, size, outline] = [0, [0, 0], [0, 0], 1];
        if (choice === VIEW_CHOICE) {
            const dpr = window.devicePixelRatio || 1;
            k = geo.base * view.scale * dpr;
            origin = [(boxed[0] * view.scale + view.x) * dpr, (boxed[1] * view.scale + view.y) * dpr];
            size = [Math.round(geo.width * dpr), Math.round(geo.height * dpr)];
            outline = dpr;
        } else {
            const [most, long] = [Number(choice), Math.max(...natural)];
            k = Math.max(
                1,
                Math.floor(
                    Math.min(
                        most,
                        MAX_CANVAS_SIDE / long,
                        Math.sqrt(MAX_CANVAS_PIXELS / (natural[0] * natural[1])),
                    ),
                ),
            );
            size = natural.map((side) => side * k);
            outline = k / geo.base;
        }
        const canvas = document.createElement("canvas");
        [canvas.width, canvas.height] = size;
        const context = canvas.getContext("2d");
        context.imageSmoothingEnabled = false;
        // The part of the picture that is on the canvas, in its own pixels: all of it, or what the view shows.
        const left = Math.max(0, -origin[0] / k);
        const top = Math.max(0, -origin[1] / k);
        const right = Math.min(natural[0], (size[0] - origin[0]) / k);
        const bottom = Math.min(natural[1], (size[1] - origin[1]) / k);
        if (right > left && bottom > top) {
            const [at, extent] = [
                [origin[0] + left * k, origin[1] + top * k],
                [(right - left) * k, (bottom - top) * k],
            ];
            context.drawImage(picture, left, top, right - left, bottom - top, ...at, ...extent);
            for (const holder of viewer.querySelectorAll(".dl-image-series-layer:not([hidden])")) {
                context.globalAlpha = Number(holder.style.opacity || 1);
                const layer = holder.querySelector(":scope > img");
                const shapes = holder.querySelector(":scope > svg");
                if (layer?.naturalWidth) {
                    // A layer that is a picture is fitted to the view's box as the picture is: its place in the box, less where
                    // the picture is in it, is its place in the picture.
                    const fitted = Math.min(geo.width / layer.naturalWidth, geo.height / layer.naturalHeight);
                    const [w, h] = [layer.naturalWidth * fitted, layer.naturalHeight * fitted];
                    const to = k / geo.base; // canvas pixels to a pixel of the view's box
                    context.drawImage(
                        layer,
                        origin[0] + ((geo.width - w) / 2 - boxed[0]) * to,
                        origin[1] + ((geo.height - h) / 2 - boxed[1]) * to,
                        w * to,
                        h * to,
                    );
                } else if (shapes) {
                    await drawShapes(context, shapes, {
                        area: [left, top, right - left, bottom - top],
                        at,
                        extent,
                        outline,
                    });
                }
            }
        }
        context.globalAlpha = 1;
        drawLegend(context, legendOf(viewer), size);
        const title = document.querySelector(`.dl-view-title[data-dl-instance="${CSS.escape(instance)}"]`);
        const name = [
            ...["dl-view-chart", "dl-view-run", "dl-view-step"].map(
                (className) => title.querySelector(`.${className}`).textContent,
            ),
            choice === VIEW_CHOICE ? "view" : `${k}x`,
        ]
            .join("_")
            .replace(/[^\w.-]+/g, "_");
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = `${name}.png`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    };

    // Pointing at "Download" offers the sizes (see the CSS); a click on one downloads at it.
    document.addEventListener("click", (event) => {
        const choice = event.target.closest?.(".dl-view-download [data-dl-download]");
        if (choice) {
            download(choice.closest(".dl-view-title").dataset.dlInstance, choice.dataset.dlDownload).catch(
                (error) => console.error("image viewer: the picture could not be downloaded:", error),
            );
        }
    });

    return { stepOf, open, close, shown };
})();
