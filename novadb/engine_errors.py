"""Small dependency-free error types shared by engine submodules."""

from __future__ import annotations


class VectorTypeError(ValueError):
    """A vector value failed its datatype contract."""
