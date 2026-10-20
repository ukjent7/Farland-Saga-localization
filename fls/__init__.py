"""Farland Saga localization and reverse-engineering toolchain."""

__version__ = '0.1.0'


def main() -> None:
    """Run the command-line entry point; ``python -m fls`` reaches this."""
    from fls.cli.main import main as run
    run()
