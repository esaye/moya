from __future__ import unicode_literals
from __future__ import print_function
from __future__ import absolute_import

from ..elements import Attribute
from ..elements.elementbase import LogicElement
from ..tags.context import ContextElementBase, DataSetter
from .. import logic
from ..urlmapper import URLMapper, MissingURLParameter, RouteError
from ..context.expressiontime import ExpressionDateTime
from ..render import render_object
from .. import http
from ..http import StatusCode, standard_response, RespondWith
from .. import errors
from ..template.errors import MissingTemplateError
from ..template.rendercontainer import RenderContainer
from .. import trace
from .. import __version__
from ..content import Content
from ..tags.content import ContentElementMixin
from ..tools import get_return
from .. import syntax
from ..timezone import Timezone
from ..settings import SettingsContainer, SettingContainer
from ..context.errors import SubstitutionError
from ..context.tools import to_expression, set_dynamic
from ..sites import LocaleProxy
from ..compat import text_type, itervalues, py2bytes, iteritems
from .. import db
from ..response import MoyaResponse
from ..request import ReplaceRequest
from ..compat import urlencode

from webob import Response

from fs.path import splitext
import pytz

import sys

import logging
log = logging.getLogger('moya.runtime')
startup_log = logging.getLogger('moya.startup')


class Mountpoint(LogicElement):
    """A [i]mountpoint[/i] defines a collection of URL routes which map URLs on to moya code.

    An app will typically have at least one mountpoint with [c]name="main"[/c] (the default) which is used when the app is mounted. Moya will check each enclosed <url> in turn until it finds a route which matches the url.

    An app may contain multiple mountpoints, which can be [i]mounted[/i] separately.

    """

    class Help:
        synopsis = "define a collection of url routes"
        example = """
        <mountpoint name="main">
            <!-- should contain <url> tags -->
        </mountpoint>

        """

    name = Attribute("Mountpoint name unique to the application", default="main", map_to="_name")
    preserve_attributes = ['urlmapper', 'middleware', 'name']

    def post_build(self, context):
        self.urlmapper = URLMapper(self.libid)
        self.middleware = dict(request=URLMapper(),
                               response=URLMapper())

        self.name = self._name(context)


class URL(LogicElement):
    """Add a URL route to a mountpoint."""

    class Help:
        synopsis = """add a url to a mountpoint"""

    mountpoint = Attribute("Name of the parent mount point",
                           required=False)
    mount = Attribute("Mountpoint to mount on this url",
                      required=False,
                      default=None)
    route = Attribute("URL route",
                      required=True)
    view = Attribute("View element",
                     required=False,
                     map_to="target",
                     example="#post")
    methods = Attribute("A list of comma separated HTTP methods",
                        type="commalist",
                        evaldefault=True,
                        required=False,
                        default="GET,POST",
                        example="GET,POST",
                        map_to='_methods')
    handler = Attribute("A list of comma separated http status codes",
                        type="commalist",
                        evaldefault=False,
                        required=False,
                        default=[],
                        example="404",
                        map_to="_handlers"
                        )
    name = Attribute("An optional name",
                     required=False,
                     default=None)

    def lib_finalize(self, context):
        if not self.check(context):
            return
        defaults = self.get_let_map(context)
        params = self.get_parameters(context)

        methods = params._methods
        handlers = []
        for h in params._handlers:
            try:
                handlers.append(StatusCode(h))
            except KeyError:
                raise errors.ElementError('''"{}" is not a valid http status code'''.format(h),
                                          element=self)

        target = params.target
        url_target = self.document.lib.qualify_libname(self.libname)

        try:
            if target is None:
                target = (url_target,)
            else:
                target = (url_target,
                          self.document.qualify_element_ref(target, lib=self.lib))
        except errors.ElementNotFoundError:
            raise errors.ElementError("No view called '{}' in the project".format(target), element=self)
        if params.mountpoint is None:
            mount_point = self.get_ancestor("mountpoint")
        else:
            _, mount_point = self.get_element(params.mountpoint)

        if params.mount:
            _, element = self.archive.get_element(params.mount, lib=self.lib)
            mount_point.urlmapper.map(params.route.rstrip('/') + '/*',
                                      [url_target],
                                      methods=methods,
                                      handlers=handlers or None,
                                      defaults=defaults)
            mount_point.urlmapper.mount(params.route,
                                        element.urlmapper,
                                        name=params.name,
                                        defaults=defaults)

        else:
            try:
                mount_point.urlmapper.map(params.route,
                                          target,
                                          methods=methods,
                                          handlers=handlers or None,
                                          name=params.name,
                                          defaults=defaults)
            except ValueError as e:
                raise errors.ElementError(text_type(e), element=self)


class Middleware(LogicElement):
    """Add middleware to a mountpoint"""

    class Help:
        synopsis = "add middleware to a mountpoint"

    route = Attribute("Route", required=True)
    methods = Attribute("A list of comma separated HTTP methods",
                        required=False,
                        type="commalist",
                        evaldefault=True,
                        default="*",
                        example="GET,POST",
                        map_to='_methods')
    mountpoint = Attribute("Mount point", required=False)
    stage = Attribute("Stage in request handling",
                      required=False,
                      default="request",
                      metavar="STAGE",
                      choices=['request', 'response'])
    macro = Attribute("Macro to call",
                      required=False,
                      default=None)
    name = Attribute("An optional name",
                     required=False,
                     default=None)

    def lib_finalize(self, context):
        if not self.check(context):
            return
        params = self.get_parameters(context)
        methods = params._methods
        target = params.macro
        url_target = self.document.lib.qualify_libname(self.libname)
        if target is None:
            target = (url_target,)
        else:
            target = (url_target,
                      self.document.qualify_element_ref(target))

        if params.mountpoint is None:
            mount_point = self.get_ancestor("mountpoint")
        else:
            _, mount_point = self.get_element(params.mountpoint)

        mapper = mount_point.middleware[params.stage]
        _route = mapper.map(params.route,
                            target,
                            methods=methods,
                            name=params.name)


class Mount(LogicElement):
    """Mount a library."""

    class Help:
        synopsis = "mount a library on a given URL"

    app = Attribute('Application', required=True)
    url = Attribute("Url", required=True)
    mountpoint = Attribute("Mount point", required=False, default="main")
    name = Attribute("Name", required=False)
    priority = Attribute("Priority (highest priority is checked first)", type="integer", required=False, default=0)

    def logic(self, context):
        self.archive.build_libs()
        params = self.get_parameters(context)
        app = self.archive.find_app(params.app)
        server = self.get_ancestor('server')
        mountpoint = app.lib.get_element_by_type_and_attribute("mountpoint",
                                                               "name",
                                                               params.mountpoint)
        app.mounts.append((params.mountpoint, params.url))
        server.urlmapper.mount(params.url,
                               mountpoint.urlmapper,
                               defaults={'app': app.name},
                               name=params.name,
                               priority=params.priority)

        for stage, urlmapper in server.middleware.items():
            urlmapper.mount(params.url,
                            mountpoint.middleware[stage],
                            defaults={'app': app.name},
                            name=params.name,
                            priority=params.priority)
        startup_log.debug("%s (%s) mounted on %s", app, params.mountpoint, params.url)


class GetURL(DataSetter):
    """Get a named URL."""

    class Help:
        synopsis = "get a named URL"

    name = Attribute("URL name", required=True)
    _from = Attribute("Application", type="application", default=None, evaldefault=True)
    query = Attribute("Mapping expression to use as a query string", metavar="EXPRESSION", required=False, default=None, type="expression", missing=False)
    _with = Attribute("Extract URL values from this object", type="expression", required=False, default=None)
    base = Attribute("Base (protocol and domain) of the URL", default=None)

    def get_value(self, context):
        params = self.get_parameters(context)
        query = params.query
        app = self.get_app(context)
        try:
            if self.has_parameter('with'):
                url_params = self.get_let_map(context)
                url_params.update(params['with'])
            else:
                url_params = self.get_let_map(context)

            for k, v in iteritems(url_params):
                if not v:
                    self.throw('bad-value.parameter', "URL parameter '{}' must not be blank or missing (it is {})".format(k, to_expression(context, v)))

            url = context['.server'].get_url(app.name, params.name, url_params)
        except MissingURLParameter as e:
            self.throw('get-url.missing-parameter', text_type(e))
        except RouteError as e:
            self.throw('get-url.no-route', text_type(e))

        if query and hasattr(query, 'items'):
            qs = urlencode(list(query.items()), True)
            url += '?' + qs

        url = self.qualify(context, url)

        return url

    def qualify(self, context, url):
        base = self.base(context)
        if base is not None:
            url = base + url
        return url


class GetFqURL(GetURL):
    """Get a [i]fully qualified[/i] (including domain name and scheme) named URL"""
    base = Attribute("Base (protocol and domain) of the URL", default=None)

    class Help:
        synopsis = "get a fully qualified URL"

    def qualify(self, context, url):
        base = self.base(context)
        if base is None:
            base = context['.request.host_url']
        url = base + url
        return url


def wrap_element_error(f):
    def deco(self, context):
        try:
            for node in f(self, context):
                yield node
        except (errors.ElementError, logic.LogicFlowException):
            raise
        except Exception as e:
            #import traceback; traceback.print_exc(e)
            raise errors.ElementError(text_type(e), self)
    return deco


class View(ContextElementBase, ContentElementMixin):
    """Define a view to handle a URL"""

    class Help:
        synopsis = "define a view to handle a URL"

    content = Attribute("Content", type="elementref", required=False, default=None)
    template = Attribute("Template", type="template", required=False, default=None)
    requires = Attribute("Permission expression", type="expression", required=False, default=None)
    withscope = Attribute("Use scope as template / content data?", type="boolean", required=False, default=True)

    @wrap_element_error
    def run(self, context):
        (content,
         template,
         requires,
         withscope) = self.get_parameters(context,
                                          "content",
                                          "template",
                                          "requires",
                                          "withscope")

        if self.has_parameter('requires'):
            if not requires:
                raise logic.EndLogic(http.RespondForbidden())

        yield logic.DeferNodeContents(self)

        if '_return' in context:
            scope = get_return(context.get('_return'))
        else:
            if withscope:
                scope = context['.call']
            else:
                scope = {}

        if scope is not None and not isinstance(scope, Content):
            app = self.get_app(context)
            template = self.resolve_template(app, template)
            if content is not None:
                #self.get_element(content)
                if not hasattr(scope, 'items'):
                    self.throw("view.bad-return",
                               "View should return a dict or other mapping object (not {})".format(to_expression(scope)))

                for defer in self.generate_content(context, content, app, td=scope):
                    yield defer
                context.copy('_content', '_return')

            elif template is not None:
                render_container = RenderContainer.create(app, template=template)
                render_container.update(scope)
                context['_return'] = render_container


class AppUrlsProxy(object):

    def __moyacontext__(self, context):
        urls = context.get('.urls')
        app = context['.app']
        return urls[app.name]


class Trace(object):
    def __init__(self, target, app=None, route_data=None, response=None):
        self.target = target
        self.app = app
        self.route_data = route_data
        if isinstance(response, http.RespondWith):
            self.response = text_type(response)
        else:
            self.response = None

    def __moyarepr__(self, context):
        return "<trace>"

    @property
    def target_html(self):
        return syntax.highlight("target", self.target, line_numbers=False)


class GetLocale(DataSetter):
    """Get an object containing locale information"""

    class Help:
        synopsis = "get locale information"

    locale = Attribute("Locale name")

    def logic(self, context):
        _locale = self.locale(context)
        try:
            locale = LocaleProxy(_locale)
        except:
            self.throw('get-locale.unknown-locale',
                       '''Couldn't get locale information for "{}"'''.format(_locale))
        self.set_context(context, self.dst(context), locale)


class SetLocale(LogicElement):
    """Switches the current locale"""

    class Help:
        synopsis = "switch the current locale"

    locale = Attribute("Locale name")

    def logic(self, context):
        _locale = self.locale(context)
        try:
            locale = LocaleProxy(_locale)
        except:
            self.throw('change-locale.unknown-locale',
                       '''Couldn't get locale information for "{}"'''.format(_locale))
        context['.locale'] = locale


class SetLanguage(LogicElement):
    """Set the current language"""

    class Help:
        synopsis = "set the current language"

    language = Attribute("Language code")

    def logic(self, context):
        language = self.language(context)
        if not isinstance(language, list):
            language = [language]
        context['.languages'] = language


class Server(LogicElement):
    """Defines a server"""

    class Help:
        synopsis = "define a server"

    @classmethod
    def get_app_from_callstack(self, context):
        call = context.get('.call', None)
        if call is None:
            return None
        return getattr(call, 'app', None)

    def post_build(self, context):
        self.urlmapper = URLMapper()
        self.middleware = dict(request=URLMapper(),
                               response=URLMapper())
        self.fs = None
        super(Server, self).post_build(context)

    def startup(self, archive, context, fs, breakpoint=False):
        self.fs = fs
        archive.build_libs()
        try:
            if breakpoint:
                logic.debug(archive, context, logic.DeferNodeContents(self))
            else:
                logic.run_logic(archive, context, logic.DeferNodeContents(self))
        except Exception as e:
            #import traceback
            #traceback.print_exc(e)
            raise
        archive.build_libs()

    def get_url(self, app_name, url_name, params=None):
        app_routes = self.urlmapper.get_routes(app_name)
        url = None
        # Could be multiple routes for this name
        # Try each one and return the url that doesn't fail
        for route in app_routes[:-1]:
            try:
                url = route.target.get_url(url_name, params, base_route=route)
            except RouteError:
                continue
            else:
                break
        else:
            # Last one, if this throws an exception, we want it to propagate
            route = app_routes[-1]
            url = route.target.get_url(url_name, params, base_route=route)

        return url

    def trace(self, archive, url, method="GET"):
        for route_match in self.urlmapper.iter_routes(url, method):
            route_data = route_match.data
            target = route_match.target
            if target:
                for element_ref in target:
                    app = archive.get_app(route_data.get('app', None))
                    yield (route_data, archive.get_element(element_ref, app))

    def process_response(self, context, response):
        cookies = context.root.get('cookiejar', {})
        for cookie in itervalues(cookies):
            cookie.set(response)
        for cookie_name in cookies.deleted_cookies:
            response.delete_cookie(cookie_name)
        return response

    def render_response(self, archive, context, obj, status=StatusCode.ok):
        result = render_object(obj, archive, context, "html")
        response = Response(charset=py2bytes('utf8'), status=int(status))
        response.text = text_type(result)
        return self.process_response(context, response)

    def _dispatch_result(self, archive, context, request, result, status=StatusCode.ok):
        if result is None:
            return None
        if isinstance(result, ReplaceRequest):
            return result
        if isinstance(result, RespondWith):
            return self.dispatch_handler(archive,
                                         context,
                                         request,
                                         result.status)
        if not isinstance(result, Response):
            html = render_object(result, archive, context, "html")
            response = MoyaResponse(charset=py2bytes('utf8'), status=status)
            response.text = html
        else:
            response = result
        return self.process_response(context, response)

    def handle_error(self, archive, context, request, error, exc_info):
        context.safe_delete('._callstack')
        context.safe_delete('.call')
        return self.dispatch_handler(archive,
                                     context,
                                     request,
                                     status=StatusCode.internal_error,
                                     error=error,
                                     exc_info=exc_info)

    def _dispatch_mapper(self, archive, context, mapper, url, method="GET", status=None, breakpoint=False):
        """Loop to call targets for a url/method/status combination"""
        dispatch_trace = context.root.get('_urltrace', [])
        if breakpoint:
            call = archive.debug_call
        else:
            call = archive.call
        root = context.root
        for route_data, target, name in mapper.iter_routes(url, method, status):
            root.update(urlname=name, headers={})
            if target:
                for element_ref in target:
                    app, element = archive.get_element(element_ref)
                    if element:
                        app = app or archive.get_app(route_data.get('app', None))
                        context.root.update(url=route_data)
                        result = call(element_ref,
                                      context,
                                      app,
                                      url=route_data)
                        dispatch_trace.append(Trace(element_ref, app, route_data, result))
                        if result is not None:
                            yield result
                    else:
                        dispatch_trace.append(Trace(element_ref))

            else:
                dispatch_trace.append(Trace(element_ref))

    @classmethod
    def set_site(cls, archive, context, request):
        """Set site data for a request"""
        domain = request.host
        if ':' in domain:
            domain = domain.split(':', 1)[0]

        site_match = archive.sites.match(domain)

        if site_match is None:
            log.error('no site matching domain "{domain}", consider adding [site:{domain}] to settings'.format(domain=domain))
            return None

        context.root['site'] = site_settings = SettingsContainer.from_dict(site_match.data)
        context.root['sys']['site'] = site_match.site

        if site_match is not None:
            with context.frame('.site'):
                new_site_data = {}
                for k, v in site_match.data.items():
                    try:
                        new_site_data[k] = SettingContainer(context.sub(v))
                    except SubstitutionError as e:
                        log.error("unable to substitute site data '%s=%s' (%s)", k, v, e)
                site_settings.update(new_site_data)
        return site_match.site

    @classmethod
    def _get_tz(self, context, default_timezone='UTC', user_timezone=False):
        """lazy insertion of .tz"""
        tz = None
        if user_timezone:
            tz = context.get('.user.timezone', None)
        if not tz:
            tz = context.get('.sys.site.timezone', None)
        if not tz:
            tz = default_timezone
        if not tz:
            return None
        try:
            return Timezone(tz)
        except pytz.UnknownTimeZoneError:
            log.error("invalid value for timezone '%s', defaulting to UTC", tz)
            return Timezone('UTC')

    def run_middleware(self, stage, archive, context, request, url, method):
        middleware = self.middleware[stage]
        try:
            for result in self._dispatch_mapper(archive, context, middleware, url, method):
                response = self._dispatch_result(archive, context, request, result)
                if response:
                    return response
        except Exception as e:
            return self.handle_error(archive, context, request, e, sys.exc_info())

    def _populate_context(self, archive, context, request):
        """Add standard values to context."""
        populate_context = {'permissions': {},
                            'libs': archive.libs,
                            'apps': archive.apps,
                            'debug': archive.debug,
                            'develop': archive.develop,
                            'sys': {},
                            'server': self,
                            'urls': self.urlmapper,
                            'now': ExpressionDateTime.moya_utcnow(),
                            'appurls': AppUrlsProxy(),
                            'moya': {"version": __version__},
                            'enum': archive.enum,
                            'accept_language': list(request.accept_language),
                            'media_url': archive.media_url,
                            'filters': archive.filters,
                            'secret': archive.secret}
        context.root.update(populate_context)
        set_dynamic(context)

    def dispatch(self, archive, context, request, breakpoint=False):
        """Dispatch a request to the server and return a response object."""
        url = request.path_info
        method = request.method
        self._populate_context(archive, context, request)

        site = self.set_site(archive, context, request)
        if site is None:
            # No site match, return a 404
            return self.dispatch_handler(archive,
                                         context,
                                         request,
                                         StatusCode.not_found)
        root = context.root
        root['locale'] = site.locale
        context.set_lazy('.tz',
                         self._get_tz,
                         context,
                         user_timezone=site.user_timezone,
                         default_timezone=site.timezone)

        # Request middleware
        response = self.run_middleware('request', archive, context, request, url, method)
        if response is not None:
            return response

        def response_middleware(response):
            context.safe_delete('._callstack', '.call')
            context.root['response'] = response
            new_response = self.run_middleware('response', archive, context, request, url, method)
            return new_response or response

        # Run main views
        root['urltrace'] = root['_urltrace'] = []
        context.safe_delete('._callstack', '.call')
        status_code = StatusCode.not_found
        response = None
        try:
            try:
                for result in self._dispatch_mapper(archive, context, self.urlmapper, url, method, breakpoint=breakpoint):
                    response = self._dispatch_result(archive, context, request, result)
                    if response:
                        return response_middleware(response)
            finally:
                db.commit_sessions(context)

        except Exception as e:
            db.rollback_sessions(context)
            return self.handle_error(archive, context, request, e, sys.exc_info())

        root['_urltrace'] = []

        # Append slash and redirect if url doesn't end in a slash
        if not url.endswith('/') and site.append_slash:
            # Check in advance if the url ending with / actually maps to anything
            if method in ('HEAD', 'GET') and self.urlmapper.has_route(url + '/', method, None):
                _, ext = splitext(url)
                # Don't redirect when the filename has an extension
                if not ext:
                    response = MoyaResponse(status=StatusCode.temporary_redirect,
                                            location=url + '/')
                    return response

        # No response returned, handle 404
        return self.dispatch_handler(archive,
                                     context,
                                     request,
                                     status=status_code)

    def dispatch_handler(self,
                         archive,
                         context,
                         request,
                         status=404,
                         error=None,
                         exc_info=None):
        """Respond to a status code"""
        context.safe_delete('._callstack',
                            '.call',
                            '.td',
                            '._td',
                            '.contentstack',
                            '.content')
        moya_trace = None
        error2 = None
        moya_trace2 = None
        if error is not None:
            moya_trace = getattr(error, 'moya_trace', None)
            if moya_trace is None:
                try:
                    moya_trace = trace.build(context, None, None, error, exc_info, request)
                except Exception as e:
                    #import traceback; traceback.print_exc(e)
                    raise
        try:
            url = request.path_info
            method = request.method
            for result in self._dispatch_mapper(archive, context, self.urlmapper, url, method, status):
                if not isinstance(result, RespondWith):
                    return self._dispatch_result(archive, context, request, result, status=status)
        except Exception as e:
            log.exception('error in dispatch_handler')
            #from traceback import print_exc
            #print_exc()
            if status != StatusCode.internal_error:
                return self.handle_error(archive, context, request, e, sys.exc_info())
            error2 = e
            moya_trace2 = getattr(error2, 'moya_trace', None)
            if moya_trace2 is None:
                moya_trace2 = trace.build(context, None, None, error2, sys.exc_info(), request)

        context.reset()
        context.safe_delete('._callstack',
                            '.call',
                            '.td',
                            '._td',
                            '.contentstack',
                            '.content',
                            '_funccalls',
                            '._for',
                            '_for_stack')
        #pilot.context = context

        # No handlers have been defined for this status code
        # We'll look for a template named <status code>.html and render that
        template_filename = "{}.html".format(int(status))
        try:
            response = MoyaResponse(charset=py2bytes('utf8'), status=status)

            rc = RenderContainer.create(None, template=template_filename)
            rc['request'] = request
            rc['status'] = status
            rc['error'] = error
            rc['trace'] = moya_trace
            rc['error2'] = error
            rc['trace2'] = moya_trace2
            rc['moya_error'] = getattr(moya_trace.exception, 'type', None) if moya_trace else None
            response.text = render_object(rc, archive, context, "html")
            return response
        except MissingTemplateError:
            pass
        except Exception as e:
            log.error('unable to render %s (%s)', template_filename, text_type(e))
            #import traceback
            #traceback.print_exc(e)
            #print(e)
            pass


        # Render a very basic response
        response = Response(charset=py2bytes("utf8"), status=status)
        url = request.path_info
        try:
            response.text = standard_response(status, url, error, moya_trace, debug=archive.debug)
        except Exception as e:
            log.exception('error generating standard response')
        return response
