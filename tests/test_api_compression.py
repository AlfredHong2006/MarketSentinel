"""Response compression at the API boundary: a transport concern, never a contract change.

The Relevant News window is deliberately uncapped, so one company's article list is a large,
highly repetitive JSON body. These pin that compressing it changes nothing a client can observe
except the transfer encoding -- and that CORS still works, since the middleware order that makes
that true is easy to invert by accident.

Deliberately no assertion on compressed size: that would pin zlib's behaviour rather than ours.
"""

from fastapi.testclient import TestClient
from test_overview import FakePrices, build_test_app, read_only_service
from test_relevant_news import seed_large_corpus

from marketsentinel.domain import RelevantNewsView


def client_for(writable_tmp_path) -> TestClient:
    """A corpus large enough that the article list is comfortably over the minimum size."""

    repository = seed_large_corpus(writable_tmp_path)
    return TestClient(build_test_app(repository, read_only_service(repository, FakePrices())))


def test_a_large_payload_is_compressed_when_the_client_accepts_it(writable_tmp_path) -> None:
    with client_for(writable_tmp_path) as client:
        response = client.get(
            "/api/v1/companies/ACME/articles", headers={"Accept-Encoding": "gzip"}
        )

    response.raise_for_status()
    assert response.headers["content-encoding"] == "gzip"


def test_compression_leaves_the_contract_byte_for_byte_intact(writable_tmp_path) -> None:
    """The decoded body must still parse as the same view with the same rows."""

    with client_for(writable_tmp_path) as client:
        compressed = client.get(
            "/api/v1/companies/ACME/articles", headers={"Accept-Encoding": "gzip"}
        )
        plain = client.get(
            "/api/v1/companies/ACME/articles", headers={"Accept-Encoding": "identity"}
        )

    assert compressed.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in plain.headers
    # httpx transparently decodes, so this compares the delivered payloads, not the wire bytes.
    assert compressed.json() == plain.json()
    view = RelevantNewsView.model_validate_json(compressed.text)
    assert len(view.articles) == len(plain.json()["articles"])


def test_a_client_that_does_not_accept_gzip_still_receives_the_payload(writable_tmp_path) -> None:
    with client_for(writable_tmp_path) as client:
        response = client.get(
            "/api/v1/companies/ACME/articles", headers={"Accept-Encoding": "identity"}
        )

    response.raise_for_status()
    assert "content-encoding" not in response.headers
    assert len(response.json()["articles"]) > 0


def test_small_responses_are_left_uncompressed(writable_tmp_path) -> None:
    """Below the minimum size compression is overhead, so these must pass through untouched."""

    with client_for(writable_tmp_path) as client:
        health = client.get("/health", headers={"Accept-Encoding": "gzip"})
        capabilities = client.get("/api/v1/capabilities", headers={"Accept-Encoding": "gzip"})

    assert "content-encoding" not in health.headers
    assert "content-encoding" not in capabilities.headers
    assert health.json()["status"] == "ok"


def test_cors_headers_survive_on_a_compressed_response(writable_tmp_path) -> None:
    """A browser client must be able to read a response that was compressed on the way out.

    Not an ordering guard: both middleware orders were checked and behave identically for these
    requests, because CORS attaches its headers whichever layer compresses the body. What this
    pins is the combination working at all, so adding compression cannot silently cost the React
    client its cross-origin access.
    """

    with client_for(writable_tmp_path) as client:
        response = client.get(
            "/api/v1/companies/ACME/articles",
            headers={"Accept-Encoding": "gzip", "Origin": "http://localhost:5173"},
        )

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
