// An option of the console chart's run picker (`console.py`), registered through DMC's "functions as props"
// (https://www.dash-mantine-components.com/functions-as-props): the run's name after its swatch, the colour and dash
// its lines are drawn in (`series_swatch_class`, which the option carries as `swatch`), so a run is found by how it
// looks on the page's other charts, not only by its name.
window.dashMantineFunctions = window.dashMantineFunctions || {};
window.dashMantineFunctions.consoleRunOption = ({ option }) =>
    window.React.createElement("span", { className: option.swatch }, option.label);
