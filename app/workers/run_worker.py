from __future__ import annotations


def main() -> None:
    print(
        "No standalone worker is required. "
        "Jobs run through the built-in in-process Python queue when the API starts."
    )


if __name__ == "__main__":
    main()
