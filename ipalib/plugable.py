# Authors:
#   Jason Gerard DeRose <jderose@redhat.com>
#
# Copyright (C) 2008  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Plugin framework.

The classes in this module make heavy use of Python container emulation. If
you are unfamiliar with this Python feature, see
http://docs.python.org/ref/sequence-types.html
"""

import sys
import inspect
import threading
import os
from os import path
import optparse
import textwrap
import collections
import importlib

import six

from ipalib import errors
from ipalib.config import Env
from ipalib.text import _
from ipalib.base import ReadOnly, NameSpace, lock, islocked
from ipalib.constants import DEFAULT_CONFIG
from ipapython.ipa_log_manager import (
    log_mgr,
    LOGGING_FORMAT_FILE,
    LOGGING_FORMAT_STDERR)
from ipapython.version import VERSION, API_VERSION

if six.PY3:
    unicode = str

# FIXME: Updated constants.TYPE_ERROR to use this clearer format from wehjit:
TYPE_ERROR = '%s: need a %r; got a %r: %r'


# FIXME: This function has no unit test
def find_modules_in_dir(src_dir):
    """
    Iterate through module names found in ``src_dir``.
    """
    if not (os.path.abspath(src_dir) == src_dir and os.path.isdir(src_dir)):
        return
    if os.path.islink(src_dir):
        return
    suffix = '.py'
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(suffix):
            continue
        pyfile = os.path.join(src_dir, name)
        if not os.path.isfile(pyfile):
            continue
        module = name[:-len(suffix)]
        if module == '__init__':
            continue
        yield module


class Registry(object):
    """A decorator that makes plugins available to the API

    Usage::

        register = Registry()

        @register()
        class obj_mod(...):
            ...

    For forward compatibility, make sure that the module-level instance of
    this object is named "register".
    """
    def __init__(self):
        self.__registry = collections.OrderedDict()

    def __call__(self, **kwargs):
        def register(klass):
            """
            Register the plugin ``klass``.

            :param klass: A subclass of `Plugin` to attempt to register.
            """
            if not inspect.isclass(klass):
                raise TypeError('plugin must be a class; got %r' % klass)

            # Raise DuplicateError if this exact class was already registered:
            if klass in self.__registry:
                raise errors.PluginDuplicateError(plugin=klass)

            # The plugin is okay, add to __registry:
            self.__registry[klass] = dict(kwargs, klass=klass)

            return klass

        return register

    def __iter__(self):
        return iter(self.__registry.values())


class Plugin(ReadOnly):
    """
    Base class for all plugins.
    """

    finalize_early = True

    def __init__(self, api):
        assert api is not None
        self.__api = api
        self.__finalize_called = False
        self.__finalized = False
        self.__finalize_lock = threading.RLock()
        log_mgr.get_logger(self, True)

    @property
    def name(self):
        return type(self).__name__

    @property
    def doc(self):
        return type(self).__doc__

    @property
    def summary(self):
        doc = self.doc
        if not _(doc).msg:
            cls = type(self)
            return u'<%s.%s>' % (cls.__module__, cls.__name__)
        else:
            return unicode(doc).split('\n\n', 1)[0].strip()

    @property
    def api(self):
        """
        Return `API` instance passed to `__init__()`.
        """
        return self.__api

    # FIXME: for backward compatibility only
    @property
    def env(self):
        return self.__api.env

    # FIXME: for backward compatibility only
    @property
    def Backend(self):
        return self.__api.Backend

    # FIXME: for backward compatibility only
    @property
    def Command(self):
        return self.__api.Command

    def finalize(self):
        """
        Finalize plugin initialization.

        This method calls `_on_finalize()` and locks the plugin object.

        Subclasses should not override this method. Custom finalization is done
        in `_on_finalize()`.
        """
        with self.__finalize_lock:
            assert self.__finalized is False
            if self.__finalize_called:
                # No recursive calls!
                return
            self.__finalize_called = True
            self._on_finalize()
            self.__finalized = True
            if not self.__api.is_production_mode():
                lock(self)

    def _on_finalize(self):
        """
        Do custom finalization.

        This method is called from `finalize()`. Subclasses can override this
        method in order to add custom finalization.
        """
        pass

    def ensure_finalized(self):
        """
        Finalize plugin initialization if it has not yet been finalized.
        """
        with self.__finalize_lock:
            if not self.__finalized:
                self.finalize()

    class finalize_attr(object):
        """
        Create a stub object for plugin attribute that isn't set until the
        finalization of the plugin initialization.

        When the stub object is accessed, it calls `ensure_finalized()` to make
        sure the plugin initialization is finalized. The stub object is expected
        to be replaced with the actual attribute value during the finalization
        (preferably in `_on_finalize()`), otherwise an `AttributeError` is
        raised.

        This is used to implement on-demand finalization of plugin
        initialization.
        """
        __slots__ = ('name', 'value')

        def __init__(self, name, value=None):
            self.name = name
            self.value = value

        def __get__(self, obj, cls):
            if obj is None or obj.api is None:
                return self.value
            obj.ensure_finalized()
            try:
                return getattr(obj, self.name)
            except RuntimeError:
                # If the actual attribute value is not set in _on_finalize(),
                # getattr() calls __get__() again, which leads to infinite
                # recursion. This can happen only if the plugin is written
                # badly, so advise the developer about that instead of giving
                # them a generic "maximum recursion depth exceeded" error.
                raise AttributeError(
                    "attribute '%s' of plugin '%s' was not set in finalize()" % (self.name, obj.name)
                )

    def __repr__(self):
        """
        Return 'module_name.class_name()' representation.

        This representation could be used to instantiate this Plugin
        instance given the appropriate environment.
        """
        return '%s.%s()' % (
            self.__class__.__module__,
            self.__class__.__name__
        )


class API(ReadOnly):
    """
    Dynamic API object through which `Plugin` instances are accessed.
    """

    def __init__(self):
        super(API, self).__init__()
        self.__plugins = {}
        self.__next = {}
        self.__done = set()
        self.env = Env()

    @property
    def bases(self):
        raise NotImplementedError

    @property
    def packages(self):
        raise NotImplementedError

    def __len__(self):
        """
        Return the number of plugin namespaces in this API object.
        """
        return len(self.bases)

    def __iter__(self):
        """
        Iterate (in ascending order) through plugin namespace names.
        """
        return (base.__name__ for base in self.bases)

    def __contains__(self, name):
        """
        Return True if this API object contains plugin namespace ``name``.

        :param name: The plugin namespace name to test for membership.
        """
        return name in set(self)

    def __getitem__(self, name):
        """
        Return the plugin namespace corresponding to ``name``.

        :param name: The name of the plugin namespace you wish to retrieve.
        """
        if name in self:
            try:
                return getattr(self, name)
            except AttributeError:
                pass

        raise KeyError(name)

    def __call__(self):
        """
        Iterate (in ascending order by name) through plugin namespaces.
        """
        for name in self:
            try:
                yield getattr(self, name)
            except AttributeError:
                raise KeyError(name)

    def is_production_mode(self):
        """
        If the object has self.env.mode defined and that mode is
        production return True, otherwise return False.
        """
        return getattr(self.env, 'mode', None) == 'production'

    def __doing(self, name):
        if name in self.__done:
            raise Exception(
                '%s.%s() already called' % (self.__class__.__name__, name)
            )
        self.__done.add(name)

    def __do_if_not_done(self, name):
        if name not in self.__done:
            getattr(self, name)()

    def isdone(self, name):
        return name in self.__done

    def bootstrap(self, parser=None, **overrides):
        """
        Initialize environment variables and logging.
        """
        self.__doing('bootstrap')
        self.log_mgr = log_mgr
        log = log_mgr.root_logger
        self.log = log
        self.env._bootstrap(**overrides)
        self.env._finalize_core(**dict(DEFAULT_CONFIG))

        # Add the argument parser
        if not parser:
            parser = self.build_global_parser()
        self.parser = parser

        # If logging has already been configured somewhere else (like in the
        # installer), don't add handlers or change levels:
        if log_mgr.configure_state != 'default' or self.env.validate_api:
            return

        log_mgr.default_level = 'info'
        log_mgr.configure_from_env(self.env, configure_state='api')
        # Add stderr handler:
        level = 'info'
        if self.env.debug:
            level = 'debug'
        else:
            if self.env.context == 'cli':
                if self.env.verbose > 0:
                    level = 'info'
                else:
                    level = 'warning'

        if 'console' in log_mgr.handlers:
            log_mgr.remove_handler('console')
        log_mgr.create_log_handlers([dict(name='console',
                                          stream=sys.stderr,
                                          level=level,
                                          format=LOGGING_FORMAT_STDERR)])

        # Add file handler:
        if self.env.mode in ('dummy', 'unit_test'):
            return  # But not if in unit-test mode
        if self.env.log is None:
            return
        log_dir = path.dirname(self.env.log)
        if not path.isdir(log_dir):
            try:
                os.makedirs(log_dir)
            except OSError:
                log.error('Could not create log_dir %r', log_dir)
                return

        level = 'info'
        if self.env.debug:
            level = 'debug'
        try:
            log_mgr.create_log_handlers([dict(name='file',
                                              filename=self.env.log,
                                              level=level,
                                              format=LOGGING_FORMAT_FILE)])
        except IOError as e:
            log.error('Cannot open log file %r: %s', self.env.log, e)
            return

    def build_global_parser(self, parser=None, context=None):
        """
        Add global options to an optparse.OptionParser instance.
        """
        if parser is None:
            parser = optparse.OptionParser(
                add_help_option=False,
                formatter=IPAHelpFormatter(),
                usage='%prog [global-options] COMMAND [command-options]',
                description='Manage an IPA domain',
                version=('VERSION: %s, API_VERSION: %s'
                                % (VERSION, API_VERSION)),
                epilog='\n'.join([
                    'See "ipa help topics" for available help topics.',
                    'See "ipa help <TOPIC>" for more information on a '
                        'specific topic.',
                    'See "ipa help commands" for the full list of commands.',
                    'See "ipa <COMMAND> --help" for more information on a '
                        'specific command.',
                ]))
            parser.disable_interspersed_args()
            parser.add_option("-h", "--help", action="help",
                help='Show this help message and exit')

        parser.add_option('-e', dest='env', metavar='KEY=VAL', action='append',
            help='Set environment variable KEY to VAL',
        )
        parser.add_option('-c', dest='conf', metavar='FILE',
            help='Load configuration from FILE',
        )
        parser.add_option('-d', '--debug', action='store_true',
            help='Produce full debuging output',
        )
        parser.add_option('--delegate', action='store_true',
            help='Delegate the TGT to the IPA server',
        )
        parser.add_option('-v', '--verbose', action='count',
            help='Produce more verbose output. A second -v displays the XML-RPC request',
        )
        if context == 'cli':
            parser.add_option('-a', '--prompt-all', action='store_true',
                help='Prompt for ALL values (even if optional)'
            )
            parser.add_option('-n', '--no-prompt', action='store_false',
                dest='interactive',
                help='Prompt for NO values (even if required)'
            )
            parser.add_option('-f', '--no-fallback', action='store_false',
                dest='fallback',
                help='Only use the server configured in /etc/ipa/default.conf'
            )

        return parser

    def bootstrap_with_global_options(self, parser=None, context=None):
        parser = self.build_global_parser(parser, context)
        (options, args) = parser.parse_args()
        overrides = {}
        if options.env is not None:
            assert type(options.env) is list
            for item in options.env:
                try:
                    (key, value) = item.split('=', 1)
                except ValueError:
                    # FIXME: this should raise an IPA exception with an
                    # error code.
                    # --Jason, 2008-10-31
                    pass
                overrides[str(key.strip())] = value.strip()
        for key in ('conf', 'debug', 'verbose', 'prompt_all', 'interactive',
            'fallback', 'delegate'):
            value = getattr(options, key, None)
            if value is not None:
                overrides[key] = value
        if hasattr(options, 'prod'):
            overrides['webui_prod'] = options.prod
        if context is not None:
            overrides['context'] = context
        self.bootstrap(parser, **overrides)
        return (options, args)

    def load_plugins(self):
        """
        Load plugins from all standard locations.

        `API.bootstrap` will automatically be called if it hasn't been
        already.
        """
        self.__doing('load_plugins')
        self.__do_if_not_done('bootstrap')
        if self.env.mode in ('dummy', 'unit_test'):
            return
        for package in self.packages:
            self.add_package(package)

    # FIXME: This method has no unit test
    def add_package(self, package):
        """
        Add plugin modules from the ``package``.

        :param package: A package from which to add modules.
        """
        package_name = package.__name__
        package_file = package.__file__
        package_dir = path.dirname(path.abspath(package_file))

        parent = sys.modules[package_name.rpartition('.')[0]]
        parent_dir = path.dirname(path.abspath(parent.__file__))
        if parent_dir == package_dir:
            raise errors.PluginsPackageError(
                name=package_name, file=package_file
            )

        self.log.debug("importing all plugin modules in %s...", package_name)
        modules = find_modules_in_dir(package_dir)
        modules = ['.'.join((package_name, name)) for name in modules]

        for name in modules:
            self.log.debug("importing plugin module %s", name)
            try:
                module = importlib.import_module(name)
            except errors.SkipPluginModule as e:
                self.log.debug("skipping plugin module %s: %s", name, e.reason)
                continue
            except Exception as e:
                if self.env.startup_traceback:
                    import traceback
                    self.log.error("could not load plugin module %s\n%s", name,
                                   traceback.format_exc())
                raise

            try:
                self.add_module(module)
            except errors.PluginModuleError as e:
                self.log.debug("%s", e)

    def add_module(self, module):
        """
        Add plugins from the ``module``.

        :param module: A module from which to add plugins.
        """
        try:
            register = module.register
        except AttributeError:
            pass
        else:
            if isinstance(register, Registry):
                for kwargs in register:
                    self.add_plugin(**kwargs)
                return

        raise errors.PluginModuleError(name=module.__name__)

    def add_plugin(self, klass, override=False):
        """
        Add the plugin ``klass``.

        :param klass: A subclass of `Plugin` to attempt to add.
        :param override: If true, override an already added plugin.
        """
        if not inspect.isclass(klass):
            raise TypeError('plugin must be a class; got %r' % klass)

        # Find the base class or raise SubclassError:
        for base in self.bases:
            if issubclass(klass, self.bases):
                break
        else:
            raise errors.PluginSubclassError(
                plugin=klass,
                bases=self.bases,
            )

        # Check override:
        prev = self.__plugins.get(klass.__name__)
        if prev:
            if not override:
                # Must use override=True to override:
                raise errors.PluginOverrideError(
                    base=base.__name__,
                    name=klass.__name__,
                    plugin=klass,
                )

            self.__next[klass] = prev
        else:
            if override:
                # There was nothing already registered to override:
                raise errors.PluginMissingOverrideError(
                    base=base.__name__,
                    name=klass.__name__,
                    plugin=klass,
                )

        # The plugin is okay, add to sub_d:
        self.__plugins[klass.__name__] = klass

    def finalize(self):
        """
        Finalize the registration, instantiate the plugins.

        `API.bootstrap` will automatically be called if it hasn't been
        already.
        """
        self.__doing('finalize')
        self.__do_if_not_done('load_plugins')

        production_mode = self.is_production_mode()
        plugins = {}
        plugin_info = {}

        for base in self.bases:
            name = base.__name__
            members = []

            for klass in self.__plugins.values():
                if not issubclass(klass, base):
                    continue
                try:
                    instance = plugins[klass]
                except KeyError:
                    instance = plugins[klass] = klass(self)
                members.append(instance)
                plugin_info.setdefault(
                    '%s.%s' % (klass.__module__, klass.__name__),
                    []).append(name)

            if not production_mode:
                assert not hasattr(self, name)
            setattr(self, name, NameSpace(members))

        for klass, instance in plugins.items():
            if not production_mode:
                assert instance.api is self
            if klass.finalize_early or not self.env.plugins_on_demand:
                instance.ensure_finalized()
                if not production_mode:
                    assert islocked(instance)

        self.__finalized = True
        self.plugins = tuple((k, tuple(v)) for k, v in plugin_info.items())

        if not production_mode:
            lock(self)

    def get_plugin_next(self, klass):
        if not inspect.isclass(klass):
            raise TypeError('plugin must be a class; got %r' % klass)

        return self.__next[klass]


class IPAHelpFormatter(optparse.IndentedHelpFormatter):
    def format_epilog(self, text):
        text_width = self.width - self.current_indent
        indent = " " * self.current_indent
        lines = text.splitlines()
        result = '\n'.join(
            textwrap.fill(line, text_width, initial_indent=indent,
                subsequent_indent=indent)
            for line in lines)
        return '\n%s\n' % result
