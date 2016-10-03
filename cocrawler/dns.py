'''
DNS-related code
'''

import logging
import urllib
import ipaddress

import aiodns

import stats

LOGGER = logging.getLogger(__name__)

async def prefetch_dns(parts, mock_url, session):
    '''
    So that we can track DNS transactions, and log them, we try to make sure
    DNS answers are in the cache before we try to fetch from a host that's not cached.

    TODO: Note that this TCPConnector's cache never expires, so we need to clear it occasionally.

    TODO: https://developers.google.com/speed/public-dns/docs/dns-over-https -- optional plugin?
    Note comments about google crawler at https://developers.google.com/speed/public-dns/docs/performance
    RR types A=1 AAAA=28 CNAME=5 NS=2
    The root of a domain cannot have CNAME. NS records are only in the root. These rules are not directly
    enforced and.
    Query for A when it's a CNAME comes back with answer list CNAME -> ... -> A,A,A...
    If you see a CNAME there should be no NS
    NS records can lie, but, it seems that most hosting companies use 'em "correctly"
    '''
    if mock_url is None:
        netlocparts = parts.netloc.split(':', maxsplit=1)
    else:
        mockurlparts = urllib.parse.urlparse(mock_url)
        netlocparts = mockurlparts.netloc.split(':', maxsplit=1)
    host = netlocparts[0]
    try:
        port = int(netlocparts[1])
    except IndexError:
        port = 80

    answer = None
    iplist = []

    if (host, port) not in session.connector.cached_hosts:
        with stats.coroutine_state('fetcher DNS lookup'):
            # if this raises an exception, it's caught in the caller
            answer = await session.connector._resolve_host(host, port)
    else:
        answer = session.connector.cached_hosts[(host, port)]

    # XXX log DNS result to warc here?
    #  we should still log the IP to warc even if private
    #  note that these results don't have the TTL in them

    for a in answer:
        ip = a['host']
        if mock_url is None and ipaddress.ip_address(ip).is_private:
            LOGGER.info('host %s has private ip of %s, ignoring', host, ip)
            continue
        if ':' in ip: # is this a valid sign of ipv6? XXX policy
            # I'm seeing ipv6 answers on an ipv4-only server :-/
            LOGGER.info('host %s has ipv6 result of %s, ignoring', host, ip)
            continue
        iplist.append(ip)

    if len(iplist) == 0:
        LOGGER.info('host %s has no addresses', host)

    return iplist

res = None

def setup_resolver(ns):
    global res
    res = aiodns.DNSResolver(nameservers=ns)

async def query(host, qtype):
    '''
    aiohttp uses aiodns under the hood, but you can't get
    directly at the .query method. So we use aiodns directly.

    Example results:

    A: [ares_query_simple_result(host='172.217.26.206', ttl=108)]
    AAAA: [ares_query_simple_result(host='2404:6800:4007:800::200e', ttl=299)]
    NS: [ares_query_ns_result(host='ns2.google.com', ttl=None),
         ares_query_ns_result(host='ns4.google.com', ttl=None),
         ares_query_ns_result(host='ns1.google.com', ttl=None),
         ares_query_ns_result(host='ns3.google.com', ttl=None)]
    CNAME: ares_query_cname_result(cname='blogger.l.google.com', ttl=None)
    '''
    try:
        return await res.query(host, qtype)
    except aiodns.error.DNSError:
        return [] # kinda un-pythonic




