'''
async fetching of urls.

Assumes robots checks have already been done.

Supports server mocking; proxies are not yet implemented.

Success returns response object and response bytes (which were already
read in order to shake out all potential network-related exceptions.)

Failure returns enough details for the caller to do something smart:
503, other 5xx, DNS fail, connect timeout, error between connect and
full response, proxy failure. Plus an errorstring good enough for logging.

'''

import time
import traceback
from collections import namedtuple
import ssl
import urllib

import asyncio
import logging
import aiohttp
import aiodns

from . import stats
from . import config

LOGGER = logging.getLogger(__name__)


# XXX should be a policy plugin
# XXX cookie handling -- no way to have a cookie jar other than at session level
#    need to directly manipulate domain-level cookie jars to get cookies
#    CookieJar.add_cookie_header(request) is tied to urlllib.request, how does aiohttp use it?! probably duck
def apply_url_policies(url, crawler):
    headers = {}
    proxy = None
    mock_url = None
    mock_robots = None

    headers['User-Agent'] = crawler.ua

    test_host = config.read('Testing', 'TestHostmapAll')
    if test_host:
        headers['Host'] = url.urlsplit.netloc
        (scheme, netloc, path, query, fragment) = url.urlsplit
        netloc = test_host
        mock_url = urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))
        mock_robots = url.urlsplit.scheme + '://' + test_host + '/robots.txt'

    if crawler.prevent_compression:
        headers['Accept-Encoding'] = 'identity'

    if crawler.upgrade_insecure_requests:
        headers['Upgrade-Insecure-Requests'] = '1'

    return headers, proxy, mock_url, mock_robots


def guess_encoding(bytes, headers=None):
    '''
    Similar to but not quite like the logic in aiohttp.response.text()
    Get claimed charset from headers and combine that with clues in the data to
    figure out the decoding:

    Cases we want to get right:
    * Header claims ascii or latin-1 but is really utf-8
    * ... claims ascii or utf-8 but is really latin-1
    * ... claims ascii or utf-8 but is really ascii with Microsoft "smart quotes"
    * are there any countries/languages with considerble legacy encoding of content?

    Just how slow is cchardet anyway?
    '''
    return


FetcherResponse = namedtuple('FetcherResponse', ['response', 'body_bytes', 'req_headers',
                                                 't_first_byte', 't_last_byte', 'is_truncated',
                                                 'last_exception'])


async def fetch(url, session, headers=None, proxy=None, mock_url=None,
                allow_redirects=None, max_redirects=None,
                stats_prefix='', max_page_size=-1):
    pagetimeout = float(config.read('Crawl', 'PageTimeout'))

    if proxy:  # pragma: no cover
        proxy = aiohttp.ProxyConnector(proxy=proxy)
        # XXX we need to preserve the existing connector config (see cocrawler.__init__ for conn_kwargs)
        # XXX we should rotate proxies every fetch in case some are borked
        # XXX use proxy history to decide not to use some
        raise ValueError('not yet implemented')

    last_exception = None
    is_truncated = False

    try:
        t0 = time.time()
        last_exception = None

        with stats.coroutine_state('fetcher fetching'):
            with stats.record_latency(stats_prefix+'fetcher fetching', url=url.url):
                with aiohttp.Timeout(pagetimeout):
                    response = None
                    response = await session.get(mock_url or url.url,
                                                 allow_redirects=allow_redirects,
                                                 max_redirects=max_redirects,
                                                 headers=headers)
                    t_first_byte = '{:.3f}'.format(time.time() - t0)

                    # reddit.com is an example of a CDN-related SSL fail
                    # XXX when we retry, if local_addr was a list, switch to a different source IP
                    #   (change out the TCPConnector)

                    # use streaming interface to limit bytecount
                    # fully receive headers and body, to cause all network errors to happen
                    body_bytes = await response.content.read(max_page_size)
                    if not response.content.at_eof():
                        response.close()  # XXX should interrupt the network transfer? -- testme
                        is_truncated = 'length'

                    t_last_byte = '{:.3f}'.format(time.time() - t0)
    except asyncio.TimeoutError as e:
        is_truncated = 'time'  # XXX test WARC of this response?
        if stats.stats_sum('fetch timeout', 1) < 10:
            LOGGER.info('Example traceback for %r:', e)
            traceback.print_exc()
        last_exception = repr(e)
    except (aiohttp.ClientError) as e:
        # ClientError is a catchall for a bunch of things
        # e.g. DNS errors, '400' errors for http parser errors
        # XXX deal with partial fetches and WARC them, is_truncated = 'disconnect'
        if stats.stats_sum('fetch ClientError', 1) < 50:
            LOGGER.info('Example traceback for %r:', e)
            traceback.print_exc()
        try:
            body_bytes = await response.content.read(max_page_size)
            # This never goes off
            stats.stats_sum('fetcher received partial response before disconnect', 1)
            LOGGER.info('I received %d bytes in the body', len(body_bytes))
        except Exception:
            pass
        last_exception = repr(e)
    except aiodns.error.DNSError as e:
        if stats.stats_sum('fetch DNS error', 1) < 10:
            LOGGER.info('Example traceback for %r:', e)
            traceback.print_exc()
        last_exception = repr(e)
    except ssl.CertificateError as e:
        # unfortunately many ssl errors raise and have tracebacks printed deep in aiohttp
        if stats.stats_sum('fetch SSL error', 1) < 10:
            LOGGER.info('Example traceback for %r:', e)
            traceback.print_exc()
        last_exception = repr(e)
    #except (ValueError, AttributeError, RuntimeError) as e:
        # supposedly aiohttp 2.1 only fires these on programmer error, but here's what I've seen in the past:
        # ValueError Location: https:/// 'Host could not be detected' -- robots fetch
        # ValueError Location: http:// /URL should be absolute/ -- robots fetch
        # ValueError 'Can redirect only to http or https' -- robots fetch -- looked OK to curl!
        # AttributeError: ?
        # RuntimeError: ?
    except ValueError as e:
        if stats.stats_sum('fetch other error - ValueError', 1) < 10:
            LOGGER.info('Example traceback for %r:', e)
            traceback.print_exc()
        last_exception = repr(e)
    except AttributeError as e:
        if stats.stats_sum('fetch other error - AttributeError', 1) < 10:
            LOGGER.info('Example traceback for %r:', e)
            traceback.print_exc()
        last_exception = repr(e)
    except RuntimeError as e:
        if stats.stats_sum('fetch other error - RuntimeError', 1) < 10:
            LOGGER.info('Example traceback for %r:', e)
            traceback.print_exc()
        last_exception = repr(e)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        last_exception = repr(e)
        stats.stats_sum('fetch surprising error', 1)
        LOGGER.info('Saw surprising exception in fetcher working on %s:\n%s', mock_url or url.url, e)
        traceback.print_exc()

    if last_exception:
        LOGGER.info('we failed working on %s, the last exception is %s', mock_url or url.url, last_exception)
        #if LOGGER.isEnabledFor(logging.DEBUG):
        #    traceback.print_last()  # this often says "no last exception and raises ValueError
        return FetcherResponse(None, None, None, None, None, False, last_exception)

    fr = FetcherResponse(response, body_bytes, response.request_info.headers,
                         t_first_byte, t_last_byte, is_truncated, None)

    if response.status >= 500:
        LOGGER.debug('server returned http status %d', response.status)

    stats.stats_sum('fetch bytes', len(body_bytes) + len(response.raw_headers))

    stats.stats_sum(stats_prefix+'fetch URLs', 1)
    stats.stats_sum(stats_prefix+'fetch http code=' + str(response.status), 1)

    # checks after fetch:
    # hsts header?
    # if ssl, check strict-transport-security header, remember max-age=foo part., other stuff like includeSubDomains
    # did we receive cookies? was the security bit set?

    return fr


def upgrade_scheme(url):
    '''
    Upgrade crawled scheme to https, if reasonable. This helps to reduce MITM attacks against the crawler.

    https://chromium.googlesource.com/chromium/src/net/+/master/http/transport_security_state_static.json

    Alternately, the return headers from a site might have strict-transport-security set ... a bit more
    dangerous as we'd have to respect the timeout to avoid permanently learning something that's broken

    TODO: use HTTPSEverwhere? would have to have a fallback if https failed, which it occasionally will
    '''
    return url
