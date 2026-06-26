import importlib


def test_plugin_package_importable():
    pkg = importlib.import_module("hermes")
    assert pkg.__name__ == "hermes"


def test_plugin_version():
    from hermes import __version__

    assert __version__ == "2.0.0"
