"""Read settings from environment variables and from a query string.

Run with: python examples/string_inputs.py

Both sources arrive as text. Coercion and stringbool are explicit. A
stringbool default is the input string ``"false"``, which the schema converts
to ``False``. ``load_settings`` copies only known names, so unrelated
environment variables stay outside the schema. Issues from one object follow
field order.
"""

from collections.abc import Mapping

from zodish import Failure, ValidationError, z


settings_schema = z.object({
    "host": z.string().min(1).default("127.0.0.1"),
    "port": z.coerce.integer().min(1).max(65535).default(8080),
    "debug": z.string().stringbool().default("false"),
    "log_level": z.enum(["debug", "info", "warning", "error"]).default("info"),
    "database_url": z.string().url(),
}).strict()

_SETTINGS_NAMES = {
    "host": "APP_HOST",
    "port": "APP_PORT",
    "debug": "APP_DEBUG",
    "log_level": "APP_LOG_LEVEL",
    "database_url": "DATABASE_URL",
}

search_schema = z.object({
    "q": z.string().trim().min(1),
    "page": z.coerce.integer().positive().default(1),
    "per_page": z.coerce.integer().min(1).max(100).default(20),
    "include_archived": z.string().stringbool().default("false"),
}).strict()


def load_settings(environ: Mapping[str, str]) -> dict[str, object]:
    raw = {
        key: environ[name]
        for key, name in _SETTINGS_NAMES.items()
        if name in environ
    }
    return settings_schema.parse(raw)


def main() -> None:
    settings = load_settings({
        "PATH": "/usr/bin",
        "APP_PORT": "443",
        "APP_DEBUG": "YES",
        "DATABASE_URL": "postgres://localhost/app",
    })
    assert settings == {
        "host": "127.0.0.1",
        "port": 443,
        "debug": True,
        "log_level": "info",
        "database_url": "postgres://localhost/app",
    }

    try:
        load_settings({})
    except ValidationError as error:
        assert [issue.path for issue in error.issues] == [("database_url",)]
        assert error.issues[0].code == "required"
    else:
        raise AssertionError("missing DATABASE_URL should be rejected")

    invalid = settings_schema.safe_parse({
        "database_url": "localhost",
        "port": "0",
        "debug": "maybe",
        "workers": "4",
    })
    assert isinstance(invalid, Failure)
    assert [(issue.path, issue.code) for issue in invalid.error.issues] == [
        (("port",), "too_small"),
        (("debug",), "invalid_value"),
        (("database_url",), "invalid_format"),
        (("workers",), "unrecognized_key"),
    ]

    search = search_schema.parse({
        "q": "  gel pens  ",
        "page": "2",
        "include_archived": "on",
    })
    assert search == {
        "q": "gel pens",
        "page": 2,
        "per_page": 20,
        "include_archived": True,
    }
    unknown = search_schema.safe_parse({"q": "pens", "sort": "name"})
    assert isinstance(unknown, Failure)
    assert unknown.error.issues[0].code == "unrecognized_key"
    assert unknown.error.issues[0].path == ("sort",)

    print(settings)
    print(search)


if __name__ == "__main__":
    main()
