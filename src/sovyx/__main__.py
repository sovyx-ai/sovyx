"""Entry point for `python -m sovyx`."""

from sovyx import __version__


def main() -> None:
    """Print version and exit."""
    print(f"Sovyx v{__version__}")


if __name__ == "__main__":
    main()
