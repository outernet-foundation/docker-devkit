import pytest
from pydantic import ValidationError

from docker_devkit.documents import BakeDocument, digest_pinned


def _bake_with(reference: str) -> BakeDocument:
    return BakeDocument.model_validate({"x-base-images": {"TEST_IMAGE": reference}})


class TestBaseImagesValidator:
    def test_should_accept_tagged_reference(self):
        bake = _bake_with("alpine:3.20")
        assert bake.base_images == {"TEST_IMAGE": "alpine:3.20"}

    def test_should_accept_digest_pinned_reference(self):
        reference = f"ghcr.io/outernet-foundation/mirror/docker.io/library/alpine@sha256:{'a' * 64}"
        assert _bake_with(reference).base_images == {"TEST_IMAGE": reference}

    def test_should_accept_tag_and_digest_reference(self):
        reference = f"ghcr.io/outernet-foundation/mirror/docker.io/library/python:3.13-slim@sha256:{'a' * 64}"
        assert _bake_with(reference).base_images == {"TEST_IMAGE": reference}

    def test_should_accept_registry_port_reference(self):
        reference = "localhost:5000/org/image:1.0"
        assert _bake_with(reference).base_images == {"TEST_IMAGE": reference}

    def test_should_default_to_empty(self):
        assert BakeDocument.model_validate({"services": {}}).base_images == {}

    def test_should_reject_bare_path(self):
        with pytest.raises(ValidationError):
            _bake_with("ghcr.io/outernet-foundation/mirror/docker.io/library/alpine")

    def test_should_reject_empty_tag(self):
        with pytest.raises(ValidationError):
            _bake_with("ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:")

    def test_should_reject_build_arg_hole(self):
        with pytest.raises(ValidationError):
            _bake_with("ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.20${ALPINE_DIGEST}")

    def test_should_reject_short_digest(self):
        with pytest.raises(ValidationError):
            _bake_with(f"ghcr.io/outernet-foundation/mirror/docker.io/library/alpine@sha256:{'a' * 63}")

    def test_should_reject_whitespace(self):
        with pytest.raises(ValidationError):
            _bake_with("ghcr.io/outernet-foundation/mirror/docker.io/library/alpine:3.20 slim")


class TestDigestPinned:
    def test_should_detect_digest_tail(self):
        assert digest_pinned(f"ghcr.io/org/image:1.0@sha256:{'a' * 64}")

    def test_should_detect_digest_without_tag(self):
        assert digest_pinned(f"ghcr.io/org/image@sha256:{'a' * 64}")

    def test_should_reject_plain_tag(self):
        assert not digest_pinned("ghcr.io/org/image:1.0")

    def test_should_reject_partial_digest(self):
        assert not digest_pinned("ghcr.io/org/image@sha256:abc")
