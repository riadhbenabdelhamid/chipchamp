"""One palette for terminal text, prompt-toolkit chrome and source code."""
from rich.style import Style
from rich.syntax import ANSISyntaxTheme
from pygments.token import Comment, Keyword, Name, Number, Operator, String


PALETTES = {
    "scope": ("#53d8fb", "#b8c9d4", "#879ca9", "#17232c", "#39d353", "#ffd75f", "#ff7373"),
    "phosphor": ("#a8ff00", "#e8ffc4", "#a0b879", "#19200f", "#a8ff00", "#ffd75f", "#ff7373"),
    "amber": ("#ffb000", "#ffe9c4", "#c0a477", "#281d0d", "#ffb000", "#ffe38a", "#ff7373"),
    "mono": ("#e8e8e8", "#d0d0d0", "#a0a0a0", "#242424", "#e8e8e8", "#d0d0d0", "#ffffff"),
    "light": ("#006d91", "#263238", "#526575", "#edf2f5", "#176b35", "#815400", "#b82020"),
    "": ("ansicyan", "ansidefault", "ansibrightblack", "ansidefault", "ansigreen", "ansiyellow", "ansired"),
}
ROLES = ("accent", "text", "muted", "surface", "success", "warning", "error")


def palette(name):
    return dict(zip(ROLES, PALETTES.get(name, PALETTES[""])))


def rich_color(color):
    return {"ansidefault": "default", "ansibrightblack": "bright_black"}.get(
        color, color.removeprefix("ansi"))


def rich_styles(name):
    p = palette(name)
    return {"ui." + role: rich_color(color) for role, color in p.items()}


def prompt_style(name):
    p = palette(name)
    return {
        "prompt": f"bold {p['accent']}",
        "bottom-toolbar": f"bg:{p['surface']} {p['text']}",
        "bottom-toolbar.text": p['text'],
        **{"status." + k: v for k, v in p.items()},
        "status.accent": f"bold {p['accent']}",
        "completion-menu": f"bg:{p['surface']} {p['text']}",
        "completion-menu.completion": f"bg:{p['surface']} {p['text']}",
        "completion-menu.meta.completion": f"bg:{p['surface']} {p['muted']}",
        "completion-menu.completion.current": f"bg:{p['accent']} {p['surface']}",
        "completion-menu.meta.completion.current": f"bg:{p['accent']} {p['surface']}",
        "completion.skill": "bold",
    }


def syntax_theme(name):
    p = {k: rich_color(v) for k, v in palette(name).items()}
    return ANSISyntaxTheme({
        Comment: Style(color=p['muted'], italic=True),
        Keyword: Style(color=p['accent'], bold=True),
        Name.Function: Style(color=p['accent']),
        Name.Class: Style(color=p['accent']),
        String: Style(color=p['success']),
        Number: Style(color=p['warning']),
        Operator: Style(color=p['text']),
    })
