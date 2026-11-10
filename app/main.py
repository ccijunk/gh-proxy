# -*- coding: utf-8 -*-
"""gh-proxy 的 asyncio 实现。

请求链路：客户端 -> nginx(:80) -> aiohttp(:127.0.0.1:8000) -> GitHub

转发全程流式：上游数据按 10KB 分块读取后立刻写给客户端，既不在内存里攒整个
响应体，也不会像同步 worker 那样在等待 GitHub 时独占一个进程。一个事件循环
即可同时持有大量进行中的 clone / 下载连接。
"""
import os
import re
from urllib.parse import quote, urljoin

import uvloop
from aiohttp import (ClientSession, ClientTimeout, DummyCookieJar,
                     TCPConnector, web)
from multidict import CIMultiDict

# config
# 分支文件使用jsDelivr镜像的开关，0为关闭，默认关闭
jsdelivr = 0
size_limit = 1024 * 1024 * 1024 * 999  # 允许的文件大小，默认999GB，相当于无限制了 https://github.com/hunshcn/gh-proxy/issues/8

"""
  先生效白名单再匹配黑名单，pass_list匹配到的会直接302到jsdelivr而忽略设置
  生效顺序 白->黑->pass，可以前往https://github.com/hunshcn/gh-proxy/issues/41 查看示例
  每个规则一行，可以封禁某个用户的所有仓库，也可以封禁某个用户的特定仓库，下方用黑名单示例，白名单同理
  user1 # 封禁user1的所有仓库
  user1/repo1 # 封禁user1的repo1
  */repo1 # 封禁所有叫做repo1的仓库
"""
white_list = '''
'''
black_list = '''
'''
pass_list = '''
'''

HOST = os.environ.get('APP_HOST', '127.0.0.1')  # 监听地址，建议监听本地然后由nginx反代
PORT = int(os.environ.get('APP_PORT', '8000'))  # 监听端口，由同容器内的 nginx 反代
LISTEN_BACKLOG = int(os.environ.get('APP_BACKLOG', '1024'))  # accept 队列长度
ASSET_URL = os.environ.get('ASSET_URL', 'https://hunshcn.github.io/gh-proxy')  # 主页

CHUNK_SIZE = 1024 * 10
MAX_REDIRECTS = 5
ASSET_TIMEOUT = ClientTimeout(total=10)
# 大 pack 传输可能持续几分钟，不能设读超时；只限制建连时间
UPSTREAM_TIMEOUT = ClientTimeout(total=None, connect=15, sock_read=None)
# 逐跳头只对单条连接有意义，转发时必须剔除
HOP_BY_HOP = frozenset((
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade',
))


def _parse_list(raw):
    return [tuple([x.replace(' ', '') for x in i.split('/')])
            for i in raw.split('\n') if i]


white_list = _parse_list(white_list)
black_list = _parse_list(black_list)
pass_list = _parse_list(pass_list)

exp1 = re.compile(r'^(?:https?://)?github\.com/(?P<author>.+?)/(?P<repo>.+?)/(?:releases|archive)/.*$')
exp2 = re.compile(r'^(?:https?://)?github\.com/(?P<author>.+?)/(?P<repo>.+?)/(?:blob|raw)/.*$')
exp3 = re.compile(r'^(?:https?://)?github\.com/(?P<author>.+?)/(?P<repo>.+?)/(?:info|git-).*$')
exp4 = re.compile(r'^(?:https?://)?raw\.(?:githubusercontent|github)\.com/(?P<author>.+?)/(?P<repo>.+?)/.+?/.+$')
exp5 = re.compile(r'^(?:https?://)?gist\.(?:githubusercontent|github)\.com/(?P<author>.+?)/.+?/.+$')

routes = web.RouteTableDef()


def check_url(u):
    for exp in (exp1, exp2, exp3, exp4, exp5):
        m = exp.match(u)
        if m:
            return m
    return False


def _hit(rule, m):
    """规则匹配：user / user/repo / */repo"""
    return m[:len(rule)] == rule or rule[0] == '*' and len(m) == 2 and m[1] == rule[1]


def _forbidden(message):
    return web.Response(status=403, text=message, content_type='text/html')


def _query_suffix(request):
    """目标 URL 的查询串；request.base_url 不含 query，因此这里等价于原实现"""
    return '?' + request.query_string if request.query_string else ''


def _fix_url(url):
    # nginx 未关闭 merge_slashes 时会把 "https://" 折叠成 "https:/"
    if url.startswith('https:/') and not url.startswith('https://'):
        return 'https://' + url[7:]
    return url


def _request_headers(request):
    return CIMultiDict(
        (k, v) for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP and k.lower() != 'host'
    )


@routes.get('/')
async def index(request):
    if 'q' in request.query:
        raise web.HTTPFound('/' + request.query['q'])
    return web.Response(text=request.app['index_html'], content_type='text/html')


@routes.get('/favicon.ico')
async def icon(request):
    return web.Response(body=request.app['icon'],
                        content_type='image/vnd.microsoft.icon')


@routes.route('*', '/{u:.*}')
async def handler(request):
    u = request.match_info['u']
    u = u if u.startswith('http') else 'https://' + u
    if u.rfind('://', 3, 9) == -1:
        u = u.replace('s:/', 's://', 1)  # nginx 合并斜杠时会把 // 传递为 /

    m = check_url(u)
    if not m:
        return _forbidden('Invalid input.')
    m = tuple(m.groups())
    if white_list and not any(_hit(i, m) for i in white_list):
        return _forbidden('Forbidden by white list.')
    if any(_hit(i, m) for i in black_list):
        return _forbidden('Forbidden by black list.')
    pass_by = any(_hit(i, m) for i in pass_list)

    if (jsdelivr or pass_by) and exp2.match(u):
        u = u.replace('/blob/', '@', 1).replace('github.com', 'cdn.jsdelivr.net/gh', 1)
        raise web.HTTPFound(u)
    if (jsdelivr or pass_by) and exp4.match(u):
        u = re.sub(r'(\.com/.*?/.+?)/(.+?/)', r'\1@\2', u, 1)
        _u = u.replace('raw.githubusercontent.com', 'cdn.jsdelivr.net/gh', 1)
        u = u.replace('raw.github.com', 'cdn.jsdelivr.net/gh', 1) if _u == u else _u
        raise web.HTTPFound(u)

    if exp2.match(u):
        u = u.replace('/blob/', '/raw/', 1)
    if pass_by:
        raise web.HTTPFound(_fix_url(u + _query_suffix(request)))
    u = quote(u, safe='/:')
    return await proxy(request, u)


async def proxy(request, u):
    """流式转发到 GitHub；遇到非 GitHub 的 3xx 由服务端继续跟随。"""
    session = request.app['session']
    method = request.method
    data = request.content if method in ('POST', 'PUT', 'PATCH') else None
    headers = _request_headers(request)
    url = _fix_url(u + _query_suffix(request))

    for _ in range(MAX_REDIRECTS + 1):
        started = False
        try:
            async with session.request(method, url, data=data, headers=headers,
                                       allow_redirects=False,
                                       timeout=UPSTREAM_TIMEOUT) as upstream:
                resp_headers = CIMultiDict(upstream.headers)
                for h in HOP_BY_HOP:
                    resp_headers.popall(h, None)

                length = upstream.headers.get('Content-Length')
                if length is not None and int(length) > size_limit:
                    raise web.HTTPFound(url)  # 超过限制直接回源

                location = upstream.headers.get('Location')
                if location is not None and not check_url(location):
                    # 对象存储/CDN 等非 GitHub 地址，服务端继续跟随
                    url = _fix_url(urljoin(url, location))
                    if upstream.status in (301, 302, 303) and method != 'HEAD':
                        method, data = 'GET', None
                        headers.popall('Content-Length', None)
                        headers.popall('Content-Type', None)
                    continue
                if location is not None:
                    # GitHub 地址改写为经过代理，客户端后续请求仍走这里
                    resp_headers['Location'] = '/' + location

                resp = web.StreamResponse(status=upstream.status,
                                          headers=resp_headers)
                await resp.prepare(request)
                started = True
                async for chunk in upstream.content.iter_chunked(CHUNK_SIZE):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except web.HTTPException:
            raise
        except Exception as e:
            if started:
                raise  # 已经向客户端输出，只能中断连接
            return web.Response(status=500, text='server error ' + str(e),
                                content_type='text/html')

    raise web.HTTPFound(url)


async def _load_assets(app):
    try:
        async with app['session'].get(ASSET_URL, timeout=ASSET_TIMEOUT) as r:
            r.raise_for_status()
            app['index_html'] = await r.text()
        async with app['session'].get(ASSET_URL + '/favicon.ico',
                                      timeout=ASSET_TIMEOUT) as r:
            r.raise_for_status()
            app['icon'] = await r.read()
    except Exception as e:
        raise RuntimeError(
            '无法加载主页静态资源 %s，请确认出网或修改 ASSET_URL：%r'
            % (ASSET_URL, e)) from e


async def _on_startup(app):
    session = ClientSession(
        connector=TCPConnector(limit=0, ttl_dns_cache=300),
        timeout=UPSTREAM_TIMEOUT,
        # 不解压，保持响应体原样透传（否则 zip/tar/git pack 会被破坏）
        auto_decompress=False,
        # 不替客户端声明压缩能力，只有客户端自己带了 Accept-Encoding 才透传
        skip_auto_headers={'Accept-Encoding'},
        cookie_jar=DummyCookieJar(),
        trust_env=True,
    )
    app['session'] = session
    try:
        await _load_assets(app)
    except Exception:
        await session.close()
        raise


async def _on_cleanup(app):
    await app['session'].close()


def create_app():
    app = web.Application(
        # 请求体始终以流的方式转给上游，这个上限只对整块读取生效
        client_max_size=1024 ** 4,
    )
    app['index_html'] = ''
    app['icon'] = b''
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.add_routes(routes)
    return app


def main():
    uvloop.install()
    web.run_app(
        create_app(),
        host=HOST,
        port=PORT,
        backlog=LISTEN_BACKLOG,
        access_log=None,  # 访问日志由 nginx 记录，避免重复
        handler_cancellation=True,  # 客户端断开时立刻取消上游流量
        shutdown_timeout=30,
        print=None,
    )


if __name__ == '__main__':
    main()
