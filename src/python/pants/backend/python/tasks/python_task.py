# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (nested_scopes, generators, division, absolute_import, with_statement,
                        print_function, unicode_literals)

from pants.backend.core.tasks.task import Task
from pants.backend.python.interpreter_cache import PythonInterpreterCache
from pants.base.exceptions import TaskError


class PythonTask(Task):
  @classmethod
  def setup_parser(cls, option_group, args, mkflag):
    option_group.add_option(mkflag('timeout'), dest='python_conn_timeout', type='int',
                            default=0, help='Number of seconds to wait for http connections.')
    option_group.add_option(mkflag('interpreter'), dest='python_interpreters', default=[],
                            action='append',
                            help="Constrain what Python interpreters to use.  Uses Requirement "
                                 "format from pkg_resources, e.g. 'CPython>=2.6,<3' or 'PyPy'. "
                                 "By default, no constraints are used.  Multiple constraints may "
                                 "be added.  They will be ORed together.")

  def __init__(self, context, workdir):
    super(PythonTask, self).__init__(context, workdir)
    self.conn_timeout = (self.context.options.python_conn_timeout or
                         self.context.config.getdefault('connection_timeout'))

    self.interpreter_cache = PythonInterpreterCache(self.context.config,
                                                    logger=self.context.log.debug)
    self.interpreter_cache.setup()

    # Select a default interpreter to use.
    compatibilities = self.context.options.python_interpreters or [b'']
    self._interpreter = self.select_interpreter(compatibilities)

  @property
  def interpreter(self):
    """Subclasses can use this if they're fine with the default interpreter (the usual case)."""
    return self._interpreter

  def select_interpreter(self, compatibilities):
    """Subclasses can use this to be more specific about interpreter selection."""
    interpreters = self.interpreter_cache.select_interpreter(
      list(self.interpreter_cache.matches(compatibilities)))
    if len(interpreters) != 1:
      raise TaskError('Unable to detect suitable interpreter.')
    interpreter = interpreters[0]
    self.context.log.debug('Selected %s' % interpreter)
    return interpreter
