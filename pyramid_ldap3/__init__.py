
import logging

from time import time

from pyramid.exceptions import ConfigurationError

try:
    import ldap3
except ImportError:  # pragma: no cover
    # this is for benefit of being able to build the docs on rtd.org
    class ldap3(object):
        LDAPException = Exception
        SEARCH_SCOPE_BASE_OBJECT = None
        SEARCH_SCOPE_SINGLE_LEVEL = None
        SEARCH_SCOPE_WHOLE_SUBTREE = None
        STRATEGY_REUSABLE_THREADED = None

logger = logging.getLogger(__name__)


class _LDAPQuery(object):
    """Represents an LDAP query.

    Provides rudimentary in-RAM caching of query results.
    """

    def __init__(self, base_dn, filter_tmpl, scope, cache_period):
        self.base_dn = base_dn
        self.filter_tmpl = filter_tmpl
        self.scope = scope
        self.cache_period = cache_period
        self.last_timeslice = 0
        self.cache = {}

    def __str__(self):
        return ('base_dn={base_dn}, filter_tmpl={filter_tmpl}, '
                'scope={scope}, cache_period={cache_period}'.format(**self.__dict__))

    def query_cache(self, cache_key):
        now = time()
        ts = _timeslice(self.cache_period, now)

        if ts > self.last_timeslice:
            logger.debug('dumping cache; now ts: %r, last_ts: %r', ts, self.last_timeslice)
            self.cache = {}
            self.last_timeslice = ts

        return self.cache.get(cache_key)

    def execute(self, conn, **kw):
        cache_key = (self.base_dn % kw, self.filter_tmpl % kw, self.scope)

        logger.debug('searching for %r', cache_key)

        if self.cache_period:
            result = self.query_cache(cache_key)
            if result is None:
                ret = conn.search(*cache_key)
                result, ret = conn.get_response(ret)
                self.cache[cache_key] = result
            else:
                logger.debug('result for %r retrieved from cache', cache_key)
        else:
            ret = conn.search(*cache_key)
            result, ret = conn.get_response(ret)

        logger.debug('search result: %r', result)

        return result


def _timeslice(period, when=None):
    if when is None:  # pragma: no cover
        when = time()
    return when - (when % period)


class ConnectionManager(object):
    """Provides API methods for managing LDAP connections."""

    def __init__(self, uri, bind=None, passwd=None, tls=None,
            use_pool=True, pool_size=10, ldap3=ldap3):
        self.ldap3 = ldap3
        self.uri = uri
        try:
            schema, host = uri.split('://', 1)
        except ValueError:
            schema, host = 'ldap', uri
        use_ssl = schema == 'ldaps'
        try:
            host, port = host.split(':', 1)
            port = int(port)
        except ValueError:
            host, port = host, 636 if use_ssl else 389
        self.server = self.ldap3.Server(
            host, port=port, use_ssl=use_ssl, tls=tls)
        self.bind, self.passwd = bind, passwd
        if use_pool:
            self.strategy = ldap3.STRATEGY_REUSABLE_THREADED
            self.pool_name = 'pyramid_ldap3'
            self.pool_size = pool_size
        else:
            self.strategy = ldap3.STRATEGY_ASYNC_THREADED
            self.pool_name = self.pool_size = None

    def __str__(self):
        return ('uri={uri}, bind={bind}/{passwd},pool={pool_size}'.format(
            **self.__dict__))

    def connection(self, user=None, password=None):
        if user:
            conn = self.ldap3.Connection(
                self.server, user=user, password=password, auto_bind=True,
                client_strategy=ldap3.STRATEGY_SYNC, read_only=True)
        else:
            conn = self.ldap3.Connection(
                self.server, user=self.bind, password=self.passwd,
                auto_bind=True, client_strategy=self.strategy, read_only=True,
                pool_name=self.pool_name, pool_size=self.pool_size)
        return conn


class Connector(object):
    """Provides API methods for accessing LDAP authentication information."""

    def __init__(self, registry, manager):
        self.registry = registry
        self.manager = manager

    def authenticate(self, login, password):
        """Validate the given login name and password.

        Given a login name and a password, return a tuple of ``(dn,
        attrdict)`` if the matching user if the user exists and his password
        is correct.  Otherwise return ``None``.

        In a ``(dn, attrdict)`` return value, ``dn`` will be the
        distinguished name of the authenticated user.  Attrdict will be a
        dictionary mapping LDAP user attributes to sequences of values.

        A zero length password will always be considered invalid since it
        results in a request for "unauthenticated authentication" which should
        not be used for LDAP based authentication. See `section 5.1.2 of
        RFC-4513 <http://tools.ietf.org/html/rfc4513#section-5.1.2>`_ for a
        description of this behavior.

        If :meth:`pyramid.config.Configurator.ldap_set_login_query` was not
        called, using this function will raise an
        :exc:`pyramid.exceptions.ConfiguratorError`.
        """

        if password == '':
            return None

        conn = self.manager.connection()
        search = getattr(self.registry, 'ldap_login_query', None)
        if search is None:
            raise ConfigurationError(
                'ldap_set_login_query was not called during setup')

        result = search.execute(conn, login=login, password=password)
        try:
            login_dn = result[0]['dn']
        except (IndexError, KeyError, TypeError):
            return None

        try:
            conn = self.manager.connection(login_dn, password)
            conn.open()
            conn.bind()
            conn.close()
        except ldap3.LDAPException:
            logger.debug('Exception in authenticate with login %r',
                login, exc_info=True)
            return None

        return result[0]

    def user_groups(self, userdn):
        """Get the groups the user belongs to.

        Given a user DN, return a sequence of LDAP attribute dictionaries
        matching the groups of which the DN is a member.  If the DN does not
        exist, return ``None``.

        In a return value ``[(dn, attrdict), ...]``, ``dn`` will be the
        distinguished name of the group.  Attrdict will be a dictionary
        mapping LDAP group attributes to sequences of values.

        If :meth:`pyramid.config.Configurator.ldap_set_groups_query` was not
        called, using this function will raise an
        :exc:`pyramid.exceptions.ConfiguratorError`

        """
        conn = self.manager.connection()
        search = getattr(self.registry, 'ldap_groups_query', None)
        if search is None:
            raise ConfigurationError(
                'set_ldap_groups_query was not called during setup')
        try:
            result = search.execute(conn, userdn=userdn)
        except ldap3.LDAPException:
            logger.debug('Exception in user_groups with userdn %r', userdn,
                exc_info=True)
            return None

        return result


def ldap_set_login_query(config, base_dn, filter_tmpl,
        scope=ldap3.SEARCH_SCOPE_SINGLE_LEVEL, cache_period=0):
    """Configurator method to set the LDAP login search.

    ``base_dn`` is the DN at which to begin the search.
    ``filter_tmpl`` is a string which can be used as an LDAP filter:
    it should contain the replacement value ``%(login)s``.
    Scope is any valid LDAP scope value
    (e.g. ``ldap3.SEARCH_SCOPE_SINGLE_LEVEL``).
    ``cache_period`` is the number of seconds to cache login search results;
    if it is 0, login search results will not be cached.

    Example::

        config.set_ldap_login_query(
            base_dn='CN=Users,DC=example,DC=com',
            filter_tmpl='(sAMAccountName=%(login)s)',
            scope=ldap3.SEARCH_SCOPE_SINGLE_LEVEL)

    The registered search must return one and only one value to be considered
    a valid login.
    """

    query = _LDAPQuery(base_dn, filter_tmpl, scope, cache_period)

    def register():
        config.registry.ldap_login_query = query

    intr = config.introspectable(
        'pyramid_ldap3 login query',
        None,
        str(query),
        'pyramid_ldap3 login query')

    config.action('ldap-set-login-query', register, introspectables=(intr,))


def ldap_set_groups_query(config, base_dn, filter_tmpl,
        scope=ldap3.SEARCH_SCOPE_WHOLE_SUBTREE, cache_period=0):
    """ Configurator method to set the LDAP groups search.

    ``base_dn`` is the DN at which to begin the search.
    ``filter_tmpl`` is a string which can be used as an LDAP filter:
    it should contain the replacement value ``%(userdn)s``.
    Scope is any valid LDAP scope value
    (e.g. ``ldap3.SEARCH_SCOPE_WHOLE_SUBTREE``).
    ``cache_period`` is the number of seconds to cache groups search results;
    if it is 0, groups search results will not be cached.

    Example::

        config.set_ldap_groups_query(
            base_dn='CN=Users,DC=example,DC=com',
            filter_tmpl='(&(objectCategory=group)(member=%(userdn)s))'
            scope=ldap3.SEARCH_SCOPE_WHOLE_SUBTREE)

    """

    query = _LDAPQuery(base_dn, filter_tmpl, scope, cache_period)

    def register():
        config.registry.ldap_groups_query = query

    intr = config.introspectable(
        'pyramid_ldap3 groups query',
        None,
        str(query),
        'pyramid_ldap3 groups query')
    config.action('ldap-set-groups-query', register, introspectables=(intr,))


def ldap_setup(config, uri,
        bind=None, passwd=None, use_tls=False, use_pool=True, pool_size=10):
    """Configurator method to set up an LDAP connection pool.

    - **uri**: ldap server uri **[mandatory]**
    - **bind**: default bind that will be used to bind a connector.
      **default: None**
    - **passwd**: default password that will be used to bind a connector.
      **default: None**
    - **use_tls**: activate TLS when connecting. **default: False**
    - **use_pool**: activates the pool. If False, will recreate a connector
       each time. **default: True**
    - **pool_size**: pool size. **default: 10**
    """

    manager = ConnectionManager(
        uri, bind, passwd, use_tls, use_pool, pool_size if use_pool else None)

    def get_connector(request):
        return Connector(request.registry, manager)

    config.set_request_property(get_connector, 'ldap_connector', reify=True)

    intr = config.introspectable(
        'pyramid_ldap3 setup',
        None,
        str(manager),
        'pyramid_ldap3 setup')
    config.action('ldap-setup', None, introspectables=(intr,))


def get_ldap_connector(request):
    """Return the LDAP connector attached to the request.

    If :meth:`pyramid.config.Configurator.ldap_setup` was not called, using
    this function will raise an :exc:`pyramid.exceptions.ConfigurationError`.
    """
    connector = getattr(request, 'ldap_connector', None)
    if connector is None:
        if ldap3.LDAPException is Exception:  # pragma: no cover
            raise ImportError(
                'You must install python3-ldap to use an LDAP connector.')
        raise ConfigurationError(
            'You must call Configurator.ldap_setup during setup '
            'to use an LDAP connector.')
    return connector


def groupfinder(userdn, request):
    """Groupfinder function for Pyramid.

    A groupfinder implementation useful in conjunction with out-of-the-box
    Pyramid authentication policies.  It returns the DN of each group
    belonging to the user specified by ``userdn`` to as a principal
    in the list of results; if the user does not exist, it returns None.
    """
    connector = get_ldap_connector(request)
    group_list = connector.user_groups(userdn)
    if group_list is None:
        return None
    return [group['dn'] for group in group_list]


def includeme(config):
    """Set up Configurator methods for pyramid_ldap3."""
    config.add_directive('ldap_setup', ldap_setup)
    config.add_directive('ldap_set_login_query', ldap_set_login_query)
    config.add_directive('ldap_set_groups_query', ldap_set_groups_query)