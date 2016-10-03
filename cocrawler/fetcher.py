'''
async fetching of urls.

Assumes robots checks have already been done.

Supports proxies and server mocking.

Success returns response object (caller must release()) and response
bytes (which were already read in order to shake out all potential
errors.)

Failure returns enough details for the caller to do something smart:
503, other 5xx, DNS fail, connect timeout, error between connect and
full response, proxy failure. Plus an errorstring good enough for logging.
'''

import time
import traceback
import urllib
from collections import namedtuple

import asyncio
import logging
import aiohttp
import aiodns

import stats
import dns

LOGGER = logging.getLogger(__name__)

# XXX should be a policy plugin
def apply_url_policies(url, parts, config):
    headers = {}
    proxy = None
    mock_url = None
    mock_robots = None

    test_host = config['Testing'].get('TestHostmapAll')
    if test_host and test_host != 'n': # why don't booleans in YAML work?
        headers['Host'] = parts.netloc
        mock_url = parts._replace(netloc=test_host).geturl()
        mock_robots = parts.scheme + '://' + test_host + '/robots.txt'

    return headers, proxy, mock_url, mock_robots

async def fetch(url, parts, session, config, headers=None, proxy=None, mock_url=None, allow_redirects=None, stats_me=True):

    maxsubtries = int(config['Crawl']['MaxSubTries'])
    pagetimeout = float(config['Crawl']['PageTimeout'])
    retrytimeout = float(config['Crawl']['RetryTimeout'])

    ret = namedtuple('fetcher_return', ['response', 'body_bytes', 'header_bytes',
                                        't_first_byte', 't_last_byte', 'last_exception'])

    if proxy: # pragma: no cover
        proxy = aiohttp.ProxyConnector(proxy=proxy)
        # XXX we need to preserve the existing connector config (see cocrawler.__init__ for conn_kwargs)
        # XXX we should rotate proxies every fetch in case some are borked
        # XXX use proxy history to decide not to use some
        raise ValueError('not yet implemented')

    subtries = 0
    last_exception = None
    response = None
    iplist = []

    while subtries < maxsubtries:
        subtries += 1
        try:
            t0 = time.time()
            last_exception = None

            if len(iplist) == 0:
                iplist = await dns.prefetch_dns(parts, mock_url, session)

            with stats.coroutine_state('fetcher fetching'):
                with aiohttp.Timeout(pagetimeout):
                    response = None # is this needed?
                    response = await session.get(mock_url or url,
                                                 allow_redirects=allow_redirects,
                                                 headers=headers)
                    t_first_byte = '{:.3f}'.format(time.time() - t0)

                    # XXX special sleepy 503 handling here - soft fail
                    # XXX json_log tries
                    # XXX serverdisconnected is a soft fail
                    # XXX aiodns.error.DNSError
                    # XXX equivalent to requests.exceptions.SSLerror ??
                    #   reddit.com is an example of a CDN-related SSL fail
                    # XXX when we retry, if local_addr was a list, switch to a different IP
                    #   (change out the TCPConnector)
                    # XXX what does a proxy error look like?
                    # XXX record proxy error

                    # fully receive headers and body.
                    # XXX if we want to limit bytecount, do it here?
                    body_bytes = await response.read() # this does a release if an exception is not thrown
                    t_last_byte = '{:.3f}'.format(time.time() - t0)
                    header_bytes = response.raw_headers

            if len(iplist) == 0:
                LOGGER.info('surprised that no-ip-address fetch of {} succeeded'.format(parts.netloc))

            # break only if we succeeded. 5xx = retry, exception = retry
            if response.status < 500:
                break

            print('will retry a {} for {}'.format(response.status, url))

        except (aiohttp.ClientError, aiohttp.DisconnectedError, aiohttp.HttpProcessingError,
                aiodns.error.DNSError, asyncio.TimeoutError) as e:
            last_exception = repr(e)
            LOGGER.debug('we sub-failed once, url is %s, exception is %s', url, last_exception)
            LOGGER.debug('elapsed is %.3f', time.time() - t0) # XXX
            if response is not None:
                response.release()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_exception = repr(e)
            print('UNKNOWN EXCEPTION SEEN in the fetcher')
            traceback.print_exc()
            LOGGER.debug('we sub-failed once WITH UNKNOWN EXCEPTION, url is %s, exception is %s',
                         url, last_exception)
            if response is not None:
                response.release()

        # treat all 5xx somewhat similar to a 503: slow down and retry
        # also doing this slow down for any exception
        # XXX record 5xx so that everyone else slows down, too (politeness)
        with stats.coroutine_state('fetcher retry sleep'):
            await asyncio.sleep(retrytimeout)

    else:
        if last_exception:
            LOGGER.debug('we failed, the last exception is %s', last_exception)
            return ret(None, None, None, None, None, last_exception)
        # fall through for the case of response.status >= 500

    if stats_me:
        stats.stats_sum('fetch URLs', 1)
        stats.stats_sum('fetch http code=' + str(response.status), 1)

    # checks after fetch:
    # hsts? if ssl, check strict-transport-security header,
    #   remember max-age=foo part., other stuff like includeSubDomains
    # did we receive cookies? was the security bit set?
    # record everything needed for warc. all headers, for example.

    return ret(response, body_bytes, header_bytes, t_first_byte, t_last_byte, None)

def upgrade_scheme(url):
    '''
    Upgrade crawled scheme to https, if reasonable. This helps to reduce MITM attacks
    against the crawler.
    https://chromium.googlesource.com/chromium/src/net/+/master/http/transport_security_state_static.json

    Alternately, the return headers from a site might have strict-transport-security set ... a bit more
    dangerous as we'd have to respect the timeout to avoid permanently learning something that's broken

    TODO: use HTTPSEverwhere? would have to have a fallback if https failed, which it occasionally will
    '''
    return url
