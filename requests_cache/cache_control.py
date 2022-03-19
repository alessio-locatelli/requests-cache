"""Internal utilities for determining cache expiration and other cache actions. This module defines
the majority of the caching policy, and resulting actions are handled in
:py:meth:`CachedSession.send`.

.. automodsumm:: requests_cache.cache_control
   :classes-only:
   :nosignatures:

.. automodsumm:: requests_cache.cache_control
   :functions-only:
   :nosignatures:
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from fnmatch import fnmatch
from logging import getLogger
from math import ceil
from typing import TYPE_CHECKING, Any, Dict, MutableMapping, Optional, Tuple, Union

from attr import define, field
from requests import PreparedRequest, Response
from requests.models import CaseInsensitiveDict

from ._utils import coalesce

__all__ = ['DO_NOT_CACHE', 'CacheActions']
if TYPE_CHECKING:
    from .models import CachedResponse, CacheSettings, RequestSettings

# May be set by either headers or expire_after param to disable caching or disable expiration
DO_NOT_CACHE = 0
NEVER_EXPIRE = -1

CacheDirective = Union[None, int, bool]
ExpirationTime = Union[None, int, float, str, datetime, timedelta]
ExpirationPatterns = Dict[str, ExpirationTime]

logger = getLogger(__name__)


@define
class CacheActions:
    """Translates cache settings and headers into specific actions to take for a given cache item.

    * See :ref:`precedence` for behavior if multiple sources provide an expiration
    * See :ref:`headers` for more details about header behavior
    """

    cache_key: str = field(default=None)
    expire_after: ExpirationTime = field(default=None)
    request_directives: Dict[str, CacheDirective] = field(factory=dict)
    resend_request: bool = field(default=False)
    revalidate: bool = field(default=False)
    error_504: bool = field(default=False)
    send_request: bool = field(default=False)
    settings: CacheSettings = field(default=None)
    skip_read: bool = field(default=False)
    skip_write: bool = field(default=False)
    validation_headers: Dict[str, str] = field(factory=dict)

    @classmethod
    def from_request(
        cls,
        cache_key: str,
        request: PreparedRequest,
        settings: 'RequestSettings',
        **kwargs,
    ):
        """Initialize from request info and cache settings.

        Notes:

        * If ``cache_control=True``, ``expire_after`` will be handled in
          :py:meth:`update_from_response()` since it may be overridden by response headers.
        * The ``requests-cache-refresh`` temporary header is used solely to support the ``refresh``
          option in :py:meth:`CachedSession.request`; see notes there on interactions between
          ``request()`` and ``send()``.
        """
        request.headers = request.headers or CaseInsensitiveDict()
        directives = get_cache_directives(request.headers)
        logger.debug(f'Cache directives from request headers: {directives}')

        # Check expiration values in order of precedence
        expire_after = coalesce(
            directives.get('max-age'),
            settings.request_expire_after,
            get_url_expiration(request.url, settings.urls_expire_after),
            settings.expire_after,
        )

        # Check conditions for cache read and write based on args and request headers
        refresh_temp_header = request.headers.pop('requests-cache-refresh', False)
        check_expiration = directives.get('max-age') if settings.cache_control else expire_after
        skip_write = check_expiration == DO_NOT_CACHE or 'no-store' in directives

        # These behaviors may be set by either request headers or keyword arguments
        settings.only_if_cached = settings.only_if_cached or 'only-if-cached' in directives
        revalidate = settings.revalidate or 'no-cache' in directives
        skip_read = any(
            [settings.refresh, skip_write, bool(refresh_temp_header), settings.disabled]
        )

        return cls(
            cache_key=cache_key,
            expire_after=expire_after,
            request_directives=directives,
            revalidate=revalidate,
            skip_read=skip_read,
            skip_write=skip_write,
            settings=settings,
        )

    @property
    def expires(self) -> Optional[datetime]:
        """Convert the user/header-provided expiration value to a datetime"""
        return get_expiration_datetime(self.expire_after)

    def update_from_cached_response(self, response: CachedResponse):
        """Check for relevant cache headers from a cached response, and set headers for a
        conditional request, if possible.

        Used after fetching a cached response, but before potentially sending a new request.
        """
        # Determine if we need to send a new request or respond with an error
        is_expired = getattr(response, 'is_expired', False)
        if self.settings.only_if_cached and (response is None or is_expired):
            self.error_504 = True
        elif response is None:
            self.send_request = True
        elif is_expired and not (self.settings.only_if_cached and self.settings.stale_if_error):
            self.resend_request = True

        if response is None:
            return

        # Revalidation may be triggered by either stale response or request/cached response headers
        directives = get_cache_directives(response.headers)
        self.revalidate = _has_validator(response.headers) and any(
            [
                response.is_expired,
                self.revalidate,
                'no-cache' in directives,
                'must-revalidate' in directives and directives.get('max-age') == 0,
            ]
        )

        if self.revalidate:
            self.send_request = True
            if response.headers.get('ETag'):
                self.validation_headers['If-None-Match'] = response.headers['ETag']
            if response.headers.get('Last-Modified'):
                self.validation_headers['If-Modified-Since'] = response.headers['Last-Modified']

    def update_from_response(self, response: Response):
        """Update expiration + actions based on headers from a new response.

        Used after receiving a new response but before saving it to the cache.
        """
        if not response or not self.settings.cache_control:
            return

        directives = get_cache_directives(response.headers)
        logger.debug(f'Cache directives from response headers: {directives}')

        # Check headers for expiration, validators, and other cache directives
        if directives.get('immutable'):
            self.expire_after = NEVER_EXPIRE
        else:
            self.expire_after = coalesce(
                directives.get('max-age'), directives.get('expires'), self.expire_after
            )
        no_store = 'no-store' in directives or 'no-store' in self.request_directives

        # If expiration is 0 and there's a validator, save it to the cache and revalidate on use
        # Otherwise, skip writing to the cache if specified by expiration or other headers
        expire_immediately = try_int(self.expire_after) == DO_NOT_CACHE
        self.skip_write = (expire_immediately or no_store) and not _has_validator(response.headers)

        # Apply filter callback, if any
        filtered_out = self.settings.filter_fn is not None and not self.settings.filter_fn(response)

        # Apply and log remaining checks needed to determine if the response should be cached
        cache_criteria = {
            'disabled cache': self.settings.disabled,
            'disabled method': str(response.request.method) not in self.settings.allowable_methods,
            'disabled status': response.status_code not in self.settings.allowable_codes,
            'disabled by filter': filtered_out,
            'disabled by headers or expiration params': self.skip_write,
        }
        logger.debug(f'Pre-cache checks for response from {response.url}: {cache_criteria}')
        self.skip_write = any(cache_criteria.values())

    def update_request(self, request: PreparedRequest) -> PreparedRequest:
        """Apply validation headers (if any) before sending a request"""
        # if self.revalidate:
        request.headers.update(self.validation_headers)
        return request

    def update_revalidated_response(
        self, response: Response, cached_response: CachedResponse
    ) -> CachedResponse:
        """After revalidation, update the cached response's headers and reset its expiration"""
        logger.debug(
            f'Response for URL {response.request.url} has not been modified; updating and using cached response'
        )
        cached_response.expires = self.expires
        cached_response.headers.update(response.headers)
        self.update_from_response(cached_response)
        return cached_response


def append_directive(
    headers: Optional[MutableMapping[str, str]], directive: str
) -> MutableMapping[str, str]:
    """Append a Cache-Control directive to existing headers (if any)"""
    headers = CaseInsensitiveDict(headers)
    directives = headers['Cache-Control'].split(',') if headers.get('Cache-Control') else []
    directives.append(directive)
    headers['Cache-Control'] = ','.join(directives)
    return headers


def get_expiration_datetime(expire_after: ExpirationTime) -> Optional[datetime]:
    """Convert an expiration value in any supported format to an absolute datetime"""
    # Never expire
    if expire_after is None or expire_after == NEVER_EXPIRE:
        return None
    # Expire immediately
    elif try_int(expire_after) == DO_NOT_CACHE:
        return datetime.utcnow()
    # Already a datetime or datetime str
    if isinstance(expire_after, str):
        return parse_http_date(expire_after)
    elif isinstance(expire_after, datetime):
        return to_utc(expire_after)

    # Otherwise, it must be a timedelta or time in seconds
    if not isinstance(expire_after, timedelta):
        expire_after = timedelta(seconds=expire_after)
    return datetime.utcnow() + expire_after


def get_expiration_seconds(expire_after: ExpirationTime) -> int:
    """Convert an expiration value in any supported format to an expiration time in seconds"""
    expires = get_expiration_datetime(expire_after)
    return ceil((expires - datetime.utcnow()).total_seconds()) if expires else NEVER_EXPIRE


def get_cache_directives(headers: MutableMapping) -> Dict[str, CacheDirective]:
    """Get all Cache-Control directives as a dict. Handle duplicate headers and comma-separated
    lists. Key-only directives are returned as ``{key: True}``.
    """
    if not headers:
        return {}

    kv_directives = {}
    if headers.get('Cache-Control'):
        cache_directives = headers['Cache-Control'].split(',')
        kv_directives = dict([split_kv_directive(value) for value in cache_directives])

    if 'Expires' in headers:
        kv_directives['expires'] = headers['Expires']
    return kv_directives


def get_504_response(request: PreparedRequest) -> Response:
    from .models import CachedResponse

    return CachedResponse(
        url=request.url or '',
        status_code=504,
        reason='Not Cached',
        request=request,  # type: ignore
    )


def get_url_expiration(
    url: Optional[str], urls_expire_after: ExpirationPatterns = None
) -> ExpirationTime:
    """Check for a matching per-URL expiration, if any"""
    if not url:
        return None

    for pattern, expire_after in (urls_expire_after or {}).items():
        if url_match(url, pattern):
            logger.debug(f'URL {url} matched pattern "{pattern}": {expire_after}')
            return expire_after
    return None


def parse_http_date(value: str) -> Optional[datetime]:
    """Attempt to parse an HTTP (RFC 5322-compatible) timestamp"""
    try:
        expire_after = parsedate_to_datetime(value)
        return to_utc(expire_after)
    except (TypeError, ValueError):
        logger.debug(f'Failed to parse timestamp: {value}')
        return None


def split_kv_directive(header_value: str) -> Tuple[str, CacheDirective]:
    """Split a cache directive into a ``(key, int)`` pair, if possible; otherwise just
    ``(key, True)``.
    """
    header_value = header_value.strip()
    if '=' in header_value:
        k, v = header_value.split('=', 1)
        return k, try_int(v)
    else:
        return header_value, True


def to_utc(dt: datetime):
    """All internal datetimes are UTC and timezone-naive. Convert any user/header-provided
    datetimes to the same format.
    """
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc)
        dt = dt.replace(tzinfo=None)
    return dt


def try_int(value: Any) -> Optional[int]:
    """Convert a value to an int, if possible, otherwise ``None``"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def url_match(url: str, pattern: str) -> bool:
    """Determine if a URL matches a pattern

    Args:
        url: URL to test. Its base URL (without protocol) will be used.
        pattern: Glob pattern to match against. A recursive wildcard will be added if not present

    Example:
        >>> url_match('https://httpbin.org/delay/1', 'httpbin.org/delay')
        True
        >>> url_match('https://httpbin.org/stream/1', 'httpbin.org/*/1')
        True
        >>> url_match('https://httpbin.org/stream/2', 'httpbin.org/*/1')
        False
    """
    url = url.split('://')[-1]
    pattern = pattern.split('://')[-1].rstrip('*') + '**'
    return fnmatch(url, pattern)


def _has_validator(headers: MutableMapping) -> bool:
    return bool(headers.get('ETag') or headers.get('Last-Modified'))
