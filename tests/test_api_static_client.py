"""Serving the built React client from the API process, for a single-origin deployment.

The public deployment runs one container that answers both the API and the client, so the
browser makes same-origin requests and CORS is never involved. That is worth pinning in two
directions: the static mount must not shadow any API route, and a misconfigured directory must
fail loudly at startup rather than turning the whole deployment into a wall of 404s.

Nothing here is enabled by default -- an unset ``frontend_dist_path`` leaves the API exactly as
it was, with the Vite dev server owning the frontend locally.
"""

import pytest
from fastapi.testclient import TestClient
from test_overview import FakePrices, build_test_app, read_only_service, seed_repository

from marketsentinel.config import Settings


def build_client_dist(root, marker: str = '<div id="root"></div>'):
    """A minimal stand-in for `vite build` output: an entry point plus a hashed asset."""

    dist = root / "frontend-dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        f"<!doctype html><html><body>{marker}"
        '<script type="module" src="/assets/index-abc123.js"></script></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "index-abc123.js").write_text("export const api = '';", encoding="utf-8")
    return dist


def app_with_client(writable_tmp_path):
    repository = seed_repository(writable_tmp_path)
    dist = build_client_dist(writable_tmp_path)
    settings = Settings(frontend_dist_path=dist, public_mode=True)
    return build_test_app(repository, read_only_service(repository, FakePrices()), settings)


def test_the_client_is_not_served_unless_a_dist_directory_is_configured(writable_tmp_path) -> None:
    """The default stays a pure API, so a local two-process run is unchanged."""

    repository = seed_repository(writable_tmp_path)
    app = build_test_app(repository, read_only_service(repository, FakePrices()))

    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/health").status_code == 200


def test_the_root_serves_the_built_client(writable_tmp_path) -> None:
    with TestClient(app_with_client(writable_tmp_path)) as client:
        response = client.get("/")

    response.raise_for_status()
    assert 'id="root"' in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_hashed_assets_are_served(writable_tmp_path) -> None:
    with TestClient(app_with_client(writable_tmp_path)) as client:
        response = client.get("/assets/index-abc123.js")

    response.raise_for_status()
    assert "export const api" in response.text


def test_the_static_mount_never_shadows_an_api_route(writable_tmp_path) -> None:
    """Routes are registered before the catch-all mount, so every API path still wins.

    This is the regression that matters: mounting at "/" is exactly the shape that silently
    swallows an API surface if the registration order is ever inverted.
    """

    with TestClient(app_with_client(writable_tmp_path)) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/v1/capabilities").json()["mode"] == "public"
        assert client.get("/api/v1/companies/acme/overview").status_code == 200
        # Public mode still closes the two spending endpoints rather than serving the client.
        assert client.post("/api/v1/analyze", json={"symbol": "ACME"}).status_code == 404


def test_an_unknown_path_does_not_masquerade_as_the_client(writable_tmp_path) -> None:
    """A missing file is a 404, not a silent index.html.

    The client routes on a ``?symbol=`` query parameter rather than on the path, so it needs no
    history fallback -- and serving index.html for an unknown path would turn a mistyped API
    call into a confusing HTTP 200 carrying HTML.
    """

    with TestClient(app_with_client(writable_tmp_path)) as client:
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/api/v1/does-not-exist").status_code == 404


def test_a_configured_directory_without_an_entry_point_fails_at_startup(writable_tmp_path) -> None:
    """A deployment that forgot to build the client must not boot into an empty shell."""

    repository = seed_repository(writable_tmp_path)
    empty = writable_tmp_path / "unbuilt"
    empty.mkdir()
    settings = Settings(frontend_dist_path=empty)

    with pytest.raises(RuntimeError, match="index.html"):
        build_test_app(repository, read_only_service(repository, FakePrices()), settings)
