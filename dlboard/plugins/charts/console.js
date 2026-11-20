// A run's console chunk (`console.py`): fetched from its artifact URL and drawn as a terminal would show it. A line that
// is a JSON object (structlog's JSON renderer, say) is drawn with its time, level and message pulled out and the rest
// after them; any other line in its colours (ANSI SGR escapes), as it was last drawn: a progress bar redraws its line
// with `\r`, so only what follows the last one is shown, and every other escape is dropped. The filter narrows the lines
// of this chunk alone: nothing searches more than the chunk.
//
// Only the lines in view (and a few either side) are in the page: a chunk of thousands of lines costs what a screenful
// does. Every line is one `lineHeight` tall and never wraps (a long one scrolls sideways), so which lines are in view
// follows from how far the text is scrolled. The lines are drawn into the chart's element here, not returned to Dash,
// which would make a component of each: the callback's output only records what was drawn.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function consoleChunk(shown, filter) {
    // Dash takes the callback's context away as soon as this returns its promise, so it is read first: one output's.
    const outputs = window.dash_clientside.callback_context.outputs_list;
    const output = Array.isArray(outputs) ? outputs[0] : outputs;
    // On `window` because Dash takes this file as one function expression, with nothing outside it.
    window.dlConsole ??= (() => {
        const cachedChunks = 16;
        const lineHeight = 18; // px: `.dl-console-line`'s height in console.css
        const overscan = 30; // lines drawn either side of the ones in view, so a scroll rarely shows a gap
        const levelKeys = ["level", "levelname", "severity"];
        const messageKeys = ["event", "msg", "message"];
        const timeKeys = ["timestamp", "time", "asctime", "ts"];

        // Fetched chunks by URL, the most recently used last: paging back and forth asks the server for nothing.
        const cache = new Map();
        const text = (url) => {
            let body = cache.get(url);
            if (body) {
                cache.delete(url);
            } else {
                body = fetch(url, { credentials: "same-origin" }).then((response) => {
                    if (!response.ok) {
                        throw new Error(`${response.status} fetching ${url}`);
                    }
                    return response.text();
                });
                body.catch(() => cache.delete(url));
            }
            cache.set(url, body);
            while (cache.size > cachedChunks) {
                cache.delete(cache.keys().next().value);
            }
            return body;
        };

        // A control sequence (CSI: colours, cursor moves, erasing), an operating system command (a window title, a
        // link), or any other two-character escape.
        const escapes =
            // biome-ignore lint/suspicious/noControlCharactersInRegex: matching terminal escapes is the point
            /\u001b\[([0-?]*)[ -/]*([@-~])|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b[@-_]/g;

        // The line as a terminal leaves it: what was drawn after its last carriage return (a line ending `\r\n` is one).
        const settled = (raw) => {
            const line = raw.endsWith("\r") ? raw.replace(/\r+$/, "") : raw;
            return line.slice(line.lastIndexOf("\r") + 1);
        };
        const plain = (line) => line.replace(escapes, "");

        // The 256-colour palette past its first 16: a 6x6x6 cube, then 24 greys.
        const indexed = (n) => {
            if (n < 232) {
                const level = (v) => (v === 0 ? 0 : 55 + v * 40);
                const at = n - 16;
                return `rgb(${level(Math.floor(at / 36))}, ${level(Math.floor(at / 6) % 6)}, ${level(at % 6)})`;
            }
            const grey = 8 + (n - 232) * 10;
            return `rgb(${grey}, ${grey}, ${grey})`;
        };
        // A colour an SGR sequence picks at `codes[at]` (38 or 48): `5;n` from the palette, `2;r;g;b` exactly.
        // Returns the colour (a palette index under 16, else CSS) and how many codes it took.
        const extended = (codes, at) => {
            if (codes[at + 1] === 5 && codes[at + 2] !== undefined) {
                const n = codes[at + 2];
                return [n < 16 ? n : indexed(n), 2];
            }
            if (codes[at + 1] === 2 && codes[at + 4] !== undefined) {
                return [`rgb(${codes[at + 2]}, ${codes[at + 3]}, ${codes[at + 4]})`, 4];
            }
            return [null, 0];
        };
        const plainStyle = {
            fg: null,
            bg: null,
            bold: false,
            dim: false,
            italic: false,
            underline: false,
            inverse: false,
        };
        const restyled = (style, params) => {
            const next = { ...style };
            const codes = params === "" ? [0] : params.split(";").map((code) => Number(code || 0));
            for (let at = 0; at < codes.length; at++) {
                const code = codes[at];
                if (code === 0) {
                    Object.assign(next, plainStyle);
                } else if (code === 1 || code === 2 || code === 3 || code === 4 || code === 7) {
                    next[{ 1: "bold", 2: "dim", 3: "italic", 4: "underline", 7: "inverse" }[code]] = true;
                } else if (code === 22) {
                    next.bold = false;
                    next.dim = false;
                } else if (code === 23 || code === 24 || code === 27) {
                    next[{ 23: "italic", 24: "underline", 27: "inverse" }[code]] = false;
                } else if (code >= 30 && code <= 37) {
                    next.fg = code - 30;
                } else if (code >= 90 && code <= 97) {
                    next.fg = code - 90 + 8;
                } else if (code >= 40 && code <= 47) {
                    next.bg = code - 40;
                } else if (code >= 100 && code <= 107) {
                    next.bg = code - 100 + 8;
                } else if (code === 39) {
                    next.fg = null;
                } else if (code === 49) {
                    next.bg = null;
                } else if (code === 38 || code === 48) {
                    const [colour, took] = extended(codes, at);
                    next[code === 38 ? "fg" : "bg"] = colour;
                    at += took;
                }
            }
            return next;
        };

        const element = (tag, className, content) => {
            const el = document.createElement(tag);
            el.className = className;
            if (content !== undefined) {
                el.textContent = content; // never as HTML: a run prints whatever it likes
            }
            return el;
        };
        // A run of text in `style`: a palette colour by class, so it follows the theme; any other inline.
        const styled = (content, style) => {
            const [fg, bg] = style.inverse ? [style.bg, style.fg] : [style.fg, style.bg];
            const classes = [
                ...["bold", "dim", "italic", "underline"]
                    .filter((flag) => style[flag])
                    .map((flag) => `dl-ansi-${flag}`),
                ...(typeof fg === "number" ? [`dl-ansi-fg-${fg}`] : []),
                ...(typeof bg === "number" ? [`dl-ansi-bg-${bg}`] : []),
                ...(style.inverse && fg === null && bg === null ? ["dl-ansi-inverse"] : []),
            ];
            if (classes.length === 0 && typeof fg !== "string" && typeof bg !== "string") {
                return document.createTextNode(content);
            }
            const span = element("span", classes.join(" "), content);
            if (typeof fg === "string") {
                span.style.color = fg;
            }
            if (typeof bg === "string") {
                span.style.backgroundColor = bg;
            }
            return span;
        };
        const terminalLine = (line) => {
            const div = element("div", "dl-console-line");
            let style = plainStyle;
            let from = 0;
            for (const match of line.matchAll(escapes)) {
                if (match.index > from) {
                    div.append(styled(line.slice(from, match.index), style));
                }
                if (match[2] === "m") {
                    style = restyled(style, match[1]);
                }
                from = match.index + match[0].length;
            }
            if (from < line.length) {
                div.append(styled(line.slice(from), style));
            }
            return div;
        };

        const firstOf = (record, keys) =>
            keys.find((key) => record[key] !== undefined && record[key] !== null);
        const shownValue = (value) => (typeof value === "string" ? value : JSON.stringify(value));
        // A JSON object's fields, or null for a line that is not one.
        const record = (line) => {
            if (!line.startsWith("{")) {
                return null;
            }
            try {
                const parsed = JSON.parse(line);
                return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
            } catch {
                return null;
            }
        };
        const recordLine = (fields, line) => {
            const [levelKey, messageKey, timeKey] = [levelKeys, messageKeys, timeKeys].map((keys) =>
                firstOf(fields, keys),
            );
            const level = levelKey ? String(fields[levelKey]).toLowerCase() : "";
            const rest = Object.entries(fields)
                .filter(([key]) => key !== levelKey && key !== messageKey && key !== timeKey)
                .map(([key, value]) => `${key}=${shownValue(value)}`)
                .join(" ");
            const div = element("div", `dl-console-line dl-console-json dl-console-${level || "none"}`);
            div.title = line;
            if (timeKey) {
                div.append(element("span", "dl-console-time", `${shownValue(fields[timeKey])} `));
            }
            if (level) {
                div.append(element("span", "dl-console-level", `${level.toUpperCase()} `));
            }
            div.append(
                element("span", "dl-console-message", messageKey ? shownValue(fields[messageKey]) : ""),
            );
            if (rest) {
                div.append(element("span", "dl-console-rest", ` ${rest}`));
            }
            return div;
        };
        // An entry of the list: a line of the chunk, or a notice about it (output dropped, the run's cap reached).
        const drawn = (entry) => {
            if (entry.notice) {
                return element("div", "dl-console-line dl-console-notice", entry.notice);
            }
            const fields = record(entry.line);
            return fields === null ? terminalLine(entry.line) : recordLine(fields, entry.line);
        };

        // The chunk's entries in the order printed: what the client said of it at either end (output it dropped before
        // it, the run's cap reached at its end) around its lines.
        const entries = (tags, lines) => [
            ...(tags?.dropped_chars
                ? [{ notice: `${tags.dropped_chars} characters were dropped before this` }]
                : []),
            ...lines.map((line) => ({ line })),
            ...(tags?.capped
                ? [{ notice: "The run's capture limit was reached here; nothing later is kept" }]
                : []),
        ];

        // Where the chunk on show is among its run's: "Chunk 3 of 40", and which end it is at.
        const position = (shown) => {
            const count = shown.last_step - shown.first_step + 1;
            const at = shown.step - shown.first_step + 1;
            const end = count === 1 ? "" : { [count]: " (newest)", 1: " (oldest)" }[at] || "";
            return `Chunk ${at} of ${count}${end}`;
        };

        // Each chart's list, by its element: the entries, and which of them are drawn.
        const lists = new WeakMap();
        const drawWindow = (root) => {
            const list = lists.get(root);
            const first = Math.max(0, Math.floor(root.scrollTop / lineHeight) - overscan);
            const last = Math.min(
                list.entries.length,
                Math.ceil((root.scrollTop + root.clientHeight) / lineHeight) + overscan,
            );
            if (first === list.first && last === list.last) {
                return;
            }
            Object.assign(list, { first, last });
            list.lines.style.transform = `translateY(${first * lineHeight}px)`;
            list.lines.replaceChildren(...list.entries.slice(first, last).map(drawn));
        };
        const show = (root, entries) => {
            let list = lists.get(root);
            if (!list) {
                const sizer = element("div", "dl-console-sizer");
                const lines = element("div", "dl-console-window");
                sizer.append(lines);
                root.replaceChildren(sizer);
                list = { sizer, lines, pending: false };
                lists.set(root, list);
                root.addEventListener(
                    "scroll",
                    () => {
                        if (!list.pending) {
                            list.pending = true;
                            requestAnimationFrame(() => {
                                list.pending = false;
                                drawWindow(root);
                            });
                        }
                    },
                    { passive: true },
                );
            }
            Object.assign(list, { entries, first: -1, last: -1 });
            list.sizer.style.height = `${entries.length * lineHeight}px`;
            root.scrollTop = 0;
            drawWindow(root);
        };

        return { text, settled, plain, entries, position, show };
    })();
    const instance = output && CSS.escape(output.id.instance);
    const root = instance && document.querySelector(`.dl-console[data-dl-instance="${instance}"]`);
    if (!shown?.url || !root) {
        return window.dash_clientside.no_update;
    }
    const view = window.dlConsole;
    const label = document.querySelector(`.dl-console-position[data-dl-instance="${instance}"]`);
    if (label) {
        label.textContent = view.position(shown);
    }
    const needle = (filter ?? "").trim().toLowerCase();
    const drawnAs = `${shown.url} ${needle}`;
    return view
        .text(shown.url)
        .then((body) => {
            const lines = body.split("\n");
            if (lines.at(-1) === "") {
                lines.pop();
            }
            const settled = lines.map(view.settled);
            const kept = needle
                ? settled.filter((line) => view.plain(line).toLowerCase().includes(needle))
                : settled;
            const printed = view.entries(shown.tags, kept);
            // Newest first: the chunk's last line at the top, so reading down goes back in time, as Older does.
            view.show(root, root.dataset.dlOrder === "newest_first" ? printed.reverse() : printed);
            return drawnAs;
        })
        .catch((error) => {
            view.show(root, [{ notice: String(error) }]);
            return drawnAs;
        });
}
