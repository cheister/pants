# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

from pants.backend.jvm.subsystems.jvm_platform import JvmPlatform
from pants.backend.jvm.targets.jvm_target import JvmTarget
from pants.base.payload import Payload
from pants.base.payload_field import PrimitiveField


class JavaTests(JvmTarget):
  """JUnit tests."""

  def __init__(self, cwd=None, test_platform=None, payload=None, timeout=None,
               extra_jvm_options=None, extra_env_vars=None, **kwargs):
    """
    :param str cwd: working directory (relative to the build root) for the tests under this
      target. If unspecified (None), the working directory will be controlled by junit_run's --cwd.
    :param str test_platform: The name of the platform (defined under the jvm-platform subsystem) to
      use for running tests (that is, a key into the --jvm-platform-platforms dictionary). If
      unspecified, the platform will default to the same one used for compilation.
    :param int timeout: A timeout (in seconds) which covers the total runtime of all tests in this
      target. Only applied if `--test-junit-timeouts` is set to True.
    :param list extra_jvm_options: A list of key value pairs of jvm options to use when running the
      tests. Example: ['-Dexample.property=1'] If unspecified, no extra jvm options will be added.
    :param dict extra_env_vars: A map of environment variables to set when running the tests, e.g.
      { 'FOOBAR': 12 }. Using `None` as the value will cause the variable to be unset.
    """
    self.cwd = cwd
    payload = payload or Payload()

    if extra_env_vars is None:
      extra_env_vars = {}
    for key, value in extra_env_vars.items():
      if value is not None:
        extra_env_vars[key] = str(value)

    payload.add_fields({
      'test_platform': PrimitiveField(test_platform),
      'extra_jvm_options': PrimitiveField(tuple(extra_jvm_options or ())),
      'extra_env_vars': PrimitiveField(tuple(extra_env_vars.items())),
    })
    self._timeout = timeout
    super(JavaTests, self).__init__(payload=payload, **kwargs)

    # TODO(John Sirois): These could be scala, clojure, etc.  'jvm' and 'tests' are the only truly
    # applicable labels - fixup the 'java' misnomer.
    self.add_labels('java', 'tests')

  @property
  def test_platform(self):
    if self.payload.test_platform:
      return JvmPlatform.global_instance().get_platform_by_name(self.payload.test_platform)
    return self.platform

  @property
  def timeout(self):
    return self._timeout
