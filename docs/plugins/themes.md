# Theme plugins

## What is this

A theme plugin sets the Mantine theme the whole app renders with — colors, fonts, light/dark mode
— by writing to the shared `MantineProvider`'s `theme` and `forceColorScheme` props. It's the
smallest plugin category: `dltrack/plugins/themes/dark.py` is the entire built-in, one callback,
no state.

The built-in theme asks for the `Inter` font and ships it: `dltrack/plugins/themes/_inter.py` serves a
bundled variable-font subset (SIL OFL, license alongside) from the app itself, so it renders the same
on every machine and works offline. A theme that names a different font is responsible for loading it
the same way -- a `fontFamily` alone only works if the font is already on the viewer's machine.

## When you'd need this

You want different branding (colors, font) or a light theme instead of the built-in dark one.
There's currently only one theme plugin (`dark`) — writing a `light` (or custom-branded) one is
the natural next theme plugin, following the same shape.

## How do I build one

```python
def plug(app: Dash) -> None:
    @app.callback(
        Output(constants.MANTINE_PROVIDER_ID, component_property="theme"),
        Output(constants.MANTINE_PROVIDER_ID, component_property="forceColorScheme"),
        Input(constants.MANTINE_PROVIDER_ID, component_property="id"),
    )
    def set_theme(_: str) -> tuple[dict[str, str | dict[str, str]], str]:
        return {"primaryColor": "teal", ...}, "dark"  # or "light"
```

The `theme` dict is a [Mantine theme object](https://mantine.dev/theming/theme-object/) — anything
valid there (colors, fonts, spacing, component defaults) is fair game. Register your theme instead
of `dark` in the plugin list passed to `app()`; only one theme plugin should be active at a time,
since they'd otherwise fight over the same callback output.
