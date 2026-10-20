// Cell components for the ag-grid tables a chart's settings are edited in
// (`serve/_pages/_experiment/_chart_editor_modal.py`), registered through dash-ag-grid's component functions: a column def
// names one as `"cellRenderer"` or `"cellEditor"` and gives it its `cellRendererParams` / `cellEditorParams`.
window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};

const h = React.createElement;

// A slider across a number's whole range, always shown in its cell (`min`, `max`, `step`, and the `fallback` a blank cell
// stands for): dragging it sets the cell's value, so there is no editing mode to enter.
window.dashAgGridComponentFunctions.DlboardSlider = (props) => {
    const { min, max, step, fallback } = props;
    const [value, setValue] = React.useState(props.value ?? fallback);
    return h(
        "div",
        { style: { display: "flex", alignItems: "center", gap: "0.5rem", height: "100%" } },
        h("input", {
            type: "range",
            min,
            max,
            step,
            value,
            style: { flex: 1, minWidth: 0 },
            onChange: (event) => {
                const next = Number(event.target.value);
                setValue(next);
                props.node.setDataValue(props.column.getColId(), next);
            },
        }),
        h("span", { style: { minWidth: "2.5rem", textAlign: "right" } }, value),
    );
};

// A few choices shown side by side in the cell, the current one marked, always there: one click on another sets it, so there
// is no editing mode to enter and no list to open (`choices`, and the `fallback` a blank cell stands for).
window.dashAgGridComponentFunctions.DlboardChoice = (props) => {
    const { choices, fallback } = props;
    const [value, setValue] = React.useState(props.value ?? fallback);
    return h(
        "div",
        { className: "dl-choice", role: "radiogroup" },
        choices.map((choice) =>
            h(
                "button",
                {
                    key: choice,
                    type: "button",
                    role: "radio",
                    "aria-checked": String(choice === value),
                    className: "dl-choice-option",
                    onClick: () => {
                        setValue(choice);
                        props.node.setDataValue(props.column.getColId(), choice);
                    },
                },
                choice,
            ),
        ),
    );
};

// A text cell that suggests as it is typed in (`options`): the names that contain what is typed, to pick with the mouse or
// the arrow keys. Anything can still be typed, as a name that is not there yet is a name too.
window.dashAgGridComponentFunctions.DlboardKeyPicker = React.forwardRef((props, ref) => {
    const [text, setText] = React.useState(props.value ?? "");
    const [active, setActive] = React.useState(-1);
    const latest = React.useRef(text);
    const matches = props.options
        .filter((option) => option.toLowerCase().includes(text.toLowerCase()))
        .slice(0, 50);
    const choose = (next) => {
        latest.current = next;
        setText(next);
        if (props.onValueChange) {
            props.onValueChange(next);
        }
        props.stopEditing();
    };
    React.useImperativeHandle(ref, () => ({ getValue: () => latest.current }));
    const list = React.useRef(null);
    React.useEffect(() => {
        // The arrow keys can move past what is in view.
        list.current?.querySelector("[data-active]")?.scrollIntoView({ block: "nearest" });
    }, [active]);
    return h(
        "div",
        { className: "dl-key-picker" },
        h("input", {
            className: "dl-key-picker-input",
            value: text,
            autoFocus: true,
            placeholder: "Type a name, or pick one",
            onChange: (event) => {
                latest.current = event.target.value;
                setText(event.target.value);
                setActive(-1);
                if (props.onValueChange) {
                    props.onValueChange(event.target.value);
                }
            },
            onKeyDown: (event) => {
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                    event.preventDefault();
                    event.stopPropagation();
                    const step = event.key === "ArrowDown" ? 1 : -1;
                    setActive((active + step + matches.length) % Math.max(matches.length, 1));
                } else if (event.key === "Enter" && matches[active] !== undefined) {
                    event.preventDefault();
                    event.stopPropagation();
                    choose(matches[active]);
                }
            },
        }),
        matches.length > 0
            ? h(
                  "div",
                  { className: "dl-key-picker-options", ref: list },
                  matches.map((option, index) =>
                      h(
                          "div",
                          {
                              key: option,
                              className: "dl-key-picker-option",
                              "data-active": index === active ? "" : undefined,
                              // `mousedown`, not `click`: the editor would lose focus, and stop, before a click landed.
                              onMouseDown: (event) => {
                                  event.preventDefault();
                                  choose(option);
                              },
                              onMouseEnter: () => setActive(index),
                          },
                          option,
                      ),
                  ),
              )
            : text &&
                  h(
                      "div",
                      { className: "dl-key-picker-empty" },
                      "Nothing logged matches; this name is used as typed",
                  ),
    );
});
