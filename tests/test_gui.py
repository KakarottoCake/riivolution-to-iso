"""GUI construction smoke tests.

These build the real widget tree in a withdrawn (never shown) window, so they
exercise the Tkinter wiring without needing a visible display. They skip
cleanly where Tk cannot initialise at all (headless CI without Xvfb).
"""

from __future__ import annotations

from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:  # no display available
        pytest.skip(f"no Tk display: {exc}")
    r.withdraw()
    yield r
    r.destroy()


def _xml(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<wiidisc version="1"><id game="SMN"/>'
        '<options><section name="Core"><option name="Game" default="1">'
        '<choice name="Enabled"><patch id="p"/></choice>'
        '<choice name="Hard"><patch id="q"/></choice></option></section></options>'
        '<patch id="p"><memory offset="0x80003000" value="60000000"/></patch>'
        '<patch id="q"><memory offset="0x80003000" value="38000000"/></patch></wiidisc>'
    )
    return path


def test_app_constructs(root):
    from riivultimatum.gui import App

    app = App(root)
    root.update_idletasks()
    assert app.mod_list.size() == 0


def test_adding_a_mod_renders_its_options(root, tmp_path):
    from riivultimatum import pipeline
    from riivultimatum.gui import App, GuiMod
    from riivultimatum.pipeline import ModSpec

    app = App(root)
    xml = _xml(tmp_path / "riivolution" / "Mod.xml")
    disc = pipeline.load_disc(ModSpec(xml=xml))
    mod = GuiMod(xml=xml, disc=disc)
    app.mods.append(mod)
    app._refresh_mod_list()
    app.mod_list.selection_set(0)
    app._show_options()
    root.update_idletasks()

    # One section label + one option row => at least two child widgets.
    assert len(app.opt_inner.winfo_children()) >= 2
    assert app.mod_list.get(0) == "1. Mod"


def test_choice_change_updates_the_summary(root, tmp_path):
    from riivultimatum import pipeline
    from riivultimatum.gui import App, GuiMod
    from riivultimatum.pipeline import ModSpec

    app = App(root)
    xml = _xml(tmp_path / "riivolution" / "Mod.xml")
    mod = GuiMod(xml=xml, disc=pipeline.load_disc(ModSpec(xml=xml)))
    app.mods.append(mod)

    app._set_choice(mod, "Core/Game", "Hard")
    assert "Game=Hard" in mod.summary()
    app._set_choice(mod, "Core/Game", "(disabled)")
    assert mod.summary() == "(no options enabled)"


def test_wbfs_toggle_updates_output_hint(root):
    from riivultimatum.gui import App

    app = App(root)
    app.wbfs_var.set(True)
    app._refresh_out_hint()
    assert "wbfs" in app.out_hint.cget("text").lower()
    app.wbfs_var.set(False)
    app._refresh_out_hint()
    assert ".iso" in app.out_hint.cget("text").lower()
