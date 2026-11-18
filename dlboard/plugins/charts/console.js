// A run's console chunk (`console.py`): fetched from its artifact URL and drawn a line at a time. A line that is a JSON
// object (structlog's JSON renderer, say) is drawn with its time, level and message pulled out and the rest after them;
// anything else as written. The filter narrows the lines of this chunk alone: nothing searches more than the chunk.
// biome-ignore lint/correctness/noUnusedVariables: read as the body of a Dash clientside_callback
function consoleChunk(shown, filter) {
    // On `window` because Dash takes this file as one function expression, with nothing outside it.
    window.dlConsole ??= (() => {
        const cachedChunks = 16;
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

        const component = (type, className, children, extra = {}) => ({
            namespace: "dash_html_components",
            type,
            props: { className, children, ...extra },
        });
        const firstOf = (record, keys) =>
            keys.find((key) => record[key] !== undefined && record[key] !== null);
        const shownValue = (value) => (typeof value === "string" ? value : JSON.stringify(value));

        // A JSON object's fields, or null for a line that is not one.
        const record = (raw) => {
            if (!raw.startsWith("{")) {
                return null;
            }
            try {
                const parsed = JSON.parse(raw);
                return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
            } catch {
                return null;
            }
        };

        const line = (raw) => {
            const fields = record(raw);
            if (fields === null) {
                return component("Div", "dl-console-line", raw);
            }
            const [levelKey, messageKey, timeKey] = [levelKeys, messageKeys, timeKeys].map((keys) =>
                firstOf(fields, keys),
            );
            const level = levelKey ? String(fields[levelKey]).toLowerCase() : "";
            const rest = Object.entries(fields)
                .filter(([key]) => key !== levelKey && key !== messageKey && key !== timeKey)
                .map(([key, value]) => `${key}=${shownValue(value)}`)
                .join(" ");
            const parts = [
                timeKey ? component("Span", "dl-console-time", `${shownValue(fields[timeKey])} `) : null,
                level ? component("Span", "dl-console-level", `${level.toUpperCase()} `) : null,
                component("Span", "dl-console-message", messageKey ? shownValue(fields[messageKey]) : ""),
                rest ? component("Span", "dl-console-rest", ` ${rest}`) : null,
            ].filter((part) => part !== null);
            return component("Div", `dl-console-line dl-console-json dl-console-${level || "none"}`, parts, {
                title: raw,
            });
        };

        // What the client said of the chunk: output it dropped before it, and the run's cap reached at it.
        const notices = (tags) => [
            ...(tags?.dropped_chars
                ? [
                      component(
                          "Div",
                          "dl-console-notice",
                          `${tags.dropped_chars} characters were dropped before this`,
                      ),
                  ]
                : []),
            ...(tags?.capped
                ? [
                      component(
                          "Div",
                          "dl-console-notice",
                          "The run's capture limit was reached here; nothing later is kept",
                      ),
                  ]
                : []),
        ];

        return { text, line, notices };
    })();
    if (!shown?.url) {
        return window.dash_clientside.no_update;
    }
    const needle = (filter ?? "").trim().toLowerCase();
    return window.dlConsole
        .text(shown.url)
        .then((body) => {
            const lines = body.split("\n");
            if (lines.at(-1) === "") {
                lines.pop();
            }
            const kept = needle ? lines.filter((each) => each.toLowerCase().includes(needle)) : lines;
            return [...window.dlConsole.notices(shown.tags), ...kept.map(window.dlConsole.line)];
        })
        .catch((error) => [
            {
                namespace: "dash_html_components",
                type: "Div",
                props: { className: "dl-console-notice", children: String(error) },
            },
        ]);
}
