"""
Pins the exception hierarchy a generated client's callers program against: every error is an
`Error`, API-returned errors are `ApiError`s (with `BadRequest`/`AuthError`/`RateLimited`
beneath), and transport/format/SDK failures are siblings, never `ApiError`s.
"""
import pytest

from truewire_core.exceptions import (
  ApiError, AuthError, BadRequest, Error, LogicError, NetworkError, RateLimited, ValidationError,
)


@pytest.mark.parametrize('cls', [
  NetworkError, ValidationError, ApiError, BadRequest, AuthError, RateLimited, LogicError,
])
def test_every_error_is_an_error_and_an_exception(cls: type[Error]):
  assert issubclass(cls, Error)
  assert issubclass(cls, Exception)


@pytest.mark.parametrize('cls', [BadRequest, AuthError, RateLimited])
def test_api_error_subclasses(cls: type[ApiError]):
  assert issubclass(cls, ApiError)


@pytest.mark.parametrize('cls', [NetworkError, ValidationError, LogicError])
def test_non_api_errors_are_not_api_errors(cls: type[Error]):
  """A caller catching `ApiError` must not swallow a dropped connection or an SDK bug."""
  assert not issubclass(cls, ApiError)


def test_api_error_leaves_are_siblings():
  assert not issubclass(AuthError, BadRequest)
  assert not issubclass(RateLimited, BadRequest)
  assert not issubclass(RateLimited, AuthError)


def test_catching_error_catches_everything():
  for cls in (NetworkError, ValidationError, BadRequest, AuthError, RateLimited, LogicError):
    with pytest.raises(Error):
      raise cls('boom')


def test_str_single_arg():
  assert str(AuthError('bad key')) == 'AuthError(bad key)'


def test_str_multiple_args():
  assert str(NetworkError('GET /x', 'timeout')) == 'NetworkError(GET /x, timeout)'


def test_str_no_args():
  assert str(LogicError()) == 'LogicError()'


def test_str_names_the_concrete_class():
  """`__str__` is defined on `Error` and reports the subclass, so a log line says which one."""
  assert str(RateLimited('slow down')).startswith('RateLimited(')
