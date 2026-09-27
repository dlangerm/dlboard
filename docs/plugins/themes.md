# Theme plugins

## What is this

A theme plugin sets the Mantine theme the whole app renders with — colors, fonts, light/dark mode
— by handing the core app a `ThemeSpec` via `dltrack.serve.set_theme` from its `plug()`. It's the
smallest plugin category: `dltrack/plugins/themes/dark.py` is the entire built-in, one call, no
state.

The core app builds its `MantineProvider` from that spec directly, in the very first response, and
paints the theme's page background before any JS loads — so a themed page costs no extra callback
round trip and never flashes an unthemed one first. With no theme plugin installed at all, the app
renders with plain Mantine defaults.

The built-in theme asks for the `Inter` font and ships it: `dltrack/plugins/themes/_inter.py` serves a
bundled variable-font subset (SIL OFL, license alongside) from the app itself, so it renders the same
on every machine and works offline. A theme that names a different font is responsible for loading it
the same way -- a `fontFamily` alone only works if the font is already on the viewer's machine.

## When you'd need this

You want different branding (colors, font) or a light theme instead of the built-in dark one.

## How do I build one

```python
from dltrack.models import ColorScheme, SchemeColors, ThemeSpec
from dltrack.serve import set_theme


def plug(app: Dash) -> None:
    set_theme(
        app,
        ThemeSpec(
            mantine={"primaryColor": "teal", ...},
            default_color_scheme=ColorScheme.DARK,  # or LIGHT, or AUTO to follow the OS
            page_background=SchemeColors(light="#ffffff", dark="#242424"),
        ),
    )
```

`mantine` is a [Mantine theme object](https://mantine.dev/theming/theme-object/) — anything valid
there (colors, fonts, spacing, component defaults) is fair game. `page_background` should match
the theme's `--mantine-color-body` for each scheme, since it's what the page shows before Mantine's
own CSS has loaded. Register your theme instead of `dark` in the plugin list passed to `app()`;
only one theme plugin can be active at a time — a second `set_theme` on the same app raises.
