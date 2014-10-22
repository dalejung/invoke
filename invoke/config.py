import copy
import os
from os.path import join
from types import DictType, BooleanType, StringTypes

from .vendor.etcaetera.config import Config as EtcConfig
from .vendor.etcaetera.adapter import File, Defaults, Overrides, Adapter

from .exceptions import AmbiguousEnvVar, UncastableEnvVar


def noop(s):
    """
    No-op 'formatter' for etcaetera adapters.

    For when we do not want auto upper/lower casing (currently, always).
    """
    return s


class NestedEnv(Adapter):
    """
    Custom etcaetera adapter for handling env vars (more flexibly than Env).
    """
    def __init__(self, config):
        """
        Initialize this adapter with a handle to a live Config object.

        :param config:
            An already-loaded Config object which can be introspected for its
            keys.
        """
        super(NestedEnv, self).__init__()
        self._config = config

    def load(self, formatter=None):
        # NOTE: This accepts a formatter argument because that's the API.
        # However we don't use it (or the centrally defined one) because we
        # have specific requirements for how keys are treated in this adapter.
        # Meh!
        env_vars = self._crawl(key_path=[], env_vars={})
        for env_var, key_path in env_vars.iteritems():
            if env_var in os.environ:
                self._path_set(key_path, os.environ[env_var])

    def _crawl(self, key_path, env_vars):
        """
        Examine config at location ``key_path`` & return potential env vars.

        Uses ``env_vars`` dict to determine if a conflict exists, and raises an
        exception if so. This dict is of the following form::

            {
                'EXPECTED_ENV_VAR_HERE': ['actual', 'nested', 'key_path'],
                ...
            }

        Returns another dictionary of new keypairs as per above.
        """
        new_vars = {}
        obj = self._path_get(key_path)
        # Sub-dict -> recurse
        if hasattr(obj, 'keys') and hasattr(obj, '__getitem__'):
            for key in obj.keys():
                merged_vars = dict(env_vars, **new_vars)
                merged_path = key_path + [key]
                crawled = self._crawl(merged_path, merged_vars)
                # Handle conflicts
                for key in crawled:
                    if key in new_vars:
                        err = "Found >1 source for {0}"
                        raise AmbiguousEnvVar(err.format(key))
                # Merge and continue
                new_vars.update(crawled)
        # Other -> is leaf, no recursion
        else:
            new_vars[self._to_env_var(key_path)] = key_path
        return new_vars

    def _to_env_var(self, key_path):
        return '_'.join(key_path).upper()

    def _path_get(self, key_path):
        obj = self._config
        for key in key_path:
            obj = obj[key]
        return obj

    def _path_set(self, key_path, value):
        obj = self._config
        for key in key_path[:-1]:
            obj = obj[key]
        old = obj[key_path[-1]]
        obj[key_path[-1]] = self._cast(old, value)

    def _cast(self, old, new_):
        if isinstance(old, BooleanType):
            return new_ not in ('0', '')
        elif isinstance(old, StringTypes):
            return new_
        elif old is None:
            return new_
        else:
            err = "Can't adapt an environment string into a {0}!"
            err = err.format(type(old))
            raise UncastableEnvVar(err)


class DataProxy(object):
    """
    Helper class implementing nested dict+attr access for `.Config`.
    """

    # Attributes which get proxied through to inner etc.Config obj.
    _proxies = tuple("""
        clear
        get
        has_key
        items
        iteritems
        iterkeys
        itervalues
        keys
        pop
        popitem
        setdefault
        update
        values
    """.split()) + tuple("__{0}__".format(x) for x in """
        cmp
        contains
        iter
        sizeof
    """.split())

    # Alt constructor used so we aren't getting in the way of Config's real
    # __init__().
    @classmethod
    def from_data(cls, data):
        obj = cls()
        obj.config = data
        return obj

    def __getattr__(self, key):
        try:
            return self._get(key)
        except KeyError:
            # Proxy most special vars to config for dict procotol.
            if key in self._proxies:
                return getattr(self.config, key)
            # Otherwise, raise useful AttributeError to follow getattr proto.
            err = "No attribute or config key found for {0!r}".format(key)
            attrs = [x for x in dir(self.__class__) if not x.startswith('_')]
            err += "\n\nValid real attributes: {0!r}".format(attrs)
            err += "\n\nValid keys: {0!r}".format(self.config.keys())
            raise AttributeError(err)

    def __hasattr__(self, key):
        return key in self.config or key in self._proxies

    def __iter__(self):
        # For some reason Python is ignoring our __hasattr__ when determining
        # whether we support __iter__. BOO
        return iter(self.config)

    def __eq__(self, other):
        # Can't proxy __eq__ because the RHS will always be an obj of the
        # current class, not the proxied-to class, and that causes
        # NotImplemented.
        return self.config == other.config

    def __len__(self):
        # Can't proxy __len__ either apparently? ugh
        return len(self.config)

    def __setitem__(self, key, value):
        # ... or __setitem__? thanks for nothing Python >:(
        self.config[key] = value

    def __delitem__(self, key):
        # OK this is really getting annoying
        del self.config[key]

    def __getitem__(self, key):
        return self._get(key)

    def _get(self, key):
        value = self.config[key]
        if isinstance(value, DictType):
            value = DataProxy.from_data(value)
        return value

    def __str__(self):
        return str(self.config)

    def __unicode__(self):
        return unicode(self.config)

    def __repr__(self):
        return repr(self.config)

    # TODO: copy()?


class Config(DataProxy):
    """
    Invoke's primary configuration handling class.

    See :doc:`/concepts/configuration` for details on the configuration system
    this class implements, including the :ref:`configuration hierarchy
    <config-hierarchy>`.

    This class lightly wraps ``etcaetera.config.Config``, allowing for another
    level of configurability (re: which files are loaded and in what order) as
    well as convenient access to configuration values, which may be accessed
    using dict syntax::

        config['foo']

    or attribute syntax::

        config.foo

    .. warning::
        Any "real" attributes (methods, etc) on `Config` take precedence over
        settings values - so if you e.g. have a top level setting named
        ``load``, you *must* use dict syntax to access it.

    Nesting works the same way - dict config values are turned into objects
    which honor both the dictionary protocol and the attribute-access method::

       config['foo']['bar']
       config.foo.bar

    This class implements the entire dictionary protocol.
    """

    def __init__(self, overrides=None, global_prefix=None, user_prefix=None, project_home=None, runtime_path=None, adapters=None):
        """
        Creates a new config object, but does not load any configuration data.

        .. note::
            To load configuration data, call `~.Config.load` after
            initialization.

        :param dict overrides:
            A dict containing override-level config data. Default: ``{}``.

        :param str global_prefix:
            Path & partial filename for the global config file location. Should
            include everything but the dot & file extension.
            
            Default: ``/etc/invoke`` (e.g. ``/etc/invoke.yaml`` or
            ``/etc/invoke.json``).

        :param str user_prefix:
            Like ``global_prefix`` but for the per-user config file.

            Default: ``~/.invoke`` (e.g. ``~/.invoke.yaml``).

        :param str project_home:
            Optional directory path location of the currently loaded
            `.Collection` (as loaded by `.Loader`). When non-empty, will
            trigger seeking of per-project config files in this location +
            ``invoke.(yaml|json|py)``.

        :param str runtime_path:
            Optional file path to a runtime configuration file.

            Used to fill the penultimate slot in the config hierarchy. Should
            be a full file path to an existing file, not a directory path, or a
            prefix.

        :param iterable adapters:
            An iterable of `Adapters` to use instead of the default
            :ref:`hierarchy <config-hierarchy>`.

            If this option is given, ``global_prefix`` and ``user_prefix`` will
            be ignored.

        .. _Adapters: http://etcaetera.readthedocs.org/en/0.4.0/howto.html#adapters
        """
        # Setup
        if overrides is None:
            overrides = {}
        if global_prefix is None:
            global_prefix = '/etc/invoke'
        if user_prefix is None:
            user_prefix = '~/.invoke'
        c = EtcConfig(formatter=noop)
        # Explicit adapter set
        if adapters is not None:
            c.register(*adapters)
        # The Hierarchy
        else:
            # Level 1 is Defaults, set via kwargs or client calling
            # set_defaults(). Normally comes from task collection tree.
            # Levels 2-4: global, user, & project config files
            prefixes = [global_prefix, user_prefix]
            if project_home is not None:
                prefixes.append(join(project_home, "invoke"))
            for prefix in prefixes:
                c.register(File("{0}.yaml".format(prefix)))
                c.register(File("{0}.json".format(prefix)))
                py = File("{0}.py".format(prefix), python_uppercase=False)
                c.register(py)
            # Level 5: environment variables. See `load()` - must be done as
            # late as possible to 'see' all other defined keys.
            # Level 6: Runtime config file
            if runtime_path is not None:
                # Give python_uppercase in case it's a .py. Is a safe no-op
                # otherwise.
                c.register(File(runtime_path, python_uppercase=False))
            # Level 7 is Overrides, typically runtime flag values
            c.register(Overrides(overrides, formatter=noop))
        # Assign to member
        self.config = c

    def load(self, defaults=None):
        """
        Performs loading and merging of all config sources.

        See :ref:`config-hierarchy` for details on load order and file
        locations.

        :param dict defaults:
            A dict containing defaults-level config data. Default: ``{}``.
        """
        # Pull in defaults (we do so at this level because typically we won't
        # know about collection-level default values until closer to runtime.
        if defaults is None:
            defaults = {}
        # Reinforce 'noop' here, Defaults calls load() in init()
        self.config.register(Defaults(defaults, formatter=noop))
        # Now that we have all other sources defined, we can load the Env
        # adapter. This sadly requires a 'pre-load' call to .load() so config
        # files get slurped up.
        self.config.load()
        env = NestedEnv(self.config)
        self.config.register(env)
        # TODO: fix up register order?? Probably just need to actually-register
        # Env prior to registering the runtime config file...
        # Re-load() so that our values get applied in the right slot in the
        # hierarchy.
        return self.config.load()

    def clone(self):
        """
        Return a copy of this configuration object.

        The new object will be identical in terms of data, but will be a
        distinct object with no shared mutable state.
        """
        # New config w/ formatter preserved
        c = EtcConfig(formatter=self.config.formatter)
        # New adapters, ditto + deepcopy internal data
        adapters = []
        for old in self.config.adapters:
            c.register(_clone_adapter(old))
        # Then deepcopy loaded data in case already loaded (we don't
        # necessarily know at this point if loading has occurred).
        c.update(copy.deepcopy(dict(self.config)))
        # All set
        new = Config()
        new.config = c
        return new


def _clone_adapter(old):
    if isinstance(old, (Defaults, Overrides)):
        new = old.__class__(formatter=old.formatter)
    elif isinstance(old, File):
        new = File(
            filepath=old.filepath,
            python_uppercase=old.python_uppercase,
            formatter=old.formatter,
        )
    elif isinstance(old, NestedEnv):
        new = NestedEnv(old._config)
    new.data = copy.deepcopy(old.data)
    return new
