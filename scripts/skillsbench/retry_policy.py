"""Classify transient transport failures without retrying task/tool failures."""

import http.client
import re
import ssl

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530}
_PERMANENT = re.compile(
    # Local I/O/configuration, authentication and certificate failures.
    # https://curl.se/libcurl/c/libcurl-errors.html
    r'\bcurl:?\s+\(?(?:23|26|27|47|58|59|60|67|77|90|91|94|98)\b|'
    r'certificate verify failed|certificate verification failed|ssl certificate problem|'
    r'self.signed certificate|no matching manifest|no matching distribution|'
    r'could not find a version that satisfies|'
    r'exec format error|no space left on device|permission denied|'
    r'\b(?:401 unauthorized|403 forbidden)\b', re.I)
_NETWORK = re.compile(
    r'(?:failed to fetch anonymous token|failed to do request):[^\n]*\bEOF\b|'
    r'DeadlineExceeded:\s*context deadline exceeded|'
    r'Curl error \((?:5|6|7|16|18|28|35|52|55|56|92|95|96)\)|'
    r'\b(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|ENETUNREACH)\b|'
    r'error decoding response body|client network socket disconnected|'
    r'UNEXPECTED_EOF_WHILE_READING|SSLEOFError|'
    r'Request middleware error: error sending request for url|'
    r'tls connection was (?:non-properly|unexpectedly) terminated|'
    r'\bcurl:?\s+\(?(?:5|6|7|16|18|28|35|52|55|56|92|95|96)\b|'
    r'(?:unable|failed) to connect|could not connect|'
    r'connection (?:failed|reset|refused|aborted|closed|timed out)|'
    r'server closed connection|request to https?://[^\s]+ timed out|'
    r'fetch-pack: unexpected disconnect while reading sideband packet|'
    r'(?:read|connect|operation|request|i/o) timed? ?out|readtimeouterror|'
    r'temporary failure (?:in name resolution|resolving)|could not resolve|'
    r'(?:failed|failure|error|wicked happened) resolving|'
    r'no address associated with hostname|name or service not known|'
    r'network is unreachable|internet (?:unreachable|not reachable)|'
    r'remote end closed|unexpected eof|broken pipe|'
    r'\b(?:502\s+bad gateway|503\s+service unavailable|504\s+gateway timeout|'
    r'429\s+too many requests)\b|tls handshake timeout', re.I)
_TRANSPORT_NAMES = {
    'APIConnectionError', 'APITimeoutError', 'Timeout', 'TimeoutException',
    'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout',
    'ConnectError', 'ReadError', 'WriteError', 'RemoteProtocolError',
    'ProtocolError', 'ChunkedEncodingError', 'ConnectionError',
}


def transient_network_failure(error):
    if isinstance(error, BaseException) and not isinstance(error, Exception):
        return False
    chain, seen = [], set()
    current = error
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain):
        return False
    for item in chain:
        status = getattr(item, 'status_code', getattr(item, 'code', None))
        if status is None:
            status = getattr(getattr(item, 'response', None), 'status_code', None)
        if type(status) is int:
            return status in RETRYABLE_STATUS
    text = '\n'.join(str(item) for item in chain) if chain else str(error)
    permanent = list(_PERMANENT.finditer(text))
    if permanent:
        missing_labels = {'no matching distribution', 'could not find a version that satisfies'}
        if any(hit.group().lower() not in missing_labels for hit in permanent):
            return False
        # pip reports no versions after losing the very package index it
        # needs. Require transport evidence for that same normalized package;
        # an unrelated failed index or an actual absent wheel is insufficient.
        normalize = lambda name: re.sub(r'[-_.]+', '-', name.lower())
        requirements = {normalize(name) for name in re.findall(
            r'(?:Could not find a version that satisfies the requirement|No matching distribution found for)\s+([A-Za-z0-9_.-]+)', text, re.I)}
        failed = set()
        for line in text.splitlines():
            match = re.search(r'Could not fetch URL https?://\S*/simple/([^/?\s]+)/', line, re.I)
            if match and _NETWORK.search(line):
                failed.add(normalize(match[1]))
        return bool(requirements) and requirements.issubset(failed)
    hash_error = re.search(r'do not match the hashes', text, re.I)
    if hash_error:
        # pip can finish a truncated HTTP body without a transport exception,
        # then reject its digest. Retry only when its final transfer progress
        # also proves truncation. Never change or bypass the expected hash.
        progress = re.findall(r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*[kMGT]?B\b',
                              text[:hash_error.start()], re.I)
        return bool(progress and float(progress[-1][0]) < float(progress[-1][1]))
    if re.search(r'unable to locate package|couldn.t find any package', text, re.I):
        # apt-get update can exit 0 after losing an index, so install then
        # reports missing packages. Re-fetch the failed metadata before
        # treating package availability as authoritative.
        return bool(re.search(r'failed to fetch', text, re.I) and _NETWORK.search(text))
    for item in chain:
        if isinstance(item, (TimeoutError, ConnectionError, http.client.IncompleteRead)):
            return True
        module = type(item).__module__.split('.')[0]
        if module in {'httpx', 'httpcore', 'requests', 'urllib3', 'openai', 'litellm'} and type(item).__name__ in _TRANSPORT_NAMES:
            return True
        if isinstance(item, ssl.SSLError):
            return True
    return bool(_NETWORK.search(text))


def retry_delay(attempt):
    # Same short exponential backoff as the ScienceWorld request client.
    return 2 ** min(attempt - 1, 2)
